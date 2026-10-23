"""The only module allowed to click, check or press keys in a page (spec rule 1: never submit).

`tests/test_safety.py` fails if any other module calls .click(), .check(), .press(), uses the
keyboard, dispatches events, or calls submit()/requestSubmit() in page JavaScript.

Defenses, in order:
1. `safe_click` / `safe_check` refuse any element that is, or looks like, a submit/apply/next button.
2. `install_submit_guard` cancels every form submission while autopilot is filling a page.
3. Field values are typed with `fill()`, which never presses Enter.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page

# Words on buttons that submit, advance, or create accounts. Anything matching is never clicked.
FORBIDDEN = re.compile(
    r"submit|apply|send|finish|complete|confirm|next|continue|review|proceed|"
    r"sign\s*up|sign\s*in|log\s*in|register|create\s+account|save\s+and|pay\b|purchase",
    re.I,
)
FORBIDDEN_TYPES = {"submit", "image", "reset"}
# Clicking into a text box (e.g. to open a searchable dropdown) can't submit a form, whatever its label says.
TEXT_INPUT_TYPES = {"text", "email", "tel", "url", "number", "search", "date", ""}

DESCRIBE_JS = """
el => {
  const label = el.labels && el.labels.length ? Array.from(el.labels).map(l => l.innerText).join(' ') : '';
  return {
    tag: el.tagName.toLowerCase(),
    type: String(el.type || el.getAttribute('type') || '').toLowerCase(),
    text: String(el.innerText || el.textContent || '').trim().slice(0, 300),
    value: String(el.value || '').slice(0, 300),
    aria: el.getAttribute('aria-label') || '',
    title: el.getAttribute('title') || '',
    id: el.id || '',
    name: el.getAttribute('name') || '',
    href: el.getAttribute('href') || '',
    label: label.slice(0, 300),
  };
}
"""

GUARD_ON_JS = """
() => {
  if (window.__autopilotGuard) return;
  const block = e => { e.preventDefault(); e.stopImmediatePropagation(); console.warn('[autopilot] blocked a form submission'); };
  const origSubmit = HTMLFormElement.prototype.submit;
  const origRequest = HTMLFormElement.prototype.requestSubmit;
  HTMLFormElement.prototype.submit = function () { console.warn('[autopilot] blocked form.submit()'); };
  HTMLFormElement.prototype.requestSubmit = function () { console.warn('[autopilot] blocked form.requestSubmit()'); };
  window.addEventListener('submit', block, true);
  window.__autopilotGuard = { block, origSubmit, origRequest };
}
"""

GUARD_OFF_JS = """
() => {
  const g = window.__autopilotGuard;
  if (!g) return;
  window.removeEventListener('submit', g.block, true);
  HTMLFormElement.prototype.submit = g.origSubmit;
  HTMLFormElement.prototype.requestSubmit = g.origRequest;
  delete window.__autopilotGuard;
}
"""


class SubmitBlocked(RuntimeError):
    pass


def forbidden_reason(info: dict) -> str | None:
    """Why this element must not be clicked, or None if it's safe."""
    if info.get("type") in FORBIDDEN_TYPES:
        return f"type={info['type']}"
    if info.get("tag") in ("textarea", "select") or (
        info.get("tag") == "input" and info.get("type") in TEXT_INPUT_TYPES
    ):
        return None
    for key in ("text", "value", "aria", "title", "id", "name", "href", "label"):
        val = info.get(key) or ""
        m = FORBIDDEN.search(val)
        if m:
            return f"{key} contains '{m.group(0)}'"
    return None


def describe(locator: Locator) -> dict:
    return locator.evaluate(DESCRIBE_JS)


def safe_click(locator: Locator) -> None:
    """Click an element unless it could submit or advance the form."""
    reason = forbidden_reason(describe(locator))
    if reason:
        raise SubmitBlocked(f"refused to click: {reason}")
    locator.click()


def safe_check(locator: Locator, checked: bool = True) -> None:
    """Tick or untick a checkbox/radio unless its label looks like a submit/consent action."""
    info = describe(locator)
    if info.get("type") not in ("checkbox", "radio"):
        raise SubmitBlocked(f"refused to check a non-checkbox element ({info.get('tag')})")
    reason = forbidden_reason(info)
    if reason:
        raise SubmitBlocked(f"refused to check: {reason}")
    if checked:
        locator.check()
    else:
        locator.uncheck()


def dismiss_popup(page: Page) -> None:
    """Close an open dropdown list. Escape never submits a form."""
    page.keyboard.press("Escape")


def install_submit_guard(page: Page) -> None:
    page.evaluate(GUARD_ON_JS)


def remove_submit_guard(page: Page) -> None:
    """Called when filling is done, so the user's own Submit click works normally."""
    page.evaluate(GUARD_OFF_JS)
