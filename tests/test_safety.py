"""Spec rule 1: nothing may ever submit an application.

1. A static scan: only fill/safety.py may click, check, press keys, use the keyboard/mouse,
   dispatch events, or call submit()/requestSubmit() from page JavaScript.
2. Browser tests: safe_click / safe_check refuse submit-like elements, and the submit guard
   blocks form submission while autopilot fills.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from autopilot.fill.safety import (
    SubmitBlocked,
    install_submit_guard,
    remove_submit_guard,
    safe_check,
    safe_click,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "autopilot"
ALLOWED_FILE = SRC / "fill" / "safety.py"

FORBIDDEN_CALLS = {
    "click",
    "dblclick",
    "tap",
    "press",
    "press_sequentially",
    "check",
    "uncheck",
    "set_checked",
    "dispatch_event",
    "submit",
    "request_submit",
}
FORBIDDEN_ATTRS = {"keyboard", "mouse", "touchscreen"}
FORBIDDEN_JS = (".click(", "requestSubmit", ".submit(", "dispatchEvent", "KeyboardEvent", "MouseEvent")


def violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_CALLS:
            found.append(f"{path.name}:{node.lineno} calls .{node.func.attr}()")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            found.append(f"{path.name}:{node.lineno} uses .{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.strip() in ("Enter", "NumpadEnter"):
                found.append(f"{path.name}:{node.lineno} has the key name '{node.value}'")
            for snippet in FORBIDDEN_JS:
                if snippet in node.value:
                    found.append(f"{path.name}:{node.lineno} has '{snippet}' in a string (page JavaScript)")
    return found


def test_only_safety_module_can_click_or_submit():
    problems = []
    for path in SRC.rglob("*.py"):
        if path != ALLOWED_FILE:
            problems += violations(path)
    assert not problems, "Route these through fill/safety.py:\n" + "\n".join(problems)


def test_scanner_catches_direct_clicks(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("page.click('#submit')\npage.keyboard.press('Enter')\npage.evaluate('f.requestSubmit()')\n")
    found = violations(bad)
    assert len(found) >= 4, found


FORM = """
<form id="f" action="/submit" onsubmit="window.__submitted = true; return false;">
  <input id="name" name="name">
  <button id="plain">Add another</button>
  <button id="addbtn" type="button">Add another</button>
  <button id="sub" type="submit">Submit application</button>
  <input id="img" type="image" alt="go">
  <a id="applylink" href="/apply">Apply now</a>
  <div id="nextdiv" role="button">Next</div>
  <span id="go" role="button" aria-label="Send">→</span>
  <input type="checkbox" id="confirm"><label for="confirm">I confirm</label>
  <input type="radio" name="r" id="r1" value="yes"><label for="r1">Yes</label>
  <label for="office">Which office are you applying to? Please confirm.</label>
  <input id="office" role="combobox" aria-autocomplete="list">
</form>
"""


@pytest.mark.browser
@pytest.mark.parametrize("selector", ["#sub", "#plain", "#img", "#applylink", "#nextdiv", "#go"])
def test_safe_click_refuses_submit_like_elements(page, selector):
    page.set_content(FORM)
    with pytest.raises(SubmitBlocked):
        safe_click(page.locator(selector))
    assert not page.evaluate("window.__submitted")


@pytest.mark.browser
def test_safe_click_and_check_allow_harmless_elements(page):
    page.set_content(FORM)
    safe_click(page.locator("#addbtn"))
    safe_click(page.locator("#office"))  # a text box can't submit, even if its label says "applying"/"confirm"
    safe_check(page.locator("#r1"))
    assert page.locator("#r1").is_checked()
    with pytest.raises(SubmitBlocked):
        safe_check(page.locator("#confirm"))
    with pytest.raises(SubmitBlocked):
        safe_check(page.locator("#addbtn"))  # not a checkbox
    assert not page.evaluate("window.__submitted")


@pytest.mark.browser
def test_submit_guard_blocks_then_releases(page):
    page.set_content(FORM)
    install_submit_guard(page)
    page.evaluate("document.getElementById('f').requestSubmit()")
    page.evaluate("document.getElementById('f').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}))")
    assert not page.evaluate("window.__submitted")
    remove_submit_guard(page)
    # After filling, the user's own submit works normally.
    page.evaluate("document.getElementById('f').requestSubmit()")
    assert page.evaluate("window.__submitted") is True
