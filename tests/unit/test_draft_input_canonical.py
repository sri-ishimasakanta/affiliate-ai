"""app/article/draft_input_canonical.py の pure テスト。"""

from datetime import UTC, datetime, timedelta, timezone

from app.article.draft_input_canonical import (
    canonical_commission,
    canonical_datetime,
    canonical_json,
    compute_content_hash,
    semantic_payload_for_hash,
)

JST = timezone(timedelta(hours=9))
EST = timezone(timedelta(hours=-5))


def test_canonical_datetime_normalizes_to_utc_seconds() -> None:
    jst = datetime(2026, 8, 28, 14, 12, 0, 500000, tzinfo=JST)
    assert canonical_datetime(jst) == "2026-08-28T05:12:00+00:00"
    # naive は UTC とみなす
    assert canonical_datetime(datetime(2026, 8, 28, 5, 12)) == "2026-08-28T05:12:00+00:00"
    assert canonical_datetime(None) is None


def test_canonical_datetime_same_instant_across_offsets() -> None:
    a = canonical_datetime(datetime(2026, 8, 28, 14, 12, tzinfo=JST))
    b = canonical_datetime(datetime(2026, 8, 28, 5, 12, tzinfo=UTC))
    c = canonical_datetime(datetime(2026, 8, 28, 0, 12, tzinfo=EST))
    assert a == b == c


def test_canonical_commission_is_fixed_scale_string() -> None:
    assert canonical_commission(35.0) == "35.0000"
    assert canonical_commission(35) == "35.0000"
    assert canonical_commission(20.5) == "20.5000"
    assert canonical_commission(None) is None
    # float 表現ゆれに依存しない
    assert canonical_commission(0.1 + 0.2) == "0.3000"


def test_semantic_payload_drops_audit_only() -> None:
    payload = {"a": 1, "b": {"x": 2}, "audit": {"built_at": "..."}}
    assert semantic_payload_for_hash(payload) == {"a": 1, "b": {"x": 2}}


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 1, "a": [3, 2]}) == '{"a":[3,2],"b":1}'


def test_content_hash_ignores_audit_and_key_order() -> None:
    p1 = {"snapshot_version": "v1", "x": {"a": 1, "b": 2}, "audit": {"built_at": "T1"}}
    p2 = {"x": {"b": 2, "a": 1}, "snapshot_version": "v1", "audit": {"built_at": "T2"}}
    assert compute_content_hash(p1) == compute_content_hash(p2)


def test_content_hash_changes_on_semantic_change() -> None:
    base = {"snapshot_version": "v1", "x": 1, "audit": {}}
    changed = {"snapshot_version": "v1", "x": 2, "audit": {}}
    assert compute_content_hash(base) != compute_content_hash(changed)


def test_content_hash_stable_list_order_matters() -> None:
    a = {"v": [1, 2, 3], "audit": {}}
    b = {"v": [3, 2, 1], "audit": {}}
    assert compute_content_hash(a) != compute_content_hash(b)
