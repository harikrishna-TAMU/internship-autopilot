"""Turn a fetched job page into a structured job record with Claude."""

from __future__ import annotations

from datetime import date

from autopilot.llm.claude import LLM
from autopilot.models import JobExtraction

MAX_PAGE_CHARS = 60_000


def extract_job(
    llm: LLM, url: str, page_text: str, note: str | None = None, today: date | None = None
) -> JobExtraction:
    today = today or date.today()
    text = page_text if len(page_text) <= MAX_PAGE_CHARS else page_text[:MAX_PAGE_CHARS] + "\n[page truncated]"
    user = f"<today>{today.isoformat()}</today>\n<url>{url}</url>\n<note>{note or ''}</note>\n<page>\n{text}\n</page>"
    return llm.ask("extract", JobExtraction, user)
