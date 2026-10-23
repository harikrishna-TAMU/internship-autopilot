"""Shared fixtures. Claude, the network and PDF rendering are replaced by fakes, so tests are free and offline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
import yaml

from autopilot.config import AppSettings, Config, Env
from autopilot.db import Database
from autopilot.extract.jd_fetcher import Page
from autopilot.models import (
    BulletCheck,
    ChangeNote,
    EducationPick,
    Eligibility,
    FitResult,
    JobExtraction,
    SkillGroup,
    TailoredBullet,
    TailoredEntry,
    TailorSelection,
    VerifyResult,
)
from autopilot.pipeline import Pipeline, Services

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 22)

PROFILE = {
    "basics": {
        "name": "Test Student",
        "email": "student@tamu.edu",
        "phone": "+1 979 555 0100",
        "location": "College Station, TX",
        "links": {"linkedin": "https://www.linkedin.com/in/test-student", "github": "https://github.com/teststudent"},
    },
    "education": [
        {
            "id": "edu1",
            "school": "Texas A&M University",
            "degree": "Bachelor of Science",
            "major": "Computer Science",
            "location": "College Station, TX",
            "start": "2024-08",
            "grad": "2028-05",
            "gpa": 3.8,
            "coursework": ["Data Structures and Algorithms", "Operating Systems", "Databases", "Machine Learning"],
        }
    ],
    "experience": [
        {
            "id": "exp1",
            "org": "Aggie Labs",
            "title": "Software Engineering Intern",
            "location": "College Station, TX",
            "dates": "May 2026 – Aug 2026",
            "bullets": [
                {
                    "id": "exp1.b1",
                    "text": "Built a FastAPI service that cut report generation time from 40 minutes to 5 minutes",
                    "skills": ["Python", "FastAPI"],
                },
                {
                    "id": "exp1.b2",
                    "text": "Wrote PostgreSQL migrations for 12 tables used by the billing team",
                    "skills": ["PostgreSQL"],
                },
                {
                    "id": "exp1.b3",
                    "text": "Added unit tests with pytest, raising coverage to 85%",
                    "skills": ["pytest"],
                },
            ],
        }
    ],
    "projects": [
        {
            "id": "proj1",
            "name": "Campus Bus Tracker",
            "link": "https://github.com/teststudent/bus",
            "dates": "Jan 2026",
            "tech": ["React", "AWS"],
            "bullets": [
                {
                    "id": "proj1.b1",
                    "text": "Built a React app showing live bus locations for 3 campus routes",
                    "skills": ["React"],
                },
                {"id": "proj1.b2", "text": "Deployed the backend on AWS Lambda with API Gateway", "skills": ["AWS"]},
            ],
        },
        {
            "id": "proj2",
            "name": "Course Planner",
            "dates": "Sep 2025",
            "tech": ["TypeScript"],
            "bullets": [
                {
                    "id": "proj2.b1",
                    "text": "Made a TypeScript tool that checks degree plan prerequisites",
                    "skills": ["TypeScript"],
                }
            ],
        },
    ],
    "skills": {
        "languages": ["Python", "TypeScript", "Java", "SQL"],
        "frameworks": ["FastAPI", "React"],
        "tools": ["Git", "Docker", "AWS", "PostgreSQL", "pytest"],
    },
    "work_authorization": {
        "summary": "F-1 student",
        "authorized_to_work_in_us": True,
        "requires_sponsorship_now": False,
        "requires_sponsorship_future": True,
    },
    "answers": {"how_did_you_hear": "Social media"},
}


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.yaml").write_text(yaml.safe_dump(PROFILE), encoding="utf-8")
    return tmp_path


@pytest.fixture
def cfg(home: Path) -> Config:
    c = Config(home=home, env=Env(gmail_address="me@gmail.com"), settings=AppSettings())
    c.ensure_dirs()
    return c


@pytest.fixture
def db(cfg: Config):
    d = Database(cfg.db_path)
    yield d
    d.close()


@pytest.fixture
def profile(cfg: Config):
    return cfg.load_profile()


# -- fakes ----------------------------------------------------------------------


class FakeLLM:
    """Answers by task name. A value can be a model instance, a callable(user, images) or a list (one per call)."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def ask(self, task, schema, user, images=None):
        self.calls.append((task, user))
        r = self.responses[task]
        if isinstance(r, list):
            r = r.pop(0)
        if callable(r) and not isinstance(r, type):
            r = r(user, images)
        if isinstance(r, Exception):
            raise r
        return r


