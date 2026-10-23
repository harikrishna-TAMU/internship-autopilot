"""Pipeline flow and the rejection log (spec §3.7, milestone M3)."""

from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from autopilot.extract.jd_fetcher import FetchError, Page
from autopilot.ingest.gmail_client import AttachmentRef, ParsedEmail
from autopilot.llm.claude import LLMError
from autopilot.models import Eligibility, Reason, ScreenshotExtraction, ScreenshotJob, Status
from autopilot.pipeline import SetupError
from autopilot.rejections import restore
from tests.conftest import FakeFetcher, FakeLLM, FakeLookup, Found, approve_all, fit, job_extraction, selection

LINK = "https://jobs.lever.co/acme/1111"


def email(mid: str, text: str, subject: str = "", images=()) -> ParsedEmail:
    return ParsedEmail(mid, "2026-10-22T10:00:00-05:00", subject, "me", text, "", list(images))


def no_images(ref):
    raise AssertionError("no images expected")


def llm(**over) -> FakeLLM:
    base = dict(extract=job_extraction(), fit=fit(), tailor=selection(), verify=approve_all)
    base.update(over)
    return FakeLLM(**base)


def rejections_for(db, job_id):
    return db.conn.execute("SELECT * FROM rejections WHERE job_id = ?", (job_id,)).fetchall()


def only_job(db):
    rows = db.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    assert len(rows) == 1
    return rows[0]


# -- happy path & idempotency --------------------------------------------------------


def test_link_email_becomes_tailored_job(make_pipeline, db, cfg):
    p = make_pipeline()
    assert p.capture_email(email("m1", f"Check this {LINK}", "Acme intern"), no_images) == 1
    summary = p.run()
    job = only_job(db)
    assert job["status"] == Status.TAILORED
    assert job["company"] == "Acme" and job["apply_url"] == LINK
    assert job["resume_path"].endswith("Test_Student_Resume.pdf")
    sidecar = json.loads((cfg.resumes_dir / job["resume_path"].split("/")[-2] / "tailor.json").read_text())
    assert sidecar["selection"]["experience"][0]["source_id"] == "exp1"
    assert summary.line() == "1 captured: 1 tailored"
    assert db.rejections() == []


def test_resume_goes_in_company_folder_with_custom_name(make_pipeline, db, cfg):
    data = yaml.safe_load(cfg.profile_path.read_text())
    data["basics"]["resume_filename"] = "Hari_Krishna_Resume.pdf"
    cfg.profile_path.write_text(yaml.safe_dump(data))
    fake = llm(
        extract=[job_extraction(company="Jane Street"), job_extraction(company="Jane Street", role="Data Intern")]
    )
    p = make_pipeline(llm=fake)
    p.capture_email(email("m1", LINK), no_images)
    p.capture_email(email("m2", "https://jobs.lever.co/acme/2222"), no_images)
    p.run()
    first, second = db.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    assert first["resume_path"] == str(cfg.resumes_dir / "Jane Street" / "Hari_Krishna_Resume.pdf")
    assert second["resume_path"] == str(cfg.resumes_dir / "Jane Street - Data Intern" / "Hari_Krishna_Resume.pdf")
    assert (cfg.resumes_dir / "Jane Street" / "tailor.json").exists()
    # Re-tailoring the same job reuses its own folder.
    assert p._resume_folder(first) == cfg.resumes_dir / "Jane Street"


def test_capture_is_idempotent(make_pipeline, db):
    p = make_pipeline()
    p.capture_email(email("m1", LINK), no_images)
    p.capture_email(email("m1", LINK), no_images)
    assert len(db.conn.execute("SELECT * FROM jobs").fetchall()) == 1


def test_instagram_only_email_needs_link_not_rejected(make_pipeline, db):
    p = make_pipeline()
    p.capture_email(email("m1", "https://www.instagram.com/stories/zero2sudo/123"), no_images)
    p.run()
    job = only_job(db)
    assert job["status"] == Status.NEEDS_LINK
    assert "Instagram" in db.last_event(job["id"])["detail"]
    assert db.rejections() == []


# -- every skip path writes exactly one rejection row ----------------------------------


def run_two(make_pipeline, db, llm_obj, second_text):
    p = make_pipeline(llm=llm_obj)
    p.capture_email(email("m1", LINK), no_images)
    p.run()
    p.capture_email(email("m2", second_text), no_images)
    p.run()
    return db.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()


