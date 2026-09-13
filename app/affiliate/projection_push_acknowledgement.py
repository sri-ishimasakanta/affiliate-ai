"""Affiliate target projection push の acknowledgement 証跡 (pure)。

D-D0.2/D-D0.3 で確定した事実を実装する:

- WordPress の projection endpoint は **request 全体が atomic** (全部 commit される
  か、全く何も変わらないかのどちらか)。レスポンスは per-target のデータを返さない
  — batch レベルの集約のみ。
- よって「何を要求したか」(request manifest) は **送信側が既に知っている事実として
  凍結する**。「受理されたか」は batch レベルの集約 (``ProjectionPushResult``) だけで
  証明できる — 個別 target の echo は不要だし、存在しない。
- ``failed`` は「明確に何も変わらなかった」と **PHP source を直接 trace して証明できた
  code** に限る。Human review により、``body_hash_mismatch`` / ``bad_signature`` は
  現行デプロイ済み PHP からの emission path が見つからなかったため、**definitive
  failed set から明示的に除外** する (受信したら ``outcome_unknown``)。

このモジュールは DB / HTTP を一切知らない。session も client も受け取らない。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.affiliate.projection import AffiliateTargetProjection
from app.article.draft_input_canonical import canonical_json
from app.models.affiliate_target_projection_push_run import (
    ATPP_FAILED,
    ATPP_OUTCOME_UNKNOWN,
    ATPP_RUNNING,
    ATPP_SUCCEEDED,
)

SNAPSHOT_SCOPE_FULL = "full"

# --- request manifest ------------------------------------------------------
_MANIFEST_FIELDS = (
    "affiliate_link_target_id",
    "token_fingerprint",
    "link_identity_hash",
    "status",
    "projection_version",
    "entry_hash",
)


def token_fingerprint(token: str) -> str:
    """full token の SHA-256 hex。manifest / 監査にはこの値のみを使う。"""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_request_manifest(
    *,
    orm_targets: Iterable[Any],
    snapshot_targets: Sequence[AffiliateTargetProjection],
) -> list[dict[str, Any]]:
    """署名済み snapshot に **実際に含まれる** target だけの、request 側の凍結事実。

    ``orm_targets`` は local ``AffiliateLinkTarget`` 行 (id 取得用、token で対応付け)。
    ``snapshot_targets`` は ``prepared``/署名対象と **同一の** snapshot オブジェクトの
    ``.targets`` を渡すこと (§D-D0.3-13: 送信後に DB を再読込しない)。

    destination_url / full token は一切含めない。
    """

    by_token = {t.token: t for t in orm_targets}
    entries: list[dict[str, Any]] = []
    for proj in snapshot_targets:
        local = by_token.get(proj.token)
        if local is None:
            # プロジェクション対象の token がロード済み ORM 行に見つからない
            # (呼び出し側のバグ) — 沈黙せずに fail closed する。
            raise ValueError(
                "snapshot target token not found among the loaded local targets"
            )
        entries.append(
            {
                "affiliate_link_target_id": local.id,
                "token_fingerprint": token_fingerprint(proj.token),
                "link_identity_hash": proj.link_identity_hash,
                "status": proj.status,
                "projection_version": proj.projection_version,
                "entry_hash": proj.projection_entry_hash,
            }
        )

    entries.sort(key=lambda e: e["affiliate_link_target_id"])
    ids = [e["affiliate_link_target_id"] for e in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate affiliate_link_target_id in projection manifest")
    return entries


def serialize_manifest(entries: Sequence[Mapping[str, Any]]) -> str:
    """canonical JSON 文字列 (配列順は呼び出し側が既に ordinal でソート済みの前提)。"""

    return canonical_json(list(entries))


def parse_manifest(manifest_json: str) -> list[dict[str, Any]]:
    data = json.loads(manifest_json)
    if not isinstance(data, list):
        raise ValueError("stored request_manifest_json is not a JSON array")
    for entry in data:
        if not isinstance(entry, dict) or set(entry) != set(_MANIFEST_FIELDS):
            raise ValueError("stored request_manifest_json entry has an unexpected shape")
    return data


# --- definitive failed classification (D-D0.3 + Human review correction) ---
# PHP handler source を直接 trace し、**受信した時点で projection データへの
# 書き込みが一切無かったことが証明できる** code のみ。body_hash_mismatch /
# bad_signature は現行デプロイ済み PHP からの emission path が見つからないため
# 意図的に除外する (Human review ruling, D-D1A §2) — 受信時は outcome_unknown。
DEFINITIVE_FAILED_SERVER_CODES = frozenset(
    {
        # HMAC 検証前後、body parse 前 (DB 未アクセス)。
        "secret_not_configured",
        "bad_timestamp",
        "content_sha256_mismatch",
        "timestamp_outside_window",
        "signature_mismatch",
        # body/schema 検証 (DB 未アクセス)。
        "invalid_json",
        "bad_schema_version",
        "bad_targets",
        # per-entry semantic validation loop (transaction 開始前)。
        "bad_entry",
        "bad_token",
        "bad_status",
        "bad_version_for_status",
        "bad_link_identity_hash",
        "bad_projection_entry_hash",
        "bad_destination_url",
        "bad_destination_host",
        "bad_activated_at",
        "bad_disabled_at",
        "entry_hash_mismatch",
        "destination_validation_failed",
        "snapshot_hash_mismatch",
        # decision-planning loop (transaction 開始前)。
        "conflict_hash_mismatch",
        "conflict_stale_version",
        "forbidden_reactivation",
        "conflict_immutable_identity_drift",
        # transaction 内で失敗 -> 応答直前に明示 ROLLBACK (source-traced)。
        "persist_failed",
    }
)


def is_definitive_failure_code(server_code: str | None) -> bool:
    """この code が「受信できれば zero-side-effect が証明される」ものか。

    ``None`` (未知/非 JSON/形状不一致/HMAC 未到達) は常に False —
    呼び出し側が PHP handler の応答だと確認できていない。
    """

    return server_code is not None and server_code in DEFINITIVE_FAILED_SERVER_CODES


# --- acknowledgement resolver ------------------------------------------
@dataclass(frozen=True)
class AcknowledgementResolution:
    """1 runtime_origin の現在の authoritative acknowledgement (pure 計算結果)。

    ``manifest`` が ``None`` なら "acknowledgement なし / fail closed"。
    """

    manifest: list[dict[str, Any]] | None
    source_run_id: int | None

    @property
    def acknowledged(self) -> bool:
        return self.manifest is not None


def resolve_latest_acknowledgement(
    runs_newest_first: Sequence[Any],
) -> AcknowledgementResolution:
    """``runs_newest_first`` は 1 runtime_origin の run を
    ``created_at DESC, id DESC`` で並べたもの (呼び出し側がその順序を保証すること)。

    - 最新が succeeded -> その request_manifest_json が authoritative。
    - 最新が running/outcome_unknown -> fail closed (それより古い row は見ない)。
    - 最新が failed (definitive) -> 透過的にスキップして 1 つ前を見る。
    - failed が連続する間はそのまま遡る。running/outcome_unknown に当たったら
      その時点で fail closed。succeeded に当たったらそれを使う。
    - 何も見つからなければ acknowledgement なし。
    """

    for run in runs_newest_first:
        if run.status == ATPP_SUCCEEDED:
            return AcknowledgementResolution(
                manifest=parse_manifest(run.request_manifest_json),
                source_run_id=run.id,
            )
        if run.status in (ATPP_RUNNING, ATPP_OUTCOME_UNKNOWN):
            return AcknowledgementResolution(manifest=None, source_run_id=None)
        if run.status != ATPP_FAILED:  # pragma: no cover - defensive
            raise ValueError(f"unexpected run status {run.status!r}")
        # failed: 透過的 (zero-side-effect が証明されている) -> 遡り続ける。

    return AcknowledgementResolution(manifest=None, source_run_id=None)


# --- per-target eligibility ------------------------------------------------
def is_target_eligible(
    resolution: AcknowledgementResolution,
    *,
    affiliate_link_target_id: int,
    expected_token_fingerprint: str,
    expected_link_identity_hash: str,
    expected_status: str,
    expected_projection_version: int,
    expected_entry_hash: str,
) -> bool:
    """target が現在 substitution 対象として適格か (D-D0.1 §7 条件 3 の一部)。

    latest authoritative manifest に一致するエントリが無い/不一致なら
    常に False (fail closed)。古い manifest を蘇らせて参照することはしない。
    """

    if resolution.manifest is None:
        return False
    for entry in resolution.manifest:
        if entry["affiliate_link_target_id"] != affiliate_link_target_id:
            continue
        return (
            entry["token_fingerprint"] == expected_token_fingerprint
            and entry["link_identity_hash"] == expected_link_identity_hash
            and entry["status"] == expected_status
            and entry["projection_version"] == expected_projection_version
            and entry["entry_hash"] == expected_entry_hash
        )
    return False
