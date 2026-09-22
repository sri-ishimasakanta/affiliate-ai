"""C3: 記事の比較 / 調査対象 (content subject) の解決と永続化。

比較対象は **affiliate link とは独立** になった。解決順序:

1. ``article_content_subjects`` に行があれば **それが正**
   (承認時に固定した編集上の選択。config を後から変えても動かない)。
2. 行が無ければ **legacy fallback**: link した affiliate program をそのまま対象とみなす
   (C3 より前に承認された記事 -- article #1 を含む -- の互換性を保つ)。

承認時の候補提示は版管理された編集カタログ (``app/config/content_subjects.json``) と
affiliate catalog の両方から作る。事実 (価格・機能・URL) はここでは扱わない。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.article.content_subjects import (
    ContentSubjectCatalog,
    load_content_subject_config,
)
from app.models import AffiliateProgram
from app.models.article_content_subject import (
    SUBJECT_SOURCE_AFFILIATE,
    SUBJECT_SOURCE_EDITORIAL,
)
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)
from app.repositories.article_content_subject_repository import (
    ArticleContentSubjectRepository,
)

SOURCE_PERSISTED = "persisted"
SOURCE_LEGACY_LINKS = "legacy_affiliate_links"


@dataclass(frozen=True)
class ResolvedSubject:
    """記事が比較 / 調査する対象 1 件 (解決後)。"""

    subject_key: str
    #: fact の subject_ref と一致する表示名
    display_name: str
    subject_source: str
    position: int
    affiliate_program_id: int | None = None
    #: affiliate 裏付けがある場合の catalog 行 (fact pack の candidate 表示に使う)
    program: AffiliateProgram | None = None

    @property
    def is_affiliate_backed(self) -> bool:
        return self.affiliate_program_id is not None


@dataclass(frozen=True)
class SubjectResolution:
    subjects: tuple[ResolvedSubject, ...]
    #: ``persisted`` (明示的に選ばれた) / ``legacy_affiliate_links`` (link からの fallback)
    source: str

    @property
    def display_names(self) -> tuple[str, ...]:
        return tuple(s.display_name for s in self.subjects)

    @property
    def programs(self) -> list[AffiliateProgram]:
        """affiliate 裏付けのある対象の catalog 行だけ (順序は subject 順)。"""

        return [s.program for s in self.subjects if s.program is not None]


def subject_key_for_program(program: AffiliateProgram) -> str:
    """affiliate program から決定論的な subject key を作る。"""

    return f"affiliate:{program.id}"


class ContentSubjectService:
    def __init__(self, session: Session, *, catalog: ContentSubjectCatalog | None = None) -> None:
        self._session = session
        self._subjects = ArticleContentSubjectRepository(session)
        self._links = ArticleAffiliateProgramRepository(session)
        self._programs = AffiliateProgramRepository(session)
        self._catalog = catalog

    @property
    def catalog(self) -> ContentSubjectCatalog:
        if self._catalog is None:
            self._catalog = load_content_subject_config()
        return self._catalog

    # -- resolution (read-only) ----------------------------------------
    def resolve(self, article_id: int) -> SubjectResolution:
        rows = self._subjects.list_by_article(article_id)
        if rows:
            resolved = tuple(
                ResolvedSubject(
                    subject_key=row.subject_key,
                    display_name=row.display_name,
                    subject_source=row.subject_source,
                    position=row.position,
                    affiliate_program_id=row.affiliate_program_id,
                    program=(
                        self._programs.get_by_id(row.affiliate_program_id)
                        if row.affiliate_program_id is not None
                        else None
                    ),
                )
                for row in rows
            )
            return SubjectResolution(resolved, SOURCE_PERSISTED)

        # legacy: link した affiliate program をそのまま比較対象とみなす (C3 以前の記事)
        legacy: list[ResolvedSubject] = []
        links = sorted(
            self._links.list_by_article(article_id), key=lambda x: x.affiliate_program_id
        )
        for position, link in enumerate(links):
            program = self._programs.get_by_id(link.affiliate_program_id)
            if program is None:
                continue
            legacy.append(
                ResolvedSubject(
                    subject_key=subject_key_for_program(program),
                    display_name=program.name,
                    subject_source=SUBJECT_SOURCE_AFFILIATE,
                    position=position,
                    affiliate_program_id=program.id,
                    program=program,
                )
            )
        return SubjectResolution(tuple(legacy), SOURCE_LEGACY_LINKS)

    # -- persistence (caller owns the transaction) ---------------------
    def persist_for_new_article(
        self,
        article_id: int,
        *,
        affiliate_program_ids: list[int],
        editorial_subject_keys: list[str],
    ) -> list[ResolvedSubject]:
        """承認時に比較対象を **行として固定** する (commit は呼び出し側)。

        affiliate 裏付けの対象を先に、編集カタログの対象をそのあとに、指定順で並べる。
        """

        created: list[ResolvedSubject] = []
        position = 0
        seen_names: set[str] = set()
        for program_id in affiliate_program_ids:
            program = self._programs.get_by_id(program_id)
            if program is None:
                raise ValueError(f"affiliate program {program_id} not found")
            if program.name in seen_names:
                continue
            seen_names.add(program.name)
            self._subjects.create(
                article_id=article_id,
                subject_key=subject_key_for_program(program),
                display_name=program.name,
                subject_source=SUBJECT_SOURCE_AFFILIATE,
                position=position,
                affiliate_program_id=program.id,
            )
            created.append(
                ResolvedSubject(
                    subject_key=subject_key_for_program(program),
                    display_name=program.name,
                    subject_source=SUBJECT_SOURCE_AFFILIATE,
                    position=position,
                    affiliate_program_id=program.id,
                    program=program,
                )
            )
            position += 1

        for key in editorial_subject_keys:
            subject = self.catalog.by_key(key)
            if subject is None:
                raise ValueError(f"unknown content subject key {key!r}")
            if subject.display_name in seen_names:
                continue
            seen_names.add(subject.display_name)
            self._subjects.create(
                article_id=article_id,
                subject_key=subject.key,
                display_name=subject.display_name,
                subject_source=SUBJECT_SOURCE_EDITORIAL,
                position=position,
            )
            created.append(
                ResolvedSubject(
                    subject_key=subject.key,
                    display_name=subject.display_name,
                    subject_source=SUBJECT_SOURCE_EDITORIAL,
                    position=position,
                )
            )
            position += 1
        return created
