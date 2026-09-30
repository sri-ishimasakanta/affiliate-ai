"""N6: サイトのプロファイル (2 つ目のサイトを、コードを変えずに手元で立ち上げて試す)。

pin する契約:

- プロファイルは秘密を持てない・本番の DB と同じ場所を使えない・本番の .env を指せない・
  連携はすべて明示する。
- 無効な連携の設定の値は、環境変数に値があっても空 (fail-closed)。秘密らしい設定の項目は
  すべてどれかの連携に属する。
- 2 つ目のサイト (``sites/example-local``) は立ち上げ (自分の DB を head へ) と試し
  (ネットワーク無し・本番の設定は不変) が通る。プロファイル無しの本番の設定は変わらない。
"""

from __future__ import annotations

import json
import re
import shutil
import socket
from pathlib import Path

import pytest

from app.config.settings import Settings, get_settings
from app.sites import profile as sp
from app.sites.dry_run import NetworkBlocked, bootstrap, dry_run, network_guard

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "sites/example-local/site.json"


def _data(**over) -> dict:
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return {**data, **over}


def test_the_example_profile_is_valid() -> None:
    errors = sp.validate(_data(), root=REPO, production_database=REPO / "affiliate_ai.db")
    assert errors == []


@pytest.mark.parametrize("over, match", [
    ({"wordpress_app_password": "abc"}, "secrets"),
    ({"notes": "token sk-abcdefghijklmnop"}, "secrets"),
    ({"database": {"sqlite_path": "affiliate_ai.db"}}, "production database"),
    ({"secrets": {"env_file": ".env"}}, "production .env"),
    ({"capabilities": {"wordpress": False}}, "stated explicitly"),
    ({"capabilities": {**_data()["capabilities"], "stripe": True}}, "unknown capabilities"),
    ({"id": "Bad Id"}, "lowercase"),
])
def test_profiles_keep_the_secret_and_data_boundaries(over, match) -> None:
    errors = sp.validate(_data(**over), root=REPO, production_database=REPO / "affiliate_ai.db")
    assert any(match in e for e in errors), errors


def test_disabled_capabilities_are_empty_even_if_the_environment_has_values(
        monkeypatch, tmp_path) -> None:  # fmt: skip
    monkeypatch.setenv("WORDPRESS_APP_PASSWORD", "from-env")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///production.db")
    data = _data(database={"sqlite_path": str(tmp_path / "site.db")})
    data["capabilities"] = {**data["capabilities"], "openai": True}
    profile = sp.SiteProfile(root=REPO, path=EXAMPLE, data=data)
    settings = sp.site_settings(profile)
    assert settings.wordpress_app_password is None and settings.wordpress_base_url is None
    assert settings.openai_api_key == "from-env"  # 有効な連携はそのサイトの env から
    assert settings.database_url.endswith("site.db")
    assert get_settings().database_url != settings.database_url  # 本番の設定は変わらない


def test_every_secret_like_setting_belongs_to_a_capability() -> None:
    covered = {f for fields in sp.CAPABILITY_FIELDS.values() for f in fields}
    secretish = re.compile(r"(password|secret|token|api_key|credentials|base_url|webhook|"
                           r"user_id|username|customer_id|property|client_id)")
    needs = {name for name in Settings.model_fields if secretish.search(name)}
    # 公開の API の場所と、プロファイルが必ず上書きする DB の場所だけ
    allowed_core = {"threads_api_base_url", "openai_api_base_url", "database_url"}
    assert needs - covered - allowed_core == set()


def test_the_network_guard_refuses_and_records() -> None:
    attempts: list[str] = []
    with network_guard(attempts), pytest.raises(NetworkBlocked):
        socket.create_connection(("example.com", 443), timeout=1)
    assert attempts and "example.com" in attempts[0]
    assert socket.create_connection is not None  # 元に戻る


