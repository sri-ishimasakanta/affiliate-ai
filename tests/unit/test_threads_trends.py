"""T6.5A-B: 記述の要約・投稿者の基準・伸びた候補 (少ない数は判定しない)。"""

from __future__ import annotations

from app.social.threads.trends import (
    BREAKOUT_ABOVE,
    BREAKOUT_CANDIDATE,
    BREAKOUT_NO_BASELINE,
    BREAKOUT_NO_METRIC,
    BREAKOUT_TYPICAL,
    BREAKOUT_ZERO_BASELINE,
    MIN_AUTHOR_SAMPLE,
    author_baseline,
    author_breakouts,
    breakout_ratio,
    classify_breakout,
    group_summary,
    median_or_none,
)


def test_breakout_ratio_example() -> None:
    assert breakout_ratio(71, 9) == 7.89
    assert breakout_ratio(None, 9) is None
    assert breakout_ratio(71, None) is None
    assert breakout_ratio(71, 0) is None


def test_the_author_baseline_needs_enough_other_posts() -> None:
    assert MIN_AUTHOR_SAMPLE == 5
    assert author_baseline([5, 7, 9, 11]) == {"n": 4, "median": 8.0, "sufficient": False,
                                              "min_sample": 5}  # fmt: skip
    baseline = author_baseline([5, 7, 9, 11, 13, None])
    assert baseline["n"] == 5 and baseline["median"] == 9.0 and baseline["sufficient"] is True


def test_classification() -> None:
    enough = {"sufficient": True, "median": 9.0}
    assert classify_breakout(71, enough) == BREAKOUT_CANDIDATE
    assert classify_breakout(15, enough) == BREAKOUT_ABOVE
    assert classify_breakout(9, enough) == BREAKOUT_TYPICAL
    assert classify_breakout(None, enough) == BREAKOUT_NO_METRIC
    assert classify_breakout(71, {"sufficient": False, "median": 9.0}) == BREAKOUT_NO_BASELINE
    assert classify_breakout(5, {"sufficient": True, "median": 0.0}) == BREAKOUT_ZERO_BASELINE


def _post(key: str, likes, replies=1, author="alice") -> dict:
    return {"external_post_key": key, "author_handle": author, "likes": likes, "replies": replies}


def test_leave_one_out_baseline_and_breakout() -> None:
    posts = [_post(f"p{i}", v) for i, v in enumerate([5, 7, 9, 11, 13])] + [_post("new", 71)]
    rows = {r["external_post_key"]: r for r in author_breakouts(posts)}
    new = rows["new"]
    assert new["author_likes_baseline"]["median"] == 9.0  # 自分自身は基準に入れない
    assert new["likes_breakout_ratio"] == 7.89
    assert new["likes_breakout"] == BREAKOUT_CANDIDATE


def test_small_authors_get_no_ratio() -> None:
    posts = [_post("a", 3), _post("b", 90)]
    for row in author_breakouts(posts):
        assert row["likes_breakout_ratio"] is None
        assert row["likes_breakout"] == BREAKOUT_NO_BASELINE


def test_authors_are_not_mixed() -> None:
    posts = [_post(f"a{i}", 1, author="alice") for i in range(6)]
    posts += [_post(f"b{i}", 100, author="bob") for i in range(6)]
    for row in author_breakouts(posts):
        assert row["likes_breakout"] == BREAKOUT_TYPICAL


def test_group_summary_marks_small_samples_and_ignores_missing_values() -> None:
    rows = [{"kind": "a", "likes": 1}, {"kind": "a", "likes": None}, {"kind": None, "likes": 4}]
    summary = group_summary(rows, "kind", ["likes"])
    assert summary["a"] == {"n": 2, "median": {"likes": 1.0}, "observed": {"likes": 1},
                            "small_sample": True, "evidence": "insufficient_sample"}  # fmt: skip
    assert summary["unknown"]["n"] == 1
    assert median_or_none([None, None]) is None
    assert median_or_none([True, 2]) == 2.0  # bool は数に入れない
