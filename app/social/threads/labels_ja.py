"""Threads の人に見せる文言 (日本語) の表示層 (T6.4)。

**内部の値は変えない。** 状態・理由の ID・enum・ログの event 名は英語のまま保存し、機械が読む。
人に見せるときだけ、ここで ``内部の値 → 日本語の見出し (+ 説明)`` に変える。

- 見出しの表は決定的 (同じ値は同じ文)。知らない値は捨てずに、内部の値をそのまま添えて出す。
- 警告は、生成のコードが書く **決まった形の文** (このリポジトリの定型) を ID に対応づける。
  定型にない警告は「システム警告」として元の文を残す (黙って落とさない)。
- 時刻は JST で見せる。保存している時刻 (UTC) は変えない。
- token・鍵・認証の値はここを通らない (表示するのは状態と理由だけ)。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

# -- 状態 ------------------------------------------------------------------------------------
STATUS_LABELS: Mapping[str, str] = {
    # 提案
    "proposed": "下書き",
    "awaiting_approval": "承認待ち",
    "approved": "承認済み",
    "rejected": "却下",
    "stale": "元記事が変わった",
    "superseded": "差し替え済み",
    # 公開
    "planned": "公開準備中",
    "creating": "公開処理中",
    "container_created": "公開処理中",
    "publishing": "公開処理中",
    "published": "公開済み",
    "failed": "失敗",
    "uncertain": "結果確認中",
    # 承認の依頼 (携帯)
    "pending": "承認待ち",
    "decided": "判断済み",
    "expired": "期限切れ",
    "revoked": "取り消し",
    # そのほか
    "blocked": "保留",
    "held": "保留",
    "needs_human_check": "要確認",
    "sent": "送信済み",
}

KIND_LABELS: Mapping[str, str] = {"article": "通常記事", "account_growth": "Growth Post"}

HOOK_LABELS: Mapping[str, str] = {
    "question": "質問",
    "choice": "選択",
    "experience": "経験",
    "opinion": "意見",
    "none": "なし",
    "legacy": "指定なし (T6.3 より前)",
}

ARTICLE_ANGLE_LABELS: Mapping[str, str] = {
    "insight": "気づき",
    "beginner_tip": "初心者向けのコツ",
    "common_mistake": "よくある間違い",
    "comparison": "比較",
    "question": "問いかけ",
    "account_growth": "Growth Post",
}

GROWTH_ANGLE_LABELS: Mapping[str, str] = {
    "account_identity": "アカウント紹介",
    "goal_progress": "目標・進捗",
    "build_in_public": "制作過程",
    "community": "交流",
    "mutual_growth": "相互成長",
}

LINK_LABELS: Mapping[str, str] = {"none": "なし", "article": "元記事へのリンクあり"}

METRIC_LABELS: Mapping[str, str] = {
    "views": "表示数",
    "likes": "いいね",
    "replies": "返信",
    "reposts": "再投稿",
    "quotes": "引用",
    "shares": "シェア",
    "clicks": "クリック",
    "followers_count": "フォロワー数",
}

NO_TOPIC = "なし"


def status_label(value: str | None, *, raw: bool = False) -> str:
    """状態の見出し。``raw=True`` なら内部の値を括弧で添える (エラー・確認向け)。"""

    if not value:
        return "-"
    label = STATUS_LABELS.get(value)
    if label is None:
        return f"不明な状態（{value}）"
    return f"{label}（{value}）" if raw else label


def kind_label(value: str | None) -> str:
    return KIND_LABELS.get(value or "", f"不明（{value}）")


def hook_label(value: str | None, *, raw: bool = True) -> str:
    if not value:
        return "-"
    label = HOOK_LABELS.get(value)
    if label is None:
        return value
    return f"{label}（{value}）" if raw and value not in ("none", "legacy") else label


def angle_label(value: str | None, *, growth_angle: str | None = None) -> str:
    """切り口の見出し。Growth Post なら Growth の切り口 (例: アカウント紹介) を出す。"""

    if growth_angle:
        return GROWTH_ANGLE_LABELS.get(growth_angle, growth_angle)
    if not value:
        return "-"
    label = ARTICLE_ANGLE_LABELS.get(value)
    return f"{label}（{value}）" if label else value


def link_label(value: str | None) -> str:
    return LINK_LABELS.get(value or "", value or "-")


def topic_label(tag: str | None) -> str:
    return tag if tag else NO_TOPIC


def metric_label(name: str) -> str:
    return METRIC_LABELS.get(name, name)


# -- 時刻 ------------------------------------------------------------------------------------


def format_jst(moment: datetime | str | None, *, seconds: bool = False) -> str:
    """``2026-09-28 13:55 JST``。保存値 (tz なし) は UTC とみなす。読めなければ ``-``。"""

    if moment is None or moment == "":
        return "-"
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            return str(moment)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    fmt = "%Y-%m-%d %H:%M:%S JST" if seconds else "%Y-%m-%d %H:%M JST"
    return moment.astimezone(JST).strftime(fmt)


# -- 警告 ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalizedMessage:
    """人に見せる 1 件 (見出し・説明) と、内部の ID と元の文。"""

    reason_id: str
    label: str
    detail: str
    raw: str

    def text(self) -> str:
        return f"{self.label}: {self.detail}" if self.detail else self.label

    def as_dict(self) -> dict:
        return {"reason_id": self.reason_id, "label": self.label, "detail": self.detail,
                "raw": self.raw}  # fmt: skip


def _hook_name(value: str) -> str:
    return HOOK_LABELS.get(value, value)


#: (ID, 生成のコードが書く定型の正規表現, 見出し, 説明の関数)。定型が変わったら試験が落ちる。
_WARNINGS = (
    ("sentence_too_long", r"(\d+) sentence\(s\) exceed (\d+) characters; prefer shorter ones",
     "長い文があります",
     lambda m: f"{m[2]}文字を超える文が{m[1]}文あります。短くすると読みやすくなります。"),
    ("too_many_sentences", r"(\d+) sentences; the policy prefers at most (\d+)",
     "文の数が多めです", lambda m: f"{m[1]}文あります（目安は{m[2]}文まで）。"),
    ("long_post", r"(\d+) characters is longer than the preferred (\d+); consider trimming",
     "長めの投稿です",
     lambda m: f"{m[1]}文字あります（目安は{m[2]}文字まで）。少し削ると読みやすくなります。"),
    ("article_style_phrase", r"article-style phrasing: (.+)", "記事のような言い回しがあります",
     lambda m: f"「{m[1]}」は記事向けの表現です。Threads では会話の言葉が向いています。"),
    ("repetitive_endings",
     r"(\d+) consecutive sentences end in '(.+)'; vary the rhythm so it does not read "
     r"mechanically",
     "同じ語尾が続いています",
     lambda m: f"「{m[2]}」で終わる文が{m[1]}文続いています。語尾を変えると自然になります。"),
    ("too_many_emoji", r"(\d+) emoji; the policy allows at most (\d+)", "絵文字が多めです",
     lambda m: f"絵文字が{m[1]}個あります（{m[2]}個まで）。"),
    ("too_many_questions", r"(\d+) questions; one natural hook is enough", "問いかけが多めです",
     lambda m: f"疑問文が{m[1]}つあります。自然な問いかけは 1 つで十分です。"),
    ("hook_none_question",
     r"conversation_hook none ends with a question; prefer a plain ending",
     "終わり方を確認してください",
     lambda m: "会話フックは「なし」ですが、質問で終わっています。言い切りの方が合います。"),
    ("hook_generic", r"the conversation hook uses a generic phrase; tie it to the topic",
     "問いかけが一般的です",
     lambda m: "決まり文句の問いかけです。話題に結びつけると答えやすくなります。"),
    ("repeated_polite_question", r"「〜ではないでしょうか」 is repeated",
     "同じ言い回しが続いています", lambda m: "「〜ではないでしょうか」が繰り返されています。"),
    ("hook_experience_weak",
     r"the experience hook does not clearly ask about the reader's own use",
     "会話フックを確認してください",
     lambda m: "「経験」型ですが、読者自身の経験を尋ねる表現がやや弱くなっています。"),
    ("hook_opinion_weak", r"the opinion hook does not clearly invite another view",
     "会話フックを確認してください",
     lambda m: "「意見」型ですが、別の意見を求める表現がやや弱くなっています。"),
    ("length_exception_dated",
     r"length exception: (\d+) chars \(> (\d+)\) kept for a dated price/condition",
     "長さの例外があります",
     lambda m: f"日付つきの条件を残すため、本文が{m[1]}文字（{m[2]}文字超）のままです。"),
    ("above_preferred_length", r"(\d+) chars is above the preferred (\d+)", "目安より長めです",
     lambda m: f"本文が{m[1]}文字あります（目安は{m[2]}文字まで）。"),
    ("several_axes", r"several comparison axes \((.+)\); prefer one", "比較の軸が複数あります",
     lambda m: f"軸: {m[1]}。1 つに絞ると伝わりやすくなります。"),
    ("many_numbers", r"(\d+) numeric facts; the post may read like a summary",
     "数値が多めです",
     lambda m: f"数値が{m[1]}個あり、記事の要約のように読めるかもしれません。"),
    ("prose_over_quality_limit",
     r"the post is (\d+) chars; keep it within (\d+) \(aim for (\d+)-(\d+)\) with one main point",
     "本文が長すぎます",
     lambda m: f"本文が{m[1]}文字あります。{m[2]}文字以内（目安 {m[3]}〜{m[4]}文字）で、"
     "要点を 1 つにすると読みやすくなります。"),
    ("excessive_price_density",
     r"(\d+) prices \((.+)\) in one post; keep one main point and at most (\d+) prices",
     "金額が多めです",
     lambda m: f"金額が{m[1]}つあります（{m[2]}）。要点を 1 つにし、金額は{m[3]}つまでにします。"),
    ("hook_semantic_mismatch_question",
     r"the requested hook is question but the ending is an A/B choice.*",
     "会話フックを確認してください",
     lambda m: "「質問」型ですが、二択の終わり方になっています。理由を聞く問いにすると合います。"),
    ("hook_semantic_mismatch_choice",
     r"the requested hook is choice but no alternatives are offered.*",
     "会話フックを確認してください",
     lambda m: "「選択」型ですが、選ぶ候補がありません。本文の 2 つの選択肢から選んでもらいます。"),
    ("recent_topic_overlap", r"recent topic overlap with (\S+ #?\d+).*",
     "最近の投稿と話題が重なっています",
     lambda m: f"{m[1].replace('proposal ', '提案 ')} と製品・数値・軸が重なっています。"),
    ("growth_length", r"prose is (\d+) chars \(target (\d+)-(\d+)\)", "長さの目安から外れています",
     lambda m: f"本文が{m[1]}文字です（目安 {m[2]}〜{m[3]}文字）。"),
)
_COMPILED = tuple((rid, re.compile(rf"^{pattern}$", re.S), label, detail)
                  for rid, pattern, label, detail in _WARNINGS)  # fmt: skip

SYSTEM_WARNING = "システム警告"


def localize_warning(text: str) -> LocalizedMessage:
    """警告 1 件を日本語にする。定型にないものは「システム警告」として元の文を残す。"""

    raw = str(text or "").strip()
    inner = raw[len("quality: "):] if raw.startswith("quality: ") else raw
    for rid, pattern, label, detail in _COMPILED:
        match = pattern.match(inner)
        if match:
            groups = {i: match.group(i) for i in range(0, (pattern.groups or 0) + 1)}
            return LocalizedMessage(rid, label, detail(groups), raw)
    return LocalizedMessage("unknown", SYSTEM_WARNING, f"詳細: {raw}" if raw else "", raw)


def localize_warnings(texts: Iterable[str] | None) -> list[LocalizedMessage]:
    return [localize_warning(t) for t in (texts or []) if str(t or "").strip()]


# -- 保留・止まっている理由 (queue・書き込み経路) ------------------------------------------------
BLOCKER_LABELS: Mapping[str, str] = {
    "gap_not_elapsed": "次の通常投稿まで待機中です（前の投稿から 120 分空けます）",
    "outside_publication_window": "現在は公開時間外です（07:00〜23:00）",
    "no_eligible_candidate": "公開できる承認済みの通常投稿がありません",
    "automatic_publication_disabled": "自動公開はオフです",
    "uncertain_publication": "公開結果を確認できないため、自動再送を停止しています",
    "threads_disabled": "Threads 連携がオフです",
    "threads_misconfigured": "Threads の設定に問題があります",
    "growth_daily_limit": "本日の Growth Post はすでに公開済みです",
    "no_eligible_growth_candidate": "公開できる承認済みの Growth Post がありません",
    # 候補ごとの理由
    "not_approved": "あなたの承認を待っています",
    "awaiting_approval": "あなたの承認を待っています",
    "expired": "公開期限を過ぎました",
    "not_before": "公開できる時刻になっていません",
    "held": "保留中です",
    "stale": "元記事が変わったため公開しません",
    "content_integrity": "本文の記録に問題があるため公開しません",
    "already_published": "すでに公開済みです",
    "eligible": "公開できます",
}


def blocker_label(value: str | None) -> str:
    if not value:
        return "-"
    return BLOCKER_LABELS.get(value, f"その他の理由（{value}）")


# -- 公開の失敗 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureExplanation:
    """人に見せる失敗の説明: 何が起きたか・投稿されたか・自動で再試行するか・対応が要るか。"""

    what: str
    posted: str
    retry: str
    action: str
    technical: dict

    def lines(self) -> list[str]:
        return [self.what, self.posted, self.retry, self.action]


def explain_publication_failure(
    *,
    status: str,
    error_category: str | None = None,
    api_code: str | None = None,
    http_status: int | None = None,
    publication_id: int | None = None,
    topic_rejected: bool = False,
    growth: bool = False,
) -> FailureExplanation:
    """公開の失敗・不確定を、日本語の 4 行と技術の識別子に分けて説明する (秘密は含めない)。"""

    technical = {k: v for k, v in (("publication_id", publication_id), ("status", status),
                                   ("error_category", error_category), ("api_code", api_code),
                                   ("http_status", http_status)) if v is not None}  # fmt: skip
    queue = (
        "Growth Post だけを止め、通常投稿は続けます。" if growth else "次の公開は止まっています。"
    )
    if topic_rejected:
        return FailureExplanation(
            "Threads 側で Topic が受け付けられませんでした。",
            "この投稿は公開されていません。",
            "自動的に Topic なしで再投稿はしません。",
            f"確認が必要です（{queue}）",
            technical,
        )
    if status == "uncertain":
        return FailureExplanation(
            "Threads への公開の結果を確認できませんでした。",
            "公開されたかどうか、まだ分かりません。",
            "安全のため自動再送は停止しています。",
            f"確認が必要です（{queue}照合: publish_threads_post.py --reconcile）",
            technical,
        )
    if error_category == "preflight":
        return FailureExplanation(
            "公開の前の確認 (読み取り) が通りませんでした。",
            "この投稿は公開されていません（コンテナも作っていません）。",
            "次の評価でもう一度確かめます。",
            "続く場合は Threads の設定・権限を確認してください。",
            technical,
        )
    return FailureExplanation(
        "Threads への公開に失敗しました。",
        "この投稿は公開されていません。",
        "一時的な問題なら次の評価で同じ内容を再試行します（二重には投稿しません）。",
        "続く場合は確認が必要です。",
        technical,
    )


__all__ = [
    "ARTICLE_ANGLE_LABELS",
    "BLOCKER_LABELS",
    "GROWTH_ANGLE_LABELS",
    "HOOK_LABELS",
    "JST",
    "KIND_LABELS",
    "LINK_LABELS",
    "METRIC_LABELS",
    "STATUS_LABELS",
    "SYSTEM_WARNING",
    "FailureExplanation",
    "LocalizedMessage",
    "angle_label",
    "blocker_label",
    "explain_publication_failure",
    "format_jst",
    "hook_label",
    "kind_label",
    "link_label",
    "localize_warning",
    "localize_warnings",
    "metric_label",
    "status_label",
    "topic_label",
]
