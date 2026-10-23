"""`autopilot fill`: open a visible Chrome (dedicated profile), one tab per job, fill each, stop before submit."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from autopilot.config import Config
from autopilot.db import Database, loads
from autopilot.extract.jd_fetcher import closed_signal
from autopilot.fill.adapters import adapter_for
from autopilot.fill.filler import banner, fill_page
from autopilot.headless import BrowserUnavailable
from autopilot.llm.claude import LLM, LLMError
from autopilot.models import Reason, Stage, Status
from autopilot.rejections import is_overridden, reject


@dataclass
class TabResult:
    job_id: int
    company: str
    role: str
    outcome: str


def select_jobs(db: Database, ids: list[int] | None, max_tabs: int) -> list:
    if ids:
        jobs = [db.get_job(i) for i in ids]
        missing = [i for i, j in zip(ids, jobs, strict=True) if j is None]
        if missing:
            raise ValueError(f"No job with id {', '.join(map(str, missing))}.")
        bad = [j for j in jobs if j["status"] not in (Status.TAILORED, Status.AWAITING_REVIEW)]
        if bad:
            raise ValueError(
                "Only tailored or awaiting_review jobs can be filled: "
                + ", ".join(f"#{j['id']} is {j['status']}" for j in bad)
            )
        return jobs[:max_tabs]
    return db.jobs_with_status(Status.TAILORED)[:max_tabs]


def launch_review_browser(pw, cfg: Config, headless: bool = False):
    """The Chrome window you review in: a dedicated profile (data/chrome-profile), not your everyday one."""
    try:
        return pw.chromium.launch_persistent_context(
            user_data_dir=str(cfg.chrome_profile_dir),
            channel=cfg.settings.browser_channel or None,
            headless=headless,
            no_viewport=True,
        )
    except PlaywrightError as e:
        first = str(e).splitlines()[0]
        hint = (
            " Is another autopilot Chrome window already open? (`autopilot status`)"
            if "ProcessSingleton" in str(e) or "in use" in str(e)
            else ""
        )
        raise BrowserUnavailable(f"Couldn't open Chrome: {first}.{hint}") from e


def fill_jobs(
    cfg: Config,
    db: Database,
    llm: LLM,
    jobs: list,
    log: Callable[[str], None] = print,
    wait: bool = True,
    headless: bool = False,
) -> list[TabResult]:
    profile = cfg.load_profile()
    results: list[TabResult] = []
    with sync_playwright() as pw:
        ctx = launch_review_browser(pw, cfg, headless)

        blank = ctx.pages[0] if ctx.pages else None
        for job in jobs:
            page = blank or ctx.new_page()
            blank = None
            label = f"#{job['id']} {job['company']} – {job['role']}"
            log(f"Opening {label}")
            try:
                outcome = fill_one(page, job, profile, llm, db)
            except LLMError as e:
                if e.fatal:
                    raise
                outcome = record_fill_error(db, job, e)
            except Exception as e:  # noqa: BLE001 - logged as an error, tab stays open for manual use
                outcome = record_fill_error(db, job, e)
            results.append(TabResult(job["id"], job["company"] or "", job["role"] or "", outcome))

        if wait and ctx.pages:
            log(
                "\nChrome is open. Review each tab and click Submit yourself. Then run "
                "`autopilot mark <id> submitted`.\nClose the Chrome window when you're done (this command waits for it)."
            )
            try:
                ctx.wait_for_event("close", timeout=0)
            except PlaywrightError:
                pass
        else:
            ctx.close()
    return results


def fill_one(page, job, profile, llm: LLM, db: Database) -> str:
    adapter = adapter_for(job["ats"])
    page.goto(adapter.form_url(job), wait_until="domcontentloaded", timeout=60000)

    if not adapter.can_fill:
        banner(page, f"<b>Autopilot</b>: {adapter.open_only_note} Nothing has been submitted.")
        db.set_status(job["id"], Status.AWAITING_REVIEW, f"opened only: {adapter.open_only_note}")
        return f"opened only ({adapter.name})"

    adapter.prepare(page)
    try:
        page_text = page.inner_text("body", timeout=5000)
    except PlaywrightError:
        page_text = ""
    closed = closed_signal(page_text)
    if closed and not is_overridden(job, Reason.CLOSED):
        reject(
            db,
            job["id"],
            stage=Stage.FILL,
            reason=Reason.CLOSED,
            detail=f"Form page says: {closed}",
            restore_to=Status.TAILORED,
        )
        page.close()
        return f"closed ({closed})"

    resume = Path(job["resume_path"]) if job["resume_path"] else None
    cache = (loads(job["answers_json"], {}) or {}).get("actions_by_label")
    report = fill_page(page, job, profile, llm, resume, upload_first=adapter.upload_first, cache=cache)
    db.update_job(job["id"], answers_json=json.dumps(report.to_dict()))
    db.set_status(job["id"], Status.AWAITING_REVIEW, report.summary())
    return report.summary()


def record_fill_error(db: Database, job, err: Exception) -> str:
    detail = f"{type(err).__name__}: {err}"[:600]
    current = db.get_job(job["id"])
    if current["status"] not in (Status.SKIPPED, Status.ERROR):
        reject(
            db, job["id"], stage=Stage.FILL, reason=Reason.ERROR, detail=detail, restore_to=Status(current["status"])
        )
    return f"error: {detail} (tab left open so you can apply manually)"
