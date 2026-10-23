"""Pydantic models: the profile, job statuses, and every structured output Claude returns.

Models used as Claude output schemas keep every field required (nullable where needed)
and avoid dict-typed fields, so they work with structured outputs.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Statuses, stages and rejection reasons
# ---------------------------------------------------------------------------


class Status(StrEnum):
    NEW = "new"
    EXTRACTED = "extracted"
    NEEDS_LINK = "needs_link"
    SCORED = "scored"
    TAILORED = "tailored"
    AWAITING_REVIEW = "awaiting_review"
    SUBMITTED = "submitted"
    SKIPPED = "skipped"
    ERROR = "error"


class Stage(StrEnum):
    EXTRACT = "extract"
    MATCH = "match"
    TAILOR = "tailor"
    FILL = "fill"
    REVIEW = "review"


class Reason(StrEnum):
    DUPLICATE = "duplicate"
    EXPIRED = "expired"
    CLOSED = "closed"
    INELIGIBLE = "ineligible"
    LOW_FIT = "low_fit"
    USER_SKIPPED = "user_skipped"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Profile (config/profile.yaml): the only source of truth about you
# ---------------------------------------------------------------------------

LEADERSHIP = "leadership"  # parent id for leadership bullets


class Links(BaseModel):
    github: str | None = None
    linkedin: str | None = None
    portfolio: str | None = None


class Address(BaseModel):
    """Mailing address, used only to fill application forms (never printed on the resume)."""

    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None


class Basics(BaseModel):
    name: str
    preferred_name: str | None = None
    email: str
    phone: str | None = None
    location: str | None = None  # shown on the resume, e.g. "College Station, TX"
    address: Address | None = None  # full address for application forms only
    links: Links = Field(default_factory=Links)
    # Which links appear on the resume. Every link is still used to fill applications.
    resume_links: list[Literal["linkedin", "github", "portfolio"]] = Field(
        default_factory=lambda: ["linkedin", "github", "portfolio"]
    )
    resume_filename: str | None = None  # e.g. "Jane_Doe_Resume.pdf"; default First_Last_Resume.pdf

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.split() else self.name

    @property
    def last_name(self) -> str:
        parts = self.name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""


class Bullet(BaseModel):
    id: str
    text: str
    skills: list[str] = Field(default_factory=list)
    optional: bool = False  # include only when the role matches `use_when`; dropped first when the page is full
    use_when: str | None = None
    position: Literal["any", "last"] = "any"  # "last" = always the final bullet of its entry


class Education(BaseModel):
    id: str
    school: str
    degree: str
    major: str | None = None
    location: str | None = None
    start: str | None = None
    grad: str  # YYYY-MM (expected or actual)
    gpa: str | float | None = None  # shown as written, e.g. "3.8/4.0" or "8.5/10"
    gpa_label: str = "GPA"  # e.g. "CGPA"
    coursework: list[str] = Field(default_factory=list)

    @property
    def grad_year(self) -> int | None:
        m = re.match(r"(\d{4})", str(self.grad))
        return int(m.group(1)) if m else None


class Experience(BaseModel):
    id: str
    org: str
    title: str
    location: str | None = None
    dates: str
    bullets: list[Bullet] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    subtitle: str | None = None  # e.g. "Autonomous Network Defense Agent"
    link: str | None = None
    dates: str | None = None
    tech: list[str] = Field(default_factory=list)
    advisor: str | None = None
    bullets: list[Bullet] = Field(default_factory=list)


class WorkAuthorization(BaseModel):
    summary: str | None = None
    authorized_to_work_in_us: bool | None = None
    requires_sponsorship_now: bool | None = None
    requires_sponsorship_future: bool | None = None


class EEO(BaseModel):
    """Optional voluntary self-identification answers. Leave null to answer these yourself."""

    gender: str | None = None
    race_ethnicity: str | None = None
    hispanic_latino: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None


class Profile(BaseModel):
    basics: Basics
    summary: str | None = None  # your own summary: the fallback, and the facts a tailored summary may use
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    leadership: list[Bullet] = Field(default_factory=list)
    leadership_title: str = "Leadership & Collaboration"
    skills: dict[str, list[str]] = Field(default_factory=dict)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    eeo: EEO = Field(default_factory=EEO)
    answers: dict[str, str] = Field(default_factory=dict)

    def to_yaml(self, exclude: set[str] | None = None, include_address: bool = True) -> str:
        """The profile as YAML for a prompt, leaving out sections a task doesn't need.

        include_address=False for resume tailoring and fit scoring: the street address is only for forms.
        """
        data = self.model_dump(exclude=exclude or set(), exclude_none=True)
        if not include_address:
            data.get("basics", {}).pop("address", None)
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    # -- lookups used by the validator and the renderer --

    def bullet_index(self) -> dict[str, tuple[str, Bullet]]:
        """bullet id → (parent entry id, bullet)."""
        out: dict[str, tuple[str, Bullet]] = {}
        for entry in [*self.experience, *self.projects]:
            for b in entry.bullets:
                out[b.id] = (entry.id, b)
        for b in self.leadership:
            out[b.id] = (LEADERSHIP, b)
        return out

    def fact_text(self) -> str:
        """Every factual line in the profile, for checking a generated summary against."""
        lines = [self.summary or ""]
        for e in self.education:
            lines.append(
                f"{e.degree} {e.major or ''} at {e.school}, {e.start or ''} to {e.grad}, {e.gpa_label} {e.gpa or ''}"
            )
        for x in self.experience:
            lines.append(f"{x.title} at {x.org} ({x.dates}, {x.location or ''})")
        for p in self.projects:
            lines.append(
                f"Project {p.name} ({p.subtitle or ''}; {', '.join(p.tech)}; advisor {p.advisor or 'none'}; {p.dates or ''})"
            )
        lines += [b.text for _, b in self.bullet_index().values()]
        return "\n".join(line for line in lines if line.strip())

    def all_skills(self) -> dict[str, str]:
        """casefolded skill → canonical spelling, from the skills section, bullets and project tech."""
        out: dict[str, str] = {}
        for items in self.skills.values():
            for s in items:
                out.setdefault(s.strip().casefold(), s.strip())
        for _, b in self.bullet_index().values():
            for s in b.skills:
                out.setdefault(s.strip().casefold(), s.strip())
        for p in self.projects:
            for s in p.tech:
                out.setdefault(s.strip().casefold(), s.strip())
        return out

    @property
    def current_education(self) -> Education | None:
        """The program with the latest graduation date (your current degree)."""
        if not self.education:
            return None
        return max(self.education, key=lambda e: str(e.grad))

    def placeholder_problems(self) -> list[str]:
        """Things in profile.yaml that still look like template placeholders ("TODO ..." values)."""
        problems = []
        todo = re.compile(r"^TODO\b|/TODO(/|$)")

        def walk(value) -> int:
            if isinstance(value, str):
                return 1 if todo.search(value.strip()) else 0
            if isinstance(value, dict):
                return sum(walk(v) for v in value.values())
            if isinstance(value, list):
                return sum(walk(v) for v in value)
            return 0

        count = walk(self.model_dump())
        if count:
            problems.append(f"profile.yaml still has {count} TODO placeholder(s)")
        if not self.experience and not self.projects:
            problems.append("profile.yaml has no experience or projects")
        return problems


# ---------------------------------------------------------------------------
# Claude outputs
# ---------------------------------------------------------------------------


class Eligibility(BaseModel):
    grad_years: list[str] = Field(
        description="Explicit graduation years the posting accepts, every year listed (e.g. "
        "'Dec 2026 to Jun 2027' gives ['2026', '2027']). Empty if not stated or open-ended."
    )
    work_authorization: str | None = Field(
        description="Work authorization / sponsorship / citizenship requirement, quoted closely. Null if none."
    )
    degree: str | None = Field(description="Required degree level or enrollment status. Null if none.")
    other: list[str] = Field(description="Other hard requirements (e.g. 'must be enrolled in Fall 2027').")


class JobExtraction(BaseModel):
    is_job_posting: bool = Field(description="True only if the page is a specific job or internship posting.")
    accepting_applications: bool = Field(
        description="False if the page says the role is closed, filled, expired or no longer available."
    )
    closed_reason: str | None = Field(description="The page's wording when it says the role is closed.")
    company: str | None
    role: str | None = Field(description="Job title exactly as posted.")
    term: str | None = Field(description="e.g. 'Summer 2027', 'Fall 2026'. Null if not stated.")
    location: str | None
    remote: Literal["onsite", "hybrid", "remote", "unknown"]
    deadline: str | None = Field(description="Application deadline as YYYY-MM-DD. Null if not stated.")
    requirements: list[str] = Field(description="Key qualifications, short phrases, most important first.")
    eligibility: Eligibility


class ScreenshotJob(BaseModel):
    company: str | None
    role: str | None
    term: str | None
    location: str | None
    deadline: str | None = Field(description="YYYY-MM-DD or null.")
    urls: list[str] = Field(description="Any URLs visible in the image, exactly as written.")
    notes: str | None = Field(description="Other useful visible details (eligibility, pay, how to apply).")


class ScreenshotExtraction(BaseModel):
    is_job_posting: bool
    jobs: list[ScreenshotJob]


class FitResult(BaseModel):
    score: int = Field(description="0-100 fit of the candidate for this role.")
    eligible: bool = Field(description="False only if a hard requirement clearly excludes the candidate.")
    blockers: list[str] = Field(
        description="Each hard eligibility blocker, comparing the requirement with the profile, e.g. "
        "'Requires grad year 2026; you graduate 2028-05'. Empty if eligible."
    )
    matched_skills: list[str]
    missing_skills: list[str]
    notes: str = Field(description="Two or three sentences on fit.")


class TailoredBullet(BaseModel):
    source_id: str = Field(description="ID of the profile bullet this comes from.")
    text: str = Field(description="The bullet, optionally rephrased. Same facts as the source.")


class TailoredEntry(BaseModel):
    source_id: str = Field(description="ID of the experience or project entry.")
    bullets: list[TailoredBullet]


class EducationPick(BaseModel):
    source_id: str
    coursework: list[str] = Field(description="Subset of this entry's coursework, most relevant first.")


class SkillGroup(BaseModel):
    category: str
    items: list[str]


class ChangeNote(BaseModel):
    source_id: str
    why: str


class TailorSelection(BaseModel):
    summary: str = Field(description="2-3 sentence professional summary for this role, using only profile facts.")
    education: list[EducationPick]
    experience: list[TailoredEntry]
    projects: list[TailoredEntry]
    leadership: list[TailoredBullet] = Field(description="0-2 leadership bullets, by source_id.")
    skills: list[SkillGroup]
    changes: list[ChangeNote] = Field(description="What was emphasized or rephrased and why.")


class BulletCheck(BaseModel):
    source_id: str
    supported: bool = Field(description="True if every claim in the rewrite is supported by the source.")
    problem: str | None = Field(description="The unsupported claim, if any.")


class VerifyResult(BaseModel):
    checks: list[BulletCheck]


FieldActionKind = Literal["fill", "select", "check", "uncheck", "leave_for_user"]


class FieldAction(BaseModel):
    field_id: str
    action: FieldActionKind
    value: str | None = Field(
        description="Text to type, or for select/radio/combobox exactly one of the field's options "
        "(several separated by ' | ' for checkbox groups). Null for leave_for_user."
    )
    is_draft: bool = Field(description="True if this is a newly written free-text answer the user must review.")
    reason: str | None = Field(description="Why it was left for the user, or the source of the value.")


class FieldMapping(BaseModel):
    actions: list[FieldAction]
