"""Read a story screenshot with Claude vision."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from autopilot.llm.claude import LLM
from autopilot.models import ScreenshotExtraction


def read_screenshot(llm: LLM, path: Path, note: str | None = None, today: date | None = None) -> ScreenshotExtraction:
    today = today or date.today()
    user = f"<today>{today.isoformat()}</today>\n<note>{note or ''}</note>\nRead the attached screenshot."
    return llm.ask("screenshot", ScreenshotExtraction, user, images=[path])
