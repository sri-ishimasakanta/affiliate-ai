"""外の Threads の観察を 1 回行う (T6.5B、読むだけ・件数の上限つき)。

- 出どころ (for_you / topic_for_you / search / custom_feed / known_account) ごとに上限。
  1 回の実行の合計にも上限。スクロールは決まった回数まで (無限にスクロールしない)。
- ログインの画面なら、そこで止めて ``login_required`` (人がログインする)。
- 画面の形が違えば (DOM drift)、そこで止めて ``dom_unrecognized``。その実行で集めたものは
  **全部捨てる** (壊れた値を残さない)。
- スクリーンショットは任意。撮るなら (T6.5B.4) **読むたびに見えている画面を 1 枚** (frame 0 =
  最初の読み・frame N = スクロール N のあと・最後の確かめの読み)。投稿のまとまりの位置も一緒に
  読み、受け入れた投稿がどの画面に写っていたかを数える (``visual_audit``。候補の勘定とは別)。
  受け入れた投稿がまだ画面の下にはみ出して写っていなければ、**上限の中の残りのスクロール** で
  写しに行く (スクロールの上限は増やさない。増えた候補は ``filtered_over_limit`` で勘定する)。

**まとまりの勘定** (T6.5B.1): 読んだ投稿の候補 (まとまり) には、1 つずつ決まった結果と理由を
付ける (``OUTCOME_*`` / ``REASON_*``)。候補の数 = 結果ごとの数の合計。さらに、投稿の時刻の
リンクから独立に数えたキーが、すべて勘定に入っているかを確かめる。合わなければ
``accounting_mismatch`` にして、その実行の投稿を捨てる。

**件数の上限の意味** (``--limit-total``): 読んだ候補の流れ (読んだ時点の画面の上からの順、
決まった回数のスクロールまで) の中から、受け入れた投稿を最大 N 件。**「画面に表示された
最初の N 件」ではない。** Threads は描いた後で、すでに描いた投稿の **間に** 投稿を差し込む
(2026-09-28 の診断で確認)。そのため、読む前に並びが落ち着くまで待ち (上限つき)、最後に
もう一度読んで、読んだ投稿より上に後から差し込まれた投稿を ``late_inserted_after_read``
として数える (読んでいないので保存しない)。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.models.threads_observer import (
    RUN_ACCOUNTING_MISMATCH,
    RUN_DOM_UNRECOGNIZED,
    RUN_LOGIN_REQUIRED,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
    SOURCE_CUSTOM_FEED,
    SOURCE_FOR_YOU,
    SOURCE_KNOWN_ACCOUNT,
    SOURCE_SEARCH,
    SOURCE_TRENDING_TOPIC,
)
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.driver import ReadOnlyPage
from app.social.threads.observer.parser import (
    CARD_DUPLICATE_IN_FRAME,
    CARD_MALFORMED_EMPTY_BODY,
    CARD_MALFORMED_NO_PERMALINK,
    CARD_OK,
    CARD_UNSUPPORTED_NESTED,
    CARD_UNSUPPORTED_OUTSIDE,
    PAGE_DOM_UNRECOGNIZED,
    PAGE_LOGIN_REQUIRED,
    TOPIC_DUPLICATE,
    TOPIC_MALFORMED,
    TOPIC_OK,
    ExternalPostRecord,
    parse_page,
    parse_trending_topics,
)
from app.social.threads.observer.visual_audit import (
    FRAME_FINAL_CHECK,
    FRAME_INITIAL,
    FRAME_SCROLL,
    VisualLedger,
    layout_from,
    merge_run,
)

ACCOUNTING_VERSION = "t6.5b-card-accounting-1"

OUTCOME_ACCEPTED = "accepted"
OUTCOME_MALFORMED = "malformed"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_FILTERED = "filtered"
OUTCOME_UNSUPPORTED = "unsupported"
OUTCOME_OTHER = "other_explicit_reason"
OUTCOMES = (OUTCOME_ACCEPTED, OUTCOME_MALFORMED, OUTCOME_DUPLICATE, OUTCOME_FILTERED,
            OUTCOME_UNSUPPORTED, OUTCOME_OTHER)  # fmt: skip

REASON_ACCEPTED = "accepted"
REASON_FILTERED_OVER_LIMIT = "filtered_over_limit"
REASON_LATE_INSERTED = "late_inserted_after_read"
#: 理由 → 結果 (安定した ID)。
OUTCOME_BY_REASON = {
    REASON_ACCEPTED: OUTCOME_ACCEPTED,
    CARD_MALFORMED_NO_PERMALINK: OUTCOME_MALFORMED,
    CARD_MALFORMED_EMPTY_BODY: OUTCOME_MALFORMED,
    CARD_DUPLICATE_IN_FRAME: OUTCOME_DUPLICATE,
    REASON_FILTERED_OVER_LIMIT: OUTCOME_FILTERED,
    CARD_UNSUPPORTED_NESTED: OUTCOME_UNSUPPORTED,
    CARD_UNSUPPORTED_OUTSIDE: OUTCOME_UNSUPPORTED,
    REASON_LATE_INSERTED: OUTCOME_OTHER,
    # トピックの一覧の候補の理由。
    TOPIC_DUPLICATE: OUTCOME_DUPLICATE,
    TOPIC_MALFORMED: OUTCOME_MALFORMED,
}

#: 並びが落ち着くまで読むときの間隔と、読む回数の上限。
SETTLE_MS = 1000
SETTLE_READS = 3


@dataclass(frozen=True)
class CollectionPlan:
    for_you: bool = False
    trending: bool = False
    search_queries: tuple[str, ...] = ()
    custom_feeds: tuple[str, ...] = ()
    known_accounts: tuple[str, ...] = ()
    screenshots: bool = False

    def source_types(self) -> list[str]:
        out = []
        if self.for_you:
            out.append(SOURCE_FOR_YOU)
        if self.trending:
            out.append(SOURCE_TRENDING_TOPIC)
        if self.search_queries:
            out.append(SOURCE_SEARCH)
        if self.custom_feeds:
            out.append(SOURCE_CUSTOM_FEED)
        if self.known_accounts:
            out.append(SOURCE_KNOWN_ACCOUNT)
        return out


@dataclass(frozen=True)
class CollectedPost:
    source_type: str
    source_query: str | None
    record: ExternalPostRecord


@dataclass
class CollectionResult:
    started_at: datetime
    finished_at: datetime | None = None
    status: str = RUN_SUCCEEDED
    reason: str | None = None
    posts: list[CollectedPost] = field(default_factory=list)
    trending_topics: list[tuple[str, int]] = field(default_factory=list)
    rejected: int = 0
    screenshots: dict[str, str] = field(default_factory=dict)
    pages_opened: int = 0
    scrolls: int = 0
    source_types: list[str] = field(default_factory=list)
    item_limit: int = 0
    #: 出どころごとの候補の勘定 (``_Ledger.summary``)。
    accounting: list[dict] = field(default_factory=list)
    #: トピックの一覧の候補の勘定 (投稿の候補とは別に数える)。
    topic_accounting: dict | None = None
    #: 出どころごとの画面の証拠 (``VisualLedger.summary``、T6.5B.4)。画面を撮ったときだけ。
    visual_audit: list[dict] = field(default_factory=list)
    screenshots_enabled: bool = False

    def visual_summary(self) -> dict:
        """受け入れた投稿の画面の証拠の勘定 (実行全体)。"""

        return merge_run(self.visual_audit, enabled=self.screenshots_enabled)

    def accounting_summary(self) -> dict:
        by_outcome: Counter = Counter()
        by_reason: Counter = Counter()
        for item in self.accounting:
            by_outcome.update(item["by_outcome"])
            by_reason.update(item["by_reason"])
        candidates = sum(item["candidate_cards"] for item in self.accounting)
        topics = self.topic_accounting
        return {
            "version": ACCOUNTING_VERSION,
            "candidate_cards": candidates,
            "by_outcome": dict(sorted(by_outcome.items())),
            "by_reason": dict(sorted(by_reason.items())),
            "equality_holds": candidates == sum(by_outcome.values()),
            "complete": all(item["complete"] for item in self.accounting)
            and (topics is None or topics["complete"]),
            "sources": self.accounting,
            "topic_list": topics,
        }


class _Stop(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


class _Ledger:
    """1 つの出どころの候補ごとの結果 (見た順)。1 つの候補に結果は 1 つだけ。"""

    def __init__(self, source_type: str, query: str | None) -> None:
        self.source_type, self.query = source_type, query
        self.entries: dict[str, dict] = {}
        self.anchor_keys: set[str] = set()
        self.virtualized_out = 0
        self._last_keys: set[str] = set()
        #: 監査用: 受け入れた投稿の、最後の確かめの読みでの数 (保存する値は最初の読みのまま)。
        self.final_check_metrics: dict[str, dict] = {}
        #: 並びが落ち着いた読みと、画面を撮った直後の読みで、投稿の並びが違った回数。
        self.changed_during_screenshot = 0

    def add(self, cid: str, *, key: str | None, reason: str, frame: int, index: int,
            handle: str | None = None) -> None:  # fmt: skip
        if cid in self.entries:
            return
        self.entries[cid] = {"seq": len(self.entries), "frame": frame, "index": index,
                             "key": key, "author_masked": _mask(handle),
                             "outcome": OUTCOME_BY_REASON[reason], "reason": reason}  # fmt: skip

    def accepted(self) -> int:
        return sum(1 for e in self.entries.values() if e["outcome"] == OUTCOME_ACCEPTED)

    def note_frame(self, keys: list[str]) -> None:
        current = set(keys)
        # 前に読んだ投稿が画面から外れた (仮想化)。すでに勘定済みなので数えるだけ。
        self.virtualized_out += len(self._last_keys - current)
        self._last_keys = current
        self.anchor_keys.update(keys)

    def summary(self) -> dict:
        by_outcome = Counter(e["outcome"] for e in self.entries.values())
        by_reason = Counter(e["reason"] for e in self.entries.values())
        keys = {e["key"] for e in self.entries.values() if e["key"]}
        unaccounted = sorted(self.anchor_keys - keys)
        valid = all(e["outcome"] in OUTCOMES and e["reason"] in OUTCOME_BY_REASON
                    for e in self.entries.values())  # fmt: skip
        equality = len(self.entries) == sum(by_outcome.values())
        surface = sel.surface_verification(self.source_type)
        return {
            "source_type": self.source_type,
            "source_query": self.query,
            "surface_selector_version": surface["version"],
            "surface_verified": surface["verified"],
            "candidate_cards": len(self.entries),
            "by_outcome": dict(sorted(by_outcome.items())),
            "by_reason": dict(sorted(by_reason.items())),
            "unaccounted_anchor_keys": unaccounted,
            "virtualized_out": self.virtualized_out,
            "final_check_metrics": self.final_check_metrics,
            "changed_during_screenshot": self.changed_during_screenshot,
            "complete": equality and valid and not unaccounted,
            "sequence": sorted(self.entries.values(), key=lambda e: e["seq"]),
        }


def collect(
    page: ReadOnlyPage,
    plan: CollectionPlan,
    *,
    limits: dict | None = None,
    screenshot_dir: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CollectionResult:
    limits = {**sel.LIMITS, **(limits or {})}
    shoot = plan.screenshots and screenshot_dir is not None
    result = CollectionResult(started_at=clock(), source_types=plan.source_types(),
                              item_limit=int(limits["run_total"]),
                              screenshots_enabled=shoot)  # fmt: skip
    try:
        if plan.for_you:
            _feed(page, result, sel.for_you_url(), SOURCE_FOR_YOU, None,
                  limits["for_you"], limits, plan, screenshot_dir, "for_you")  # fmt: skip
        if plan.trending:
            _trending(page, result, limits, plan, screenshot_dir)
        for query in plan.search_queries:
            _feed(page, result, sel.search_url(query), SOURCE_SEARCH, query, limits["search"],
                  limits, plan, screenshot_dir, f"search-{_slug(query)}")  # fmt: skip
        for feed in plan.custom_feeds:
            _feed(page, result, sel.custom_feed_url(feed), SOURCE_CUSTOM_FEED, feed,
                  limits["custom_feed"], limits, plan, screenshot_dir,
                  f"custom-{_slug(feed)}")  # fmt: skip
        for handle in plan.known_accounts:
            _feed(page, result, sel.account_url(handle), SOURCE_KNOWN_ACCOUNT, handle,
                  limits["known_account"], limits, plan, screenshot_dir,
                  f"account-{_slug(handle)}")  # fmt: skip
    except _Stop as stop:
        result.status, result.reason = stop.status, stop.reason
        if stop.status in (RUN_DOM_UNRECOGNIZED, RUN_ACCOUNTING_MISMATCH):
            result.posts.clear()  # 壊れたかもしれない実行の値は残さない
            result.trending_topics.clear()
    else:
        summary = result.accounting_summary()
        skipped = {r: n for r, n in summary["by_reason"].items()
                   if OUTCOME_BY_REASON[r] in (OUTCOME_MALFORMED, OUTCOME_UNSUPPORTED,
                                               OUTCOME_OTHER)}  # fmt: skip
        if skipped and result.status == RUN_SUCCEEDED:
            result.status = RUN_PARTIAL
            result.reason = "candidate cards not collected: " + ", ".join(
                f"{reason}={n}" for reason, n in sorted(skipped.items())
            )
    result.finished_at = clock()
    return result


def _room(result: CollectionResult, limits: dict) -> int:
    return max(0, int(limits["run_total"]) - len(result.posts))


def _settled(page: ReadOnlyPage) -> str:
    """投稿の並びが 2 回続けて同じになるまで読む (最大 ``SETTLE_READS`` 回)。"""

    html = page.content()
    keys = parse_page(html, limit=0).anchor_keys
    for _ in range(SETTLE_READS - 1):
        page.wait(SETTLE_MS)
        again = page.content()
        again_keys = parse_page(again, limit=0).anchor_keys
        html = again
        if again_keys == keys:
            break
        keys = again_keys
    return html


def _check(parsed, source_type: str) -> None:
    if parsed.status == PAGE_LOGIN_REQUIRED:
        raise _Stop(RUN_LOGIN_REQUIRED, f"{source_type}: {parsed.reason}")
    if parsed.status == PAGE_DOM_UNRECOGNIZED:
        raise _Stop(RUN_DOM_UNRECOGNIZED, f"{source_type}: {parsed.reason}")


def _feed(page, result, url, source_type, query, per_source, limits, plan, shot_dir, shot_name):
    want = min(int(per_source), _room(result, limits))
    if want <= 0:
        return
    page.goto(url)
    result.pages_opened += 1
    ledger = _Ledger(source_type, query)
    visual = VisualLedger(source_type, query)
    gathered: list[ExternalPostRecord] = []
    shoot = plan.screenshots and shot_dir is not None
    attempt = 0
    for attempt in range(int(limits["max_scrolls"]) + 1):
        html = _settled(page)
        if shoot:
            # 並びが落ち着いた状態を撮り、**撮った直後にもう一度読んで、その読みを使う** (画面の
            # 保存と読んだものを同じ時点にそろえる)。スクロールのたびに 1 枚 (T6.5B.4)。
            after_shot = _capture(page, result, visual, shot_dir, shot_name, attempt,
                                  FRAME_INITIAL if attempt == 0 else FRAME_SCROLL)  # fmt: skip
            if parse_page(after_shot, limit=0).anchor_keys != parse_page(html, limit=0).anchor_keys:
                ledger.changed_during_screenshot += 1
            html = after_shot
        parsed = parse_page(html, limit=10_000)
        _check(parsed, source_type)
        ledger.note_frame(parsed.anchor_keys)
        occurrences: Counter = Counter()
        for card in parsed.cards:
            if card.reason == CARD_DUPLICATE_IN_FRAME:
                occurrences[card.key] += 1
                cid = f"dup:{card.key}:{occurrences[card.key]}"
            elif card.key is None:
                cid = f"nokey:{card.fingerprint or f'{attempt}:{card.index}'}"
            else:
                cid = card.key if card.reason == CARD_OK else f"{card.reason}:{card.key}"
            if cid in ledger.entries:
                continue  # 前の読みで勘定済みの同じ候補
            reason = card.reason
            if reason == CARD_OK:
                if ledger.accepted() < want:
                    reason = REASON_ACCEPTED
                    gathered.append(card.record)
                else:
                    reason = REASON_FILTERED_OVER_LIMIT
            ledger.add(cid, key=card.key, reason=reason, frame=attempt, index=card.index,
                       handle=card.handle)  # fmt: skip
        if attempt == int(limits["max_scrolls"]):
            break
        if ledger.accepted() >= want and not (
                shoot and visual.missing_below(r.external_post_key for r in gathered)):  # fmt: skip
            break
        page.scroll()
        result.scrolls += 1
    # 最後にもう一度読む: 読んだ投稿より上に後から差し込まれた投稿を数える (保存しない)。
    # 画面を保存するなら、撮った直後に読む (最後の画面と最後の読みを同じ時点にそろえる)。
    page.wait(SETTLE_MS)
    if shoot:
        final_html = _capture(page, result, visual, shot_dir, shot_name, attempt + 1,
                              FRAME_FINAL_CHECK, scroll=attempt)  # fmt: skip
    else:
        final_html = page.content()
    final = parse_page(final_html, limit=0)
    accepted_keys = {r.external_post_key for r in gathered}
    for card in final.cards:
        if card.record is not None and card.key in accepted_keys:
            ledger.final_check_metrics[card.key] = {"likes": card.record.likes,
                                                    "replies": card.record.replies}  # fmt: skip
    if final.status not in (PAGE_LOGIN_REQUIRED, PAGE_DOM_UNRECOGNIZED):
        known = {e["key"] for e in ledger.entries.values() if e["key"]}
        read_keys = set(ledger.anchor_keys)  # 読んだときに画面にあった投稿
        seen_positions = [i for i, k in enumerate(final.anchor_keys) if k in known]
        if seen_positions:
            for position, key in enumerate(final.anchor_keys[: seen_positions[-1]]):
                # 読んだときには無かった投稿だけが「後から差し込まれた」。読んだときにあったのに
                # 勘定に無い投稿は、ここで救わない (勘定が合わない → 実行を捨てる)。
                if key not in known and key not in read_keys:
                    ledger.add(f"late:{key}", key=key, reason=REASON_LATE_INSERTED,
                               frame=attempt + 1, index=position)  # fmt: skip
                    ledger.anchor_keys.add(key)
    summary = ledger.summary()
    result.accounting.append(summary)
    if shoot:
        result.visual_audit.append(visual.summary([r.external_post_key for r in gathered]))
    if not summary["complete"]:
        raise _Stop(RUN_ACCOUNTING_MISMATCH,
                    f"{source_type}: candidate accounting mismatch "
                    f"(unaccounted {len(summary['unaccounted_anchor_keys'])})")  # fmt: skip
    result.rejected += summary["by_outcome"].get(OUTCOME_MALFORMED, 0)
    for record in gathered:
        result.posts.append(CollectedPost(source_type, query, record))


def _layout(page):
    """投稿のまとまりの位置を読む (読むだけ)。読めないページ (偽のページ等) は ``None``。"""

    read = getattr(page, "layout", None)
    if read is None:
        return None
    try:
        return layout_from(read())
    except Exception:  # noqa: BLE001 - 位置が読めないことも記録する (証拠なし)
        return None


def _capture(page, result, visual: VisualLedger, shot_dir: Path, name: str, frame: int,
             kind: str, *, scroll: int | None = None) -> str:  # fmt: skip
    """位置を読む → 画面を撮る → HTML と位置をもう一度読む。撮った直後の HTML を返す。"""

    before = _layout(page)
    file = f"{name}/frame-{frame:03d}.png"
    path = shot_dir / file
    page.screenshot(path)
    result.screenshots[f"{name}/frame-{frame:03d}"] = str(path)
    html = page.content()
    visual.add_frame(file, kind=kind, scroll=frame if scroll is None else scroll,
                     before=before, after=_layout(page))  # fmt: skip
    return html


def _shot(page, result, shot_dir: Path, name: str) -> None:
    path = shot_dir / f"{name}.png"
    page.screenshot(path)
    result.screenshots[name] = str(path)


def _trending(page, result, limits, plan, shot_dir):
    """トピックの一覧 (検索の最初の画面の「おすすめのトピック」) → 決まった数のトピックの投稿。

    一覧の候補にはすべて結果と理由を付ける (``topic_accounting``)。受け入れたトピックを画面の
    順にたどり、投稿が 1 件以上取れたトピックが ``trending_topics_follow`` 個になったら止める
    (読めたが受け入れた投稿が 0 件のトピックは ``posts=0`` として残し、次へ。投稿のまとまりが
    1 つも無いページは、画面の形の違いと区別できないので fail closed で実行を止める)。
    トピックのページは、一覧の
    **リンクそのもの** を開く (URL を作り直さない)。ページのトピックを投稿に写さない
    (投稿のトピックは、その投稿のまとまりに出ているものだけ)。
    """

    page.goto(sel.trends_url())
    result.pages_opened += 1
    page.wait(SETTLE_MS)
    if plan.screenshots and shot_dir is not None:
        _shot(page, result, shot_dir, "trends")  # 撮った直後に読む
    maximum = int(limits["trending_topics_max"])
    parsed = parse_trending_topics(page.content(), limit=maximum)
    if parsed.status == PAGE_LOGIN_REQUIRED:
        raise _Stop(RUN_LOGIN_REQUIRED, f"trending: {parsed.reason}")
    if parsed.status == PAGE_DOM_UNRECOGNIZED:
        raise _Stop(RUN_DOM_UNRECOGNIZED, f"trending: {parsed.reason}")
    sequence = []
    accepted = []
    for entry in parsed.topic_entries:
        if entry.reason == TOPIC_OK:
            reason = REASON_ACCEPTED if len(accepted) < maximum else REASON_FILTERED_OVER_LIMIT
            if reason == REASON_ACCEPTED:
                accepted.append(entry)
        else:
            reason = entry.reason
        sequence.append({"rank": entry.rank, "name": entry.name, "query": entry.query,
                         "serp_type": entry.serp_type, "kind": entry.kind, "href": entry.href,
                         "outcome": OUTCOME_BY_REASON[reason], "reason": reason,
                         "followed": False, "posts": None})  # fmt: skip
    by_outcome = Counter(e["outcome"] for e in sequence)
    surface = sel.surface_verification("topic_for_you_list")
    result.topic_accounting = {
        "surface_selector_version": surface["version"],
        "surface_verified": surface["verified"],
        "candidate_topics": len(sequence),
        "by_outcome": dict(sorted(by_outcome.items())),
        "by_reason": dict(sorted(Counter(e["reason"] for e in sequence).items())),
        "complete": len(sequence) == sum(by_outcome.values()) == len(parsed.topic_entries),
        "sequence": sequence,
    }
    follow = int(limits.get("trending_topics_follow", maximum))
    followed = 0
    for entry in accepted:
        if followed >= follow:
            break
        row = next(e for e in sequence if e["rank"] == entry.rank)
        before = len(result.posts)
        url = entry.href if entry.href.startswith("https://") else f"{sel.BASE_URL}{entry.href}"
        _feed(page, result, url, SOURCE_TRENDING_TOPIC, entry.name, limits["trending_topic"],
              limits, plan, shot_dir, f"topic-{entry.rank}")  # fmt: skip
        gained = len(result.posts) - before
        row["followed"], row["posts"] = True, gained
        result.trending_topics.append((entry.name, gained))
        if gained > 0:
            followed += 1


def _mask(handle: str | None) -> str | None:
    """診断用の名前 (先頭 3 文字だけ)。"""

    return f"{handle[:3]}…" if handle else None


def _slug(text: str) -> str:
    keep = "".join(ch if ch.isalnum() else "-" for ch in text.strip().lower())
    return keep.strip("-")[:40] or "x"


__all__ = ["ACCOUNTING_VERSION", "OUTCOMES", "OUTCOME_BY_REASON", "CollectedPost",
           "CollectionPlan", "CollectionResult", "collect"]  # fmt: skip
