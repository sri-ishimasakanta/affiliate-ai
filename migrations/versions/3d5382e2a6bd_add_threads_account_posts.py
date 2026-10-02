"""add threads account posts

Revision ID: 3d5382e2a6bd
Revises: a4a74a5bcb8b
Create Date: 2026-10-02 12:57:31.643746

Threads manual-post coexistence (human-approved design, 2026-10-02). Additive:

1. ``threads_account_posts``: one row per post of our own Threads account, keyed by the Threads
   post id, with its origin (system / manual / unknown). System rows point at their
   ``threads_publications`` row; no proposal is invented.
2. ``threads_account_post_events``: append-only history (discovered, reconciled_system,
   origin_changed, classification_changed, text_changed, missing_from_listing, reappeared,
   metrics_unavailable, proposal_superseded).
3. ``threads_insight_snapshots``: every snapshot now belongs to an account post
   (``threads_account_post_id`` NOT NULL); ``threads_publication_id`` becomes nullable (manual
   posts have none). Same account post + observed_at is unique.

Backfill: every **published** publication becomes a system account post with its existing
Threads post id; every existing snapshot gets its account post. The migration **fails** instead
of guessing when an identity cannot be resolved safely (a published publication without a post
id, a snapshot whose post id differs from its publication, or a snapshot on a publication that
is not published). Publication counts and quotas are not touched (they keep reading
``threads_publications``).

Downgrade refuses when manual / unknown posts or publication-less snapshots exist (they would
be lost).
"""
import json
from datetime import UTC, datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d5382e2a6bd'
down_revision: Union[str, Sequence[str], None] = 'a4a74a5bcb8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GROWTH_KIND = "account_growth"


def _backfill_rows(bind) -> list[dict]:
    """published の公開ごとに system の行を作る材料。安全に決められなければ止める。"""

    rows = bind.execute(sa.text(
        "SELECT p.id, p.status, p.threads_media_id, p.permalink, p.published_at, "
        "p.exact_published_text, p.source_article_id, p.angle, pr.learning_guidance_json "
        "FROM threads_publications p JOIN threads_post_proposals pr ON pr.id = p.proposal_id "
        "ORDER BY p.id")).mappings().all()
    snapshot_pubs = {r[0]: r[1] for r in bind.execute(sa.text(
        "SELECT threads_publication_id, COUNT(*) FROM threads_insight_snapshots "
        "GROUP BY threads_publication_id")).all()}
    mismatched = bind.execute(sa.text(
        "SELECT COUNT(*) FROM threads_insight_snapshots s JOIN threads_publications p "
        "ON p.id = s.threads_publication_id "
        "WHERE p.threads_media_id IS NULL OR s.threads_media_id != p.threads_media_id")).scalar()
    if mismatched:
        raise RuntimeError(f"refusing to migrate: {mismatched} insight snapshot(s) carry a Threads "
                           "post id that differs from (or is missing on) their publication")
    out = []
    for row in rows:
        if row["status"] != "published":
            if snapshot_pubs.get(row["id"]):
                raise RuntimeError(f"refusing to migrate: publication {row['id']} is "
                                   f"{row['status']!r} but has insight snapshots")
            continue  # 公開を確かめていない行は system の証拠にしない
        if not row["threads_media_id"]:
            raise RuntimeError(f"refusing to migrate: published publication {row['id']} has no "
                               "Threads post id (identity cannot be resolved)")
        raw = row["learning_guidance_json"]
        guidance = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        # 実行時 (system_post_kind) と同じ規則。提案の印と公開の切り口が食い違えば unknown。
        marked = isinstance(guidance, dict) and guidance.get("content_kind") == _GROWTH_KIND
        if row["source_article_id"] is not None:
            kind = "normal"
        elif marked and row["angle"] == _GROWTH_KIND:
            kind = "growth"
        else:
            kind = "unknown"
        out.append({**row, "post_kind": kind})
    orphans = set(snapshot_pubs) - {r["id"] for r in out}
    if orphans:
        raise RuntimeError(f"refusing to migrate: snapshots reference publications {sorted(orphans)}"
                           " that are not resolvable")
    return out


