"""Threads 投稿提案の検査 (T2、pure)。

**不透明な点数は付けない。** 名前の付いた検査を個別に通し、落ちた理由をそのまま
返す。主観的な「品質スコア」は作らない。

2 段階にする:

``errors``
    これがあると提案として保存しない。API の事実 (500 文字) や、外に出しては
    いけないもの (``/go/`` リンク・アフィリエイト直リンク・生 HTML) など、
    判断の余地がないもの。

``warnings``
    保存はするが、人の確認画面に出す。「AI っぽい言い回し」や「丁寧語の連続」など、
    文脈によっては自然でありうるもの。**自動で落とさない** -- 判断は人に残す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.social.threads.policy import ThreadsStylePolicy
from app.social.threads.proposal import (
    LINK_MODE_ARTICLE,
    LINK_MODE_NONE,
    ThreadsProposal,
    canonical_identity,
)

#: 制御文字 (改行とタブは許す)。
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: 生 HTML / script の混入。
_HTML_RE = re.compile(r"<\s*/?\s*(script|iframe|style|img|a|div|span)\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s*")
#: 絵文字のおおよその範囲 (装飾記号・顔文字・ピクトグラム)。
_EMOJI_RE = re.compile("[\U0001f300-\U0001f9ff\U0001fa00-\U0001faff☀-➿⬀-⯿]")


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


def validate_proposal(proposal: ThreadsProposal, policy: ThreadsStylePolicy) -> ValidationResult:
    """1 件の提案を検査する。errors があれば保存しない。"""

    result = ValidationResult()
    text = proposal.publish_text or ""

    _check_shape(text, proposal, policy, result)
    _check_links(text, proposal, policy, result)
    _check_style(text, policy, result)
    return result


# -- hard checks ---------------------------------------------------------------
def _check_shape(
    text: str, proposal: ThreadsProposal, policy: ThreadsStylePolicy, result: ValidationResult
) -> None:
    if not text.strip():
        result.errors.append("the post text is empty")
    # 公式の上限。ここは文体ではなく API の事実なので必ず error。
    if len(text) > policy.max_characters:
        result.errors.append(
            f"the post is {len(text)} characters; the Threads limit is {policy.max_characters}"
        )
    if _CONTROL_RE.search(text):
        result.errors.append("the post contains control characters")
    if _HTML_RE.search(text):
        result.errors.append("the post contains raw HTML")
    if proposal.angle not in policy.angles:
        result.errors.append(f"angle {proposal.angle!r} is not an allowed angle")
    if proposal.link_mode not in policy.allowed_link_modes:
        result.errors.append(f"link mode {proposal.link_mode!r} is not allowed")
    if proposal.character_count != len(text):
        result.errors.append("the recorded character count does not match the text")

    if len(text) > policy.preferred_max_characters:
        result.warnings.append(
            f"{len(text)} characters is longer than the preferred "
            f"{policy.preferred_max_characters}; consider trimming"
        )


def _check_links(
    text: str, proposal: ThreadsProposal, policy: ThreadsStylePolicy, result: ValidationResult
) -> None:
    urls = _URL_RE.findall(text)

    # 宛先の検査は link_mode に関係なく **すべての URL** に効かせる。
    # モードが none でも、本文に紛れ込んだ外部リンクやアフィリエイト誘導は通さない。
    for url in urls + ([proposal.destination_url] if proposal.destination_url else []):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        allowed = policy.allowed_link_host.lower()
        if host != allowed and not host.endswith(f".{allowed}"):
            # Threads から記事以外へ直接送らない (アフィリエイト先は特に)。
            result.errors.append(f"links must point at {allowed}; found {host or 'no host'}")
        for prefix in policy.forbidden_path_prefixes:
            if parts.path.startswith(prefix):
                result.errors.append(f"links must not use {prefix} (affiliate redirect)")

    if proposal.link_mode == LINK_MODE_NONE:
        if urls:
            result.errors.append("link_mode is 'none' but the post contains a URL")
        if proposal.destination_url:
            result.errors.append("link_mode is 'none' but a destination URL was recorded")
        return

    if proposal.link_mode == LINK_MODE_ARTICLE:
        if not proposal.destination_url:
            result.errors.append("link_mode is 'article' but no destination URL was built")
            return
        if proposal.destination_url not in text:
            result.errors.append("the destination URL does not appear in the post text")
        if len(urls) > 1:
            result.errors.append("an article post must carry exactly one link")


# -- style lint (warnings only) -------------------------------------------------
def _check_style(text: str, policy: ThreadsStylePolicy, result: ValidationResult) -> None:
    for phrase in policy.banned_phrases:
        if phrase and phrase in text:
            # 煽り表現は文脈を問わず出さない。
            result.errors.append(f"the post uses a banned phrase: {phrase}")

    for phrase in policy.discouraged_phrases:
        if phrase and phrase in text:
            result.warnings.append(f"article-style phrasing: {phrase}")

    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) > policy.max_sentences:
        result.warnings.append(
            f"{len(sentences)} sentences; the policy prefers at most {policy.max_sentences}"
        )
    long_ones = [s for s in sentences if len(s) > policy.preferred_max_sentence_characters]
    if long_ones:
        result.warnings.append(
            f"{len(long_ones)} sentence(s) exceed "
            f"{policy.preferred_max_sentence_characters} characters; prefer shorter ones"
        )

    run = 0
    for sentence in sentences:
        stripped = sentence.rstrip("。！？!? \n")
        if any(stripped.endswith(marker) for marker in policy.polite_markers):
            run += 1
            if run > policy.max_consecutive_polite_endings:
                joined = "/".join(policy.polite_markers)
                result.warnings.append(
                    f"{run} consecutive sentences end in '{joined}'; "
                    "vary the rhythm so it does not read mechanically"
                )
                break
        else:
            run = 0

    emoji = _EMOJI_RE.findall(text)
    if len(emoji) > policy.max_emoji:
        result.warnings.append(f"{len(emoji)} emoji; the policy allows at most {policy.max_emoji}")

    questions = text.count("？") + text.count("?")
    if questions > policy.max_questions:
        result.warnings.append(f"{questions} questions; one natural hook is enough")


def normalized_identity(text: str) -> str:
    """重複判定に使う正規形。

    投稿される文字列 (:func:`~app.social.threads.proposal.normalize_text`) とは
    別物で、こちらは NFKC まで畳んで「？」と「?」の違いを吸収する。
    """

    return canonical_identity(text).casefold()


def find_duplicates(proposals) -> list[tuple[int, int]]:
    """同じ内容の提案の組を返す (言い回しだけ違う重複を作らないため)。"""

    seen: dict[str, int] = {}
    duplicates: list[tuple[int, int]] = []
    for index, proposal in enumerate(proposals):
        key = normalized_identity(proposal.publish_text)
        if key in seen:
            duplicates.append((seen[key], index))
        else:
            seen[key] = index
    return duplicates
