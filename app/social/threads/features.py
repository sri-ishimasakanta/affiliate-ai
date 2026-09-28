"""Threads の投稿の特徴 (T6.5A/B)。自分の投稿と外の投稿で **同じ形** を使う。

- 決定的 (Python の規則だけ)。Luna は使わない。モデルが出した特徴を足すときは
  ``source="model"`` を付けて別に置く (この版には無い)。
- 分からない値は推測しない (``None``)。0 と「分からない」を混ぜない。
- 数字・文字は本文だけから数える (追跡 URL は長さに入れない)。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

FEATURE_VERSION = "t6.5-features-1"
JST = ZoneInfo("Asia/Tokyo")

CTA_CLASSES = ("none", "question", "opinion_request", "follow_connect", "reply_request", "other")
STRUCTURE_CLASSES = (
    "short_single_point", "comparison", "checklist_like", "experience", "goal_progress",
    "informational", "community",
)  # fmt: skip
CHAR_BUCKETS = ((0, 120, "0-119"), (120, 200, "120-199"), (200, 280, "200-279"),
                (280, 361, "280-360"), (361, 10_000, "361+"))  # fmt: skip

_URL = re.compile(r"https?://\S+")
_SENTENCE_END = re.compile(r"[。！？!?]+")
_QUESTION = re.compile(r"[？?]")
_NUMBER = re.compile(r"\d[\d,，.]*")
_PRICE = re.compile(r"(?:[$¥￥]\s*\d[\d,.]*|\d[\d,.]*\s*(?:円|万円|ドル))")
_FOLLOW = re.compile(r"フォロー|フォロバ|つなが|繋が|follow", re.I)
_REPLY_REQUEST = re.compile(r"コメント|返信で|リプ|教えて(?:ください|ね)?|reply", re.I)
_OPINION = re.compile(r"どう思|ご意見|意見を|どちら派|あなたは.*派|賛成|反対")
_LIST_LINE = re.compile(r"^\s*(?:[①②③④⑤⑥⑦⑧⑨]|\d+[.)．）]|[・•\-])\s*\S", re.M)
_COMPARISON = re.compile(r"比較|比べ|違い|どちら|vs\.?|VS", re.I)
_EXPERIENCE = re.compile(r"試して|試した|使ってみ|やってみ|経験|実際に")
_GOAL = re.compile(r"目標|目指|フォロワー\s*\d+|あと\s*\d+\s*人")
_COMMUNITY = re.compile(r"一緒に|仲間|つながり|交流|みなさん|皆さん")


@dataclass(frozen=True)
class PostFeatures:
    """1 件の投稿の決定的な特徴 (自分・外で共通)。"""

    feature_version: str
    character_count: int
    body_length: int
    paragraph_count: int
    sentence_count: int
    question_count: int
    emoji_count: int
    numeric_facts_count: int
    price_count: int
    has_url: bool
    has_question: bool
    has_number: bool
    cta_class: str
    structure_class: str
    char_bucket: str
    topic: str | None
    media_type: str | None
    weekday: int | None
    hour: int | None
    source: str = "rule"

    def as_dict(self) -> dict:
        return asdict(self)


def emoji_count(text: str) -> int:
    return sum(1 for ch in text if unicodedata.category(ch) == "So" and ord(ch) >= 0x2190
               and ch not in "→←↑↓")  # fmt: skip


def cta_class(text: str) -> str:
    """呼びかけの種類 (決定的)。強いものから 1 つ。"""

    body = _URL.sub("", text or "")
    if _FOLLOW.search(body):
        return "follow_connect"
    if _REPLY_REQUEST.search(body):
        return "reply_request"
    if _OPINION.search(body):
        return "opinion_request"
    if _QUESTION.search(body):
        return "question"
    return "none"


def structure_class(text: str, *, body_length: int) -> str:
    body = _URL.sub("", text or "")
    if _GOAL.search(body):
        return "goal_progress"
    if len(_LIST_LINE.findall(body)) >= 2 or body.count("①") and body.count("②"):
        return "checklist_like"
    if _COMPARISON.search(body):
        return "comparison"
    if _EXPERIENCE.search(body):
        return "experience"
    if _COMMUNITY.search(body):
        return "community"
    if body_length < 140:
        return "short_single_point"
    return "informational"


def char_bucket(length: int) -> str:
    return next(label for lo, hi, label in CHAR_BUCKETS if lo <= length < hi)


def extract(
    text: str,
    *,
    topic: str | None = None,
    media_type: str | None = None,
    posted_at: datetime | None = None,
) -> PostFeatures:
    """本文から決定的な特徴を取る。時刻が分からなければ曜日・時は ``None``。"""

    raw = unicodedata.normalize("NFC", text or "")
    body = _URL.sub("", raw).strip()
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    sentences = [s for s in _SENTENCE_END.split(body) if s.strip()]
    local = posted_at.astimezone(JST) if posted_at is not None else None
    length = len(body)
    return PostFeatures(
        feature_version=FEATURE_VERSION,
        character_count=len(raw),
        body_length=length,
        paragraph_count=len(paragraphs),
        sentence_count=len(sentences),
        question_count=len(_QUESTION.findall(body)),
        emoji_count=emoji_count(body),
        numeric_facts_count=len(_NUMBER.findall(body)),
        price_count=len(_PRICE.findall(body)),
        has_url=bool(_URL.search(raw)),
        has_question=bool(_QUESTION.search(body)),
        has_number=bool(_NUMBER.search(body)),
        cta_class=cta_class(raw),
        structure_class=structure_class(raw, body_length=length),
        char_bucket=char_bucket(length),
        topic=topic,
        media_type=media_type,
        weekday=local.weekday() if local else None,
        hour=local.hour if local else None,
    )


__all__ = ["CTA_CLASSES", "FEATURE_VERSION", "STRUCTURE_CLASSES", "PostFeatures", "cta_class",
           "extract", "structure_class"]  # fmt: skip
