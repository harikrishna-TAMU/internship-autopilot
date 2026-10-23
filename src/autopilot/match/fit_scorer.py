"""Fit score + eligibility. Claude judges fit; a deterministic grad-year check backs up eligibility."""

from __future__ import annotations

import json
import re

from autopilot.llm.claude import LLM
from autopilot.models import FitResult, Profile


def job_summary(job) -> str:
    fields = {k: job[k] for k in ("company", "role", "term", "location", "remote", "deadline", "apply_url")}
    fields["requirements"] = json.loads(job["requirements_json"] or "[]")
    fields["eligibility"] = json.loads(job["eligibility_json"] or "{}")
    return json.dumps(fields, indent=2)


def grad_year_blocker(profile: Profile, eligibility: dict) -> str | None:
    """'Requires grad year 2026; you graduate 2028-05' when the posting lists explicit years that exclude you."""
    edu = profile.current_education
    if edu is None or edu.grad_year is None:
        return None
    years = set()
    for item in eligibility.get("grad_years") or []:
        years.update(int(y) for y in re.findall(r"\b(20\d\d)\b", str(item)))
    if not years or edu.grad_year in years:
        return None
    listed = ", ".join(str(y) for y in sorted(years))
    return f"Requires grad year {listed}; you graduate {edu.grad}"


def score_fit(llm: LLM, profile: Profile, job, locations: list[str]) -> FitResult:
    prefs = {"preferred_locations": locations} if locations else {}
    user = (
        f"<profile>\n{profile.to_yaml(exclude={'eeo', 'answers'}, include_address=False)}\n</profile>\n"
        f"<preferences>{json.dumps(prefs)}</preferences>\n"
        f"<job>\n{job_summary(job)}\n</job>\n"
        f"<job_description>\n{(job['jd_text'] or '')[:40000]}\n</job_description>"
    )
    result = llm.ask("fit", FitResult, user)
    result.score = max(0, min(100, result.score))
    blocker = grad_year_blocker(profile, json.loads(job["eligibility_json"] or "{}"))
    if blocker and not any("grad" in b.lower() for b in result.blockers):
        result.blockers.append(blocker)
        result.eligible = False
    return result