def test_a_second_site_bootstraps_and_dry_runs_without_code_changes(tmp_path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(REPO / "app/config", root / "app/config")
    data = _data(database={"sqlite_path": str(tmp_path / "site2" / "affiliate_ai.db")},
                 paths={"logs": str(tmp_path / "site2" / "logs")})
    profile = sp.SiteProfile(root=REPO, path=EXAMPLE, data=data)
    result = bootstrap(profile)
    assert result["returncode"] == 0, result["output"]
    report = dry_run(profile)
    assert report["ok"], report["steps"]
    assert report["network_attempts"] == [] and report["production_config_unchanged"]
    assert report["steps"]["schema"]["summary"]["at_head"]
    assert report["steps"]["system_health"]["summary"]["healthy"]
    # そのサイトの内容の方針だけを見る (本番のクラスタ・種を継がない)
    assert report["content_policy"] == "profile"
    assert report["steps"]["content_intelligence"]["summary"]["next_articles"] == 1
    inherited = sp.SiteProfile(root=REPO, path=EXAMPLE, data={
        k: v for k, v in data.items() if k != "content_policy"})
    assert "inherited" in dry_run(inherited)["content_policy"]


@pytest.mark.parametrize("policy, match", [
    ({"clusters": "sites/example-local/policy/content_clusters.json"}, "needs exactly"),
    ({"clusters": "nope.json", "portfolio": "nope.json", "discovery_seeds": "nope.json"},
     "is missing"),
])
def test_content_policy_paths_are_checked(policy, match) -> None:
    errors = sp.validate(_data(content_policy=policy), root=REPO,
                         production_database=REPO / "affiliate_ai.db")
    assert any(match in e for e in errors), errors


def test_the_cli_refuses_an_invalid_profile(tmp_path, capsys) -> None:
    from scripts.site_profile import main

    bad = tmp_path / "site.json"
    bad.write_text(json.dumps(_data(database={"sqlite_path": "affiliate_ai.db"})),
                   encoding="utf-8")
    assert main(["validate", str(bad)], root=REPO,
                production_database=(REPO / "affiliate_ai.db").resolve()) == 2
    assert "production database" in capsys.readouterr().out


# == N6 hardening: per-site policies and log paths ===================================
def test_site_policies_override_only_what_the_profile_names(tmp_path) -> None:
    from app.sites.policies import load_all, policy_names

    loaded = load_all({"threads_growth_facts":
                       REPO / "sites/example-local/policy/threads_growth_facts.json"})
    assert set(loaded) == set(policy_names()) and all(v["ok"] for v in loaded.values())
    assert loaded["threads_growth_facts"]["source"] == "profile"
    assert {v["source"] for k, v in loaded.items() if k != "threads_growth_facts"} == {
        "inherited"}
    broken = tmp_path / "facts.json"
    broken.write_text('{"facts": [{"id": "x", "kind": "nope", "text": "t",'
                      ' "valid_from": "2026-01-01", "valid_until": "2026-12-31"}]}',
                      encoding="utf-8")
    assert load_all({"threads_growth_facts": broken})["threads_growth_facts"]["ok"] is False


@pytest.mark.parametrize("policies, match", [
    ({"stripe_prices": "x.json"}, "unknown policies"),
    ({"threads_style_policy": "missing.json"}, "is missing"),
])
def test_site_policy_paths_are_checked(policies, match) -> None:
    errors = sp.validate(_data(policies=policies), root=REPO,
                         production_database=REPO / "affiliate_ai.db")
    assert any(match in e for e in errors), errors


def test_log_defaults_follow_the_launcher_variable(monkeypatch, tmp_path) -> None:
    from app.operations.threads_worker_task import build_threads_worker_task_plan
    from app.project_state import runtime_records
    from app.sites import paths

    monkeypatch.delenv(paths.LOG_DIR_ENV, raising=False)
    assert paths.default_worker_log() == runtime_records.DEFAULT_WORKER_LOG  # 本番は変わらない
    plan = build_threads_worker_task_plan(project_root=REPO)
    assert plan.log_path == str(runtime_records.DEFAULT_WORKER_LOG)
    monkeypatch.setenv(paths.LOG_DIR_ENV, str(tmp_path))
    assert runtime_records.default_worker_log() == tmp_path / "threads-worker.log"
    assert build_threads_worker_task_plan(project_root=REPO).log_path == str(
        tmp_path / "threads-worker.log")
