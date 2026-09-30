"""サイトのプロファイル (N6 System Productization)。

今の本番は 1 つのサイトで、設定は ``.env`` と ``app/config/*.json`` から読む (プロファイル無し)。
プロファイル無しの動きは **変えない**。プロファイルは 2 つ目以降のサイトを、コードを変えずに
手元で立ち上げて試すための入口:

- ``sites/<id>/site.json`` (``site-profile/1``): id・表示名・自分の DB (SQLite の場所)・
  タイムゾーン・記録の置き場・**使える外の連携 (capabilities)**・秘密の置き場 (そのサイトの
  env ファイルの場所だけ。値は書かない)。
- 秘密の境界: プロファイルに秘密らしい値を書けない。env ファイルは本番の ``.env`` を指せない。
- データの分離: サイトごとに 1 つの DB。本番の DB と同じ場所は拒む。
- capabilities が false の連携は、その設定の値をすべて空にする (fail-closed。env や .env に
  値があっても使わない)。秘密らしい設定の項目は、必ずどれかの capability に属する (契約の
  テストで確かめる)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "site-profile/1"
SITES_DIR = Path("sites")
CAPABILITY_FIELDS: dict[str, dict[str, object]] = {
    "wordpress": {"wordpress_base_url": None, "wordpress_username": None,
                  "wordpress_app_password": None},
    "search_console": {"search_console_property_uri": None,
                       "search_console_credentials_file": None},
    "ga4": {"ga4_property_id": None},
    "google_ads": {"google_ads_developer_token": None, "google_ads_client_id": None,
                   "google_ads_client_secret": None, "google_ads_refresh_token": None,
                   "google_ads_customer_id": None, "google_ads_login_customer_id": None},
    "threads": {"threads_enabled": False, "threads_user_id": None,
                "threads_access_token": None},
    "openai": {"openai_api_key": None, "threads_generation_provider": None},
    "email": {"operations_email_enabled": False, "operations_email_smtp_host": None,
              "operations_email_username": None, "operations_email_password": None,
              "operations_email_from": None, "operations_email_to": None},
    "webhook": {"operations_webhook_url": None},
    "make": {"make_api_base_url": None, "make_api_token": None},
    "approval_relay": {"approval_relay_shared_secret": None,
                       "affiliate_runtime_shared_secret": None,
                       "affiliate_probe_state_file": None},
}  # fmt: skip
#: 外に書き込むか、常駐する仕組み (今の核では手元の試しに使わない)。
RUNTIME_CAPABILITIES = ("scheduler", "resident_worker")
CAPABILITIES = (*CAPABILITY_FIELDS, *RUNTIME_CAPABILITIES)
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
_SECRETISH_KEY = re.compile(r"(password|secret|token|api_key|credential)", re.I)
_SECRETISH_VALUE = re.compile(r"(sk-[A-Za-z0-9]{8,}|Bearer\s|[A-Za-z0-9+/]{40,}={0,2})")


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class SiteProfile:
    root: Path
    path: Path
    data: dict

    @property
    def id(self) -> str:
        return self.data["id"]

    def resolve(self, rel: str) -> Path:
        path = Path(rel)
        return (path if path.is_absolute() else self.root / path).resolve()

    @property
    def database_path(self) -> Path:
        return self.resolve(self.data["database"]["sqlite_path"])

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def logs_dir(self) -> Path:
        return self.resolve(self.data["paths"]["logs"])

    @property
    def env_file(self) -> Path | None:
        rel = (self.data.get("secrets") or {}).get("env_file")
        return self.resolve(rel) if rel else None

    def enabled(self, capability: str) -> bool:
        return bool((self.data.get("capabilities") or {}).get(capability))


def _walk(value, key=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v, key)
    else:
        yield key, value


def validate(data: dict, *, root: Path, production_database: Path | None) -> list[str]:
    errors = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if not _ID.match(str(data.get("id") or "")):
        errors.append("id must be lowercase-hyphen (3-41 chars)")
    for key in ("display_name", "timezone"):
        if not str(data.get(key) or "").strip():
            errors.append(f"{key} is required")
    if not (data.get("database") or {}).get("sqlite_path"):
        errors.append("database.sqlite_path is required (one database per site)")
    if not (data.get("paths") or {}).get("logs"):
        errors.append("paths.logs is required")
    caps = data.get("capabilities") or {}
    unknown = sorted(set(caps) - set(CAPABILITIES))
    if unknown:
        errors.append(f"unknown capabilities {unknown}")
    missing = sorted(set(CAPABILITIES) - set(caps))
    if missing:
        errors.append(f"every capability must be stated explicitly; missing {missing}")
    for key, value in _walk(data):
        if key == "env_file":
            continue
        secretish_value = isinstance(value, str) and _SECRETISH_VALUE.search(value)
        if _SECRETISH_KEY.search(key) or secretish_value:
            errors.append(f"profile must not contain secrets ({key!r}); use the site's env file")
    if errors:
        return errors
    profile = SiteProfile(root=root, path=root, data=data)
    if production_database is not None and profile.database_path == production_database.resolve():
        errors.append("the profile database is the production database (data isolation)")
    env = profile.env_file
    if env is not None and env == (root / ".env").resolve():
        errors.append("the profile env file is the production .env (secret boundary)")
    return errors


def production_database_path(root: Path) -> Path | None:
    """本番の DB の場所 (``.env`` の DATABASE_URL から。値の中身は外に出さない)。"""

    from app.config.settings import get_settings

    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        return None
    rel = Path(url.removeprefix("sqlite:///"))
    return (rel if rel.is_absolute() else root / rel).resolve()


def load_profile(path: Path, *, root: Path, production_database: Path | None = None) -> SiteProfile:
    if not path.exists():
        raise ProfileError(f"no profile at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = validate(data, root=root, production_database=production_database)
    if errors:
        raise ProfileError("; ".join(errors))
    return SiteProfile(root=root, path=path, data=data)


def site_settings(profile: SiteProfile):
    """このサイトの Settings。無効な連携の値はすべて空 (fail-closed)。本番の .env は読まない。"""

    from app.config.settings import Settings

    overrides: dict[str, object] = {"database_url": profile.database_url}
    for capability, fields in CAPABILITY_FIELDS.items():
        if not profile.enabled(capability):
            overrides.update(fields)
    env = profile.env_file
    return Settings(_env_file=str(env) if env else None, **overrides)


__all__ = ["CAPABILITIES", "CAPABILITY_FIELDS", "ProfileError", "RUNTIME_CAPABILITIES",
           "SCHEMA", "SITES_DIR", "SiteProfile", "load_profile", "production_database_path",
           "site_settings", "validate"]
