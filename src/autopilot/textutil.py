"""Small text helpers shared by ingest and extract."""

from __future__ import annotations

import re

from lxml import html as lhtml

BLOCK_TAGS = (
    "p",
    "div",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "section",
    "article",
    "header",
    "footer",
    "table",
)


def html_to_text(markup: str | None) -> str:
    """Readable plain text from an HTML document or fragment, keeping line breaks between blocks."""
    if not markup or not markup.strip():
        return ""
    try:
        doc = lhtml.fromstring(markup)
    except (ValueError, lhtml.etree.ParserError):
        return collapse(re.sub(r"<[^>]+>", " ", markup))
    for bad in doc.xpath("//script|//style|//noscript|//template"):
        bad.drop_tree()
    for br in doc.xpath("//br"):
        br.tail = "\n" + (br.tail or "")
    for el in doc.iter(*BLOCK_TAGS):
        if el.tag == "li":
            el.text = "- " + (el.text or "")
        el.tail = "\n" + (el.tail or "")
    return collapse(doc.text_content())


def hrefs(markup: str | None) -> list[str]:
    """Link targets in an HTML body (catches links hidden behind text)."""
    if not markup or not markup.strip():
        return []
    try:
        doc = lhtml.fromstring(markup)
    except (ValueError, lhtml.etree.ParserError):
        return []
    return [h.strip() for h in doc.xpath("//a/@href") if h and h.strip().lower().startswith(("http://", "https://"))]


def collapse(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def folder_name(value: str | None, fallback: str, max_len: int = 80) -> str:
    """A readable folder name ("Jane Street", "AT&T"): keeps spaces, drops characters macOS/Windows forbid."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", value or "")
    s = " ".join(s.split()).strip(" .-")
    return s[:max_len].rstrip(" .-") or fallback


def slug(value: str | None, max_len: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")
    return (s[:max_len].rstrip("_")) or "unknown"