def test_duplicate_by_url(make_pipeline, db):
    first, second = run_two(make_pipeline, db, llm(), LINK + "?utm_source=instagram")
    assert first["status"] == Status.TAILORED
    assert second["status"] == Status.SKIPPED and second["duplicate_of"] == first["id"]
    rows = rejections_for(db, second["id"])
    assert len(rows) == 1 and rows[0]["reason_code"] == Reason.DUPLICATE and f"job #{first['id']}" in rows[0]["detail"]
    assert rejections_for(db, first["id"]) == []


def test_duplicate_by_company_role_term(make_pipeline, db):
    first, second = run_two(make_pipeline, db, llm(), "https://acme.com/careers/swe-intern")
    rows = rejections_for(db, second["id"])
    assert len(rows) == 1 and rows[0]["reason_code"] == Reason.DUPLICATE
    assert "Same company, role and term" in rows[0]["detail"]
    assert second["apply_url_norm"] is None


SKIP_CASES = {
    "expired": (
        dict(llm=llm(extract=job_extraction(deadline="2026-10-01"))),
        Reason.EXPIRED,
        "Deadline was 2026-10-01",
    ),
    "closed_by_fetch": (
        dict(
            fetcher=FakeFetcher(
                default=Page(
                    "", "", "", "lever", closed=True, closed_reason="Lever says this posting no longer exists (404)"
                )
            )
        ),
        Reason.CLOSED,
        "no longer exists",
    ),
    "closed_by_page_text": (
        dict(llm=llm(extract=job_extraction(accepting_applications=False, closed_reason="This role has been filled"))),
        Reason.CLOSED,
        "has been filled",
    ),
    "ineligible": (
        dict(
            llm=llm(
                fit=fit(eligible=False, blockers=["No visa sponsorship; your profile says you'll need sponsorship"])
            )
        ),
        Reason.INELIGIBLE,
        "No visa sponsorship",
    ),
    "grad_year": (
        dict(
            llm=llm(
                extract=job_extraction(
                    eligibility=Eligibility(grad_years=["2026"], work_authorization=None, degree=None, other=[])
                )
            )
        ),
        Reason.INELIGIBLE,
        "Requires grad year 2026; you graduate 2028-05",
    ),
    "low_fit": (dict(llm=llm(fit=fit(score=30))), Reason.LOW_FIT, "Score 30 < 55. Missing: Go"),
    "error": (dict(fetcher=FakeFetcher(error=FetchError("acme.com returned HTTP 503"))), Reason.ERROR, "HTTP 503"),
}


@pytest.mark.parametrize("case", SKIP_CASES)
def test_every_skip_path_writes_one_rejection(case, make_pipeline, db, cfg):
    kwargs, reason, text = SKIP_CASES[case]
    p = make_pipeline(**kwargs)
    p.capture_email(email("m1", LINK, "Acme"), no_images)
    summary = p.run()
    job = only_job(db)
    rows = rejections_for(db, job["id"])
    assert len(rows) == 1, f"{case}: expected exactly one rejection row"
    assert rows[0]["reason_code"] == reason
    assert text in rows[0]["detail"]
    assert job["status"] == (Status.ERROR if reason == Reason.ERROR else Status.SKIPPED)
    assert summary.outcomes[str(reason)] == 1
    assert cfg.rejected_csv.exists() and reason in cfg.rejected_csv.read_text()


def test_user_skip_via_cli_and_listing(make_pipeline, db, home, monkeypatch):
    p = make_pipeline()
    p.capture_email(email("m1", LINK), no_images)
    p.run()
    monkeypatch.setenv("AUTOPILOT_HOME", str(home))
    from rich.console import Console

    from autopilot import cli
    from autopilot.cli import app

    monkeypatch.setattr(cli, "console", Console(width=250))
    runner = CliRunner()
    res = runner.invoke(app, ["mark", "1", "skipped", "--note", "not interested"])
    assert res.exit_code == 0, res.output
    rows = rejections_for(db, 1)
    assert len(rows) == 1 and rows[0]["reason_code"] == Reason.USER_SKIPPED and rows[0]["detail"] == "not interested"
    listing = runner.invoke(app, ["rejected", "--reason", "user_skipped"])
    assert "not interested" in listing.output
    res = runner.invoke(app, ["restore", "1"])
    assert res.exit_code == 0 and db.get_job(1)["status"] == Status.TAILORED


# -- restore ---------------------------------------------------------------------------


