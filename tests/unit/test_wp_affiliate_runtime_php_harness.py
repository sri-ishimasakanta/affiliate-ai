"""実 PHP 実行検証 (php run.php)。

D-C0.1 で PHP CLI をローカルに用意した (winget PHP.PHP.8.2 / php.net 公式 zip)。
PHP が見つからない環境では skip する — 静的検証
(test_wp_affiliate_runtime_plugin_source.py) では代替できないため、
skip は「PHP 未実行 = pre-deployment BLOCKER 残存」を意味する。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HARNESS = (
    _ROOT
    / "wordpress"
    / "mu-plugins"
    / "bizfluxlab-affiliate-runtime"
    / "tests"
    / "run.php"
)


def _find_php() -> str | None:
    found = shutil.which("php")
    if found:
        return found
    # winget portable install location (Windows)
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        pkgs = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
        for exe in pkgs.glob("PHP.PHP*/php.exe"):
            return str(exe)
    return None


def test_php_harness_passes() -> None:
    php = _find_php()
    if php is None:
        pytest.skip(
            "no PHP CLI found; pre-deployment BLOCKER remains until "
            "`php wordpress/mu-plugins/bizfluxlab-affiliate-runtime/tests/run.php` "
            "runs green"
        )

    lint = subprocess.run(
        [php, "-l", str(_HARNESS)], capture_output=True, text=True, timeout=60
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr

    result = subprocess.run(
        [php, str(_HARNESS)], capture_output=True, text=True, timeout=120
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "0 failed" in out, out
    assert "FAIL" not in out, out


def test_php_lints_all_runtime_files() -> None:
    php = _find_php()
    if php is None:
        pytest.skip("no PHP CLI found")
    base = _ROOT / "wordpress" / "mu-plugins"
    for rel in (
        "bizfluxlab-affiliate-runtime.php",
        "bizfluxlab-affiliate-runtime/lib-core.php",
        "bizfluxlab-affiliate-runtime/tests/run.php",
    ):
        r = subprocess.run(
            [php, "-l", str(base / rel)], capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 0, f"{rel}: {r.stdout}{r.stderr}"
        assert "No syntax errors detected" in r.stdout
