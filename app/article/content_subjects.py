"""C3: 記事が比較・調査する **編集上の subject** (pure)。

C2 までは「比較対象 = 記事に link した AffiliateProgram」だったため、catalog に案件が無い
テーマ (RPA / 生成AI ツール 等) の比較記事が構造的に書けなかった。ここで

    「この記事が比較・調査する対象」  !=  「どの affiliate 案件に link しているか」

を分ける。affiliate 案件に裏付けられた subject は catalog の行をそのまま使い、そうでない
subject は版管理された編集カタログ (``app/config/content_subjects.json``) から来る。

このモジュールは DB / network に触れない。承認時の **選択の固定** は
``article_content_subjects`` 行が担う (config を後から変えても承認済み記事は動かない)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.models.article_content_subject import (
    SUBJECT_SOURCE_AFFILIATE,
    SUBJECT_SOURCE_EDITORIAL,
)

CONFIG_VERSION = 1
DEFAULT_SUBJECTS_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "content_subjects.json"
)
_TOP_KEYS = frozenset({"version", "note", "subjects", "topic_suggestions"})
_SUBJECT_KEYS = frozenset(
    {"key", "display_name", "canonical_name", "source", "topics", "note",
     "affiliate_program_name"}
)


class ContentSubjectConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class ContentSubject:
    """比較 / 調査の対象 1 件 (identity のみ。事実は ArticleFact が持つ)。"""

    key: str
    display_name: str
    canonical_name: str
    source: str
    topics: tuple[str, ...] = ()
    #: affiliate catalog に裏付けがある場合の program 名 (catalog 側と突き合わせる)
    affiliate_program_name: str | None = None
    note: str | None = None

    @property
    def is_affiliate_backed(self) -> bool:
        return self.source == SUBJECT_SOURCE_AFFILIATE


@dataclass(frozen=True)
class ContentSubjectCatalog:
    version: int
    subjects: tuple[ContentSubject, ...]
    topic_suggestions: Mapping[str, tuple[str, ...]]

    def by_key(self, key: str) -> ContentSubject | None:
        return next((s for s in self.subjects if s.key == key), None)

    def for_topic(self, topic: str) -> tuple[ContentSubject, ...]:
        """topic の推奨 subject を宣言順で返す (未知の topic は空)。"""

        keys = self.topic_suggestions.get(topic, ())
        found = [self.by_key(k) for k in keys]
        return tuple(s for s in found if s is not None)


def parse_content_subject_config(raw: object) -> ContentSubjectCatalog:
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise ContentSubjectConfigError(["content subject config must be a JSON object"])
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        errors.append(f"unknown top-level keys: {unknown}")
    if raw.get("version") != CONFIG_VERSION:
        errors.append(f"version must be {CONFIG_VERSION}")

    rows = raw.get("subjects")
    subjects: list[ContentSubject] = []
    if not isinstance(rows, list) or not rows:
        errors.append("subjects must be a non-empty list")
        rows = []
    seen_keys: set[str] = set()
    seen_names: set[str] = set()
    for entry in rows:
        if not isinstance(entry, Mapping):
            errors.append("every subject must be an object")
            continue
        extra = sorted(set(entry) - _SUBJECT_KEYS)
        if extra:
            errors.append(f"subject {entry.get('key')!r} has unknown keys: {extra}")
        key = entry.get("key")
        name = entry.get("display_name")
        canonical = entry.get("canonical_name")
        source = entry.get("source")
        if not isinstance(key, str) or not key.strip():
            errors.append("subject key must be a non-empty string")
            continue
        if key in seen_keys:
            errors.append(f"duplicate subject key {key!r}")
        seen_keys.add(key)
        if not isinstance(name, str) or not name.strip():
            errors.append(f"subject {key!r}: display_name must be a non-empty string")
            continue
        if name in seen_names:
            errors.append(f"duplicate subject display_name {name!r}")
        seen_names.add(name)
        if not isinstance(canonical, str) or not canonical.strip():
            errors.append(f"subject {key!r}: canonical_name must be a non-empty string")
            continue
        if source not in (SUBJECT_SOURCE_AFFILIATE, SUBJECT_SOURCE_EDITORIAL):
            errors.append(
                f"subject {key!r}: source must be {SUBJECT_SOURCE_AFFILIATE!r} or "
                f"{SUBJECT_SOURCE_EDITORIAL!r}"
            )
            continue
        topics = entry.get("topics", [])
        if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
            errors.append(f"subject {key!r}: topics must be a list of strings")
            continue
        affiliate_name = entry.get("affiliate_program_name")
        if affiliate_name is not None and not isinstance(affiliate_name, str):
            errors.append(f"subject {key!r}: affiliate_program_name must be a string")
            continue
        if source == SUBJECT_SOURCE_EDITORIAL and affiliate_name:
            errors.append(
                f"subject {key!r}: an editorial subject must not claim an affiliate program"
            )
            continue
        subjects.append(
            ContentSubject(
                key=key.strip(),
                display_name=name.strip(),
                canonical_name=canonical.strip(),
                source=source,
                topics=tuple(topics),
                affiliate_program_name=affiliate_name,
                note=entry.get("note") if isinstance(entry.get("note"), str) else None,
            )
        )

    raw_topics = raw.get("topic_suggestions", {})
    topic_suggestions: dict[str, tuple[str, ...]] = {}
    if not isinstance(raw_topics, Mapping):
        errors.append("topic_suggestions must be an object")
        raw_topics = {}
    for topic, keys in raw_topics.items():
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            errors.append(f"topic_suggestions[{topic!r}] must be a list of subject keys")
            continue
        missing = [k for k in keys if k not in seen_keys]
        if missing:
            errors.append(f"topic_suggestions[{topic!r}] references unknown subjects: {missing}")
            continue
        topic_suggestions[str(topic)] = tuple(keys)

    if errors:
        raise ContentSubjectConfigError(errors)
    return ContentSubjectCatalog(
        version=CONFIG_VERSION,
        subjects=tuple(subjects),
        topic_suggestions=topic_suggestions,
    )


def load_content_subject_config(
    path: str | Path = DEFAULT_SUBJECTS_PATH,
) -> ContentSubjectCatalog:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContentSubjectConfigError([f"content subject config not found: {path}"]) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentSubjectConfigError(
            [f"content subject config is not readable JSON: {exc}"]
        ) from exc
    return parse_content_subject_config(raw)
