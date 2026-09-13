"""D-C3-C synthetic runtime click E2E probe — state machine + safety primitives (pure)。

approved design (D-C3-C0 / C0.1 / C0.2):

- production runtime-only synthetic target。**ローカル AffiliateLinkTarget は作らない**。
- destination は固定 ``https://example.com/`` のみ (real ASP / self-host は拒否)。
- 完全な token は **外部 untracked state file にのみ** 永続化する。通常出力・ログ・
  例外文言・lock metadata には ``token_fingerprint`` (SHA-256) のみを出す。
- 10 状態の append-only lifecycle。``*_attempted`` は対応する **ちょうど 1 回の** network
  呼び出しの **前に** 耐久的に書き込む。``*_confirmed`` は検証済み成功後のみ。
  attempted 状態からの自動 retry / 自動 rollback は行わない — Human reconciliation のみ。
- state file は crash-safe な atomic replace (`temp file -> fsync -> os.replace`) で書き込み、
  書き込み前に candidate を厳格 validate する。
- state file の隣に OS-backed な排他 lock (`<path>.lock`) を置き、action 全体を保持する。
  contention は即 fail closed (0 network)。

このモジュールは HTTP を「送る」判断はしない — 呼び出し側 (CLI) が明示的に
``*_execute`` action を呼んだときだけ、既存の ``projection_push_client`` /
``click_export_client`` を **ちょうど 1 回** 使って通信する。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.affiliate.click_export_client import (
    CLICK_EXPORT_MAX_LIMIT,
    execute_click_export,
    prepare_click_export,
)
from app.affiliate.destination_policy import DEFAULT_DESTINATION_HOST_POLICY
from app.affiliate.destination_safety import SELF_HOSTS
from app.affiliate.projection import (
    AffiliateTargetProjection,
    build_projection_entry,
    build_projection_snapshot,
)
from app.affiliate.projection_push_client import (
    ProjectionPushResult,
    execute_projection_push,
    prepare_projection_push,
)
from app.affiliate.runtime_http import require_https_origin
from app.affiliate.token import generate_token, is_well_formed_token
from app.article.draft_input_canonical import canonical_datetime, canonical_json
from app.exceptions import AffiliateProjectionPushError
from app.models.affiliate_click_import_run import AffiliateClickImportRun
from app.models.affiliate_link_target import ALT_ACTIVE, ALT_DISABLED
from app.models.affiliate_outbound_click import AffiliateOutboundClick
from app.repositories.affiliate_link_target_repository import AffiliateLinkTargetRepository

# ==================== fixed probe constants (never caller-supplied) =====
PROBE_SCHEMA_VERSION = 1
PROBE_NAMESPACE = "d-c3-c-synthetic-runtime-click-e2e"
PROBE_DESTINATION_URL = "https://example.com/"
PROBE_DESTINATION_HOST = "example.com"

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


def _assert_probe_destination_is_safe() -> None:
    """import 時に一度だけ: probe destination が self-host / 承認済み ASP host で
    ないことを構造的に保証する (実行時の caller 入力では変わらないが defense-in-depth)。"""

    if PROBE_DESTINATION_HOST in SELF_HOSTS:
        raise RuntimeError("synthetic probe destination must not be a self-host")
    for approved in DEFAULT_DESTINATION_HOST_POLICY.values():
        if PROBE_DESTINATION_HOST in approved:
            raise RuntimeError(
                "synthetic probe destination must not be an approved ASP host"
            )


_assert_probe_destination_is_safe()


# ==================== exceptions (メッセージに full token を含めない) ====
class SyntheticProbeError(RuntimeError):
    """probe tool の例外基底。full token を含めない。"""


class SyntheticProbeConfigError(SyntheticProbeError):
    """state path / settings が未構成・不正。state 書き込みなし・通信なし。"""


class SyntheticProbeStateError(SyntheticProbeError):
    """state file が存在しない・壊れている・現在の action を許さない state にある。"""


class SyntheticProbeLockError(SyntheticProbeError):
    """排他 lock を取得できなかった (別プロセスが保持中)。"""


class SyntheticProbeAmbiguousError(SyntheticProbeError):
    """/go probe が失敗した、または期待外のレスポンスだった。outcome 不明。"""


# ==================== state machine ======================================
STATE_INITIALIZED = "initialized"
STATE_ACTIVE_PREPARED = "active_prepared"
STATE_ACTIVE_ATTEMPTED = "active_attempted"
STATE_ACTIVE_CONFIRMED = "active_confirmed"
STATE_CLICK_ATTEMPTED = "click_attempted"
STATE_CLICK_CONFIRMED = "click_confirmed"
STATE_IMPORT_CONFIRMED = "import_confirmed"
STATE_DISABLE_PREPARED = "disable_prepared"
STATE_DISABLE_ATTEMPTED = "disable_attempted"
STATE_DISABLED_CONFIRMED = "disabled_confirmed"

PROBE_STATES = frozenset(
    {
        STATE_INITIALIZED,
        STATE_ACTIVE_PREPARED,
        STATE_ACTIVE_ATTEMPTED,
        STATE_ACTIVE_CONFIRMED,
        STATE_CLICK_ATTEMPTED,
        STATE_CLICK_CONFIRMED,
        STATE_IMPORT_CONFIRMED,
        STATE_DISABLE_PREPARED,
        STATE_DISABLE_ATTEMPTED,
        STATE_DISABLED_CONFIRMED,
    }
)

# 許可された state 遷移 (逆行は reconciliation のみ)。
PROBE_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_INITIALIZED: frozenset({STATE_ACTIVE_PREPARED}),
    STATE_ACTIVE_PREPARED: frozenset({STATE_ACTIVE_ATTEMPTED}),
    STATE_ACTIVE_ATTEMPTED: frozenset({STATE_ACTIVE_CONFIRMED, STATE_ACTIVE_PREPARED}),
    STATE_ACTIVE_CONFIRMED: frozenset({STATE_CLICK_ATTEMPTED}),
    STATE_CLICK_ATTEMPTED: frozenset(
        {STATE_CLICK_CONFIRMED, STATE_ACTIVE_CONFIRMED, STATE_CLICK_ATTEMPTED}
    ),
    STATE_CLICK_CONFIRMED: frozenset({STATE_IMPORT_CONFIRMED}),
    STATE_IMPORT_CONFIRMED: frozenset({STATE_DISABLE_PREPARED}),
    STATE_DISABLE_PREPARED: frozenset({STATE_DISABLE_ATTEMPTED}),
    STATE_DISABLE_ATTEMPTED: frozenset(
        {STATE_DISABLED_CONFIRMED, STATE_DISABLE_PREPARED}
    ),
    STATE_DISABLED_CONFIRMED: frozenset(),
}

# 各遷移で「初めて non-null になることが許される」フィールド。他の全フィールドは
# 不変 (前回値と完全一致必須)。これにより「必須フィールド」と
# 「null/non-null 制約」の両方を 1 つの汎用チェックで表現する。
TRANSITION_NEW_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    (STATE_INITIALIZED, STATE_ACTIVE_PREPARED): frozenset(
        {"activated_at", "active_projection_entry_hash", "active_projection_snapshot_hash"}
    ),
    (STATE_ACTIVE_PREPARED, STATE_ACTIVE_ATTEMPTED): frozenset({"active_attempted_at"}),
    (STATE_ACTIVE_ATTEMPTED, STATE_ACTIVE_CONFIRMED): frozenset({"active_confirmed_at"}),
    (STATE_ACTIVE_ATTEMPTED, STATE_ACTIVE_PREPARED): frozenset(),
    (STATE_ACTIVE_CONFIRMED, STATE_CLICK_ATTEMPTED): frozenset({"click_attempted_at"}),
    (STATE_CLICK_ATTEMPTED, STATE_CLICK_CONFIRMED): frozenset({"click_confirmed_at"}),
    (STATE_CLICK_ATTEMPTED, STATE_ACTIVE_CONFIRMED): frozenset(),
    (STATE_CLICK_ATTEMPTED, STATE_CLICK_ATTEMPTED): frozenset(),  # contamination flag のみ
    (STATE_CLICK_CONFIRMED, STATE_IMPORT_CONFIRMED): frozenset(
        {"source_click_id", "import_run_id", "import_confirmed_at"}
    ),
    (STATE_IMPORT_CONFIRMED, STATE_DISABLE_PREPARED): frozenset(
        {
            "disabled_at",
            "disabled_projection_entry_hash",
            "disabled_projection_snapshot_hash",
        }
    ),
    (STATE_DISABLE_PREPARED, STATE_DISABLE_ATTEMPTED): frozenset({"disable_attempted_at"}),
    (STATE_DISABLE_ATTEMPTED, STATE_DISABLED_CONFIRMED): frozenset(
        {"disabled_confirmed_at"}
    ),
    (STATE_DISABLE_ATTEMPTED, STATE_DISABLE_PREPARED): frozenset(),
}

# --init で確定する identity フィールド (以降 immutable)。
_INIT_FIELDS = frozenset(
    {"token", "token_fingerprint", "destination_url", "destination_host", "link_identity_hash"}
)

_HASH_FIELDS = (
    "active_projection_entry_hash",
    "active_projection_snapshot_hash",
    "disabled_projection_entry_hash",
    "disabled_projection_snapshot_hash",
)

_TIMESTAMP_FIELDS = (
    "activated_at",
    "active_attempted_at",
    "active_confirmed_at",
    "click_attempted_at",
    "click_confirmed_at",
    "import_confirmed_at",
    "disabled_at",
    "disable_attempted_at",
    "disabled_confirmed_at",
)

# ``*_prepared -> *_attempted`` may legitimately re-fire after a
# ``--reconcile-*  --observed absent`` sends the state back to ``*_prepared``
# for a retry (C0.2 §6/§9/§10). Those 3 "attempt" timestamps record the *most
# recent* attempt and may be overwritten on a subsequent legitimate re-entry.
# Every other field (identity/hash material, all ``*_confirmed_at`` fields —
# none of which has a backward-reconcile path re-entering their transition)
# stays permanently frozen the first time it is set.
_OVERWRITABLE_ON_REENTRY = frozenset(
    {"active_attempted_at", "click_attempted_at", "disable_attempted_at"}
)

_POSITIVE_INT_FIELDS = ("source_click_id", "import_run_id")

FIELD_NAMES = frozenset(
    {
        "probe_schema_version",
        "state",
        "token",
        "token_fingerprint",
        "destination_url",
        "destination_host",
        "link_identity_hash",
        "activated_at",
        "active_projection_entry_hash",
        "active_projection_snapshot_hash",
        "active_attempted_at",
        "active_confirmed_at",
        "click_attempted_at",
        "click_confirmed_at",
        "source_click_id",
        "import_run_id",
        "import_confirmed_at",
        "disabled_at",
        "disabled_projection_entry_hash",
        "disabled_projection_snapshot_hash",
        "disable_attempted_at",
        "disabled_confirmed_at",
        "click_contaminated",
    }
)

# state file には絶対に入れてはいけないフィールド (defense-in-depth の明示チェック用)。
FORBIDDEN_STATE_FIELDS = frozenset(
    {
        "shared_secret",
        "affiliate_runtime_shared_secret",
        "signature",
        "headers",
        "raw_response",
        "response_body",
        "article_id",
        "affiliate_program_id",
    }
)


# ==================== token / hash helpers ===============================
def token_fingerprint(token: str) -> str:
    """full token の SHA-256 hex。通常出力・報告書はこの値のみを使う。"""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def masked_prefix(token: str) -> str:
    return f"{token[:4]}…" if len(token) > 4 else "…"


def compute_synthetic_link_identity_hash(token: str) -> str:
    """承認済みの namespaced synthetic identity (D-C3-C0 §10)。

    article_id / affiliate_program_id を一切含まない — 実 AffiliateLinkTarget の
    identity を騙らない。token + 固定 destination からのみ決定的に計算する。
    """

    identity = {
        "probe": PROBE_NAMESPACE,
        "schema_version": PROBE_SCHEMA_VERSION,
        "token": token,
        "destination_url": PROBE_DESTINATION_URL,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def _is_canonical_utc(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return canonical_datetime(parsed) == value


# ==================== structural + transition validation =================
def _validate_structure(candidate: dict[str, Any]) -> None:
    if not isinstance(candidate, dict):
        raise SyntheticProbeStateError("probe state is not a JSON object")

    keys = set(candidate)
    if keys != FIELD_NAMES:
        missing = sorted(FIELD_NAMES - keys)
        extra = sorted(keys - FIELD_NAMES)
        raise SyntheticProbeStateError(
            f"probe state has unexpected shape (missing={missing}, extra={extra})"
        )
    if not FORBIDDEN_STATE_FIELDS.isdisjoint(keys):
        raise SyntheticProbeStateError("probe state contains a forbidden field")

    if candidate["probe_schema_version"] != PROBE_SCHEMA_VERSION:
        raise SyntheticProbeStateError("unsupported probe_schema_version")
    if candidate["state"] not in PROBE_STATES:
        raise SyntheticProbeStateError(f"unknown probe state {candidate['state']!r}")

    token = candidate["token"]
    if not is_well_formed_token(token):
        raise SyntheticProbeStateError("token is not well-formed")
    if candidate["token_fingerprint"] != token_fingerprint(token):
        raise SyntheticProbeStateError("token_fingerprint does not match token")
    if candidate["destination_url"] != PROBE_DESTINATION_URL:
        raise SyntheticProbeStateError("destination_url does not match the fixed probe destination")
    if candidate["destination_host"] != PROBE_DESTINATION_HOST:
        raise SyntheticProbeStateError(
            "destination_host does not match the fixed probe destination"
        )
    if candidate["link_identity_hash"] != compute_synthetic_link_identity_hash(token):
        raise SyntheticProbeStateError(
            "link_identity_hash is not recomputable from token + destination"
        )

    for field in _HASH_FIELDS:
        value = candidate[field]
        if value is not None and not _HEX64.match(value):
            raise SyntheticProbeStateError(f"{field} is not a hex64 hash")

    for field in _TIMESTAMP_FIELDS:
        value = candidate[field]
        if value is not None and not _is_canonical_utc(value):
            raise SyntheticProbeStateError(f"{field} is not a canonical UTC timestamp")

    for field in _POSITIVE_INT_FIELDS:
        value = candidate[field]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise SyntheticProbeStateError(f"{field} must be a positive integer")

    if not isinstance(candidate["click_contaminated"], bool):
        raise SyntheticProbeStateError("click_contaminated must be a boolean")


def _validate_initial(candidate: dict[str, Any]) -> None:
    if candidate["state"] != STATE_INITIALIZED:
        raise SyntheticProbeStateError("the first persisted probe state must be 'initialized'")
    if candidate["click_contaminated"] is not False:
        raise SyntheticProbeStateError("click_contaminated must start false")
    always_null = (
        FIELD_NAMES - _INIT_FIELDS - {"state", "probe_schema_version", "click_contaminated"}
    )
    for field in always_null:
        if candidate[field] is not None:
            raise SyntheticProbeStateError(f"{field} must be null in the initial state")


def _validate_transition(previous: dict[str, Any], candidate: dict[str, Any]) -> None:
    prev_state = previous["state"]
    new_state = candidate["state"]
    allowed = PROBE_TRANSITIONS.get(prev_state, frozenset())
    if new_state not in allowed:
        raise SyntheticProbeStateError(
            f"illegal probe state transition: {prev_state!r} -> {new_state!r}"
        )
    new_fields = TRANSITION_NEW_FIELDS[(prev_state, new_state)]
    for field in FIELD_NAMES:
        if field in ("state", "click_contaminated"):
            continue
        prev_val = previous.get(field)
        new_val = candidate.get(field)
        if field in new_fields:
            if field in _OVERWRITABLE_ON_REENTRY:
                # legitimate re-attempt after a reconcile-absent revert: replace
                # with a fresh timestamp, no matter whether one was set before.
                if new_val is None:
                    raise SyntheticProbeStateError(f"{field} is required for this transition")
            else:
                if prev_val is not None:
                    raise SyntheticProbeStateError(
                        f"{field} was already frozen; cannot set again"
                    )
                if new_val is None:
                    raise SyntheticProbeStateError(f"{field} is required for this transition")
        elif new_val != prev_val:
            raise SyntheticProbeStateError(f"{field} must not change on this transition")

    if previous.get("click_contaminated") and not candidate.get("click_contaminated"):
        raise SyntheticProbeStateError("click_contaminated must never be cleared")


def _new_initialized_state(token: str) -> dict[str, Any]:
    state: dict[str, Any] = dict.fromkeys(FIELD_NAMES - _INIT_FIELDS - {"click_contaminated"})
    state.update(
        probe_schema_version=PROBE_SCHEMA_VERSION,
        state=STATE_INITIALIZED,
        token=token,
        token_fingerprint=token_fingerprint(token),
        destination_url=PROBE_DESTINATION_URL,
        destination_host=PROBE_DESTINATION_HOST,
        link_identity_hash=compute_synthetic_link_identity_hash(token),
        click_contaminated=False,
    )
    return state


# ==================== state-file path resolution =========================
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_state_path(*, cli_path: str | None, settings: object) -> Path:
    """``--state-file`` -> ``Settings.affiliate_probe_state_file`` -> fail closed。

    repo working tree の内側 (root 自身を含む) は拒否する。親ディレクトリは
    既に存在していなければならない (自動作成しない)。
    """

    raw = cli_path or getattr(settings, "affiliate_probe_state_file", None)
    if not raw:
        raise SyntheticProbeConfigError(
            "probe state file path is not configured "
            "(--state-file or AFFILIATE_PROBE_STATE_FILE)"
        )
    path = Path(str(raw)).expanduser().resolve()

    repo_root = _repo_root()
    try:
        path.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise SyntheticProbeConfigError(
            "probe state file must be outside the repository working tree"
        )

    if not path.parent.is_dir():
        raise SyntheticProbeConfigError(
            f"probe state file parent directory does not exist: {path.parent}"
        )
    return path


# ==================== atomic, crash-safe persistence ======================
def _serialize(candidate: dict[str, Any]) -> bytes:
    return json.dumps(candidate, ensure_ascii=False, sort_keys=True, indent=2).encode(
        "utf-8"
    ) + b"\n"


def _atomic_replace(path: Path, data: bytes) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(data)
            fh.flush()
            with suppress(OSError, AttributeError):
                os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with suppress(OSError):
            tmp_path.unlink()
        raise
    with suppress(OSError, AttributeError, NotImplementedError):
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SyntheticProbeStateError(f"probe state file does not exist: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        candidate = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise SyntheticProbeStateError(
            "probe state file is unreadable or not valid JSON"
        ) from exc
    _validate_structure(candidate)
    return candidate


def write_state_atomic(
    path: Path, candidate: dict[str, Any], *, previous: dict[str, Any] | None
) -> None:
    """candidate を厳格 validate してから crash-safe に atomic replace する。

    validation 失敗時は temp file すら作らず、既存の authoritative file を
    一切変更しない。
    """

    _validate_structure(candidate)
    if previous is None:
        _validate_initial(candidate)
    else:
        _validate_transition(previous, candidate)
    _atomic_replace(path, _serialize(candidate))


# ==================== exclusive, OS-backed, auto-releasing lock ===========
class ProbeLock:
    """``<state path>.lock`` に対する排他 lock。action 全体を通して保持する。

    Windows: ``msvcrt.locking`` によるバイト範囲 lock。
    POSIX: ``fcntl.flock(LOCK_EX | LOCK_NB)``。

    どちらもプロセス終了 (crash 含む) で OS が自動的に解放する。lock file 自体の
    存在は「locked」を意味しない — OS lock だけが authoritative。
    """

    _REGION_LEN = 1

    def __init__(self, state_path: Path) -> None:
        self._lock_path = state_path.with_name(state_path.name + ".lock")
        self._meta_path = state_path.with_name(state_path.name + ".lock.meta.json")
        self._fh: Any = None

    def __enter__(self) -> ProbeLock:
        fh = open(self._lock_path, "a+b")
        try:
            fh.seek(0)
            if len(fh.read(self._REGION_LEN)) < self._REGION_LEN:
                fh.seek(0)
                fh.write(b"\x00" * self._REGION_LEN)
                fh.flush()
            fh.seek(0)
            _platform_lock(fh, self._REGION_LEN)
        except OSError as exc:
            fh.close()
            raise SyntheticProbeLockError(
                "probe state is locked by another process"
            ) from exc
        self._fh = fh
        self._write_metadata(token_fingerprint=None)
        return self

    def record_fingerprint(self, fingerprint: str) -> None:
        """diagnostics 専用。lock の authoritative 性には影響しない。"""

        self._write_metadata(token_fingerprint=fingerprint)

    def _write_metadata(self, *, token_fingerprint: str | None) -> None:
        meta = {
            "pid": os.getpid(),
            "acquired_at": canonical_datetime(datetime.now(UTC)),
            "token_fingerprint": token_fingerprint,
        }
        with suppress(OSError):
            self._meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )

    def __exit__(self, *exc_info: object) -> None:
        if self._fh is not None:
            try:
                with suppress(OSError):
                    self._fh.seek(0)
                    _platform_unlock(self._fh, self._REGION_LEN)
            finally:
                self._fh.close()
                self._fh = None


def _platform_lock(fh: Any, length: int) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, length)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _platform_unlock(fh: Any, length: int) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, length)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ==================== local collision check ================================
def check_no_local_collision(session: Session, token: str) -> None:
    """synthetic token が既存の local AffiliateLinkTarget と衝突していないか (read-only)。

    WordPress runtime の token を列挙する endpoint は存在しない — ここでは
    Human が確認済みの「runtime targets = 0」という前提を local 側で補強するだけ。
    """

    existing = AffiliateLinkTargetRepository(session).get_by_token(token)
    if existing is not None:
        raise SyntheticProbeStateError(
            "synthetic probe token collides with an existing local AffiliateLinkTarget"
        )


# ==================== projection material (active / disabled) =============
def _build_active_projection(
    *, token: str, link_identity_hash: str, activated_at: datetime
) -> tuple[AffiliateTargetProjection, Any]:
    entry = build_projection_entry(
        token=token,
        destination_url=PROBE_DESTINATION_URL,
        destination_host=PROBE_DESTINATION_HOST,
        link_identity_hash=link_identity_hash,
        alt_status=ALT_ACTIVE,
        activated_at=activated_at,
        disabled_at=None,
    )
    snapshot = build_projection_snapshot([entry])
    return entry, snapshot


def _build_disabled_projection(
    *, token: str, link_identity_hash: str, activated_at: datetime, disabled_at: datetime
) -> tuple[AffiliateTargetProjection, Any]:
    entry = build_projection_entry(
        token=token,
        destination_url=PROBE_DESTINATION_URL,
        destination_host=PROBE_DESTINATION_HOST,
        link_identity_hash=link_identity_hash,
        alt_status=ALT_DISABLED,
        activated_at=activated_at,
        disabled_at=disabled_at,
    )
    snapshot = build_projection_snapshot([entry])
    return entry, snapshot


def _assert_active_material_consistent(state: dict[str, Any]) -> None:
    activated_at = datetime.fromisoformat(state["activated_at"])
    entry, snapshot = _build_active_projection(
        token=state["token"],
        link_identity_hash=state["link_identity_hash"],
        activated_at=activated_at,
    )
    if entry.projection_entry_hash != state["active_projection_entry_hash"]:
        raise SyntheticProbeStateError(
            "frozen active_projection_entry_hash is no longer reproducible"
        )
    if snapshot.projection_snapshot_hash != state["active_projection_snapshot_hash"]:
        raise SyntheticProbeStateError(
            "frozen active_projection_snapshot_hash is no longer reproducible"
        )


def _require_projection_outcome(
    result: ProjectionPushResult, *, expected: tuple[int, int, int, int], label: str
) -> None:
    actual = (
        result.received_count,
        result.inserted_count,
        result.updated_count,
        result.unchanged_count,
    )
    if actual != expected:
        raise AffiliateProjectionPushError(
            f"unexpected {label} projection outcome for the synthetic probe"
        )


def _assert_disabled_material_consistent(state: dict[str, Any]) -> None:
    activated_at = datetime.fromisoformat(state["activated_at"])
    disabled_at = datetime.fromisoformat(state["disabled_at"])
    entry, snapshot = _build_disabled_projection(
        token=state["token"],
        link_identity_hash=state["link_identity_hash"],
        activated_at=activated_at,
        disabled_at=disabled_at,
    )
    if entry.projection_entry_hash != state["disabled_projection_entry_hash"]:
        raise SyntheticProbeStateError(
            "frozen disabled_projection_entry_hash is no longer reproducible"
        )
    if snapshot.projection_snapshot_hash != state["disabled_projection_snapshot_hash"]:
        raise SyntheticProbeStateError(
            "frozen disabled_projection_snapshot_hash is no longer reproducible"
        )


# ==================== actions: init / status ================================
def action_init(path: Path) -> dict[str, Any]:
    """既存の有効な state があれば再利用する (token は再生成しない)。壊れていれば
    fail closed (置き換えない)。通信なし。"""

    if path.is_file():
        return load_state(path)
    token = generate_token()
    state = _new_initialized_state(token)
    write_state_atomic(path, state, previous=None)
    return state


def action_status(path: Path) -> dict[str, Any]:
    return load_state(path)


# ==================== actions: active =======================================
def action_active_plan(
    path: Path, session: Session, *, now: datetime | None = None
) -> dict[str, Any]:
    state = load_state(path)
    if state["state"] not in (STATE_INITIALIZED, STATE_ACTIVE_PREPARED):
        raise SyntheticProbeStateError(
            f"--active requires state=initialized or active_prepared "
            f"(current={state['state']!r})"
        )
    check_no_local_collision(session, state["token"])

    if state["state"] == STATE_ACTIVE_PREPARED:
        _assert_active_material_consistent(state)
        return state

    now = now or datetime.now(UTC)
    entry, snapshot = _build_active_projection(
        token=state["token"], link_identity_hash=state["link_identity_hash"], activated_at=now
    )
    candidate = dict(state)
    candidate["state"] = STATE_ACTIVE_PREPARED
    candidate["activated_at"] = entry.activated_at
    candidate["active_projection_entry_hash"] = entry.projection_entry_hash
    candidate["active_projection_snapshot_hash"] = snapshot.projection_snapshot_hash
    write_state_atomic(path, candidate, previous=state)
    return candidate


def action_active_execute(
    path: Path,
    *,
    settings: object,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any], ProjectionPushResult]:
    state = load_state(path)
    if state["state"] != STATE_ACTIVE_PREPARED:
        raise SyntheticProbeStateError(
            f"--active --execute requires active_prepared (current={state['state']!r})"
        )
    _assert_active_material_consistent(state)
    if not getattr(settings, "affiliate_runtime_push_configured", False):
        raise SyntheticProbeConfigError("affiliate runtime is not configured")

    attempted = dict(state)
    attempted["state"] = STATE_ACTIVE_ATTEMPTED
    attempted["active_attempted_at"] = canonical_datetime(datetime.now(UTC))
    write_state_atomic(path, attempted, previous=state)  # durable BEFORE network

    activated_at = datetime.fromisoformat(state["activated_at"])
    entry, snapshot = _build_active_projection(
        token=state["token"],
        link_identity_hash=state["link_identity_hash"],
        activated_at=activated_at,
    )
    prepared = prepare_projection_push(
        snapshot,
        base_url=settings.wordpress_base_url,
        shared_secret=settings.affiliate_runtime_shared_secret,
    )
    result = execute_projection_push(
        prepared,
        transport=transport,
        verify_tls=getattr(settings, "wordpress_verify_tls", True),
    )
    _require_projection_outcome(result, expected=(1, 1, 0, 0), label="active")

    confirmed = dict(attempted)
    confirmed["state"] = STATE_ACTIVE_CONFIRMED
    confirmed["active_confirmed_at"] = canonical_datetime(datetime.now(UTC))
    write_state_atomic(path, confirmed, previous=attempted)
    return confirmed, result


# ==================== actions: /go ==========================================
def _go_url(base_url: str, token: str) -> str:
    origin = require_https_origin(base_url, error_cls=SyntheticProbeConfigError)
    return f"{origin}/go/{token}"


def _execute_go_request(
    url: str, *, transport: httpx.BaseTransport | None, verify_tls: bool
) -> httpx.Response:
    try:
        with httpx.Client(
            transport=transport, verify=verify_tls, timeout=15.0, follow_redirects=False
        ) as client:
            return client.get(url)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise SyntheticProbeAmbiguousError("/go request failed; outcome unknown") from exc


def _assert_go_success(response: httpx.Response) -> None:
    if response.status_code != 302:
        raise SyntheticProbeAmbiguousError(
            f"/go returned HTTP {response.status_code}, expected 302"
        )
    if response.headers.get("location") != PROBE_DESTINATION_URL:
        raise SyntheticProbeAmbiguousError(
            "/go Location did not match the exact synthetic destination"
        )
    cache_control = (response.headers.get("cache-control") or "").lower()
    if "no-store" not in cache_control:
        raise SyntheticProbeAmbiguousError("/go response is missing Cache-Control: no-store")
    robots = (response.headers.get("x-robots-tag") or "").lower()
    if "noindex" not in robots or "nofollow" not in robots:
        raise SyntheticProbeAmbiguousError("/go response is missing the expected X-Robots-Tag")


def action_go_plan(path: Path) -> dict[str, Any]:
    """--go (PLAN)。read-only: 期待する assertion を説明するだけで通信しない。"""

    state = load_state(path)
    if state["state"] != STATE_ACTIVE_CONFIRMED:
        raise SyntheticProbeStateError(
            f"--go requires active_confirmed (current={state['state']!r})"
        )
    return state


def action_go_execute(
    path: Path, *, settings: object, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    state = load_state(path)
    if state["state"] != STATE_ACTIVE_CONFIRMED:
        raise SyntheticProbeStateError(
            f"--go --execute requires active_confirmed (current={state['state']!r})"
        )
    base_url = getattr(settings, "wordpress_base_url", None)
    if not base_url:
        raise SyntheticProbeConfigError("WORDPRESS_BASE_URL is not configured")

    attempted = dict(state)
    attempted["state"] = STATE_CLICK_ATTEMPTED
    attempted["click_attempted_at"] = canonical_datetime(datetime.now(UTC))
    write_state_atomic(path, attempted, previous=state)  # durable BEFORE network

    url = _go_url(base_url, state["token"])
    response = _execute_go_request(
        url, transport=transport, verify_tls=getattr(settings, "wordpress_verify_tls", True)
    )
    _assert_go_success(response)

    confirmed = dict(attempted)
    confirmed["state"] = STATE_CLICK_CONFIRMED
    confirmed["click_confirmed_at"] = canonical_datetime(datetime.now(UTC))
    write_state_atomic(path, confirmed, previous=attempted)
    return confirmed


# ==================== action: record-import (read-only) ====================
def action_record_import(path: Path, session: Session) -> dict[str, Any]:
    """click_confirmed -> import_confirmed。**read-only**: click import 自体は
    行わない・WordPress へは通信しない。C4 で Human が既存の formal import CLI
    を実行した後の local evidence を確認するだけ。"""

    state = load_state(path)
    if state["state"] != STATE_CLICK_CONFIRMED:
        raise SyntheticProbeStateError(
            f"--record-import requires click_confirmed (current={state['state']!r})"
        )

    token = state["token"]
    rows = session.scalars(
        select(AffiliateOutboundClick).where(AffiliateOutboundClick.token == token)
    ).all()
    if len(rows) != 1:
        raise SyntheticProbeStateError(
            "expected exactly one imported replica click for the synthetic token, "
            f"found {len(rows)}"
        )
    click = rows[0]

    run = session.get(AffiliateClickImportRun, click.source_import_run_id)
    if run is None or run.status != "succeeded":
        raise SyntheticProbeStateError(
            "the import run that persisted the synthetic click is not succeeded"
        )
    if run.unresolved_token_count != 1:
        raise SyntheticProbeStateError(
            f"expected unresolved_token_count=1 on the import run, found "
            f"{run.unresolved_token_count!r}"
        )

    replica_max = session.scalar(
        select(AffiliateOutboundClick.source_click_id).order_by(
            AffiliateOutboundClick.source_click_id.desc()
        )
    )
    if (replica_max or 0) != (run.response_next_since_id or 0):
        raise SyntheticProbeStateError(
            "local click cursor invariant does not hold after the synthetic import"
        )

    confirmed = dict(state)
    confirmed["state"] = STATE_IMPORT_CONFIRMED
    confirmed["source_click_id"] = click.source_click_id
    confirmed["import_run_id"] = run.id
    confirmed["import_confirmed_at"] = canonical_datetime(datetime.now(UTC))
    write_state_atomic(path, confirmed, previous=state)
    return confirmed


# ==================== actions: disable ======================================
def action_disable_plan(
    path: Path, session: Session, *, now: datetime | None = None
) -> dict[str, Any]:
    state = load_state(path)
    if state["state"] not in (STATE_IMPORT_CONFIRMED, STATE_DISABLE_PREPARED):
        raise SyntheticProbeStateError(
            f"--disable requires state=import_confirmed or disable_prepared "
            f"(current={state['state']!r})"
        )
    if state["state"] == STATE_DISABLE_PREPARED:
        _assert_disabled_material_consistent(state)
        return state

    now = now or datetime.now(UTC)
    activated_at = datetime.fromisoformat(state["activated_at"])
    entry, snapshot = _build_disabled_projection(
        token=state["token"],
        link_identity_hash=state["link_identity_hash"],
        activated_at=activated_at,
        disabled_at=now,
    )
    candidate = dict(state)
    candidate["state"] = STATE_DISABLE_PREPARED
    candidate["disabled_at"] = entry.disabled_at
    candidate["disabled_projection_entry_hash"] = entry.projection_entry_hash
    candidate["disabled_projection_snapshot_hash"] = snapshot.projection_snapshot_hash
    write_state_atomic(path, candidate, previous=state)
    return candidate


def action_disable_execute(
    path: Path,
    *,
    settings: object,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any], ProjectionPushResult]:
    state = load_state(path)
    if state["state"] != STATE_DISABLE_PREPARED:
        raise SyntheticProbeStateError(
            f"--disable --execute requires disable_prepared (current={state['state']!r})"
        )
    _assert_disabled_material_consistent(state)
    if not getattr(settings, "affiliate_runtime_push_configured", False):
        raise SyntheticProbeConfigError("affiliate runtime is not configured")

    attempted = dict(state)
    attempted["state"] = STATE_DISABLE_ATTEMPTED
    attempted["disable_attempted_at"] = canonical_datetime(datetime.now(UTC))
    write_state_atomic(path, attempted, previous=state)  # durable BEFORE network

    activated_at = datetime.fromisoformat(state["activated_at"])
    disabled_at = datetime.fromisoformat(state["disabled_at"])
    entry, snapshot = _build_disabled_projection(
        token=state["token"],
        link_identity_hash=state["link_identity_hash"],
        activated_at=activated_at,
        disabled_at=disabled_at,
    )
    prepared = prepare_projection_push(
        snapshot,
        base_url=settings.wordpress_base_url,
        shared_secret=settings.affiliate_runtime_shared_secret,
    )
    result = execute_projection_push(
        prepared,
        transport=transport,
        verify_tls=getattr(settings, "wordpress_verify_tls", True),
    )
    _require_projection_outcome(result, expected=(1, 0, 1, 0), label="disable")

    confirmed = dict(attempted)
    confirmed["state"] = STATE_DISABLED_CONFIRMED
    confirmed["disabled_confirmed_at"] = canonical_datetime(datetime.now(UTC))
    write_state_atomic(path, confirmed, previous=attempted)
    return confirmed, result


# ==================== reconciliation (explicit only; never implicit) =======
def action_reconcile_active(path: Path, *, observed: str) -> dict[str, Any]:
    if observed not in ("present", "absent"):
        raise SyntheticProbeStateError("--observed must be 'present' or 'absent'")
    state = load_state(path)
    if state["state"] != STATE_ACTIVE_ATTEMPTED:
        raise SyntheticProbeStateError(
            f"--reconcile-active requires active_attempted (current={state['state']!r})"
        )
    candidate = dict(state)
    if observed == "present":
        candidate["state"] = STATE_ACTIVE_CONFIRMED
        candidate["active_confirmed_at"] = canonical_datetime(datetime.now(UTC))
    else:
        candidate["state"] = STATE_ACTIVE_PREPARED
    write_state_atomic(path, candidate, previous=state)
    return candidate


def _count_matching_click_rows_via_export(
    *, settings: object, transport: httpx.BaseTransport | None, token: str
) -> int:
    """例外的な read-only reconciliation GET。通常 C1-C5 の request 数には含めない。"""

    prepared = prepare_click_export(
        base_url=settings.wordpress_base_url,
        shared_secret=settings.affiliate_runtime_shared_secret,
        since_id=0,
        limit=CLICK_EXPORT_MAX_LIMIT,
    )
    page = execute_click_export(
        prepared, transport=transport, verify_tls=getattr(settings, "wordpress_verify_tls", True)
    )
    return sum(1 for row in page.rows if row.token == token)


def action_reconcile_go(
    path: Path, *, settings: object, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    state = load_state(path)
    if state["state"] != STATE_CLICK_ATTEMPTED:
        raise SyntheticProbeStateError(
            f"--reconcile-go requires click_attempted (current={state['state']!r})"
        )
    matching = _count_matching_click_rows_via_export(
        settings=settings, transport=transport, token=state["token"]
    )
    candidate = dict(state)
    if matching == 0:
        candidate["state"] = STATE_ACTIVE_CONFIRMED
    elif matching == 1:
        candidate["state"] = STATE_CLICK_CONFIRMED
        candidate["click_confirmed_at"] = canonical_datetime(datetime.now(UTC))
    else:
        # fail closed: state を進めない。行は絶対に削除しない。
        candidate["state"] = STATE_CLICK_ATTEMPTED
        candidate["click_contaminated"] = True
    write_state_atomic(path, candidate, previous=state)
    return candidate


def action_reconcile_disable(path: Path, *, observed: str) -> dict[str, Any]:
    if observed not in ("present", "absent"):
        raise SyntheticProbeStateError("--observed must be 'present' or 'absent'")
    state = load_state(path)
    if state["state"] != STATE_DISABLE_ATTEMPTED:
        raise SyntheticProbeStateError(
            f"--reconcile-disable requires disable_attempted (current={state['state']!r})"
        )
    candidate = dict(state)
    if observed == "present":
        candidate["state"] = STATE_DISABLED_CONFIRMED
        candidate["disabled_confirmed_at"] = canonical_datetime(datetime.now(UTC))
    else:
        candidate["state"] = STATE_DISABLE_PREPARED
    write_state_atomic(path, candidate, previous=state)
    return candidate
