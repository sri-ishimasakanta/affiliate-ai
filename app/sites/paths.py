"""記録 (ログ) の置き場の既定 (N6 hardening)。

起動の ``.cmd`` と同じ環境変数 ``AFFILIATE_AI_LOG_DIR`` を見て、無ければ本番の既定
``D:/Logs/affiliate-ai``。本番はこの変数を設定していないので、動きは変わらない。
サイトのプロファイルは ``paths.logs`` を使い、ここには頼らない。
"""

from __future__ import annotations

import os
from pathlib import Path

LOG_DIR_ENV = "AFFILIATE_AI_LOG_DIR"
DEFAULT_LOG_DIR = Path(r"D:\Logs\affiliate-ai")


def default_log_dir() -> Path:
    value = os.environ.get(LOG_DIR_ENV, "").strip()
    return Path(value) if value else DEFAULT_LOG_DIR


def default_worker_log() -> Path:
    return default_log_dir() / "threads-worker.log"


__all__ = ["DEFAULT_LOG_DIR", "LOG_DIR_ENV", "default_log_dir", "default_worker_log"]