def job_extraction(**over) -> JobExtraction:
    data = dict(
        is_job_posting=True,
        accepting_applications=True,
        closed_reason=None,
        company="Acme",
        role="Software Engineering Intern",
        term="Summer 2027",
        location="Austin, TX",
        remote="hybrid",
        deadline=None,
        requirements=["Python", "REST APIs"],
        eligibility=Eligibility(grad_years=[], work_authorization=None, degree=None, other=[]),
    )
    data.update(over)
    return JobExtraction(**data)


def fit(score=80, eligible=True, blockers=None) -> FitResult:
    return FitResult(
        score=score,
        eligible=eligible,
        blockers=blockers or [],
        matched_skills=["Python"],
        missing_skills=["Go"],
        notes="Good backend match.",
    )


def selection(**over) -> TailorSelection:
    data = dict(
        summary="Computer Science student at Texas A&M building backend services with **Python** and **FastAPI**.",
        leadership=[],
        education=[EducationPick(source_id="edu1", coursework=["Databases", "Operating Systems"])],
        experience=[
            TailoredEntry(
                source_id="exp1",
                bullets=[
                    TailoredBullet(
                        source_id="exp1.b1",
                        text="Built a FastAPI service cutting report generation from 40 minutes to 5 minutes",
                    ),
                    TailoredBullet(
                        source_id="exp1.b2", text="Wrote PostgreSQL migrations for 12 tables used by the billing team"
                    ),
                ],
            )
        ],
        projects=[
            TailoredEntry(
                source_id="proj1",
                bullets=[
                    TailoredBullet(
                        source_id="proj1.b1", text="Built a React app showing live bus locations for 3 campus routes"
                    ),
                ],
            )
        ],
        skills=[
            SkillGroup(category="Languages", items=["Python", "SQL"]),
            SkillGroup(category="Tools", items=["PostgreSQL", "Docker"]),
        ],
        changes=[ChangeNote(source_id="exp1.b1", why="Lead with the API work")],
    )
    data.update(over)
    return TailorSelection(**data)


def approve_all(user, images):
    import json
    import re

    pairs = json.loads(re.search(r"<pairs>\n(.*)\n</pairs>", user, re.S).group(1))
    return VerifyResult(checks=[BulletCheck(source_id=p["source_id"], supported=True, problem=None) for p in pairs])


class FakeFetcher:
    def __init__(
        self, pages: dict[str, Page] | None = None, default: Page | None = None, error: Exception | None = None
    ):
        self.pages = pages or {}
        self.default = default
        self.error = error
        self.calls: list[str] = []

    def fetch(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        if url in self.pages:
            return self.pages[url]
        if self.default:
            return Page(
                url=url,
                title=self.default.title,
                text=self.default.text,
                ats=self.default.ats,
                closed=self.default.closed,
                closed_reason=self.default.closed_reason,
            )
        return Page(url=url, title="Software Engineering Intern", text="We are hiring interns. " * 40, ats="greenhouse")


@dataclass
class Found:
    url: str
    title: str
    ats: str
    score: float = 0.95


class FakeLookup:
    def __init__(self, found: Found | None = None):
        self.found = found

    def find(self, company, role):
        return self.found


@dataclass
class FakeRenderResult:
    path: Path
    pages: int = 1
    dropped: list = None


class FakeRenderer:
    def render(self, profile, sel, out_pdf, max_pages=1, target=""):
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(b"%PDF-fake")
        return FakeRenderResult(out_pdf, 1, [])


@pytest.fixture
def make_pipeline(cfg, db):
    def _make(llm=None, fetcher=None, lookup=None, renderer=None) -> Pipeline:
        llm = llm or FakeLLM(extract=job_extraction(), fit=fit(), tailor=selection(), verify=approve_all)
        services = Services(
            cfg=cfg,
            db=db,
            llm=llm,
            fetcher=fetcher or FakeFetcher(),
            lookup=lookup or FakeLookup(),
            renderer=renderer or FakeRenderer(),
            http=None,
            today=TODAY,
            log=lambda m: None,
        )
        return Pipeline(services)

    return _make


# -- real browser (skipped if Chrome/Chromium can't start) -------------------------------


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    from autopilot.headless import BrowserUnavailable, launch_chromium

    pw = sync_playwright().start()
    try:
        b = launch_chromium(pw, "chrome", headless=True)
    except BrowserUnavailable as e:
        pw.stop()
        pytest.skip(str(e))
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()
