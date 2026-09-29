"""再利用できる SaaS の事実 (C10-2、``saas-facts/1``、pure)。

記事を書くたびに同じ SaaS の情報を調べ直さないために、既存の記事ごとの事実
(``article_facts``: 記事・対象・事実の鍵・値・確かめた時刻・出典) を **対象ごと** の見方に
まとめる。新しい表は作らない (保存は既存の ``article_facts`` / ``sources`` のまま)。

- 対象 (``subject_key``) は表示名を正規化したもの (例: 「Fireflies.ai」)。
- 事実の鍵ごとに、どの記事のどの出典の値が最新か (``checked_at``)・鮮度
  (``app/article/fact_freshness.py`` の境: 料金 30 日・機能 90 日・静的 180 日)・食い違い
  (記事ごとの最新の値が違う) を持つ。
- **古い事実を最新として使わない** (``fresh=False`` は ``stale``)。**出典の無い値は作らない**
  (``unknown`` の行は値として数えない)。
- 準備度: 必須の鍵 (``REQUIRED_FACT_KEYS``) が全部新しい → ``ready``、古いものがある →
  ``stale``、無いものがある → ``partial``、何も無い → ``missing``。
- 外から新しく集める仕組みはここに無い。足りない・古い対象は ``research_plan`` に並べるだけ。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

from app.article.fact_freshness import ensure_aware, is_fresh
from app.article.fact_keys import REQUIRED_FACT_KEYS, FactKey
from app.content.text import phrase_key, stable_hash

FACTS_SCHEMA = "saas-facts/1"
READY = "ready"
STALE = "stale"
PARTIAL = "partial"
MISSING = "missing"


@dataclass(frozen=True)
class StoredFact:
    article_id: int
    subject_ref: str
    fact_key: str
    fact_value: object
    value_status: str
    checked_at: datetime
    source_id: int | None
    affiliate_program_id: int | None = None


@dataclass(frozen=True)
class FactView:
    subject_key: str
    subject_ref: str
    fact_key: str
    value: object
    value_status: str
    checked_at: str
    fresh: bool
    source_article_id: int
    source_id: int | None
    conflicting: bool
    articles: tuple[int, ...]

    @property
    def state(self) -> str:
        return READY if self.fresh else STALE

    def as_dict(self) -> dict:
        return {**asdict(self), "state": self.state}


@dataclass(frozen=True)
class SubjectFacts:
    subject_key: str
    subject_ref: str
    readiness: str
    facts: tuple[FactView, ...]
    missing_required: tuple[str, ...]
    stale_required: tuple[str, ...]
    affiliate_program_ids: tuple[int, ...]
    articles: tuple[int, ...]

    def as_dict(self) -> dict:
        return {**asdict(self), "facts": [f.as_dict() for f in self.facts]}


def _key(fact_key: str) -> FactKey | None:
    try:
        return FactKey(fact_key)
    except ValueError:
        return None


def build_subject_facts(rows: Sequence[StoredFact], *, now: datetime) -> list[SubjectFacts]:
    """記事ごとの事実 → 対象ごとの事実 (決定論的)。"""

    now = ensure_aware(now)
    per_article: dict[tuple[str, str, int], StoredFact] = {}
    for r in rows:
        if ensure_aware(r.checked_at) > now:
            continue  # 観測の時点より後の事実は使わない
        key = (phrase_key(r.subject_ref), r.fact_key, r.article_id)
        current = per_article.get(key)
        if current is None or ensure_aware(r.checked_at) > ensure_aware(current.checked_at):
            per_article[key] = r
    by_subject: dict[str, dict[str, list[StoredFact]]] = {}
    names: dict[str, str] = {}
    for (subject, fact_key, _aid), r in per_article.items():
        by_subject.setdefault(subject, {}).setdefault(fact_key, []).append(r)
        names.setdefault(subject, r.subject_ref)
    out = []
    for subject in sorted(by_subject):
        views = []
        for fact_key, facts in sorted(by_subject[subject].items()):
            usable = [f for f in facts if f.value_status != "unknown"]
            if not usable:
                continue  # 出典の無い値は作らない (unknown は値ではない)
            latest = max(usable, key=lambda f: (ensure_aware(f.checked_at), f.article_id))
            fk = _key(fact_key)
            fresh = bool(fk) and is_fresh(fk, ensure_aware(latest.checked_at), now=now)
            values = {stable_hash(f.fact_value) for f in usable}
            views.append(FactView(subject, latest.subject_ref, fact_key, latest.fact_value,
                                  latest.value_status, ensure_aware(latest.checked_at).isoformat(),
                                  fresh, latest.article_id, latest.source_id, len(values) > 1,
                                  tuple(sorted({f.article_id for f in usable}))))
        present = {v.fact_key: v for v in views}
        missing = tuple(k.value for k in REQUIRED_FACT_KEYS if k.value not in present)
        stale = tuple(k.value for k in REQUIRED_FACT_KEYS
                      if k.value in present and not present[k.value].fresh)
        readiness = (MISSING if not views else PARTIAL if missing else STALE if stale
                     else READY)
        programs = tuple(sorted({f.affiliate_program_id for facts in by_subject[subject].values()
                                 for f in facts if f.affiliate_program_id}))
        articles = tuple(sorted({a for v in views for a in v.articles}))
        out.append(SubjectFacts(subject, names[subject], readiness, tuple(views), missing, stale,
                                programs, articles))
    return out


def research_plan(subjects: Sequence[SubjectFacts]) -> list[dict]:
    """外から調べ直すものの一覧 (呼ばない。人・後の段階が実行する)。"""

    return [{"subject": s.subject_ref, "readiness": s.readiness,
             "stale_required": list(s.stale_required),
             "missing_required": list(s.missing_required),
             "conflicting": sorted(v.fact_key for v in s.facts if v.conflicting)}
            for s in subjects if s.readiness != READY or any(v.conflicting for v in s.facts)]


__all__ = ["FACTS_SCHEMA", "FactView", "MISSING", "PARTIAL", "READY", "STALE", "StoredFact",
           "SubjectFacts", "build_subject_facts", "research_plan"]
