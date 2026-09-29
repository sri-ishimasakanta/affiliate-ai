"""T6.5B.4: スクロールごとの画面と、受け入れた投稿 → 画面の対応 (偽のページだけ)。

段階 1 (T6.5B.3) では、ページ全体の 1 枚の画面で 26 件のうち 4 件が描かれていなかった。
ここでは、読むたびに見えている画面を撮り、投稿のまとまりの位置から「どの画面に写っていたか」を
数えることを確かめる。候補の勘定 (collector の ``_Ledger``) とは別に数える。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.social.threads.observer import driver
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionPlan, collect
from app.social.threads.observer.visual_audit import (
    CROSSCHECK_MATCHED,
    CROSSCHECK_MISMATCH,
    CROSSCHECK_NOT_REVIEWED,
    FRAME_FINAL_CHECK,
    FRAME_INITIAL,
    FRAME_SCROLL,
    VISUAL_EVIDENCE_AVAILABLE,
    VISUAL_EVIDENCE_MISSING,
    VISUAL_VERIFIED,
    Layout,
    VisualLedger,
    coverage,
    layout_from,
    merge_run,
    visual_status,
)
from tests.support.threads_observer_pages import FakePage, card, layout_of, page

URL = sel.for_you_url()
H = 1000  # 偽の画面の高さ


def _card(code: str) -> str:
    return card("a", code, f"投稿 {code}", likes="1", replies="1")


def _multi_scroll() -> FakePage:
    """最初の画面 A〜C → スクロール 1 で C〜F → スクロール 2 で F〜H (重なりあり)。"""

    frames = [page(*map(_card, "ABC")), page(*map(_card, "CDEF")), page(*map(_card, "FGH"))]
    layouts = [
        layout_of(("a", "A", 0, 300), ("a", "B", 300, 600), ("a", "C", 600, 900), height=H),
        layout_of(("a", "C", 0, 300), ("a", "D", 300, 500), ("a", "E", 500, 700),
                  ("a", "F", 700, 1000), height=H),  # fmt: skip
        layout_of(("a", "F", 0, 300), ("a", "G", 300, 600), ("a", "H", 600, 900), height=H),
    ]
    return FakePage({URL: frames}, layouts={URL: layouts})


def _collect(fake: FakePage, tmp_path: Path, **limits):
    return collect(fake, CollectionPlan(for_you=True, screenshots=True), screenshot_dir=tmp_path,
                   limits={"for_you": 8, **limits})  # fmt: skip


# -- 撮る時と数 ------------------------------------------------------------------------------


def test_a_frame_before_the_first_scroll_and_after_each_performed_scroll(tmp_path: Path) -> None:
    fake = _multi_scroll()
    order: list[str] = []
    shot, scroll = fake.screenshot, fake.scroll
    fake.screenshot = lambda path: (order.append(f"shot:{path.name}"), shot(path))
    fake.scroll = lambda: (order.append("scroll"), scroll())
    result = _collect(fake, tmp_path)
    assert order == ["shot:frame-000.png", "scroll", "shot:frame-001.png", "scroll",
                     "shot:frame-002.png", "shot:frame-003.png"]  # fmt: skip
    assert fake.scrolls == 2 < sel.LIMITS["max_scrolls"]  # 使わなかった回の画面は無い
    [audit] = result.visual_audit
    assert [(f["file"], f["kind"], f["scroll"]) for f in audit["frames"]] == [
        ("for_you/frame-000.png", FRAME_INITIAL, 0), ("for_you/frame-001.png", FRAME_SCROLL, 1),
        ("for_you/frame-002.png", FRAME_SCROLL, 2),
        ("for_you/frame-003.png", FRAME_FINAL_CHECK, 2)]  # fmt: skip
    assert list(result.screenshots) == [f"for_you/frame-{n:03d}" for n in range(4)]


def test_no_screenshots_means_no_frames_and_no_extra_scrolls(tmp_path: Path) -> None:
    fake = _multi_scroll()
    result = collect(fake, CollectionPlan(for_you=True), screenshot_dir=tmp_path,
                     limits={"for_you": 8})  # fmt: skip
    assert fake.screenshots == [] and result.visual_audit == []
    assert result.visual_summary()["enabled"] is False
    assert fake.scrolls == 2


# -- 投稿 → 画面 -----------------------------------------------------------------------------


def test_every_accepted_post_maps_to_frames_and_overlap_is_kept(tmp_path: Path) -> None:
    result = _collect(_multi_scroll(), tmp_path)
    keys = [p.record.external_post_key for p in result.posts]
    assert keys == [f"threads:{c}" for c in "ABCDEFGH"]
    [audit] = result.visual_audit
    frames = {k: v["audit_frames"] for k, v in audit["posts"].items()}
    assert frames["threads:A"] == ["for_you/frame-000.png"]
    assert frames["threads:C"] == ["for_you/frame-000.png", "for_you/frame-001.png"]  # 重なり
    assert frames["threads:F"] == ["for_you/frame-001.png", "for_you/frame-002.png",
                                   "for_you/frame-003.png"]  # fmt: skip
    assert all(v["visual_evidence_available"] for v in audit["posts"].values())
    assert audit["accepted_posts"] == 8 and audit["coverage_pct"] == 100.0
    assert audit["complete"] is True and audit["posts_without_visual_evidence"] == []


def test_screenshot_accounting_is_separate_from_candidate_accounting(tmp_path: Path) -> None:
    with_shots = _collect(_multi_scroll(), tmp_path)
    without = collect(_multi_scroll(), CollectionPlan(for_you=True), limits={"for_you": 8})
    strip = [{k: v for k, v in s.items() if k != "changed_during_screenshot"}
             for s in with_shots.accounting]  # fmt: skip
    assert strip == [{k: v for k, v in s.items() if k != "changed_during_screenshot"}
                     for s in without.accounting]  # fmt: skip
    assert "visual" not in str(with_shots.accounting_summary())
    # 候補の勘定: 画面から外れた (仮想化の) 投稿も勘定に入ったまま。
    [source] = with_shots.accounting
    assert source["complete"] is True and source["virtualized_out"] > 0


def test_an_accepted_post_never_painted_is_reported_missing(tmp_path: Path) -> None:
    # D は DOM にあるが、画面の下にはみ出したまま (スクロールしても位置が変わらない)。
    html = page(*map(_card, "ABCD"))
    layout = layout_of(("a", "A", 0, 300), ("a", "B", 300, 600), ("a", "C", 600, 900),
                       ("a", "D", 1100, 1400), height=H)  # fmt: skip
    fake = FakePage({URL: html}, layouts={URL: layout})
    result = _collect(fake, tmp_path, for_you=4)
    assert len(result.posts) == 4  # 集め方は変わらない (証拠のために値を捨てない)
    # 残りのスクロールで写しに行くが、上限 (3 回) で止まる。上限は増やさない。
    assert fake.scrolls == sel.LIMITS["max_scrolls"]
    summary = result.visual_summary()
    assert summary["posts_without_visual_evidence"] == ["threads:D"]
    assert summary["posts_with_visual_evidence"] == 3 and summary["coverage_pct"] == 75.0
    assert summary["complete"] is False
    assert summary["posts"]["threads:D"]["visual_crosscheck"] == CROSSCHECK_NOT_REVIEWED


def test_a_post_below_the_viewport_is_captured_with_a_remaining_scroll(tmp_path: Path) -> None:
    html = page(*map(_card, "ABCD"))
    layouts = [layout_of(("a", "A", 0, 300), ("a", "B", 300, 600), ("a", "C", 600, 900),
                         ("a", "D", 1100, 1400), height=H),
               layout_of(("a", "C", -300, 0), ("a", "D", 100, 400), height=H)]  # fmt: skip
    fake = FakePage({URL: [html, html]}, layouts={URL: layouts})
    result = _collect(fake, tmp_path, for_you=4)
    assert fake.scrolls == 1 and result.visual_summary()["complete"] is True
    [source] = result.accounting
    assert source["by_reason"] == {"accepted": 4}  # 同じ候補を二度数えない


def test_a_page_without_layout_reports_missing_evidence_without_scrolling(tmp_path: Path) -> None:
    fake = FakePage({URL: page(*map(_card, "AB"))})  # 位置が読めない
    result = _collect(fake, tmp_path, for_you=2)
    assert fake.scrolls == 0
    summary = result.visual_summary()
    assert summary["coverage_pct"] == 0.0 and len(summary["posts_without_visual_evidence"]) == 2
    assert result.visual_audit[0]["frames"][0]["layout_available"] is False


# -- 1 枚の画面の中 ----------------------------------------------------------------------------


def _layout(**boxes) -> Layout:
    return Layout(H, {f"threads:{k}": v for k, v in boxes.items()})


def test_a_card_that_moved_during_the_capture_is_not_evidence() -> None:
    ledger = VisualLedger("for_you", None)
    ledger.add_frame("f/frame-000.png", kind=FRAME_INITIAL, scroll=0,
                     before=_layout(A=(0, 300), B=(300, 600)),
                     after=_layout(A=(0, 300), B=(340, 640)))  # fmt: skip
    assert ledger.evidence("threads:A")["visual_evidence_available"] is True
    assert ledger.evidence("threads:B") == {"audit_frames": [],
                                            "visual_evidence_available": False}  # fmt: skip
    assert ledger.frames[0]["cards_moved_during_capture"] == 1


def test_a_card_split_across_two_frames_is_covered_but_a_gap_is_not() -> None:
    ledger = VisualLedger("for_you", None)
    ledger.add_frame("f/frame-000.png", kind=FRAME_INITIAL, scroll=0,
                     before=_layout(A=(800, 1300)), after=_layout(A=(800, 1300)))  # fmt: skip
    assert ledger.evidence("threads:A")["visual_evidence_available"] is False  # 上半分だけ
    ledger.add_frame("f/frame-001.png", kind=FRAME_SCROLL, scroll=1,
                     before=_layout(A=(-150, 350)), after=_layout(A=(-150, 350)))  # fmt: skip
    assert ledger.evidence("threads:A")["visual_evidence_available"] is True
    gap = VisualLedger("for_you", None)
    gap.add_frame("f/frame-000.png", kind=FRAME_INITIAL, scroll=0,
                  before=_layout(A=(800, 1300)), after=_layout(A=(800, 1300)))  # fmt: skip
    gap.add_frame("f/frame-001.png", kind=FRAME_SCROLL, scroll=1,
                  before=_layout(A=(-400, 100)), after=_layout(A=(-400, 100)))  # fmt: skip
    evidence = gap.evidence("threads:A")
    assert evidence["visual_evidence_available"] is False  # 200〜400 が写っていない
    assert gap.summary(["threads:A"])["partial_only"] == ["threads:A"]


def test_layout_parsing_uses_the_post_permalink_and_drops_ambiguous_keys() -> None:
    raw = {"viewport": {"height": 900}, "cards": [
        {"top": 0, "bottom": 100, "hrefs": ["/@a/post/K1", "/@b/post/QUOTED"]},
        {"top": 100, "bottom": 200, "hrefs": ["/@a/post/K2"]},
        {"top": 300, "bottom": 400, "hrefs": ["/@a/post/K2"]},
        {"top": 400, "bottom": 500, "hrefs": ["https://example.invalid/x"]}]}  # fmt: skip
    layout = layout_from(raw)
    assert layout.viewport_height == 900 and layout.boxes == {"threads:K1": (0.0, 100.0)}
    assert layout_from(None) is None and layout_from({"cards": []}) is None


# -- 状態と割合 --------------------------------------------------------------------------------


def test_evidence_is_not_verification() -> None:
    assert visual_status(True) == VISUAL_EVIDENCE_AVAILABLE
    assert visual_status(False) == VISUAL_EVIDENCE_MISSING
    assert visual_status(True, CROSSCHECK_MATCHED) == VISUAL_VERIFIED
    assert visual_status(True, CROSSCHECK_MISMATCH) == VISUAL_EVIDENCE_AVAILABLE
    with pytest.raises(ValueError):
        visual_status(True, "looked_fine")


def test_coverage_percentage_and_run_merge() -> None:
    posts = {"threads:A": {"audit_frames": ["x"], "visual_evidence_available": True},
             "threads:B": {"audit_frames": [], "visual_evidence_available": False},
             "threads:C": {"audit_frames": ["y"], "visual_evidence_available": True}}  # fmt: skip
    assert coverage(posts)["coverage_pct"] == 66.7
    assert coverage({})["coverage_pct"] is None and coverage({})["complete"] is False
    # 同じ投稿が 2 つの出どころで受け入れられたら、どちらかの証拠で足りる。
    a = {"frames": [], "posts": {"threads:B": {"audit_frames": [],
                                               "visual_evidence_available": False}}}  # fmt: skip
    b = {"frames": [], "posts": {"threads:B": {"audit_frames": ["step2/s/frame-000.png"],
                                               "visual_evidence_available": True}}}  # fmt: skip
    merged = merge_run([a, b], enabled=True)
    assert merged["accepted_posts"] == 1 and merged["complete"] is True
    assert merged["posts"]["threads:B"]["visual_crosscheck"] == CROSSCHECK_NOT_REVIEWED


# -- 読むだけ ---------------------------------------------------------------------------------


def test_the_layout_script_only_reads() -> None:
    script = driver._LAYOUT_JS
    for word in ("click", "dispatchEvent", "setAttribute", "removeAttribute", "innerHTML",
                 ".remove(", "append", "insert", "focus", "submit", "scroll", "fetch",
                 "Storage", "cookie", "XMLHttpRequest", "value ="):  # fmt: skip
        assert word not in script, word
    assert "getBoundingClientRect" in script
    assert driver.VIEWPORT["height"] > driver.SCROLL_PIXELS  # 続けて撮った画面が重なる


def test_the_visual_audit_does_not_import_a_browser() -> None:
    code = ("import sys, app.social.threads.observer.visual_audit, "
            "app.social.threads.observer.collector; "
            "print(any(m.startswith('playwright') for m in sys.modules))")  # fmt: skip
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[2], check=True)  # fmt: skip
    assert out.stdout.strip() == "False"
