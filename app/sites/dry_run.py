"""2 つ目のサイトのプロファイルを立ち上げて試す (N6。手元だけ)。

- ``bootstrap(profile)``: そのサイトの DB を作り、Alembic で head まで上げる (本番の DB には
  触れない。別の process に DATABASE_URL を渡す)。
- ``dry_run(profile)``: そのサイトの Settings と DB で、読むだけの点検と計画を動かす。
  **ネットワークへの接続はすべて拒む** (試みがあれば失敗として記録する)。本番の設定
  (``app/config/*.json``) の hash が前後で変わらないこと、DB が本番と別であることを確かめる。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.sites.profile import SiteProfile, site_settings


class NetworkBlocked(RuntimeError):
    pass


@contextlib.contextmanager
def network_guard(attempts: list[str]):
    """この間の接続の試みをすべて拒み、記録する。"""

    original_connect = socket.socket.connect
    original_create = socket.create_connection

    def refuse_connect(self, address, *args, **kwargs):
        attempts.append(f"socket.connect {address!r}")
        raise NetworkBlocked(f"network is blocked in a site dry-run ({address!r})")

    def refuse_create(address, *args, **kwargs):
        attempts.append(f"create_connection {address!r}")
        raise NetworkBlocked(f"network is blocked in a site dry-run ({address!r})")

    socket.socket.connect = refuse_connect  # type: ignore[method-assign]
    socket.create_connection = refuse_create  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.create_connection = original_create  # type: ignore[assignment]


def config_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "app/config").glob("*.json")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def bootstrap(profile: SiteProfile, *, runner: Callable | None = None) -> dict:
    profile.database_path.parent.mkdir(parents=True, exist_ok=True)
    profile.logs_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DATABASE_URL": profile.database_url}
    command = [sys.executable, "-m", "alembic", "upgrade", "head"]
    run = runner or (lambda cmd, env: subprocess.run(cmd, cwd=profile.root, env=env,
                                                     capture_output=True, text=True))
    done = run(command, env)
    return {"database": str(profile.database_path), "returncode": done.returncode,
            "output": (done.stdout + done.stderr).strip().splitlines()[-3:]}


def _schema(session) -> dict:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    heads = set(ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_heads())
    current = set(MigrationContext.configure(session.connection()).get_current_heads())
    return {"current": sorted(current), "heads": sorted(heads), "at_head": current == heads}


def dry_run(profile: SiteProfile, *, now: datetime | None = None) -> dict:
    from app.services.content_intelligence_service import ContentIntelligenceService
    from app.services.discovery_promotion_service import DiscoveryPromotionService
    from app.services.google_ads_refresh_service import GoogleAdsRefreshService
    from app.services.manual_metrics_service import ManualMetricsService
    from app.services.nightly_analysis_service import NightlyAnalysisService
    from app.services.note_ledger_service import NoteLedgerService
    from app.services.site_growth_orchestrator_service import SiteGrowthOrchestratorService
    from app.services.system_health_service import SystemHealthService

    now = now or datetime.now(UTC)
    settings = site_settings(profile)
    before = config_fingerprint(profile.root)
    engine = create_engine(profile.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    attempts: list[str] = []
    steps: dict[str, dict] = {}

    def step(name: str, fn: Callable[[object], object]) -> None:
        with factory() as session:
            try:
                value = fn(session)
                steps[name] = {"ok": True, "summary": value}
            except Exception as exc:  # noqa: BLE001 - 手順ごとの結果を残す
                steps[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            finally:
                session.rollback()

    def health(session):
        result = SystemHealthService(
            session, settings=settings, worker_log=profile.logs_dir / "threads-worker.log",
            task_registered=lambda _name: profile.enabled("scheduler"),
            expect_worker=profile.enabled("resident_worker")).evaluate(now=now)
        return {"healthy": result["healthy"],
                "actionable": [f["check"] for f in result["actionable"]]}

    policy = profile.content_policy

    def intelligence(session):
        if policy is None:
            return ContentIntelligenceService(session, settings=settings)
        from app.content.clusters import load_registry

        return ContentIntelligenceService(
            session, settings=settings,
            registry=load_registry(policy["clusters"], policy["portfolio"]),
            seeds_path=policy["discovery_seeds"])

    def site_policies(_session):
        from app.sites.policies import load_all

        loaded = load_all(profile.policies)
        broken = {k: v["error"] for k, v in loaded.items() if not v["ok"]}
        if broken:
            raise ValueError(f"policy files do not load: {broken}")
        return {k: v["source"] for k, v in loaded.items()}

    with network_guard(attempts):
        step("schema", _schema)
        step("site_policies", site_policies)
        step("system_health", health)
        step("content_intelligence", lambda s: {
            "next_articles": len(intelligence(s).build(now=now).next_articles)})
        step("nightly_plan", lambda s: {
            k: v for k, v in NightlyAnalysisService(s, settings=settings,
                                                    intelligence=intelligence(s))
            .plan(now=now).get("counts", {}).items()
            if k in ("universe", "eligible", "analyzed")})
        step("google_ads_refresh_plan", lambda s: {
            "terms": GoogleAdsRefreshService(s, settings=settings).plan(now=now)["terms"]})
        step("discovery_plan", lambda s: {
            "tracked": DiscoveryPromotionService(s, settings=settings,
                                                 intelligence=intelligence(s))
            .plan(now=now)["tracked"]})
        step("growth_orchestrator", lambda s: {
            "stages": SiteGrowthOrchestratorService(s, settings=settings)
            .status(now=now)["stages"]})
        step("note_ledger", lambda s: NoteLedgerService(s).loop_status()["by_status"])
        step("manual_metrics", lambda s: {"entries": len(ManualMetricsService(s).active_rows())})
    engine.dispose()
    after = config_fingerprint(profile.root)
    ok = all(v["ok"] for v in steps.values()) and not attempts and before == after
    return {"site": profile.id, "database": str(profile.database_path),
            "content_policy": ("profile" if policy else
                               "inherited from app/config (this site's content policy)"),
            "capabilities": {k: profile.enabled(k) for k in profile.data["capabilities"]},
            "steps": steps, "network_attempts": attempts,
            "production_config_unchanged": before == after, "ok": ok}


__all__ = ["NetworkBlocked", "bootstrap", "config_fingerprint", "dry_run", "network_guard"]
