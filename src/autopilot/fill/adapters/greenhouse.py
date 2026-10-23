from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from autopilot.fill.adapters.generic import Adapter


class GreenhouseAdapter(Adapter):
    """Greenhouse: the embed URL (set at extract time) is a standalone application form."""

    name = "greenhouse"

    def prepare(self, page: Page) -> None:
        # Company-hosted pages embed the form in an iframe; open the form itself instead.
        try:
            frame = page.locator('iframe[src*="greenhouse.io"]').first
            if frame.count():
                src = frame.get_attribute("src")
                if src:
                    page.goto(src, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightError:
            pass
        super().prepare(page)
