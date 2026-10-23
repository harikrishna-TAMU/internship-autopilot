"""Background worker: tabs open for ready jobs, submissions are detected, closed tabs wait for `open`."""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import sync_playwright

from autopilot.models import Status
from autopilot.worker import CONFIRM_RE, TabManager, pid_file, running_pid
from tests.conftest import FIXTURES, FakeLLM
from tests.test_fill import fake_mapping


def add_ready_job(db, n: int, resume) -> int:
    form = (FIXTURES / "greenhouse_form.html").as_uri() + f"?job={n}"
    return db.create_job(
        source_key=f"m{n}:x",
        source_kind="link",
        source_value=form,
        status=Status.TAILORED,
        company=f"Company {n}",
        role="SWE Intern",
        apply_url=form,
        form_url=form,
        ats="greenhouse",
        resume_path=str(resume),
        requirements_json="[]",
        eligibility_json="{}",
    )


@pytest.mark.browser
def test_tab_lifecycle(cfg, db, tmp_path):
    resume = tmp_path / "Hari_Krishna_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    first, second = add_ready_job(db, 1, resume), add_ready_job(db, 2, resume)
    fake = FakeLLM(map_fields=fake_mapping)
    logs: list[str] = []

    with sync_playwright() as pw:
        tabs = TabManager(cfg, db, fake, pw, logs.append, headless=True)
        try:
            assert tabs.open_ready() == 2
            assert {db.get_job(i)["status"] for i in (first, second)} == {Status.AWAITING_REVIEW}
            assert {db.get_job(i)["tab_state"] for i in (first, second)} == {"open"}
            assert tabs.open_ready() == 0  # nothing new
            claude_calls = len(fake.calls)

            # You submit job 1 and the site shows its confirmation page.
            tabs.open[first].evaluate("() => { document.body.innerHTML = '<h1>Thank you for applying!</h1>'; }")
            assert tabs.watch() == [first]
            assert db.get_job(first)["status"] == Status.SUBMITTED

            # You close job 2's tab without submitting: it waits for `autopilot open`.
            tabs.open[second].close()
            tabs.watch()
            assert db.get_job(second)["tab_state"] == "dismissed"
            assert tabs.open_ready() == 0

            # `autopilot open` clears the flag; the tab comes back filled from saved answers (no new Claude call).
            db.update_job(second, tab_state=None)
            assert tabs.open_ready() == 1
            assert len(fake.calls) == claude_calls
            page = tabs.open[second]
            assert page.locator("#why").input_value().startswith("I enjoy building backend services")
            assert not page.evaluate("window.__submitted")
        finally:
            tabs.close()


def test_confirmation_wording():
    for text in (
        "Thank you for applying!",
        "Thanks for applying to Acme",
        "Your application has been submitted.",
        "Application submitted!",
        "We've received your application",
        "Your application was successfully submitted",
    ):
        assert CONFIRM_RE.search(text), text
    for text in (
        "Thank you for your interest in Acme.",
        "Applications submitted after Nov 1 won't be considered",
        "Submit application",
    ):
        assert not CONFIRM_RE.search(text), text


def test_running_pid_cleans_stale_file(cfg):
    pid_file(cfg).write_text("999999")
    assert running_pid(cfg) is None
    assert not pid_file(cfg).exists()
    pid_file(cfg).write_text(str(os.getpid()))
    assert running_pid(cfg) == os.getpid()
