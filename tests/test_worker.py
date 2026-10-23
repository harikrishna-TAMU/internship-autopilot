"""Background worker: tabs open for ready jobs, submissions are detected, closed tabs wait for `open`."""

from __future__ import annotations

import json
import os

import pytest
from playwright.sync_api import sync_playwright

from autopilot.models import Status
from autopilot.worker import CONFIRM_RE, TabManager, pid_file, running_pid
from tests.conftest import FIXTURES, FakeLLM
from tests.test_fill import fake_mapping


def settle(tabs, times: int = 2):
    """A new page is filled once it looks the same on two checks in a row (it has finished loading)."""
    for _ in range(times):
        tabs.watch()


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


@pytest.mark.browser
def test_fills_after_you_sign_in_and_on_each_new_page(cfg, db, tmp_path):
    resume = tmp_path / "Hari_Krishna_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    login = (FIXTURES / "login.html").as_uri()
    form = (FIXTURES / "greenhouse_form.html").as_uri()
    jid = db.create_job(
        source_key="w1:x", source_kind="link", source_value=login, status=Status.TAILORED, company="Acme",
        role="SWE Intern", apply_url=login, form_url=login, ats="workday", resume_path=str(resume),
        requirements_json="[]", eligibility_json="{}",
    )  # fmt: skip
    with sync_playwright() as pw:
        tabs = TabManager(cfg, db, FakeLLM(map_fields=fake_mapping), pw, lambda m: None, headless=True)
        try:
            assert tabs.open_ready() == 1
            page = tabs.open[jid]
            assert page.locator("#em").input_value() == ""  # sign-in pages are never filled
            assert tabs.watch() == [] and "sign in" in json.loads(db.get_job(jid)["answers_json"])["open_only"]

            # You sign in; the site shows the application form. The next watch fills it.
            page.goto(form)
            settle(tabs)
            assert page.locator("#first_name").input_value() == "Test"
            assert page.evaluate("document.getElementById('resume').files.length") == 1
            assert "filled the new page" in db.last_event(jid)["detail"]

            # You change a value, then click Next and a new step appears: only the new field is filled.
            page.locator("#first_name").fill("Hari")
            page.evaluate(
                "() => document.querySelector('form').insertAdjacentHTML('afterbegin',"
                " '<label for=lk2>LinkedIn URL</label><input id=lk2 name=linkedin_url>')"
            )
            settle(tabs)
            assert page.locator("#lk2").input_value() == "https://www.linkedin.com/in/test-student"
            assert page.locator("#first_name").input_value() == "Hari"  # your edit is kept

            # Going back to a page it already filled never refills it.
            page.evaluate("() => { document.getElementById('lk2').remove(); }")
            page.locator("#first_name").fill("Hari K")
            settle(tabs)
            assert page.locator("#first_name").input_value() == "Hari K"
            assert not page.evaluate("window.__submitted")
        finally:
            tabs.close()


def test_review_window_uses_sync_and_real_keychain(cfg):
    from autopilot.fill.browser import REAL_CHROME_ARGS, launch_review_browser

    class Chromium:
        def launch_persistent_context(self, **kw):
            self.kw = kw
            return "ctx"

    class PW:
        chromium = Chromium()

    launch_review_browser(PW(), cfg, headless=False)
    assert PW.chromium.kw["ignore_default_args"] == REAL_CHROME_ARGS
    assert "--enable-automation" not in REAL_CHROME_ARGS  # the window still shows it's automated
    launch_review_browser(PW(), cfg, headless=True)
    assert "ignore_default_args" not in PW.chromium.kw  # tests stay isolated


def test_browser_setup_opens_a_normal_chrome_on_the_review_profile(monkeypatch):
    from autopilot.cli import chrome_launch_command

    cmd = chrome_launch_command("/tmp/review-profile", "chrome://settings/people")
    assert cmd is not None and "--user-data-dir=/tmp/review-profile" in cmd
    assert not any("remote-debugging" in a or "enable-automation" in a for a in cmd)


@pytest.mark.browser
def test_apply_now_in_a_new_tab_is_followed(cfg, db, tmp_path):
    resume = tmp_path / "Hari_Krishna_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    job_page = (FIXTURES / "job_page.html").as_uri()
    jid = db.create_job(
        source_key="n1:x", source_kind="link", source_value=job_page, status=Status.TAILORED, company="Acme",
        role="Security Intern", apply_url=job_page, form_url=job_page, ats="other", resume_path=str(resume),
        requirements_json="[]", eligibility_json="{}",
    )  # fmt: skip
    with sync_playwright() as pw:
        tabs = TabManager(cfg, db, FakeLLM(map_fields=fake_mapping), pw, lambda m: None, headless=True)
        try:
            assert tabs.open_ready() == 1
            page = tabs.open[jid]
            assert page.evaluate("document.getElementById('up').files.length") == 0  # match widget untouched
            with page.context.expect_page() as new_tab:
                page.locator("#apply").click()  # you click "Apply now"; it opens a new tab
            form_tab = new_tab.value
            form_tab.wait_for_load_state()
            tabs.idle(0.5)
            settle(tabs)
            assert form_tab.locator("#first_name").input_value() == "Test"
            page.close()  # closing the job page keeps following the application tab
            tabs.watch()
            assert jid in tabs.open and tabs.open[jid] is form_tab
        finally:
            tabs.close()


@pytest.mark.browser
def test_confirmation_page_after_submit_is_never_filled(cfg, db, tmp_path):
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF-1.4")
    jid = add_ready_job(db, 7, resume)
    with sync_playwright() as pw:
        tabs = TabManager(cfg, db, FakeLLM(map_fields=fake_mapping), pw, lambda m: None, headless=True)
        try:
            tabs.open_ready()
            page = tabs.open[jid]
            # The site shows its thank-you page with a feedback survey (which has form fields).
            page.set_content(
                "<h1>Your application has been submitted, Test!</h1><label>First name<input name=fn></label>"
                "<label>Email<input name=em type=email></label><label>Comments<textarea></textarea></label>"
            )
            submitted = tabs.watch()
            assert submitted == [jid]
            assert page.locator("input[name=fn]").input_value() == ""
        finally:
            tabs.close()
