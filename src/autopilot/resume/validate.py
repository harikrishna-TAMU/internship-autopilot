"""Resume validation (spec §3.4). Runs before anything is rendered.

- every source_id exists in the profile, under the right parent
- every skill and coursework item exists in the profile
- no numbers that aren't in the source bullet (or, for the summary, anywhere in the profile)
- intern applications never claim years of experience in the summary
- rewrites the verify pass flags are reverted to the source wording
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autopilot.models import (
    LEADERSHIP,
    EducationPick,
    Profile,
    SkillGroup,
    TailoredBullet,
    TailoredEntry,
    TailorSelection,
    VerifyResult,
)

NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
YEARS_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*\+?\s*(?:years?|yrs?)\b|\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\s+years?\b", re.I
)
INTERN_RE = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b", re.I)
# Work-authorization details belong in applications only, never on the resume.
PRIVATE_RE = re.compile(
    r"\b(?:visas?|sponsor\w*|citizen\w*|green\s*card|work\s*authori[sz]\w*|H-?1B|immigration)\b", re.I
)
PRIVATE_CASE_RE = re.compile(r"\b(?:F-?1|CPT|OPT|EAD)\b")


@dataclass
class Problem:
    kind: str  # unknown_id | wrong_parent | duplicate | unknown_skill | unknown_coursework | new_number | summary
    source_id: str | None
    message: str


def numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUM_RE.findall(text or "")}


def new_numbers(rewrite: str, source: str) -> set[str]:
    return numbers(rewrite) - numbers(source)


def is_internship(job) -> bool:
    return any(INTERN_RE.search(str(job[k] or "")) for k in ("role", "term"))


def summary_problems(summary: str, profile: Profile, internship: bool) -> list[str]:
    problems = []
    extra = numbers(summary) - numbers(profile.fact_text())
    if extra:
        problems.append(f"summary has numbers not in the profile: {', '.join(sorted(extra))}")
    if internship and YEARS_RE.search(summary or ""):
        problems.append(
            f"summary mentions years of experience ('{YEARS_RE.search(summary).group(0)}'), not allowed for internships"
        )
    private = PRIVATE_RE.search(summary or "") or PRIVATE_CASE_RE.search(summary or "")
    if private:
        problems.append(
            f"summary mentions work-authorization details ('{private.group(0)}'); those belong in applications only"
        )
    return problems


def check_selection(sel: TailorSelection, profile: Profile, internship: bool = False) -> list[Problem]:
    """Every rule violation in a tailored selection. Empty list = valid."""
    problems: list[Problem] = []
    edu = {e.id: e for e in profile.education}
    entries = {"experience": {e.id for e in profile.experience}, "projects": {p.id for p in profile.projects}}
    bullets = profile.bullet_index()
    skills = profile.all_skills()

    for msg in summary_problems(sel.summary, profile, internship):
        problems.append(Problem("summary", "summary", msg))

    for pick in sel.education:
        if pick.source_id not in edu:
            problems.append(Problem("unknown_id", pick.source_id, f"Education '{pick.source_id}' isn't in the profile"))
            continue
        allowed = {c.casefold() for c in edu[pick.source_id].coursework}
        for c in pick.coursework:
            if c.casefold() not in allowed:
                problems.append(
                    Problem(
                        "unknown_coursework", pick.source_id, f"Coursework '{c}' isn't listed under {pick.source_id}"
                    )
                )

    seen: set[str] = set()

    def check_bullet(b: TailoredBullet, parent: str) -> None:
        if b.source_id not in bullets:
            problems.append(Problem("unknown_id", b.source_id, f"Bullet '{b.source_id}' isn't in the profile"))
        elif bullets[b.source_id][0] != parent:
            problems.append(
                Problem(
                    "wrong_parent",
                    b.source_id,
                    f"Bullet '{b.source_id}' belongs to '{bullets[b.source_id][0]}', not '{parent}'",
                )
            )
        elif b.source_id in seen:
            problems.append(Problem("duplicate", b.source_id, f"Bullet '{b.source_id}' is used twice"))
        else:
            seen.add(b.source_id)
            extra = new_numbers(b.text, bullets[b.source_id][1].text)
            if extra:
                problems.append(
                    Problem(
                        "new_number",
                        b.source_id,
                        f"Bullet '{b.source_id}' has numbers not in its source: {', '.join(sorted(extra))}",
                    )
                )

    for section, ids in entries.items():
        for entry in getattr(sel, section):
            if entry.source_id not in ids:
                problems.append(
                    Problem("unknown_id", entry.source_id, f"{section} entry '{entry.source_id}' isn't in the profile")
                )
                continue
            for b in entry.bullets:
                check_bullet(b, entry.source_id)
    for b in sel.leadership:
        check_bullet(b, LEADERSHIP)

    for group in sel.skills:
        for item in group.items:
            if item.strip().casefold() not in skills:
                problems.append(Problem("unknown_skill", None, f"Skill '{item}' isn't in the profile"))
    return problems


def fallback_summary(profile: Profile, internship: bool) -> str:
    """Your own summary from the profile, if it passes the same rules; otherwise no summary."""
    own = (profile.summary or "").strip()
    return own if own and not summary_problems(own, profile, internship) else ""


def sanitize(sel: TailorSelection, profile: Profile, internship: bool = False) -> tuple[TailorSelection, list[str]]:
    """Apply the safe fix for every problem: drop unknown items, revert bullets with new numbers."""
    notes: list[str] = []
    edu = {e.id: e for e in profile.education}
    bullets = profile.bullet_index()
    skills = profile.all_skills()

    summary = sel.summary.strip()
    if problems := summary_problems(summary, profile, internship):
        notes.append("replaced the summary with your own (" + "; ".join(problems) + ")")
        summary = fallback_summary(profile, internship)

    education: list[EducationPick] = []
    for pick in sel.education:
        if pick.source_id not in edu:
            notes.append(f"dropped unknown education '{pick.source_id}'")
            continue
        canon = {c.casefold(): c for c in edu[pick.source_id].coursework}
        kept = [canon[c.casefold()] for c in pick.coursework if c.casefold() in canon]
        dropped = [c for c in pick.coursework if c.casefold() not in canon]
        if dropped:
            notes.append(f"dropped coursework not in profile: {', '.join(dropped)}")
        education.append(EducationPick(source_id=pick.source_id, coursework=kept))
    # Education is always shown, even if Claude left an entry out.
    for e in profile.education:
        if e.id not in {p.source_id for p in education}:
            education.append(EducationPick(source_id=e.id, coursework=e.coursework[:6]))

    seen: set[str] = set()

    def clean_bullets(items: list[TailoredBullet], parent: str) -> list[TailoredBullet]:
        kept: list[TailoredBullet] = []
        for b in items:
            src = bullets.get(b.source_id)
            if src is None or src[0] != parent or b.source_id in seen:
                notes.append(f"dropped bullet '{b.source_id}' (unknown, misplaced or repeated)")
                continue
            seen.add(b.source_id)
            text = b.text.strip()
            if new_numbers(text, src[1].text):
                notes.append(f"reverted '{b.source_id}' to the original wording (rewrite added numbers)")
                text = src[1].text
            kept.append(TailoredBullet(source_id=b.source_id, text=text))
        return kept

    def clean_entries(section: str, ids: set[str]) -> list[TailoredEntry]:
        out: list[TailoredEntry] = []
        for entry in getattr(sel, section):
            if entry.source_id not in ids or any(e.source_id == entry.source_id for e in out):
                notes.append(f"dropped unknown or repeated {section} entry '{entry.source_id}'")
                continue
            kept = clean_bullets(entry.bullets, entry.source_id)
            # Bullets marked `position: last` always end their entry.
            kept.sort(key=lambda b: bullets[b.source_id][1].position == "last")
            if kept:
                out.append(TailoredEntry(source_id=entry.source_id, bullets=kept))
        return out

    experience = clean_entries("experience", {e.id for e in profile.experience})
    projects = clean_entries("projects", {p.id for p in profile.projects})
    leadership = clean_bullets(sel.leadership, LEADERSHIP)

    groups: list[SkillGroup] = []
    used: set[str] = set()
    for g in sel.skills:
        items = []
        for item in g.items:
            key = item.strip().casefold()
            if key not in skills:
                notes.append(f"dropped skill not in profile: {item}")
            elif key not in used:
                used.add(key)
                items.append(skills[key])
        if items:
            groups.append(SkillGroup(category=g.category, items=items))

    clean = TailorSelection(
        summary=summary,
        education=education,
        experience=experience,
        projects=projects,
        leadership=leadership,
        skills=groups,
        changes=sel.changes,
    )
    return clean, notes


def all_bullets(sel: TailorSelection) -> list[TailoredBullet]:
    return [b for entry in [*sel.experience, *sel.projects] for b in entry.bullets] + list(sel.leadership)


def changed_bullets(sel: TailorSelection, profile: Profile) -> list[dict]:
    """Bullets whose wording differs from the source: these go to the verify pass."""
    bullets = profile.bullet_index()
    pairs = []
    for b in all_bullets(sel):
        source = bullets[b.source_id][1].text
        if " ".join(b.text.split()) != " ".join(source.split()):
            pairs.append({"source_id": b.source_id, "source": source, "rewrite": b.text})
    return pairs


def apply_verification(
    sel: TailorSelection, profile: Profile, pairs: list[dict], result: VerifyResult, internship: bool = False
) -> tuple[TailorSelection, list[dict]]:
    """Revert every rewrite the verify pass didn't explicitly approve. Returns (selection, reverted)."""
    approved = {c.source_id for c in result.checks if c.supported}
    problems = {c.source_id: c.problem for c in result.checks if not c.supported}
    bullets = profile.bullet_index()
    to_revert = {p["source_id"] for p in pairs if p["source_id"] not in approved}
    reverted = []
    for b in all_bullets(sel):
        if b.source_id in to_revert:
            reverted.append(
                {
                    "source_id": b.source_id,
                    "rewrite": b.text,
                    "problem": problems.get(b.source_id) or "not confirmed by the verify pass",
                }
            )
            b.text = bullets[b.source_id][1].text
    if "summary" in to_revert:
        reverted.append(
            {
                "source_id": "summary",
                "rewrite": sel.summary,
                "problem": problems.get("summary") or "not confirmed by the verify pass",
            }
        )
        sel.summary = fallback_summary(profile, internship)
    return sel, reverted
