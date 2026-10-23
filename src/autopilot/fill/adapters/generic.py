"""Generic adapter: label-based field discovery on any page. Base class for the ATS adapters."""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page


class Adapter:
    name = "generic"
    can_fill = True
    upload_first = False
    open_only_note = ""

    def form_url(self, job) -> str:
        return job["form_url"] or job["apply_url"]

    def prepare(self, page: Page) -> None:
        """Wait for the form to render."""
        try:
            page.wait_for_selector("input:not([type=hidden]), textarea, select", timeout=15000)
        except PlaywrightError:
            pass
        page.wait_for_timeout(800)


class OpenOnly(Adapter):
    """Portals that need accounts or ban automation: open the tab, fill nothing."""

    can_fill = False

    def __init__(self, name: str, note: str):
        self.name = name
        self.open_only_note = note
