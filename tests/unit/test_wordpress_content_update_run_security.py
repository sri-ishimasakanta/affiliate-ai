"""D-D5B security/leak audit: WordPressContentUpdateRun 周りの新規コードが
credential / secret 相当の値を一切扱わないことを確認する (§30)。
"""

from __future__ import annotations

from pathlib import Path

from app.models.wordpress_content_update_run import WordPressContentUpdateRun

_FORBIDDEN_COLUMN_NAMES = frozenset(
    {
        "username",
        "password",
        "application_password",
        "app_password",
        "authorization",
        "auth_header",
        "shared_secret",
        "secret",
        "signature",
        "hmac",
        "access_token",
        "refresh_token",
        "api_key",
    }
)

_SOURCE_FILES = (
    Path("app/models/wordpress_content_update_run.py"),
    Path("app/repositories/wordpress_content_update_run_repository.py"),
    Path("app/wordpress/content_update_request.py"),
)

_FORBIDDEN_SOURCE_TOKENS = (
    "Authorization",
    "application_password",
    "app_password",
    "shared_secret",
    ".env",
    "getenv(",
    "os.environ",
)


def test_model_columns_exclude_credential_and_secret_fields() -> None:
    cols = {c.name for c in WordPressContentUpdateRun.__table__.columns}
    assert cols.isdisjoint(_FORBIDDEN_COLUMN_NAMES)


def test_source_files_never_reference_credential_material() -> None:
    for path in _SOURCE_FILES:
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN_SOURCE_TOKENS:
            assert token not in text, f"{path}: forbidden token {token!r} found"


def test_response_snapshot_column_is_generic_json_not_a_secret_field() -> None:
    col = WordPressContentUpdateRun.__table__.columns["response_snapshot"]
    assert col.nullable is True
    # JSON 型であること (safe snapshot 用途。credential を強制する型制約は無いが、
    # 呼び出し側 (将来の execution service) が id/status/modified_gmt/link のみを
    # 詰める契約は D-D5A.1 §20 のドキュメント上の契約として model docstring に
    # 記載済み -- この test はカラムが汎用 JSON であることのみを pin する)。
    assert col.type.__class__.__name__ == "JSON"


def test_no_relationship_leaks_credential_bearing_target_config() -> None:
    # このモデルは Article / ArticlePublicationArtifact への FK のみを持ち、
    # WordPress 認証情報を保持する設定オブジェクトへの参照は一切持たない。
    fk_targets = {fk.column.table.name for fk in WordPressContentUpdateRun.__table__.foreign_keys}
    assert fk_targets == {"articles", "article_publication_artifacts"}
