"""Threads 投稿提案の identity と最終テキスト組み立て (T2、pure)。

**hash と URL の循環を避ける**のがこのモジュールの主題である。

投稿に記事リンクを入れると、URL には ``utm_content`` として提案の識別子が入る。
一方で提案の hash は最終テキスト (= URL を含む) から作りたい。素朴にやると

    hash -> URL -> テキスト -> hash

と循環してしまう。そこで **2 段階** にする:

1. ``content_seed``
       記事 id / 記事本文 hash / 切り口 / policy 版 / generator 版 /
       正規化した下書き (URL を入れる前) / link_mode から決まる。
       URL より **先に** 決まるので、循環しない。
2. ``destination_url``
       ``utm_content = content_seed[:16]`` を含む決定的な URL。
3. ``publish_text``
       下書きの ``{link}`` を URL で置き換えた、**実際に投稿される文字列**。
4. ``proposal_hash``
       content_seed / URL / publish_text から決まる最終 identity。
       人が承認するのはこの hash に結び付いた 1 つの文章である。

同じ入力からは必ず同じ hash と同じ URL が出る。承認後に文章を作り直すことは
できない (作り直せば hash が変わり、別の提案になる)。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from app.social.threads.attribution import decorate

#: 下書きの中でリンクを差し込む位置。
LINK_PLACEHOLDER = "{link}"

LINK_MODE_NONE = "none"
LINK_MODE_ARTICLE = "article"
LINK_MODES = (LINK_MODE_NONE, LINK_MODE_ARTICLE)

#: 提案生成器の版。規則を変えたら上げる (過去の提案と区別するため)。
GENERATOR_VERSION = "threads-proposal-1"

_WHITESPACE_RE = re.compile(r"[ \t　]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


@dataclass
class ThreadsProposalDraft:
    """生成器から受け取る下書き (まだ identity を持たない)。"""

    angle: str
    body: str
    link_mode: str = LINK_MODE_NONE

    @property
    def wants_link(self) -> bool:
        return self.link_mode == LINK_MODE_ARTICLE


@dataclass
class ThreadsProposal:
    """identity の確定した提案。ここから先は不変として扱う。"""

    source_article_id: int
    source_article_body_hash: str
    angle: str
    link_mode: str
    content_seed: str
    destination_url: str | None
    publish_text: str
    proposal_hash: str
    policy_version: str
    generator_version: str
    character_count: int
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_article_id": self.source_article_id,
            "source_article_body_hash": self.source_article_body_hash,
            "angle": self.angle,
            "link_mode": self.link_mode,
            "content_seed": self.content_seed,
            "destination_url": self.destination_url,
            "publish_text": self.publish_text,
            "proposal_hash": self.proposal_hash,
            "policy_version": self.policy_version,
            "generator_version": self.generator_version,
            "character_count": self.character_count,
            "warnings": list(self.warnings),
        }


def normalize_text(text: str) -> str:
    """**投稿される文字列** の整形。空白まわりだけを均す。

    NFKC はここでは **かけない**。日本語の「？」「（）」を半角へ畳んでしまうと、
    人が承認した文字とは別の文字が投稿されることになる。表記の統一より、
    承認したものがそのまま出ることのほうが大事である。

    ここでやるのは改行コードの統一、行末の空白落とし、連続空白の圧縮、過剰な
    空行の圧縮だけ。
    """

    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in normalized.split("\n")]
    return _BLANKLINES_RE.sub("\n\n", "\n".join(lines)).strip()


def canonical_identity(text: str) -> str:
    """**同一性の判定** に使う正規形。こちらは NFKC まで畳む。

    「？」と「?」の違いだけで別の提案として扱わないため。投稿される文字列は
    :func:`normalize_text` のほうで、元の表記のまま残る。
    """

    return unicodedata.normalize("NFKC", normalize_text(text))


def compute_content_seed(
    *,
    source_article_id: int,
    source_article_body_hash: str,
    angle: str,
    link_mode: str,
    draft_body: str,
    policy_version: str,
    generator_version: str = GENERATOR_VERSION,
) -> str:
    """URL より **先に** 決まる内容 identity。

    ここに URL を入れないことが、循環を避ける唯一の要点である。
    """

    payload = chr(31).join(
        [
            str(source_article_id),
            str(source_article_body_hash),
            str(angle),
            str(link_mode),
            canonical_identity(draft_body),
            str(policy_version),
            str(generator_version),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_destination_url(
    *, article_url: str, source_article_id: int, angle: str, content_seed: str
) -> str:
    """``utm_content`` に ``content_seed`` の先頭を使う決定的な URL。

    記事側に既にクエリが付いていても壊さない (:mod:`attribution` が引き継ぐ)。
    """

    return decorate(
        article_url,
        article_id=source_article_id,
        angle=angle,
        proposal_hash=content_seed,
    )


def build_publish_text(draft_body: str, destination_url: str | None) -> str:
    """実際に投稿される文字列を作る。

    ``{link}`` があればそこへ URL を差し込む。リンクを使わない提案では
    placeholder を取り除く (空行を残さない)。
    """

    text = normalize_text(draft_body)
    if destination_url:
        if LINK_PLACEHOLDER in text:
            text = text.replace(LINK_PLACEHOLDER, destination_url)
        else:
            text = f"{text}\n{destination_url}"
    else:
        text = text.replace(LINK_PLACEHOLDER, "")
    return normalize_text(text)


def compute_proposal_hash(
    *, content_seed: str, destination_url: str | None, publish_text: str
) -> str:
    """承認が結び付く最終 identity。1 文字でも変われば別の提案になる。"""

    payload = chr(31).join([content_seed, destination_url or "", publish_text])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_proposal(
    draft: ThreadsProposalDraft,
    *,
    source_article_id: int,
    source_article_body_hash: str,
    article_url: str | None,
    policy_version: str,
    generator_version: str = GENERATOR_VERSION,
) -> ThreadsProposal:
    """下書きを、identity の確定した不変の提案に変える。"""

    seed = compute_content_seed(
        source_article_id=source_article_id,
        source_article_body_hash=source_article_body_hash,
        angle=draft.angle,
        link_mode=draft.link_mode,
        draft_body=draft.body,
        policy_version=policy_version,
        generator_version=generator_version,
    )
    destination = None
    if draft.wants_link and article_url:
        destination = build_destination_url(
            article_url=article_url,
            source_article_id=source_article_id,
            angle=draft.angle,
            content_seed=seed,
        )
    publish_text = build_publish_text(draft.body, destination)
    return ThreadsProposal(
        source_article_id=source_article_id,
        source_article_body_hash=source_article_body_hash,
        angle=draft.angle,
        link_mode=draft.link_mode,
        content_seed=seed,
        destination_url=destination,
        publish_text=publish_text,
        proposal_hash=compute_proposal_hash(
            content_seed=seed, destination_url=destination, publish_text=publish_text
        ),
        policy_version=policy_version,
        generator_version=generator_version,
        character_count=len(publish_text),
    )
