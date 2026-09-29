"""画面の証拠の勘定 (T6.5B.4)。**候補の勘定 (collector の ``_Ledger``) とは別に数える。**

T6.5B.3 の段階 1 では、ページ全体の 1 枚のスクリーンショットで、画面の外の投稿が描かれず
(白いまま) 26 件のうち 4 件を目で確かめられなかった。そこで:

- スクロールのたびに (最初の読み = frame 0、スクロール 1 のあと = frame 1 ...) **見えている
  画面 (viewport)** を撮る。実際に起きた回だけ撮る (使わなかったスクロールの分は撮らない)。
- 撮る直前と直後に、投稿のまとまりの位置 (ページが返す ``layout``: 画面の中の上端・下端) を読む。
  前後で位置が変わらなかった (``POSITION_TOLERANCE_PX`` 以内) まとまりだけを、その画面に
  **写っていた** とする (撮っている間に動いたものは証拠にしない)。
- 受け入れた投稿は、写っていた画面の一覧 (``audit_frames``) を持つ。写っていた部分を合わせて
  まとまり全体 (上端〜下端) が覆われたら ``visual_evidence_available = True``。
  一部だけ・一度も写っていない投稿は ``False`` で、はっきり数える (作らない・埋めない)。

**証拠があること ≠ 人が確かめたこと。** 自動の記録は ``visual_evidence_available`` だけを持つ。
人 (またはデータの質の確認) が画面と値を照らした結果は ``visual_crosscheck``
(``matched`` / ``mismatch`` / ``not_reviewed``) として別に残す
(``app/config/threads_observation_reviews.json``)。``visual_verified`` は照らして合った投稿だけ。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.social.threads.observer import selectors as sel

#: -2 (T6.5B.5): 上に固定された見出しに隠れた部分を「写っていた」にしない (``visible``)。
#: -3 (T6.5B.5): まとまりの上端・下端の数 px (区切りの線・枠) は ``EDGE_TOLERANCE_PX`` まで許す。
AUDIT_VERSION = "t6.5b-visual-audit-3"

FRAME_INITIAL = "initial"
FRAME_SCROLL = "scroll"
FRAME_FINAL_CHECK = "final_check"

#: 投稿ごとの画面の証拠の状態 (確認の報告の段階)。
VISUAL_VERIFIED = "visual_verified"
VISUAL_EVIDENCE_AVAILABLE = "visual_evidence_available"
VISUAL_EVIDENCE_MISSING = "visual_evidence_missing"

#: 人 / データの質の確認が画面と値を照らした結果。
CROSSCHECK_MATCHED = "matched"
CROSSCHECK_MISMATCH = "mismatch"
CROSSCHECK_NOT_REVIEWED = "not_reviewed"
CROSSCHECKS = (CROSSCHECK_MATCHED, CROSSCHECK_MISMATCH, CROSSCHECK_NOT_REVIEWED)

#: 撮る前後で同じ位置とみなす差 (px)。
POSITION_TOLERANCE_PX = 2.0
#: まとまりの **自分の上端・下端** で、描かれた範囲が欠けてよい幅 (px)。まとまりの間の区切りの
#: 線 (1px) が上に重なる・座標の端数、の分 (2026-09-29 の本物の画面: 全体が見えるまとまりで
#: 1〜4px)。見出しに隠れた部分 (約 72px) や、まとまりの途中の抜けは許さない。
EDGE_TOLERANCE_PX = 4.0

REVIEWS_PATH = Path(__file__).resolve().parents[3] / "config" / "threads_observation_reviews.json"

_PERMALINK = re.compile(sel.PERMALINK_PATTERN)


@dataclass(frozen=True)
class Layout:
    """1 回の読みの、見えている画面の高さと、投稿のまとまりの位置 (画面の中の座標)。"""

    viewport_height: float
    boxes: dict[str, tuple[float, float]]
    #: 実際に描かれて見えていた縦の範囲 (画面の中の座標)。``None`` = 隠れていた。キーが無い
    #: (古い形のページ) なら、位置だけから数える。
    visible: dict[str, tuple[float, float] | None] | None = None


def layout_from(raw: dict | None) -> Layout | None:
    """ページが返した位置 (``{"viewport": {"height"}, "cards": [{"top", "bottom", "hrefs"}]}``)
    を投稿のキーごとにする。同じ画面に同じキーが 2 つあれば、どちらか決められないので外す。"""

    if not raw or not isinstance(raw, dict):
        return None
    try:
        height = float(raw["viewport"]["height"])
        cards = list(raw["cards"])
    except (KeyError, TypeError, ValueError):
        return None
    boxes: dict[str, tuple[float, float]] = {}
    shown: dict[str, tuple[float, float] | None] = {}
    ambiguous: set[str] = set()
    for card in cards:
        key = next((f"threads:{m.group(2)}" for m in (_PERMALINK.match(h or "")
                                                       for h in card.get("hrefs", [])) if m),
                   None)  # fmt: skip
        if key is None:
            continue
        if key in boxes:
            ambiguous.add(key)
        boxes[key] = (float(card["top"]), float(card["bottom"]))
        if "visible" in card:
            part = card["visible"]
            shown[key] = (float(part[0]), float(part[1])) if part else None
    for key in ambiguous:
        boxes.pop(key)
        shown.pop(key, None)
    return Layout(height, boxes, shown if shown else None)


def _stable_boxes(before: Layout | None, after: Layout | None) -> dict[str, tuple[float, float]]:
    if before is None or after is None:
        return {}
    out = {}
    for key, (top, bottom) in after.boxes.items():
        old = before.boxes.get(key)
        if old and abs(old[0] - top) <= POSITION_TOLERANCE_PX \
                and abs(old[1] - bottom) <= POSITION_TOLERANCE_PX:  # fmt: skip
            out[key] = (top, bottom)
    return out


def _visible(top: float, bottom: float, height: float) -> tuple[float, float] | None:
    """画面に写っていた部分 (まとまりの中の座標、上端 = 0)。写っていなければ ``None``。"""

    start, end = max(0.0, -top), min(bottom - top, height - top)
    return (start, end) if end > start else None


def _visible_part(layout: Layout, key: str, top: float, bottom: float
                  ) -> tuple[float, float] | None:  # fmt: skip
    """写っていた部分 (まとまりの中の座標)。描かれて見えていた範囲があれば、それだけ。"""

    if layout.visible is not None and key in layout.visible:
        shown = layout.visible[key]
        if shown is None:
            return None  # 位置は画面の中でも、上に重なった見出し等に隠れていた
        start, end = max(0.0, shown[0] - top), min(bottom - top, shown[1] - top)
        return (start, end) if end > start else None
    return _visible(top, bottom, layout.viewport_height)


def _covers(intervals: list[tuple[float, float]], length: float) -> bool:
    """写っていた部分を合わせて、まとまり全体 (上端〜下端) が覆われたか。

    上端・下端は ``EDGE_TOLERANCE_PX`` まで、途中の抜けは ``POSITION_TOLERANCE_PX`` まで。
    """

    reach = None
    for start, end in sorted(intervals):
        if reach is None:
            if start > EDGE_TOLERANCE_PX:
                return False
        elif start > reach + POSITION_TOLERANCE_PX:
            return False
        reach = end if reach is None else max(reach, end)
    return reach is not None and reach >= length - EDGE_TOLERANCE_PX


class VisualLedger:
    """1 つの出どころのページの、撮った画面と、画面ごとに写っていた投稿。"""

    def __init__(self, source_type: str, query: str | None) -> None:
        self.source_type, self.query = source_type, query
        self.frames: list[dict] = []
        #: キー → [(画面の file, まとまりの高さ, 写っていた部分)]
        self._seen: dict[str, list[tuple[str, float, tuple[float, float]]]] = {}
        self._latest: Layout | None = None

    def add_frame(self, file: str, *, kind: str, scroll: int, before: Layout | None,
                  after: Layout | None) -> None:  # fmt: skip
        stable = _stable_boxes(before, after)
        visible = 0
        for key, (top, bottom) in stable.items():
            part = _visible_part(after, key, top, bottom)
            if part is not None:
                visible += 1
                self._seen.setdefault(key, []).append((file, bottom - top, part))
        self._latest = after
        self.frames.append({
            "frame": len(self.frames), "file": file, "kind": kind, "scroll": scroll,
            "layout_available": before is not None and after is not None,
            "viewport_height": after.viewport_height if after else None,
            "cards_visible": visible,
            "cards_moved_during_capture": len((after.boxes if after else {}).keys() - stable),
        })  # fmt: skip

    @property
    def layout_available(self) -> bool:
        return self._latest is not None

    def evidence(self, key: str) -> dict:
        seen = self._seen.get(key, [])
        frames = list(dict.fromkeys(file for file, _, _ in seen))
        # 高さが最後と同じ画面だけで覆いを数える (画像が後から読み込まれて伸びた前の画面は
        # 座標が合わない)。
        height = seen[-1][1] if seen else 0.0
        parts = [p for _, h, p in seen if abs(h - height) <= POSITION_TOLERANCE_PX]
        return {"audit_frames": frames,
                "visual_evidence_available": bool(seen) and _covers(parts, height)}  # fmt: skip

    def missing_below(self, keys) -> list[str]:
        """証拠がまだ無く、最後の読みで画面の下にはみ出している投稿 (スクロールで写る見込み)。"""

        if self._latest is None:
            return []
        out = []
        for key in keys:
            box = self._latest.boxes.get(key)
            if box and box[1] > self._latest.viewport_height \
                    and not self.evidence(key)["visual_evidence_available"]:  # fmt: skip
                out.append(key)
        return out

    def summary(self, accepted_keys: list[str]) -> dict:
        posts = {key: self.evidence(key) for key in accepted_keys}
        return {"source_type": self.source_type, "source_query": self.query,
                "frames": self.frames, "posts": posts,
                **coverage(posts)}  # fmt: skip


def coverage(posts: dict[str, dict]) -> dict:
    """受け入れた投稿のうち、画面の証拠がある数と割合。**候補の勘定とは別。**"""

    with_frame = [k for k, v in posts.items() if v["audit_frames"]]
    available = [k for k, v in posts.items() if v["visual_evidence_available"]]
    missing = [k for k in posts if k not in set(available)]
    total = len(posts)
    return {
        "accepted_posts": total,
        "posts_with_frame": len(with_frame),
        "posts_with_visual_evidence": len(available),
        "posts_without_visual_evidence": missing,
        "partial_only": [k for k in with_frame if k in set(missing)],
        "coverage_pct": round(100 * len(available) / total, 1) if total else None,
        "complete": total > 0 and not missing,
    }


def merge_run(audits: list[dict], *, enabled: bool) -> dict:
    """実行全体の証拠の勘定。同じ投稿が複数の出どころで受け入れられたら、どれかの証拠で足りる。"""

    posts: dict[str, dict] = {}
    for audit in audits:
        for key, item in audit["posts"].items():
            entry = posts.setdefault(key, {"audit_frames": [], "visual_evidence_available": False})
            entry["audit_frames"] += [f for f in item["audit_frames"]
                                      if f not in entry["audit_frames"]]  # fmt: skip
            entry["visual_evidence_available"] |= item["visual_evidence_available"]
    for item in posts.values():
        item["visual_crosscheck"] = CROSSCHECK_NOT_REVIEWED  # 自動の記録は照らしていない
    return {"version": AUDIT_VERSION, "enabled": enabled, "posts": posts,
            "sources": [{k: v for k, v in a.items() if k != "posts"} for a in audits],
            **coverage(posts)}  # fmt: skip


def visual_status(evidence_available: bool, crosscheck: str = CROSSCHECK_NOT_REVIEWED) -> str:
    """確認の報告の状態。``visual_verified`` は、照らして合った (``matched``) 投稿だけ。"""

    if crosscheck not in CROSSCHECKS:
        raise ValueError(f"unknown visual crosscheck {crosscheck!r}")
    if crosscheck == CROSSCHECK_MATCHED:
        return VISUAL_VERIFIED
    return VISUAL_EVIDENCE_AVAILABLE if evidence_available else VISUAL_EVIDENCE_MISSING


def load_reviews(path: Path | None = None) -> dict:
    """保存した実行ごとの、画面との照合の記録 (人 / データの質の確認が書く。元の行は変えない)。"""

    return json.loads((path or REVIEWS_PATH).read_text(encoding="utf-8"))


def run_review(run_id: int, reviews: dict | None = None) -> dict | None:
    data = reviews if reviews is not None else load_reviews()
    return next((r for r in data["runs"] if r["run_id"] == run_id), None)


def review_statuses(review: dict) -> dict[str, str]:
    """照合の記録の投稿ごとの状態 (``visual_status``)。"""

    out = {}
    for key, item in review["posts"].items():
        out[key] = visual_status(item["visual_evidence_available"], item["visual_crosscheck"])
    return out


def run_report(run_id: int, accepted_keys: list[str], artifacts: dict | None,
               review: dict | None) -> dict:  # fmt: skip
    """保存した実行の画面の証拠と照合の報告 (読むだけ)。

    証拠: 実行の記録の ``visual_audit`` (T6.5B.4 以降) があればそれ、無ければ照合の記録の値。
    照合 (``visual_crosscheck``): 照合の記録だけから (無ければ ``not_reviewed``)。
    """

    frame_audit = (artifacts or {}).get("visual_audit") or None
    recorded = bool(frame_audit and frame_audit.get("enabled"))
    posts = {}
    for key in accepted_keys:
        auto = (frame_audit or {}).get("posts", {}).get(key) if recorded else None
        seen = (review or {}).get("posts", {}).get(key)
        if auto is not None:
            available, frames = auto["visual_evidence_available"], auto["audit_frames"]
        else:
            available, frames = bool(seen and seen["visual_evidence_available"]), []
        crosscheck = seen["visual_crosscheck"] if seen else CROSSCHECK_NOT_REVIEWED
        posts[key] = {"audit_frames": frames, "visual_evidence_available": available,
                      "visual_crosscheck": crosscheck,
                      "visual_status": visual_status(available, crosscheck)}  # fmt: skip
    counts = {name: sum(1 for p in posts.values() if p["visual_status"] == name)
              for name in (VISUAL_VERIFIED, VISUAL_EVIDENCE_AVAILABLE, VISUAL_EVIDENCE_MISSING)}
    return {
        "run_id": run_id,
        "frame_audit_recorded": recorded,
        "review_recorded": review is not None,
        "visual_crosscheck": {name: sum(1 for p in posts.values() if p["visual_crosscheck"] == name)
                              for name in CROSSCHECKS},
        "visual_status": counts,
        "posts": posts,
        **coverage(posts),
    }  # fmt: skip


__all__ = [
    "AUDIT_VERSION", "CROSSCHECKS", "CROSSCHECK_MATCHED", "CROSSCHECK_MISMATCH",
    "CROSSCHECK_NOT_REVIEWED", "EDGE_TOLERANCE_PX", "FRAME_FINAL_CHECK", "FRAME_INITIAL",
    "FRAME_SCROLL", "Layout",
    "POSITION_TOLERANCE_PX", "REVIEWS_PATH", "VISUAL_EVIDENCE_AVAILABLE",
    "VISUAL_EVIDENCE_MISSING", "VISUAL_VERIFIED", "VisualLedger", "coverage", "layout_from",
    "load_reviews", "merge_run", "review_statuses", "run_report", "run_review", "visual_status",
]  # fmt: skip
