"""Article の外部入出力用スキーマ。

SQLAlchemy モデルを直接 API 入出力に使わないための境界。
モデル属性 ``body`` / ``wordpress_post_id`` はここでは
``draft_content`` / ``wordpress_id`` として公開する (対応付けは Service 層で行う)。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ArticleStatus


class ArticleCreate(BaseModel):
    """記事新規登録の入力。"""

    keyword_id: int | None = None
    title: str = Field(min_length=1, max_length=512)
    slug: str = Field(min_length=1, max_length=255)


class ArticleUpdate(BaseModel):
    """記事部分更新の入力。

    未指定のフィールドは変更しない (``model_dump(exclude_unset=True)`` を利用)。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=512)
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    draft_content: str | None = None


class ArticleStatusUpdate(BaseModel):
    """status 変更専用の入力。

    Enum を用いるため、存在しない status 文字列は validation error (422) になる。
    """

    model_config = ConfigDict(extra="forbid")

    status: ArticleStatus


class ArticleRead(BaseModel):
    """記事の出力表現。"""

    id: int
    keyword_id: int | None
    title: str
    slug: str
    status: ArticleStatus
    draft_content: str | None
    published_url: str | None
    wordpress_id: int | None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