def upgrade() -> None:
    """Upgrade schema."""
    # 同一性を先に確かめる (SQLite の DDL は途中で戻せない。止めるなら何も作る前に止める)。
    bind = op.get_bind()
    rows = _backfill_rows(bind)
    op.create_table('threads_account_posts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('threads_media_id', sa.String(length=64), nullable=False),
    sa.Column('permalink', sa.String(length=1024), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('origin', sa.String(length=16), nullable=False),
    sa.Column('origin_evidence', sa.String(length=32), nullable=False),
    sa.Column('threads_publication_id', sa.Integer(), nullable=True),
    sa.Column('post_kind', sa.String(length=16), nullable=False),
    sa.Column('media_type', sa.String(length=32), nullable=True),
    sa.Column('latest_text', sa.Text(), nullable=True),
    sa.Column('latest_text_hash', sa.String(length=64), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('missing_since', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("(origin = 'system' AND threads_publication_id IS NOT NULL) OR (origin != 'system' AND threads_publication_id IS NULL)", name=op.f('ck_threads_account_posts_threads_account_post_system_has_publication')),
    sa.CheckConstraint("origin IN ('system', 'manual', 'unknown')", name=op.f('ck_threads_account_posts_threads_account_post_origin')),
    sa.CheckConstraint("origin_evidence IN ('publication_record', 'no_publication_record', 'publication_in_flight')", name=op.f('ck_threads_account_posts_threads_account_post_evidence')),
    sa.CheckConstraint("post_kind IN ('normal', 'growth', 'unknown')", name=op.f('ck_threads_account_posts_threads_account_post_kind')),
    sa.ForeignKeyConstraint(['threads_publication_id'], ['threads_publications.id'], name=op.f('fk_threads_account_posts_threads_publication_id_threads_publications'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_threads_account_posts')),
    sa.UniqueConstraint('threads_media_id', name='uq_threads_account_posts_media'),
    sa.UniqueConstraint('threads_publication_id', name='uq_threads_account_posts_publication')
    )
    with op.batch_alter_table('threads_account_posts', schema=None) as batch_op:
        batch_op.create_index('ix_threads_account_posts_published_at', ['published_at'], unique=False)

    op.create_table('threads_account_post_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('threads_account_post_id', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('detail_json', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("event_type IN ('discovered', 'reconciled_system', 'origin_changed', 'classification_changed', 'text_changed', 'missing_from_listing', 'reappeared', 'metrics_unavailable', 'proposal_superseded')", name=op.f('ck_threads_account_post_events_threads_account_post_event_type')),
    sa.ForeignKeyConstraint(['threads_account_post_id'], ['threads_account_posts.id'], name=op.f('fk_threads_account_post_events_threads_account_post_id_threads_account_posts'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_threads_account_post_events'))
    )
    with op.batch_alter_table('threads_account_post_events', schema=None) as batch_op:
        batch_op.create_index('ix_threads_account_post_events_post', ['threads_account_post_id', 'occurred_at'], unique=False)

    # -- backfill: published publications -> system account posts ------------------------
    now = datetime.now(UTC).replace(tzinfo=None)  # 保存は UTC の naive (to_storage_utc と同じ)
    posts = sa.table('threads_account_posts', sa.column('threads_media_id', sa.String),
                     sa.column('permalink', sa.String), sa.column('published_at', sa.DateTime),
                     sa.column('origin', sa.String), sa.column('origin_evidence', sa.String),
                     sa.column('threads_publication_id', sa.Integer),
                     sa.column('post_kind', sa.String), sa.column('latest_text', sa.Text),
                     sa.column('latest_text_hash', sa.String),
                     sa.column('first_seen_at', sa.DateTime),
                     sa.column('last_seen_at', sa.DateTime))
    import hashlib

    if rows:
        op.bulk_insert(posts, [{
            "threads_media_id": r["threads_media_id"], "permalink": r["permalink"],
            "published_at": (datetime.fromisoformat(r["published_at"])
                             if isinstance(r["published_at"], str) else r["published_at"]),
            "origin": "system", "origin_evidence": "publication_record",
            "threads_publication_id": r["id"], "post_kind": r["post_kind"],
            "latest_text": r["exact_published_text"],
            "latest_text_hash": (hashlib.sha256(r["exact_published_text"].encode("utf-8"))
                                 .hexdigest() if r["exact_published_text"] else None),
            "first_seen_at": now, "last_seen_at": now} for r in rows])
    events = sa.table('threads_account_post_events',
                      sa.column('threads_account_post_id', sa.Integer),
                      sa.column('event_type', sa.String), sa.column('occurred_at', sa.DateTime),
                      sa.column('actor', sa.String), sa.column('detail_json', sa.JSON))
    created = bind.execute(sa.text(
        "SELECT id, threads_publication_id FROM threads_account_posts ORDER BY id")).all()
    if created:
        op.bulk_insert(events, [{
            "threads_account_post_id": pid, "event_type": "reconciled_system",
            "occurred_at": now, "actor": "system:migration_backfill",
            "detail_json": {"publication_id": pub, "source": "migration 3d5382e2a6bd"}}
            for pid, pub in created])

    # -- snapshots: account post reference (nullable -> filled -> NOT NULL) ----------------
    with op.batch_alter_table('threads_insight_snapshots', schema=None) as batch_op:
        batch_op.add_column(sa.Column('threads_account_post_id', sa.Integer(), nullable=True))
    bind.execute(sa.text(
        "UPDATE threads_insight_snapshots SET threads_account_post_id = ("
        "SELECT a.id FROM threads_account_posts a "
        "WHERE a.threads_publication_id = threads_insight_snapshots.threads_publication_id)"))
    unlinked = bind.execute(sa.text(
        "SELECT COUNT(*) FROM threads_insight_snapshots WHERE threads_account_post_id IS NULL"
    )).scalar()
    if unlinked:
        raise RuntimeError(f"refusing to migrate: {unlinked} insight snapshot(s) could not be "
                           "linked to an account post")
    with op.batch_alter_table('threads_insight_snapshots', schema=None) as batch_op:
        batch_op.alter_column('threads_account_post_id', existing_type=sa.Integer(),
                              nullable=False)
        batch_op.alter_column('threads_publication_id',
               existing_type=sa.INTEGER(),
               nullable=True)
        batch_op.create_index('ix_threads_insight_snapshots_account_post', ['threads_account_post_id'], unique=False)
        batch_op.create_unique_constraint('uq_threads_insight_account_post_observation', ['threads_account_post_id', 'observed_at'])
        batch_op.create_foreign_key(batch_op.f('fk_threads_insight_snapshots_threads_account_post_id_threads_account_posts'), 'threads_account_posts', ['threads_account_post_id'], ['id'], ondelete='RESTRICT')


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    non_system = bind.execute(sa.text(
        "SELECT COUNT(*) FROM threads_account_posts WHERE origin != 'system'")).scalar()
    unlinked = bind.execute(sa.text(
        "SELECT COUNT(*) FROM threads_insight_snapshots WHERE threads_publication_id IS NULL"
    )).scalar()
    if non_system or unlinked:
        raise RuntimeError(f"refusing to downgrade: {non_system} manual/unknown account post(s) "
                           f"and {unlinked} publication-less snapshot(s) would be lost")
    with op.batch_alter_table('threads_insight_snapshots', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_threads_insight_snapshots_threads_account_post_id_threads_account_posts'), type_='foreignkey')
        batch_op.drop_constraint('uq_threads_insight_account_post_observation', type_='unique')
        batch_op.drop_index('ix_threads_insight_snapshots_account_post')
        batch_op.alter_column('threads_publication_id',
               existing_type=sa.INTEGER(),
               nullable=False)
        batch_op.drop_column('threads_account_post_id')

    with op.batch_alter_table('threads_account_post_events', schema=None) as batch_op:
        batch_op.drop_index('ix_threads_account_post_events_post')

    op.drop_table('threads_account_post_events')
    with op.batch_alter_table('threads_account_posts', schema=None) as batch_op:
        batch_op.drop_index('ix_threads_account_posts_published_at')

    op.drop_table('threads_account_posts')
