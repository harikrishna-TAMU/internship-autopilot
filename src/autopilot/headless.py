"""A lazily started headless Chrome, shared by JD rendering (JS-heavy pages) and resume PDF rendering."""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


class BrowserUnavailable(RuntimeError):
    pass


def launch_chromium(pw, channel: str | None, headless: bool):
    """Launch Chrome via `channel`, falling back to Playwright's bundled Chromium."""
    try:
        return pw.chromium.launch(channel=channel or None, headless=headless)
    except PlaywrightError as first:
        try:
            return pw.chromium.launch(headless=headless)
        except PlaywrightError:
            raise BrowserUnavailable(
                "Couldn't start Chrome. Install Google Chrome, or run `uv run playwright install chromium`."
            ) from first


class HeadlessBrowser:
    """Pass `pw` to share one Playwright instance (the sync API allows only one per thread)."""

    def __init__(self, channel: str | None = "chrome", pw=None):
        self.channel = channel
        self._pw = pw
        self._own_pw = pw is None
        self._browser = None

    def _ensure(self):
        if self._browser is None:
            if self._pw is None:
                self._pw = sync_playwright().start()
            self._browser = launch_chromium(self._pw, self.channel, headless=True)
        return self._browser

    def render(self, url: str, wait_ms: int = 2500) -> tuple[str, str, str]:
        """Load a page with JavaScript. Returns (final_url, html, title)."""
        page = self._ensure().new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightError:
                pass
            page.wait_for_timeout(wait_ms)
            return page.url, page.content(), page.title()
        finally:
            page.close()

    def pdf(self, html: str) -> bytes:
        page = self._ensure().new_page()
        try:
            page.set_content(html, wait_until="load")
            return page.pdf(format="Letter", print_background=True, prefer_css_page_size=True)
        finally:
            page.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._own_pw and self._pw is not None:
            self._pw.stop()
            self._pw = None