def test_restore_low_fit_skips_the_check_and_continues(make_pipeline, db):
    fake = llm(fit=fit(score=30))
    p = make_pipeline(llm=fake)
    p.capture_email(email("m1", LINK), no_images)
    p.run()
    job = only_job(db)
    assert job["status"] == Status.SKIPPED
    restore(db, job["id"])
    job = db.get_job(job["id"])
    assert job["status"] == Status.SCORED and "low_fit" in job["overrides"]
    make_pipeline(llm=fake).run()
    assert db.get_job(job["id"])["status"] == Status.TAILORED
    assert [t for t, _ in fake.calls].count("fit") == 1  # the check that rejected it didn't run again
    assert db.rejections()[0]["overridden_at"] is not None  # the log keeps the history


def test_restore_refuses_duplicates(make_pipeline, db):
    _, second = run_two(make_pipeline, db, llm(), LINK)
    with pytest.raises(ValueError, match="duplicate"):
        restore(db, second["id"])


def test_restore_error_retries_the_stage(make_pipeline, db):
    make_pipeline(fetcher=FakeFetcher(error=FetchError("timeout"))).capture_email(email("m1", LINK), no_images)
    p = make_pipeline(fetcher=FakeFetcher(error=FetchError("timeout")))
    p.run()
    job = only_job(db)
    assert job["status"] == Status.ERROR
    assert "retry the extract step" in restore(db, job["id"])
    make_pipeline().run()
    assert db.get_job(job["id"])["status"] == Status.TAILORED


# -- run control -------------------------------------------------------------------------


def test_fatal_llm_error_stops_run_without_marking_jobs(make_pipeline, db):
    p = make_pipeline(llm=llm(extract=LLMError("bad key", fatal=True)))
    p.capture_email(email("m1", LINK), no_images)
    with pytest.raises(LLMError):
        p.run()
    assert only_job(db)["status"] == Status.NEW
    assert db.rejections() == []


def test_unfinished_profile_stops_before_scoring(make_pipeline, db, cfg):
    data = yaml.safe_load(cfg.profile_path.read_text())
    data["basics"]["name"] = "TODO Full Name"
    cfg.profile_path.write_text(yaml.safe_dump(data))
    p = make_pipeline()
    p.capture_email(email("m1", LINK), no_images)
    with pytest.raises(SetupError, match="TODO"):
        p.run()
    assert only_job(db)["status"] == Status.EXTRACTED


# -- screenshots -------------------------------------------------------------------------


def test_screenshot_with_two_postings(make_pipeline, db, cfg, tmp_path):
    shot = ScreenshotExtraction(
        is_job_posting=True,
        jobs=[
            ScreenshotJob(
                company="Acme",
                role="SWE Intern",
                term="Summer 2027",
                location=None,
                deadline=None,
                urls=["jobs.lever.co/acme/1111"],
                notes=None,
            ),
            ScreenshotJob(
                company="Globex",
                role="Data Intern",
                term="Summer 2027",
                location=None,
                deadline="2026-12-01",
                urls=[],
                notes="Apply on their careers site",
            ),
        ],
    )
    fake = llm(screenshot=shot)
    p = make_pipeline(llm=fake, lookup=FakeLookup(None))
    png = tmp_path / "story.png"
    png.write_bytes(b"\x89PNG fake")

    def save(ref: AttachmentRef):
        dest = cfg.attachments_dir / "m9" / ref.filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png.read_bytes())
        return dest

    p.capture_email(email("m9", "", "story", images=[AttachmentRef("story.png", "image/png", data=b"x")]), save)
    p.run()
    jobs = db.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    assert [j["status"] for j in jobs] == [Status.TAILORED, Status.NEEDS_LINK]
    assert jobs[1]["company"] == "Globex" and jobs[1]["deadline"] == "2026-12-01"
    assert [t for t, _ in fake.calls].count("screenshot") == 1


def test_screenshot_without_link_uses_ats_lookup(make_pipeline, db, cfg, tmp_path):
    shot = ScreenshotExtraction(
        is_job_posting=True,
        jobs=[
            ScreenshotJob(
                company="Acme",
                role="Software Engineering Intern",
                term=None,
                location=None,
                deadline=None,
                urls=[],
                notes=None,
            )
        ],
    )
    found = Found("https://boards.greenhouse.io/acme/jobs/42", "Software Engineering Intern", "greenhouse")
    p = make_pipeline(llm=llm(screenshot=shot), lookup=FakeLookup(found))
    img = tmp_path / "s.png"
    img.write_bytes(b"\x89PNG")
    p.capture_manual(str(img))
    p.run()
    job = only_job(db)
    assert job["status"] == Status.TAILORED and job["apply_url"] == found.url
