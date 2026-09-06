"""WordPress runtime 向け Affiliate target **projection contract** (pure)。

affiliate-ai (control plane) が保持する :class:`AffiliateLinkTarget` から、公開 redirect
runtime (WordPress/XServer) が redirect 実行だけに必要な最小状態を deterministic に
取り出す。ここでは **成果物 (artifact) の形と hash と upsert 判定** を定義するだけで、
HTTP client も WordPress code も持たない。

Article / program / commission / notes / credential / IP / UA / Referer / cookie /
session は projection に入れない。``destination_url`` は D-B1 で凍結した Human 入力の
exact 文字列を **無改変** で運ぶ。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from app.article.draft_input_canonical import canonical_datetime, canonical_json
from app.models.affiliate_link_target import (
    ALT_ACTIVE,
    ALT_DISABLED,
    ALT_SUPERSEDED,
)

PROJECTION_SCHEMA_VERSION = 1

PROJECTION_STATUS_ACTIVE = "active"
PROJECTION_STATUS_DISABLED = "disabled"

# per-token の deterministic version。control-plane target は 1 回の terminal 遷移
# しか持たないので active=1 / disabled(=superseded 含む)=2 で十分。
PROJECTION_VERSION_ACTIVE = 1
PROJECTION_VERSION_DISABLED = 2

# 同一 token に対して runtime が上書きしてはならない不変フィールド。
IMMUTABLE_ENTRY_FIELDS = (
    "token",
    "destination_url",
    "destination_host",
    "link_identity_hash",
)

# upsert 判定結果。
UPSERT_INSERT = "insert"
UPSERT_NOOP = "noop"
UPSERT_UPDATE = "update"
REJECT_HASH_CONFLICT = "conflict_hash_mismatch"
REJECT_STALE_VERSION = "conflict_stale_version"
REJECT_FORBIDDEN_REACTIVATION = "forbidden_reactivation"
REJECT_IMMUTABLE_DRIFT = "conflict_immutable_identity_drift"
_OK_RESULTS = frozenset({UPSERT_INSERT, UPSERT_NOOP, UPSERT_UPDATE})


class AffiliateProjectionError(ValueError):
    """control-plane target が V1 runtime projection の対象として不適
    (Unicode host / host 不一致 / 未知 status など)。control-plane 保存自体は妨げない。
    メッセージに destination_url 本体は含めない。
    """


@dataclass(frozen=True)
class AffiliateTargetProjection:
    token: str
    destination_url: str  # D-B1 で凍結した exact 文字列 (無改変)
    destination_host: str  # ascii lowercase の正規化ホスト名
    status: str  # active | disabled
    link_identity_hash: str
    projection_version: int  # 1 | 2
    activated_at: str | None  # canonical UTC 文字列
    disabled_at: str | None
    projection_entry_hash: str

    def entry_payload(self) -> dict:
        """batch/response で使う payload。``projection_entry_hash`` も含む。"""

        return {
            "token": self.token,
            "destination_url": self.destination_url,
            "destination_host": self.destination_host,
            "status": self.status,
            "link_identity_hash": self.link_identity_hash,
            "projection_version": self.projection_version,
            "activated_at": self.activated_at,
            "disabled_at": self.disabled_at,
            "projection_entry_hash": self.projection_entry_hash,
        }


@dataclass(frozen=True)
class AffiliateProjectionSnapshot:
    schema_version: int
    targets: tuple[AffiliateTargetProjection, ...]  # token 昇順
    projection_snapshot_hash: str

    def payload(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "targets": [t.entry_payload() for t in self.targets],
        }


@dataclass(frozen=True)
class ProjectionUpsertDecision:
    result: str

    @property
    def ok(self) -> bool:
        return self.result in _OK_RESULTS


@dataclass(frozen=True)
class ProjectionBatchEvaluation:
    decisions: tuple[tuple[str, ProjectionUpsertDecision], ...]  # (token, decision)
    atomic_accepted: bool
    received_count: int
    inserted_count: int
    updated_count: int
    unchanged_count: int
    rejected_count: int


# -- status / version mapping -------------------------------------------
def runtime_status_and_version(alt_status: str) -> tuple[str, int]:
    """control-plane status -> (runtime status, projection_version)。"""

    if alt_status == ALT_ACTIVE:
        return PROJECTION_STATUS_ACTIVE, PROJECTION_VERSION_ACTIVE
    if alt_status in (ALT_DISABLED, ALT_SUPERSEDED):
        return PROJECTION_STATUS_DISABLED, PROJECTION_VERSION_DISABLED
    raise AffiliateProjectionError(
        f"unknown control-plane target status {alt_status!r}"
    )


# -- cross-runtime hostname contract ---------------------------------
def assert_runtime_projection_eligible(
    *, destination_url: str, destination_host: str
) -> None:
    """V1 runtime projection eligibility (PHP 側と deterministic に比較できること)。

    前提: ``destination_url`` は D-B1 の safety 検証を通過済み。ここでは:

    1. scheme が https
    2. parsed hostname が存在する
    3. runtime 比較に使う hostname 文字が **ASCII**
    4. ``ascii-lowercase(parsed hostname) == destination_host`` (完全一致)

    Unicode hostname (PHP 側で IDNA 変換が要る) は **fail closed**。
    destination_url を punycode へ書き換えて通すことはしない。
    """

    parts = urlsplit(destination_url)
    if parts.scheme.lower() != "https":
        raise AffiliateProjectionError(
            "destination_url scheme must be https for runtime projection"
        )
    host = parts.hostname
    if not host:
        raise AffiliateProjectionError("destination_url has no host")
    if not host.isascii():
        raise AffiliateProjectionError(
            "destination_url hostname is not ASCII-compatible; not "
            "runtime-projection-eligible in V1"
        )
    if host.lower() != destination_host:
        raise AffiliateProjectionError(
            "parsed hostname does not match the stored normalized destination_host"
        )


def is_runtime_projection_eligible(
    *, destination_url: str, destination_host: str
) -> bool:
    try:
        assert_runtime_projection_eligible(
            destination_url=destination_url, destination_host=destination_host
        )
    except AffiliateProjectionError:
        return False
    return True


# -- entry build -----------------------------------------------------
def _entry_hash(
    *,
    token: str,
    destination_url: str,
    destination_host: str,
    status: str,
    link_identity_hash: str,
    projection_version: int,
    activated_at: str | None,
    disabled_at: str | None,
) -> str:
    payload = {
        "token": token,
        "destination_url": destination_url,
        "destination_host": destination_host,
        "status": status,
        "link_identity_hash": link_identity_hash,
        "projection_version": projection_version,
        "activated_at": activated_at,
        "disabled_at": disabled_at,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_projection_entry(
    *,
    token: str,
    destination_url: str,
    destination_host: str,
    link_identity_hash: str,
    alt_status: str,
    activated_at: datetime | None,
    disabled_at: datetime | None,
) -> AffiliateTargetProjection:
    """1 target -> deterministic な projection entry。eligibility を満たさなければ raise。"""

    assert_runtime_projection_eligible(
        destination_url=destination_url, destination_host=destination_host
    )
    status, version = runtime_status_and_version(alt_status)
    activated_s = canonical_datetime(activated_at)
    disabled_s = (
        canonical_datetime(disabled_at)
        if status == PROJECTION_STATUS_DISABLED
        else None
    )
    entry_hash = _entry_hash(
        token=token,
        destination_url=destination_url,
        destination_host=destination_host,
        status=status,
        link_identity_hash=link_identity_hash,
        projection_version=version,
        activated_at=activated_s,
        disabled_at=disabled_s,
    )
    return AffiliateTargetProjection(
        token=token,
        destination_url=destination_url,
        destination_host=destination_host,
        status=status,
        link_identity_hash=link_identity_hash,
        projection_version=version,
        activated_at=activated_s,
        disabled_at=disabled_s,
        projection_entry_hash=entry_hash,
    )


def projection_from_target(target: object) -> AffiliateTargetProjection:
    """``AffiliateLinkTarget`` 風オブジェクト (token / destination_url 等の属性) から作る。"""

    return build_projection_entry(
        token=target.token,
        destination_url=target.destination_url,
        destination_host=target.destination_host,
        link_identity_hash=target.link_identity_hash,
        alt_status=str(target.status),
        activated_at=target.created_at,
        disabled_at=target.disabled_at,
    )


# -- snapshot ------------------------------------------------------
def build_projection_snapshot(
    entries: Iterable[AffiliateTargetProjection],
) -> AffiliateProjectionSnapshot:
    """token 昇順の deterministic snapshot。生成時刻 / nonce / host は含めない。"""

    ordered = tuple(sorted(entries, key=lambda e: e.token))
    seen: set[str] = set()
    for e in ordered:
        if e.token in seen:
            raise AffiliateProjectionError(
                "duplicate token in projection snapshot"
            )
        seen.add(e.token)
    payload = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "targets": [e.entry_payload() for e in ordered],
    }
    snap_hash = hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return AffiliateProjectionSnapshot(
        schema_version=PROJECTION_SCHEMA_VERSION,
        targets=ordered,
        projection_snapshot_hash=snap_hash,
    )


def build_snapshot_from_targets(
    targets: Iterable[object],
) -> tuple[AffiliateProjectionSnapshot, list[str]]:
    """target 群 -> (snapshot, runtime-projection-ineligible な token list)。

    ineligible な target は snapshot から **除外** される (削除ではなく単に非対象)。
    """

    entries: list[AffiliateTargetProjection] = []
    ineligible: list[str] = []
    for t in targets:
        try:
            entries.append(projection_from_target(t))
        except AffiliateProjectionError:
            ineligible.append(t.token)
    return build_projection_snapshot(entries), ineligible


# -- future WordPress upsert semantics (pure model) -----------------
def decide_projection_upsert(
    *,
    current: AffiliateTargetProjection | None,
    incoming: AffiliateTargetProjection,
) -> ProjectionUpsertDecision:
    """将来の WordPress endpoint が 1 token に対して行う判定を pure に再現する。"""

    if current is None:
        # 未 projection の token は version 1 (active) でも version 2 (disabled) でも挿入可。
        return ProjectionUpsertDecision(UPSERT_INSERT)

    if any(
        getattr(current, f) != getattr(incoming, f) for f in IMMUTABLE_ENTRY_FIELDS
    ):
        return ProjectionUpsertDecision(REJECT_IMMUTABLE_DRIFT)

    if (
        incoming.status == PROJECTION_STATUS_ACTIVE
        and current.status == PROJECTION_STATUS_DISABLED
    ):
        return ProjectionUpsertDecision(REJECT_FORBIDDEN_REACTIVATION)

    if incoming.projection_version == current.projection_version:
        if incoming.projection_entry_hash == current.projection_entry_hash:
            return ProjectionUpsertDecision(UPSERT_NOOP)
        return ProjectionUpsertDecision(REJECT_HASH_CONFLICT)

    if incoming.projection_version < current.projection_version:
        return ProjectionUpsertDecision(REJECT_STALE_VERSION)

    # incoming.version > current.version: 許可されるのは 1 -> 2 (active -> disabled) のみ。
    if (
        current.projection_version == PROJECTION_VERSION_ACTIVE
        and incoming.projection_version == PROJECTION_VERSION_DISABLED
        and incoming.status == PROJECTION_STATUS_DISABLED
    ):
        return ProjectionUpsertDecision(UPSERT_UPDATE)
    return ProjectionUpsertDecision(REJECT_STALE_VERSION)


def evaluate_projection_batch(
    *,
    current_by_token: dict[str, AffiliateTargetProjection],
    incoming: Iterable[AffiliateTargetProjection],
) -> ProjectionBatchEvaluation:
    """batch UPSERT を pure に評価する。**atomic**: 1 件でも reject なら全体不採用。

    D-C の WordPress endpoint はこの評価結果を 1 DB transaction で適用する
    (``atomic_accepted`` が False なら projection 行を 1 つも変更しない)。
    missing token は評価対象外 (= 触らない、削除しない)。
    """

    incoming_list = list(incoming)
    decisions: list[tuple[str, ProjectionUpsertDecision]] = []
    inserted = updated = unchanged = rejected = 0
    for entry in incoming_list:
        decision = decide_projection_upsert(
            current=current_by_token.get(entry.token), incoming=entry
        )
        decisions.append((entry.token, decision))
        if decision.result == UPSERT_INSERT:
            inserted += 1
        elif decision.result == UPSERT_UPDATE:
            updated += 1
        elif decision.result == UPSERT_NOOP:
            unchanged += 1
        else:
            rejected += 1
    return ProjectionBatchEvaluation(
        decisions=tuple(decisions),
        atomic_accepted=rejected == 0,
        received_count=len(incoming_list),
        inserted_count=inserted,
        updated_count=updated,
        unchanged_count=unchanged,
        rejected_count=rejected,
    )


# -- future batch request body (no network) -----------------------
def build_projection_batch_request(snapshot: AffiliateProjectionSnapshot) -> dict:
    """将来 POST する logical body。``replace all`` ではなく明示 batch UPSERT。"""

    return {
        "schema_version": snapshot.schema_version,
        "projection_snapshot_hash": snapshot.projection_snapshot_hash,
        "targets": [t.entry_payload() for t in snapshot.targets],
    }
