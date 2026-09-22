"""管理用 READ-ONLY CLI: canonical ``Article.body`` から外部リンク occurrence を
決定的に discover し、既存 mapping/target/runtime acknowledgement に基づく現在の
substitution eligibility を preview する (D-D2)。

    uv run python -m scripts.preview_article_link_occurrences --article-id 1

- DB は **read only** (write 0 — commit を一度も呼ばない)。WordPress へは一切
  通信しない (HTTP request 0)。
- ArticleLinkSubstitutionMapping / ArticlePublicationArtifact / AffiliateLinkTarget
  の作成・変更は一切行わない。PLAN/PREVIEW only — ``--execute`` フラグは無い。
- 出力は安全な要約のみ。full token は一切出力しない (token fingerprint の先頭
  12 文字のみ)。destination_url / shared secret / HMAC は一切出力しない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.exceptions import EntityNotFoundError  # noqa: E402
from app.services.article_link_occurrence_preview_service import (  # noqa: E402
    ArticleLinkOccurrencePreview,
    ArticleLinkOccurrencePreviewService,
)

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_UNEXPECTED = 2


def run_preview(
    *, article_id: int, session_factory=SessionLocal
) -> ArticleLinkOccurrencePreview:
    with session_factory() as session:
        service = ArticleLinkOccurrencePreviewService(session, settings=get_settings())
        return service.preview(article_id)


def _fp(value: str | None, chars: int = 12) -> str:
    if value is None:
        return "-"
    return f"{value[:chars]}…"


def format_preview(preview: ArticleLinkOccurrencePreview) -> str:
    lines = [
        "=== Article Link Occurrence Preview (READ-ONLY) ===",
        f"article_id                  = {preview.article_id}",
        f"article_title               = {preview.article_title}",
        f"canonical_body_hash         = {preview.canonical_body_hash}",
        f"renderer_version            = {preview.renderer_version}",
        f"occurrence_schema_version   = {preview.occurrence_schema_version}",
        f"external_occurrence_count   = {preview.external_occurrence_count}",
        f"active_mapping_count        = {preview.active_mapping_count}",
        f"eligible_substitution_count = {preview.eligible_substitution_count}",
        "",
        "occurrences (occurrence_ordinal ASC):",
    ]
    for occ in preview.occurrences:
        lines.append(
            f"  [{occ.occurrence_ordinal}] host={occ.original_host} "
            f"identity={_fp(occ.occurrence_identity_hash)}"
        )
        lines.append(f"      original_href = {occ.original_href}")
        lines.append(
            f"      mapped={occ.mapped} mapping_id={occ.mapping_id} "
            f"mapping_status={occ.mapping_status}"
        )
        lines.append(
            f"      target_id={occ.target_id} target_status={occ.target_status} "
            f"token_fingerprint={_fp(occ.target_token_fingerprint)}"
        )
        lines.append(f"      eligible={occ.eligible} reason={occ.reason} ({occ.reason_text})")
    lines.append("")
    lines.append(
        "dry-run: no DB write, no WordPress request, no projection push, "
        "no mapping/artifact created."
    )
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="preview_article_link_occurrences",
        description=(
            "1 Article の外部リンク occurrence を discover し、mapping/target "
            "eligibility を preview する (read-only, PLAN/PREVIEW only)"
        ),
    )
    parser.add_argument("--article-id", type=int, required=True, metavar="ID")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        # SessionLocal は default 引数ではなく **呼び出し時** に解決する
        # (default に束縛すると test の差し替えが効かず実 DB を見てしまう)。
        preview = run_preview(article_id=args.article_id, session_factory=SessionLocal)
    except EntityNotFoundError as exc:
        print(f"NOT FOUND: {exc}")
        return EXIT_NOT_FOUND
    except Exception as exc:  # noqa: BLE001 - 管理用 CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}: {exc}")
        return EXIT_UNEXPECTED

    print(format_preview(preview))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
