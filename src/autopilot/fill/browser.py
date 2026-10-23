"""`autopilot fill`: open a visible Chrome (dedicated profile), one tab per job, fill each, stop before submit."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from autopilot.config import Config
from autopilot.db import Database, loads
from autopilot.extract.jd_fetcher import closed_signal
from autopilot.fill.adapters import adapter_for
from autopilot.fill.filler import FillReport, banner, fill_page
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


# Playwright turns these on for test isolation. The review window drops them so it behaves like your own
# Chrome: sync (your saved passwords, offered by Chrome itself) and the real macOS keychain, which keeps
# sites you signed into during `autopilot browser-setup` signed in. ApplyPilot never reads or types passwords.
REAL_CHROME_ARGS = ["--disable-sync", "--password-store=basic", "--use-mock-keychain"]


def launch_review_browser(pw, cfg: Config, headless: bool = False):
    """The Chrome window you review in: a dedicated profile (data/chrome-profile), not your everyday one."""
    extra = {} if headless else {"ignore_default_args": REAL_CHROME_ARGS}
    try:
        return pw.chromium.launch_persistent_context(
            user_data_dir=str(cfg.chrome_profile_dir),
            channel=cfg.settings.browser_channel or None,
            headless=headless,
            no_viewport=True,
            **extra,
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

        follow = FollowUp(cfg, db, llm, log)
        pages: dict[int, object] = {}
        extra: dict[int, list] = {}  # tabs a job's page opened itself (e.g. "Apply now" in a new tab)
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
            if not page.is_closed():
                pages[job["id"]] = page
                follow.track(db.get_job(job["id"]), page)
                page.on("popup", lambda p, jid=job["id"]: extra.setdefault(jid, []).append(p))

        if wait and ctx.pages:
            log(
                "\nChrome is open. Review each tab and click Submit yourself. Then run "
                "`autopilot mark <id> submitted`.\nIf a site needs you to sign in, sign in: the form is filled right after.\n"
                "Close the Chrome window when you're done (this command waits for it)."
            )

            def all_pages():
                return [(jid, p) for jid in pages for p in [pages[jid], *extra.get(jid, [])] if not p.is_closed()]

            while all_pages():
                for jid, p in all_pages():
                    if msg := follow.update(jid, p):
                        log(f"#{jid}: {msg}")
                alive = next((p for _, p in all_pages()), None)
                try:
                    alive.wait_for_timeout(3000) if alive else time.sleep(3)
                except PlaywrightError:
                    time.sleep(3)
        try:
            ctx.close()
        except PlaywrightError:
            pass
    return results


def fill_one(page, job, profile, llm: LLM, db: Database) -> str:
    adapter = adapter_for(job["ats"])
    page.goto(adapter.form_url(job), wait_until="domcontentloaded", timeout=60000)

    if not adapter.can_fill:
        banner(page, f"{adapter.open_only_note} Nothing has been submitted.", short="opened only")
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
    if report.open_only:
        banner(page, f"{report.open_only}. The form is filled as soon as it appears.", short="waiting for you")
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


# Wording ATS confirmation pages use after a successful submit.
CONFIRM_RE = re.compile(
    r"thank(?:s| you) for (?:applying|your application|submitting)"
    r"|application (?:has been |was )?(?:successfully )?(?:submitted|received)"
    r"|we(?:'|’)?ve received your application|we have received your application"
    r"|your application (?:is|has been) (?:complete|submitted|received)",
    re.I,
)
DONE_URL_RE = re.compile(r"success|thank|confirm|submitted|complete", re.I)
BODY_TEXT_JS = "() => document.body ? document.body.innerText.slice(0, 20000) : ''"

# What's on the page right now: used to notice a sign-in finishing or the next step of a form.
FINGERPRINT_JS = """
() => {
  const shown = el => {
    const t = String(el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(t)) return false;
    if (t === 'file') return true;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const els = Array.from(document.querySelectorAll('input, select, textarea')).filter(shown);
  return {
    url: location.href,
    key: location.origin + location.pathname + '#' + els.map(e => e.name || e.id || e.getAttribute('aria-label') || e.type || e.tagName).join('|'),
    password: els.some(e => String(e.type || '').toLowerCase() === 'password'),
    count: els.filter(e => String(e.type || '').toLowerCase() !== 'file').length,
  };
}
"""


@dataclass
class TabState:
    seen: set[str] = field(default_factory=set)
    pending: str | None = None  # a new page seen once; filled only if it's unchanged on the next check
    resume_done: bool = False
    asked_login: bool = False


def merge_answers(old: dict, new: FillReport) -> dict:
    merged = dict(old or {})
    for k in ("filled", "drafted", "left_for_you", "kept"):
        merged[k] = list(merged.get(k) or []) + list(getattr(new, k))
    merged["resume_uploaded"] = bool(merged.get("resume_uploaded")) or new.resume_uploaded
    merged["open_only"] = None
    merged["actions_by_label"] = {**(merged.get("actions_by_label") or {}), **new.actions_by_label}
    return merged


class FollowUp:
    """Keeps filling a tab after the first pass: once you sign in, and on each new page of a multi-step form.

    It only reacts when the page's set of fields changes, never re-fills a page it has seen, keeps any value
    that's already there, and never clicks Next or Submit (you do). LinkedIn tabs are never touched.
    """

    def __init__(self, cfg: Config, db: Database, llm: LLM, log: Callable[[str], None], notify=None):
        self.cfg, self.db, self.llm, self.log = cfg, db, llm, log
        self.notify = notify or (lambda message: None)
        self.states: dict[int, TabState] = {}

    def fingerprint(self, page) -> dict | None:
        try:
            return page.evaluate(FINGERPRINT_JS)
        except PlaywrightError:
            return None

    def track(self, job, page) -> None:
        if job is None or not adapter_for(job["ats"]).follow_up:
            return
        answers = loads(job["answers_json"], {}) or {}
        state = TabState(resume_done=bool(answers.get("resume_uploaded")))
        if (info := self.fingerprint(page)) is not None:
            state.seen.add(info["key"])
            if info["password"]:
                self._ask_to_sign_in(job, state)
        self.states[job["id"]] = state

    def _looks_submitted(self, page) -> bool:
        try:
            return bool(CONFIRM_RE.search(page.evaluate(BODY_TEXT_JS)))
        except PlaywrightError:
            return False

    def forget(self, job_id: int) -> None:
        self.states.pop(job_id, None)

    def _ask_to_sign_in(self, job, state: TabState) -> None:
        if state.asked_login:
            return
        state.asked_login = True
        message = f"{job['company']}: sign in in the review window; the application is filled after you log in."
        self.log(f"#{job['id']} waiting for you to sign in")
        self.notify(message)

    def update(self, job_id: int, page) -> str | None:
        """Fill the page if it's a new application page. Returns a summary when something was filled."""
        state = self.states.get(job_id)
        if state is None or (info := self.fingerprint(page)) is None or info["key"] in state.seen:
            return None
        if state.pending != info["key"]:
            state.pending = info["key"]  # still loading or changing: look again on the next check
            return None
        state.seen.add(info["key"])
        job = self.db.get_job(job_id)
        if job is None or job["status"] not in (Status.TAILORED, Status.AWAITING_REVIEW):
            return None
        # After you click Submit: confirmation and survey pages are never filled.
        if DONE_URL_RE.search(info.get("url", "")) or self._looks_submitted(page):
            return None
        if info["password"]:
            self._ask_to_sign_in(job, state)
            return None
        if info["count"] < 2:
            return None
        answers = loads(job["answers_json"], {}) or {}
        resume = Path(job["resume_path"]) if job["resume_path"] else None
        try:
            report = fill_page(
                page,
                job,
                self.cfg.load_profile(),
                self.llm,
                resume,
                cache=answers.get("actions_by_label"),
                keep_existing=True,
                upload_resume=not state.resume_done,
            )
        except LLMError as e:
            self.log(f"#{job_id} couldn't fill the new page: {e}")
            return None
        if report.open_only:
            return None
        state.resume_done = state.resume_done or report.resume_uploaded
        self.db.update_job(job_id, answers_json=json.dumps(merge_answers(answers, report)))
        summary = (
            f"filled the new page: {len(report.filled)} filled, {len(report.drafted)} drafted, "
            f"{len(report.left_for_you)} left for you, {len(report.kept)} kept as they were"
        )
        self.db.set_status(job_id, Status.AWAITING_REVIEW, summary)
        return summary
