"""Tailor a resume: Claude selects and rephrases, the validator enforces honesty, a second pass verifies."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from autopilot.llm.claude import LLM
from autopilot.match.fit_scorer import job_summary
from autopilot.models import Profile, TailorSelection, VerifyResult
from autopilot.resume.validate import (
    apply_verification,
    changed_bullets,
    check_selection,
    is_internship,
    sanitize,
)


class TailorError(RuntimeError):
    pass


@dataclass
class TailorOutcome:
    selection: TailorSelection
    first_attempt_problems: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    reverted: list[dict] = field(default_factory=list)


def tailor_resume(llm: LLM, profile: Profile, job) -> TailorOutcome:
    internship = is_internship(job)
    user = (
        f"<profile>\n{profile.to_yaml(exclude={'eeo', 'answers', 'work_authorization'})}\n</profile>\n"
        f"<job>\n{job_summary(job)}\n</job>\n"
        f"<application_type>{'internship' if internship else 'full-time or other'}</application_type>\n"
        f"<job_description>\n{(job['jd_text'] or '')[:40000]}\n</job_description>"
    )
    sel = llm.ask("tailor", TailorSelection, user)
    problems = check_selection(sel, profile, internship)
    first_problems = [p.message for p in problems]
    if problems:
        retry = (
            user
            + "\n<problems_from_last_attempt>\n"
            + "\n".join(f"- {m}" for m in first_problems)
            + "\n</problems_from_last_attempt>"
        )
        sel = llm.ask("tailor", TailorSelection, retry)

    # Whatever is still wrong gets the safe fix (drop or revert), never passed through.
    sel, fixes = sanitize(sel, profile, internship)
    if not any(e.bullets for e in [*sel.experience, *sel.projects]):
        raise TailorError("Tailoring produced no usable bullets.")

    pairs = changed_bullets(sel, profile)
    if sel.summary and sel.summary.strip() != (profile.summary or "").strip():
        pairs.append({"source_id": "summary", "source": profile.fact_text(), "rewrite": sel.summary})
    reverted: list[dict] = []
    if pairs:
        result = llm.ask("verify", VerifyResult, f"<pairs>\n{json.dumps(pairs, indent=2)}\n</pairs>")
        sel, reverted = apply_verification(sel, profile, pairs, result, internship)

    remaining = check_selection(sel, profile, internship)
    if remaining:  # should be impossible after sanitize; refuse to render if it happens
        raise TailorError("Validation failed after fixes: " + "; ".join(p.message for p in remaining))
    return TailorOutcome(sel, first_problems, fixes, reverted)
