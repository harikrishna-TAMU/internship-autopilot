"""`autopilot fill` end to end (own module: it starts its own browser, which can't overlap the shared one)."""

from __future__ import annotations

import json

import pytest

from tests.conftest import FIXTURES, FakeLLM
from tests.test_fill import DRAFT, fake_mapping

pytestmark = pytest.mark.browser


def test_fill_jobs_updates_status_and_saves_answers(cfg, db, tmp_path):
    from autopilot.fill.browser import fill_jobs, select_jobs
    from autopilot.models import Status

    resume = tmp_path / "Test_Student_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    form = (FIXTURES / "greenhouse_form.html").as_uri()
    jid = db.create_job(
        source_key="m1:x",
        source_kind="link",
        source_value=form,
        status=Status.TAILORED,
        company="Acme",
        role="SWE Intern",
        apply_url=form,
        form_url=form,
        ats="greenhouse",
        resume_path=str(resume),
        requirements_json="[]",
        eligibility_json="{}",
    )
    open_only = db.create_job(
        source_key="m2:x",
        source_kind="link",
        source_value=form,
        status=Status.TAILORED,
        company="Bigco",
        role="Intern",
        apply_url=form,
        ats="workday",
        resume_path=str(resume),
        requirements_json="[]",
        eligibility_json="{}",
    )
    jobs = select_jobs(db, None, cfg.settings.max_tabs)
    results = fill_jobs(cfg, db, FakeLLM(map_fields=fake_mapping), jobs, log=lambda m: None, wait=False, headless=True)

    assert len(results) == 2
    job = db.get_job(jid)
    assert job["status"] == Status.AWAITING_REVIEW
    answers = json.loads(job["answers_json"])
    assert answers["resume_uploaded"] and answers["drafted"][0]["value"] == DRAFT
    assert "opened only" in db.last_event(open_only)["detail"]
    assert db.get_job(open_only)["status"] == Status.AWAITING_REVIEW
