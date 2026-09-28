"""読むだけのブラウザの操作 (T6.5B)。**押す・書く・送る操作は、この型に存在しない。**

使えるのは: 許可した Threads の URL を開く / 決まった量だけスクロールする / HTML を読む /
画面を保存する、だけ。いいね・返信・フォロー・再投稿・引用・DM・投稿・ログインの自動入力の
方法は作らない (型に無いので、呼び出す道も無い)。

Playwright は実際に観察するときだけ読み込む (試験では使わない)。ブラウザのプロファイルは
``data/threads-observer/browser-profile`` (git に入らない)。ログインは人が画面で行う。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from app.social.threads.observer import selectors as sel

DEFAULT_PROFILE_DIR = Path("data/threads-observer/browser-profile")
SCROLL_PIXELS = 2400


class ObserverError(RuntimeError):
    pass


class ReadOnlyPage(Protocol):
    """観察に使う面。**これ以外の操作は無い。**"""

    def goto(self, url: str) -> None: ...

    def scroll(self) -> None: ...

    def content(self) -> str: ...

    def screenshot(self, path: Path) -> None: ...


def check_url(url: str) -> str:
    """Threads の読む画面だけを許す (ほかの host・書く画面は開かない)。"""

    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in sel.ALLOWED_HOSTS:
        raise ObserverError(f"refusing to open a non-Threads URL: {parts.hostname}")
    lowered = parts.path.lower()
    if any(word in lowered for word in ("/intent", "/compose", "/login", "/logout", "/accounts")):
        raise ObserverError(f"refusing to open a non-read page: {parts.path}")
    return url


class PlaywrightPage:
    """Playwright の持続するプロファイルで開く、読むだけのページ。"""

    def __init__(self, *, profile_dir: Path = DEFAULT_PROFILE_DIR, headless: bool = True,
                 timeout_ms: int = 30_000) -> None:  # fmt: skip
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - パイロットの準備で入れる
            raise ObserverError(
                "Playwright is not installed; install it for the manual pilot "
                "(uv add playwright && uv run playwright install chromium)"
            ) from exc
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            str(profile_dir), headless=headless, locale="ja-JP", timezone_id="Asia/Tokyo"
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(timeout_ms)

    def goto(self, url: str) -> None:
        self._page.goto(check_url(url), wait_until="domcontentloaded")
        # 投稿は後から描かれる (パイロットで確認)。まとまりが出るまで待つ。出なくても止めない
        # (ログインの画面・形の違いの判定は parser が行う)。
        try:
            self._page.wait_for_selector(
                '[data-pressable-container="true"]', timeout=sel.RENDER_WAIT_MS
            )
        except Exception:  # noqa: BLE001 - 見つからないことも観察の結果
            pass
        self._page.wait_for_timeout(1500)

    def scroll(self) -> None:
        self._page.mouse.wheel(0, SCROLL_PIXELS)
        self._page.wait_for_timeout(1500)

    def content(self) -> str:
        return self._page.content()

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 監査のため、集めた投稿が画面と照らせるようにページ全体を撮る。
        self._page.screenshot(path=str(path), full_page=True)

    def wait_for_human(self) -> None:  # pragma: no cover - 人がログインする間だけ
        """人が画面でログインし、ブラウザを閉じるまで待つ (入力はしない)。"""

        self._page.goto(sel.BASE_URL)
        self._context.wait_for_event("close", timeout=0)

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._pw.stop()


__all__ = ["DEFAULT_PROFILE_DIR", "ObserverError", "PlaywrightPage", "ReadOnlyPage", "check_url"]
