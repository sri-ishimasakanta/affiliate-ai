"""migration 3d5382e2a6bd (Threads manual-post coexistence) の upgrade / downgrade。

**合成の小さな DB だけを使う** (一時ファイル)。本番の DB には触れない。本番の写しでの予行は
人の承認の前に別に行い、結果は docs/operations/threads-manual-posts.md に残す。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = "a4a74a5bcb8b"
REVISION = "3d5382e2a6bd"


def _alembic(db: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": "sqlite:///" + db.as_posix()}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300)  # fmt: skip


def _proposal(c, pid, *, angle="insight", article=1, guidance=None):
    c.execute(
        "INSERT INTO threads_post_proposals (id, source_article_id, source_article_body_hash, "
        "angle, link_mode, content_text, character_count, content_seed, proposal_hash, "
        "policy_version, generator_version, status, learning_guidance_json) VALUES "
        "(?, ?, 'h', ?, 'none', ?, 5, ?, ?, 't', 'g', 'approved', ?)",
        (pid, article, angle, f"text {pid}", f"s{pid}", f"P{pid}", guidance))


def _publication(c, pub_id, proposal_id, *, media, status="published", article=1,
                 angle="insight"):  # fmt: skip
    c.execute(
        "INSERT INTO threads_publications (id, proposal_id, proposal_hash, source_article_id, "
        "angle, exact_published_text, threads_media_id, permalink, status, trigger, "
        "reconciliation_required, retry_count, published_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, 'automatic', 0, 0, '2026-09-30 01:00:00')",
        (pub_id, proposal_id, f"P{proposal_id}", article, angle, f"text {proposal_id}", media,
         f"https://www.threads.net/@x/post/{media}" if media else None, status))


def _snapshot(c, pub_id, media, observed):
    c.execute("INSERT INTO threads_insight_snapshots (threads_publication_id, threads_media_id, "
              "observed_at, views, outcome) VALUES (?, ?, ?, 10, 'observed')",
              (pub_id, media, observed))


@pytest.fixture
def old_db(tmp_path) -> Path:
    db = tmp_path / "old.db"
    result = _alembic(db, "upgrade", PREVIOUS)
    assert result.returncode == 0, result.stderr[-2000:]
    c = sqlite3.connect(db)
    c.execute("INSERT INTO articles (id, title, slug, status) VALUES (1, 't', 's', 'published')")
    _proposal(c, 1)
    _proposal(c, 2, angle="account_growth", article=None,
              guidance='{"content_kind": "account_growth", "growth": {"date_jst": "2026-09-30"}}')
    _proposal(c, 3)
    _publication(c, 10, 1, media="m-normal")
    _publication(c, 11, 2, media="m-growth", article=None, angle="account_growth")
    _publication(c, 12, 3, media=None, status="failed")
    _snapshot(c, 10, "m-normal", "2026-09-30 02:00:00")
    _snapshot(c, 10, "m-normal", "2026-09-30 03:00:00")
    _snapshot(c, 11, "m-growth", "2026-09-30 02:00:00")
    c.commit()
    c.close()
    return db


def test_upgrade_backfills_system_posts_and_preserves_publications_and_insights(old_db) -> None:
    c = sqlite3.connect(old_db)
    before_pubs = c.execute("SELECT * FROM threads_publications ORDER BY id").fetchall()
    before_snaps = c.execute("SELECT id, threads_publication_id, threads_media_id, observed_at, "
                             "views FROM threads_insight_snapshots ORDER BY id").fetchall()
    c.close()
    result = _alembic(old_db, "upgrade", "head")
    assert result.returncode == 0, result.stderr[-2000:]
    c = sqlite3.connect(old_db)
    assert c.execute("SELECT version_num FROM alembic_version").fetchall() == [(REVISION,)]
    # 公開の記録と観測は 1 行も変わらない (本数・枠の数え方の正はそのまま)
    assert c.execute("SELECT * FROM threads_publications ORDER BY id").fetchall() == before_pubs
    assert c.execute("SELECT id, threads_publication_id, threads_media_id, observed_at, views "
                     "FROM threads_insight_snapshots ORDER BY id").fetchall() == before_snaps
    posts = c.execute("SELECT threads_media_id, origin, origin_evidence, threads_publication_id, "
                      "post_kind FROM threads_account_posts ORDER BY id").fetchall()
    # published だけが system になる (failed の公開は system の証拠にしない)。架空の行は無い
    assert posts == [("m-normal", "system", "publication_record", 10, "normal"),
                     ("m-growth", "system", "publication_record", 11, "growth")]
    linked = c.execute("SELECT s.threads_media_id = a.threads_media_id AND "
                       "s.threads_publication_id = a.threads_publication_id "
                       "FROM threads_insight_snapshots s JOIN threads_account_posts a "
                       "ON a.id = s.threads_account_post_id").fetchall()
    assert linked == [(1,), (1,), (1,)]
    assert c.execute("SELECT event_type, actor FROM threads_account_post_events").fetchall() == [
        ("reconciled_system", "system:migration_backfill")] * 2
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    c.close()


def test_the_new_constraints_hold_after_upgrade(old_db) -> None:
    assert _alembic(old_db, "upgrade", "head").returncode == 0
    c = sqlite3.connect(old_db)
    with pytest.raises(sqlite3.IntegrityError):  # 同じ投稿 ID は 1 行だけ
        c.execute("INSERT INTO threads_account_posts (threads_media_id, origin, origin_evidence, "
                  "post_kind, first_seen_at, last_seen_at) VALUES ('m-normal', 'manual', "
                  "'no_publication_record', 'unknown', '2026-10-01', '2026-10-01')")
    with pytest.raises(sqlite3.IntegrityError):  # manual は公開の記録を持たない
        c.execute("INSERT INTO threads_account_posts (threads_media_id, origin, origin_evidence, "
                  "threads_publication_id, post_kind, first_seen_at, last_seen_at) VALUES "
                  "('m-x', 'manual', 'no_publication_record', 12, 'unknown', '2026-10-01', "
                  "'2026-10-01')")
    with pytest.raises(sqlite3.IntegrityError):  # 観測は自アカウントの投稿を必ず持つ
        c.execute("INSERT INTO threads_insight_snapshots (threads_publication_id, "
                  "threads_media_id, observed_at, outcome) VALUES "
                  "(10, 'm-normal', '2026-10-01', 'observed')")
    c.close()


@pytest.mark.parametrize("breakage", ["published_without_media", "snapshot_media_mismatch"])
def test_unresolvable_identity_stops_the_migration_before_any_change(old_db, breakage) -> None:
    c = sqlite3.connect(old_db)
    if breakage == "published_without_media":
        _proposal(c, 4)
        _publication(c, 13, 4, media=None)
    else:
        c.execute("UPDATE threads_insight_snapshots SET threads_media_id = 'other' WHERE id = 1")
    c.commit()
    c.close()
    result = _alembic(old_db, "upgrade", "head")
    assert result.returncode != 0 and "refusing to migrate" in result.stderr
    c = sqlite3.connect(old_db)
    assert c.execute("SELECT version_num FROM alembic_version").fetchall() == [(PREVIOUS,)]
    # 何も作らないうちに止まる (SQLite の DDL は戻せないので、確かめてから作る)
    assert c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'threads_account%'"
                     ).fetchall() == []
    c.close()


def test_downgrade_restores_the_previous_schema_and_refuses_to_lose_manual_posts(old_db) -> None:
    assert _alembic(old_db, "upgrade", "head").returncode == 0
    c = sqlite3.connect(old_db)
    c.execute("INSERT INTO threads_account_posts (threads_media_id, origin, origin_evidence, "
              "post_kind, first_seen_at, last_seen_at) VALUES ('m-manual', 'manual', "
              "'no_publication_record', 'unknown', '2026-10-01', '2026-10-01')")
    c.commit()
    c.close()
    refused = _alembic(old_db, "downgrade", PREVIOUS)
    assert refused.returncode != 0 and "refusing to downgrade" in refused.stderr
    c = sqlite3.connect(old_db)
    c.execute("DELETE FROM threads_account_posts WHERE origin = 'manual'")
    c.commit()
    c.close()
    assert _alembic(old_db, "downgrade", PREVIOUS).returncode == 0
    c = sqlite3.connect(old_db)
    assert c.execute("SELECT version_num FROM alembic_version").fetchall() == [(PREVIOUS,)]
    assert c.execute("SELECT COUNT(*) FROM threads_insight_snapshots").fetchall() == [(3,)]
    assert c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'threads_account%'"
                     ).fetchall() == []
    c.close()
