"""既存 Source / ArticleFact の ``checked_at`` を正しい UTC instant へ修復する
**一度きりの maintenance script** (Phase 3B-3B.2A)。

背景 (Phase 3B-3B.1 で確認された timezone 保存バグ)
--------------------------------------------------------
``DateTime(timezone=True)`` でも SQLite は tzinfo を保存しない。修正前の書き込み
経路は aware datetime を UTC へ正規化せずに渡していたため、``+09:00`` の
``2026-08-28T14:12:00+09:00`` が offset を落とした naive wall-clock
``2026-08-28 14:12:00`` として保存されていた。読み出し側は naive を UTC とみなす
ため、本来 ``05:12:00Z`` の instant が ``14:12:00Z`` として扱われ、+9 時間ずれる。

書き込み経路自体は Phase 3B-3B.1 で :func:`app.article.fact_freshness.to_storage_utc`
により修正済み。この script は **その修正前に投入された既存行** の ``checked_at``
だけを、調査結果 JSON (正本) の timezone-aware ``checked_at`` から計算した
正しい UTC instant へ書き換える。

immutable 履歴との関係
----------------------
ArticleFact は通常 immutable (事実の更新は新しい行の append) だが、ここで直すのは
「観測した事実の内容」ではなく「保存実装バグで誤記録された ``checked_at``」である。
したがって **新しい行を append せず、既存行の id を保ったまま ``checked_at`` のみ**
を修復する。これは storage-layer の一度きりの是正であり、通常運用では行わない。

安全設計
--------
* ``--article-id`` で指定した 1 記事のみ対象。JSON の ``article_id`` と不一致なら停止。
* preflight (記事の存在 / status / body / primary link 数) を確認。
* JSON <-> DB を Source は (article_id, canonical source_url)、Fact は
  (article_id, subject_ref, fact_key) で 1:1 対応付け。件数不一致・重複・欠落・
  余剰・所有権不一致・identity 不一致・source 参照不一致はすべて停止条件。
* 各行の現在値を分類する:
  ``already_correct`` (= ``to_storage_utc(json_checked_at)``) /
  ``needs_repair`` (= JSON の naive wall-clock そのまま = 既知の broken 状態) /
  それ以外は ``unexpected_current_value`` として全体停止。
* 1 件でも error があれば **何も書き込まない**。
* ``--apply`` 指定時のみ、単一 transaction で ``sources.checked_at`` と
  ``article_facts.checked_at`` **だけ** を UPDATE する。他 column / 他テーブルは触らない。
* 途中失敗は full rollback。再実行しても既に正しい行は ``already_correct`` 扱いで
  二重補正しない。

使い方
------
    uv run python scripts/repair_article_fact_timestamps.py \
      --article-id 1 --file data/article_1_facts_research.json --dry-run

    uv run python scripts/repair_article_fact_timestamps.py \
      --article-id 1 --file data/article_1_facts_research.json --apply

``--dry-run`` / ``--apply`` は排他。どちらも無指定なら dry-run 扱い (safe default)。
外部 Web アクセスなし。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.article.fact_freshness import to_storage_utc  # noqa: E402
from app.article.fact_validation import validate_fact  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.models import Article, ArticleAffiliateProgram, ArticleFact, Source  # noqa: E402
from app.repositories.article_fact_repository import ArticleFactRepository  # noqa: E402
from app.repositories.source_repository import SourceRepository  # noqa: E402
from app.services.source_service import SourceService  # noqa: E402

EXIT_OK = 0
EXIT_INVALID = 1  # 検証失敗 / 想定外の状態 -> 何も変更していない
EXIT_BAD_INPUT = 2  # file がない / JSON ではない / 引数エラー

EXPECTED_ARTICLE_STATUS = "planned"

_SAMPLE_SOURCE_LABELS = frozenset(
    {
        "make_pricing",
        "hubspot_pricing",
        "reclaim_pricing",
        "monday_help_languages",
        "todoist_help_language",
    }
)
_MAX_SAMPLE_FACTS = 5


# -- 結果モデル -------------------------------------------------------


@dataclass
class EntityDiff:
    kind: str  # "source" | "fact"
    entity_id: int
    label: str
    old_checked_at: datetime  # DB 現在値 (naive UTC 解釈)
    json_checked_at: datetime  # JSON の aware 値
    new_checked_at: datetime  # 修復後の保存値 (naive UTC wall-clock)
    classification: str  # "needs_repair" | "already_correct"


@dataclass
class RepairResult:
    article_id: int
    apply_requested: bool = False
    applied: bool = False
    article_status: str | None = None
    article_body_is_none: bool | None = None
    affiliate_link_count: int = 0
    primary_count: int = 0
    source_json_count: int = 0
    source_db_count: int = 0
    source_matched: int = 0
    source_needs_repair: int = 0
    source_already_correct: int = 0
    fact_json_count: int = 0
    fact_db_count: int = 0
    fact_matched: int = 0
    fact_needs_repair: int = 0
    fact_already_correct: int = 0
    diffs: list[EntityDiff] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def total_would_update(self) -> int:
        return self.source_needs_repair + self.fact_needs_repair

    @property
    def ok(self) -> bool:
        return not self.errors


# -- helpers (pure) ------------------------------------------------


def _parse_aware(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("checked_at must be an ISO-8601 string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"checked_at is not ISO-8601: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"checked_at must be timezone-aware: {value!r}")
    return dt


def _as_naive_utc(dt: datetime) -> datetime:
    """DB から読んだ値を naive UTC wall-clock へそろえる (naive はそのまま UTC とみなす)。"""

    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def _norm_reason(value: str | None) -> str | None:
    return (value or "").strip() or None


# -- planner --------------------------------------------------------


class _RepairPlanner:
    """read-only で全検証を行い、``needs_repair`` 行の (entity, new_checked_at) を貯める。

    ``apply()`` は ``plan()`` が error 0 を返した後にのみ呼ぶこと。
    """

    def __init__(
        self, session, article_id: int, payload: dict, now: datetime
    ) -> None:
        self._session = session
        self._article_id = article_id
        self._payload = payload
        self._now = now
        self._sources_repo = SourceRepository(session)
        self._facts_repo = ArticleFactRepository(session)
        self._source_service = SourceService(session)
        self.errors: list[str] = []
        self._tmp_source_map: dict[str, Source] = {}
        self._source_updates: list[tuple[Source, datetime]] = []
        self._fact_updates: list[tuple[ArticleFact, datetime]] = []

    # -- public ---------------------------------------------------
    def plan(self, *, apply_requested: bool) -> RepairResult:
        r = RepairResult(article_id=self._article_id, apply_requested=apply_requested)

        body_aid = self._payload.get("article_id")
        if body_aid is not None and int(body_aid) != self._article_id:
            self.errors.append(
                f"file article_id {body_aid!r} does not match --article-id "
                f"{self._article_id}"
            )

        article = self._session.get(Article, self._article_id)
        if article is None:
            self.errors.append(f"Article {self._article_id} not found")
            r.errors = self.errors
            return r

        self._check_article(article, r)
        self._tmp_source_map = self._plan_sources(r)
        self._plan_facts(r)

        r.errors = self.errors
        return r

    def apply(self) -> None:
        for entity, new_dt in self._source_updates:
            entity.checked_at = new_dt
        for entity, new_dt in self._fact_updates:
            entity.checked_at = new_dt
        self._session.flush()

    # -- preflight ----------------------------------------------
    def _check_article(self, article: Article, r: RepairResult) -> None:
        r.article_status = article.status
        r.article_body_is_none = article.body is None
        links = list(
            self._session.scalars(
                select(ArticleAffiliateProgram).where(
                    ArticleAffiliateProgram.article_id == self._article_id
                )
            ).all()
        )
        r.affiliate_link_count = len(links)
        r.primary_count = sum(1 for link in links if link.is_primary)

        if article.status != EXPECTED_ARTICLE_STATUS:
            self.errors.append(
                f"Article status is {article.status!r}, expected "
                f"{EXPECTED_ARTICLE_STATUS!r} (refusing to repair an article "
                "that moved past planning)"
            )
        if article.body is not None:
            self.errors.append(
                "Article.body is not None (article already has draft content); "
                "refusing to repair"
            )
        if r.primary_count != 0:
            self.errors.append(
                f"Article has {r.primary_count} primary affiliate link(s), "
                "expected 0"
            )

    # -- sources ------------------------------------------------
    def _plan_sources(self, r: RepairResult) -> dict[str, Source]:
        raw_sources = self._payload.get("sources") or []
        db_sources = self._sources_repo.list_by_article(self._article_id)
        r.source_json_count = len(raw_sources)
        r.source_db_count = len(db_sources)

        json_by_url: dict[str, dict] = {}
        for i, raw in enumerate(raw_sources):
            if not isinstance(raw, dict):
                self.errors.append(f"sources[{i}] is not an object")
                continue
            try:
                canon = self._source_service.safe_url(str(raw.get("source_url") or ""))
            except Exception as exc:  # noqa: BLE001 - report and stop
                self.errors.append(f"sources[{i}] url not canonicalizable: {exc}")
                continue
            if canon in json_by_url:
                self.errors.append(
                    f"sources[{i}] duplicate canonical url in JSON: {canon}"
                )
                continue
            json_by_url[canon] = raw

        db_by_url: dict[str, list[Source]] = {}
        for s in db_sources:
            db_by_url.setdefault(s.source_url or "", []).append(s)

        matched: set[int] = set()
        tmp_to_db_source: dict[str, Source] = {}
        for canon, raw in json_by_url.items():
            tmp_id = str(raw.get("tmp_id") or "").strip()
            label = tmp_id or canon
            cands = db_by_url.get(canon, [])
            if len(cands) == 0:
                self.errors.append(
                    f"source not found in DB for url {canon} (tmp_id={tmp_id!r})"
                )
                continue
            if len(cands) > 1:
                self.errors.append(
                    f"multiple DB sources {[c.id for c in cands]} for url {canon}"
                )
                continue
            db_s = cands[0]
            matched.add(db_s.id)
            if tmp_id:
                tmp_to_db_source[tmp_id] = db_s

            if db_s.article_id != self._article_id:
                self.errors.append(
                    f"source {db_s.id} belongs to article {db_s.article_id}, "
                    f"not {self._article_id}"
                )
            if (raw.get("source_type") or None) != db_s.source_type:
                self.errors.append(
                    f"source {db_s.id} ({label}) source_type mismatch: "
                    f"db={db_s.source_type!r} json={raw.get('source_type')!r}"
                )
            if (raw.get("title") or None) != (db_s.title or None):
                self.errors.append(
                    f"source {db_s.id} ({label}) title mismatch: "
                    f"db={db_s.title!r} json={raw.get('title')!r}"
                )
            self._classify(
                kind="source",
                entity=db_s,
                entity_id=db_s.id,
                label=label,
                json_raw_checked=raw.get("checked_at"),
                r=r,
            )

        extra = sorted(s.id for s in db_sources if s.id not in matched)
        if extra:
            self.errors.append(f"extra DB sources not present in JSON: {extra}")
        r.source_matched = len(matched)
        return tmp_to_db_source

    # -- facts --------------------------------------------------
    def _plan_facts(self, r: RepairResult) -> None:
        raw_tools = self._payload.get("tools") or []
        db_facts = self._facts_repo.list_by_article(self._article_id)
        r.fact_db_count = len(db_facts)

        json_facts: dict[tuple[str, str], tuple[dict, dict]] = {}
        for t_i, tool in enumerate(raw_tools):
            if not isinstance(tool, dict):
                self.errors.append(f"tools[{t_i}] is not an object")
                continue
            subj = str(tool.get("subject_ref") or "").strip()
            facts = tool.get("facts") or {}
            if not isinstance(facts, dict):
                self.errors.append(f"tools[{t_i}].facts is not an object")
                continue
            for fk, fd in facts.items():
                key = (subj, fk)
                if key in json_facts:
                    self.errors.append(f"duplicate JSON fact {subj}/{fk}")
                    continue
                json_facts[key] = (fd, tool)
        r.fact_json_count = len(json_facts)

        db_by_key: dict[tuple[str, str], list[ArticleFact]] = {}
        for f in db_facts:
            db_by_key.setdefault((f.subject_ref, f.fact_key), []).append(f)

        matched: set[int] = set()
        for (subj, fk), (fd, tool) in json_facts.items():
            label = f"{subj}/{fk}"
            cands = db_by_key.get((subj, fk), [])
            if len(cands) == 0:
                self.errors.append(f"fact not found in DB: {label}")
                continue
            if len(cands) > 1:
                self.errors.append(
                    f"multiple DB facts {[c.id for c in cands]} for {label}"
                )
                continue
            db_f = cands[0]
            matched.add(db_f.id)

            if db_f.article_id != self._article_id:
                self.errors.append(
                    f"fact {db_f.id} ({label}) belongs to article "
                    f"{db_f.article_id}, not {self._article_id}"
                )
            if tool.get("affiliate_program_id") != db_f.affiliate_program_id:
                self.errors.append(
                    f"fact {db_f.id} ({label}) affiliate_program_id mismatch: "
                    f"db={db_f.affiliate_program_id!r} "
                    f"json={tool.get('affiliate_program_id')!r}"
                )

            mapped_src = self._check_fact_source(db_f, fd, label)
            self._crosscheck_fact_value(db_f, fd, fk, mapped_src, label)
            self._classify(
                kind="fact",
                entity=db_f,
                entity_id=db_f.id,
                label=label,
                json_raw_checked=fd.get("checked_at"),
                r=r,
            )

        extra = sorted(f.id for f in db_facts if f.id not in matched)
        if extra:
            self.errors.append(f"extra DB facts not present in JSON: {extra}")
        r.fact_matched = len(matched)

    def _check_fact_source(
        self, db_f: ArticleFact, fd: dict, label: str
    ) -> Source | None:
        src_tmp = fd.get("source")
        if src_tmp is None:
            if db_f.source_id is not None:
                self.errors.append(
                    f"fact {db_f.id} ({label}): DB has source_id "
                    f"{db_f.source_id} but JSON fact has no source"
                )
            return None
        mapped = self._tmp_source_map.get(str(src_tmp).strip())
        if mapped is None:
            self.errors.append(
                f"fact {db_f.id} ({label}): JSON source tmp_id {src_tmp!r} "
                "did not map to a DB source"
            )
            return None
        if db_f.source_id != mapped.id:
            self.errors.append(
                f"fact {db_f.id} ({label}): source_id mismatch "
                f"db={db_f.source_id!r} expected={mapped.id}"
            )
        return mapped

    def _crosscheck_fact_value(
        self,
        db_f: ArticleFact,
        fd: dict,
        fk: str,
        mapped_src: Source | None,
        label: str,
    ) -> None:
        """import と同じ共有バリデータで JSON を正規化し、DB の内容と突き合わせる。

        ここで内容差を見つけても **修復しない** (timestamp だけが対象)。差があれば
        error として全体を停止させる (mapping ミス検知)。
        """

        try:
            validated = validate_fact(
                fact_key=fk,
                value_status=str(fd.get("value_status")),
                fact_value=fd.get("value"),
                unknown_reason=fd.get("unknown_reason"),
                source_type=mapped_src.source_type if mapped_src is not None else None,
                source_present=mapped_src is not None,
                checked_at=_parse_aware(fd.get("checked_at")),
                now=self._now,
            )
        except Exception as exc:  # noqa: BLE001 - report and stop
            self.errors.append(
                f"fact {db_f.id} ({label}): JSON fails shared validation: {exc}"
            )
            return
        if str(validated.value_status) != db_f.value_status:
            self.errors.append(
                f"fact {db_f.id} ({label}): value_status mismatch "
                f"db={db_f.value_status!r} json={str(validated.value_status)!r}"
            )
        if validated.fact_value != db_f.fact_value:
            self.errors.append(
                f"fact {db_f.id} ({label}): fact_value differs between JSON and "
                "DB (content is not repaired by this script)"
            )
        if validated.unknown_reason != _norm_reason(db_f.unknown_reason):
            self.errors.append(
                f"fact {db_f.id} ({label}): unknown_reason differs between JSON "
                "and DB"
            )

    # -- timestamp classification --------------------------------
    def _classify(
        self,
        *,
        kind: str,
        entity,
        entity_id: int,
        label: str,
        json_raw_checked: object,
        r: RepairResult,
    ) -> None:
        try:
            json_dt = _parse_aware(json_raw_checked)
        except ValueError as exc:
            self.errors.append(f"{kind} {entity_id} ({label}): {exc}")
            return

        new_storage = to_storage_utc(json_dt)  # naive UTC wall-clock
        broken_expected = json_dt.replace(tzinfo=None)  # naive local wall-clock

        if entity.checked_at is None:
            self.errors.append(f"{kind} {entity_id} ({label}): checked_at is NULL")
            return
        old = _as_naive_utc(entity.checked_at)

        if old == new_storage:
            r.diffs.append(
                EntityDiff(
                    kind, entity_id, label, old, json_dt, new_storage,
                    "already_correct",
                )
            )
            if kind == "source":
                r.source_already_correct += 1
            else:
                r.fact_already_correct += 1
            return

        offset = json_dt.utcoffset()
        if old == broken_expected and (broken_expected - new_storage) == offset:
            r.diffs.append(
                EntityDiff(
                    kind, entity_id, label, old, json_dt, new_storage,
                    "needs_repair",
                )
            )
            if kind == "source":
                r.source_needs_repair += 1
                self._source_updates.append((entity, new_storage))
            else:
                r.fact_needs_repair += 1
                self._fact_updates.append((entity, new_storage))
            return

        self.errors.append(
            f"{kind} {entity_id} ({label}): unexpected current checked_at "
            f"{old.isoformat()} (neither the known broken wall-clock "
            f"{broken_expected.isoformat()} nor the repaired UTC "
            f"{new_storage.isoformat()}); refusing to touch it"
        )


def run_repair(
    *,
    article_id: int,
    path: Path,
    apply: bool,
    session_factory=SessionLocal,
    now: datetime | None = None,
) -> RepairResult:
    now = now or datetime.now(UTC)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")

    with session_factory() as session:
        planner = _RepairPlanner(session, article_id, payload, now)
        result = planner.plan(apply_requested=apply)

        if result.errors or not apply:
            session.rollback()
            if result.errors:
                result.messages.append(
                    "validation failed: no changes were committed"
                )
            else:
                result.messages.append("dry-run: no changes were committed")
            return result

        try:
            planner.apply()
            session.commit()
        except Exception:
            session.rollback()
            raise
        result.applied = True
        result.messages.append(
            f"applied: updated {result.total_would_update} checked_at value(s) "
            f"({result.source_needs_repair} source, {result.fact_needs_repair} fact)"
        )
        return result


# -- CLI ----------------------------------------------------------


def _sample_diffs(result: RepairResult) -> list[EntityDiff]:
    sources = [
        d
        for d in result.diffs
        if d.kind == "source" and d.label in _SAMPLE_SOURCE_LABELS
    ]
    facts = [d for d in result.diffs if d.kind == "fact"][:_MAX_SAMPLE_FACTS]
    return sources + facts


def _print(result: RepairResult) -> None:
    print(f"article_id: {result.article_id}")
    print(
        f"  status={result.article_status!r} body_is_none="
        f"{result.article_body_is_none} affiliate_links="
        f"{result.affiliate_link_count} primary={result.primary_count}"
    )
    print(
        "sources: "
        f"json={result.source_json_count} db={result.source_db_count} "
        f"matched={result.source_matched} "
        f"needs_repair={result.source_needs_repair} "
        f"already_correct={result.source_already_correct}"
    )
    print(
        "facts:   "
        f"json={result.fact_json_count} db={result.fact_db_count} "
        f"matched={result.fact_matched} "
        f"needs_repair={result.fact_needs_repair} "
        f"already_correct={result.fact_already_correct}"
    )
    print(f"total_would_update: {result.total_would_update}")

    samples = _sample_diffs(result)
    if samples:
        print("\nsample checked_at changes:")
        for d in samples:
            print(
                f"  [{d.kind}] id={d.entity_id} {d.label}\n"
                f"      old (DB, UTC)   : {d.old_checked_at.isoformat()}\n"
                f"      json checked_at : {d.json_checked_at.isoformat()}\n"
                f"      new (DB, UTC)   : {d.new_checked_at.isoformat()}\n"
                f"      shift           : -{d.old_checked_at - d.new_checked_at} "
                f"({d.classification})"
            )

    if result.errors:
        print(f"\nERRORS ({len(result.errors)}): nothing was changed")
        for e in result.errors:
            print(f"  - {e}")
        return

    for m in result.messages:
        print(f"\n{m}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="repair_article_fact_timestamps",
        description=(
            "既存 Source / ArticleFact の checked_at を調査 JSON の正しい UTC "
            "instant へ修復する一度きりの maintenance script"
        ),
    )
    parser.add_argument("--article-id", required=True, type=int)
    parser.add_argument("--file", required=True, metavar="PATH")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="検証と差分表示のみ (default)。DB は変更しない。",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="全検証を通過した場合のみ checked_at を単一 transaction で UPDATE する。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    path = Path(args.file)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        result = run_repair(
            article_id=args.article_id, path=path, apply=bool(args.apply)
        )
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"cannot repair: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    _print(result)
    return EXIT_INVALID if result.errors else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
