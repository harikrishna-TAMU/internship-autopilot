"""Rejection log (spec §3.7): every job that leaves the pipeline early is recorded with a reason.

All skip/error transitions go through `reject()`, so nothing drops out silently.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from autopilot.db import Database
from autopilot.models import Reason, Stage, Status

CSV_FIELDS = [
    "at",
    "job_id",
    "company",
    "role",
    "stage",
    "reason_code",
    "detail",
    "apply_url",
    "duplicate_of",
    "overridden_at",
]


def reject(
    db: Database,
    job_id: int,
    *,
    stage: Stage,
    reason: Reason,
    detail: str,
    restore_to: Status | None,
    duplicate_of: int | None = None,
) -> int:
    """Move a job to skipped/error and write exactly one rejection row. Returns the rejection id."""
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(f"job {job_id} not found")
    status = Status.ERROR if reason == Reason.ERROR else Status.SKIPPED
    if duplicate_of is not None:
        # A duplicate is never the canonical record for its link.
        db.update_job(job_id, duplicate_of=duplicate_of, apply_url_norm=None)
    db.set_status(job_id, status, f"{reason}: {detail}")
    return db.add_rejection(
        job_id=job_id,
        message_id=job["message_id"],
        stage=str(stage),
        reason_code=str(reason),
        detail=detail,
        company=job["company"],
        role=job["role"],
        apply_url=job["apply_url"] or (job["source_value"] if job["source_kind"] == "link" else None),
        duplicate_of=duplicate_of,
        restore_to=str(restore_to) if restore_to else None,
    )


def overrides(job) -> set[str]:
    return {x for x in (job["overrides"] or "").split(",") if x}


def is_overridden(job, reason: Reason) -> bool:
    return str(reason) in overrides(job)


def restore(db: Database, job_id: int) -> str:
    """Undo a skip or retry an error. Returns a message for the user. Raises ValueError if not possible."""
    job = db.get_job(job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found.")
    if job["status"] not in (Status.SKIPPED, Status.ERROR):
        raise ValueError(f"Job {job_id} is '{job['status']}', not skipped or errored.")
    rej = db.open_rejection(job_id)
    if rej is None:
        raise ValueError(f"Job {job_id} has no open rejection to restore.")
    if rej["reason_code"] == Reason.DUPLICATE:
        raise ValueError(f"Job {job_id} is a duplicate of job #{rej['duplicate_of']}. Use that job instead.")
    target = rej["restore_to"]
    if not target:
        raise ValueError(f"Job {job_id} can't be restored (no restore point recorded).")
    if rej["reason_code"] != Reason.ERROR:
        # Skip the check that rejected it from now on.
        ov = overrides(job) | {rej["reason_code"]}
        db.update_job(job_id, overrides=",".join(sorted(ov)))
    db.mark_overridden(rej["id"])
    db.set_status(job_id, target, f"restored (overrode {rej['reason_code']})")
    if rej["reason_code"] == Reason.ERROR:
        return f"Job {job_id} will retry the {rej['stage']} step on the next run."
    return f"Job {job_id} is back in the pipeline at '{target}'; the {rej['reason_code']} check won't run on it again."


def export_csv(db: Database, path: Path) -> int:
    rows = db.rejections()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in CSV_FIELDS})
    return len(rows)


def parse_since(value: str | None) -> str | None:
    """'7d', '12h', '2w' or 'YYYY-MM-DD' → ISO timestamp lower bound."""
    if not value:
        return None
    m = re.fullmatch(r"(\d+)\s*([hdw])", value.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return (datetime.now().astimezone() - delta).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError as e:
        raise ValueError(f"Can't read --since '{value}'. Use 7d, 12h, 2w or YYYY-MM-DD.") from e


class RunSummary:
    """Counts outcomes for the one-line summary printed after each run."""

    def __init__(self) -> None:
        self.captured = 0
        self.outcomes: Counter[str] = Counter()

    def add(self, outcome: str) -> None:
        self.outcomes[outcome] += 1

    def line(self) -> str:
        order = ["tailored", "needs_link", *[str(r) for r in Reason]]
        parts = [f"{self.outcomes[k]} {k}" for k in order if self.outcomes.get(k)]
        parts += [f"{v} {k}" for k, v in self.outcomes.items() if k not in order and v]
        head = f"{self.captured} captured"
        return f"{head}: {', '.join(parts)}" if parts else f"{head}, nothing to process"
