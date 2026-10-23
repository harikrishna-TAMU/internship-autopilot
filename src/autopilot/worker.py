"""Background worker (`autopilot start`).

Every `poll_minutes` it checks Gmail, then extracts, scores and tailors. Every few seconds it keeps
a Chrome window up to date: one filled tab for each job waiting for your review. It never submits.

- New tabs open only while you're not using that Chrome window, so it doesn't take the page out from under you.
- When you submit and the site shows its "thank you for applying" page, the job is marked submitted.
- Closing a tab without submitting means "later": it won't reopen until `autopilot open` or a restart.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from autopilot.config import Config
from autopilot.db import Database
from autopilot.extract.ats_lookup import ATSLookup
from autopilot.extract.jd_fetcher import USER_AGENT, JDFetcher
from autopilot.fill.browser import fill_one, launch_review_browser, record_fill_error
from autopilot.headless import BrowserUnavailable, HeadlessBrowser
from autopilot.ingest.gmail_client import GmailAuthError, GmailClient
from autopilot.llm.claude import LLM, ClaudeLLM, LLMError
from autopilot.models import Status
from autopilot.pipeline import Pipeline, Services, SetupError
from autopilot.resume.render import make_renderer

# Wording ATS confirmation pages use after a successful submit.
CONFIRM_RE = re.compile(
    r"thank(?:s| you) for (?:applying|your application|submitting)"
    r"|application (?:has been |was )?(?:successfully )?(?:submitted|received)"
    r"|we(?:'|’)?ve received your application|we have received your application"
    r"|your application (?:is|has been) (?:complete|submitted|received)",
    re.I,
)
BODY_TEXT_JS = "() => document.body ? document.body.innerText.slice(0, 20000) : ''"
TAB_CHECK_SECONDS = 20


def pid_file(cfg: Config) -> Path:
    return cfg.data_dir / "autopilot.pid"


def log_file(cfg: Config) -> Path:
    return cfg.data_dir / "autopilot.log"


def running_pid(cfg: Config) -> int | None:
    """PID of the background worker if it's running (a stale PID file is cleaned up)."""
    path = pid_file(cfg)
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return None
    except PermissionError:
        pass
    return pid


def notify(message: str, title: str = "ApplyPilot") -> None:
    """A macOS notification (no-op elsewhere)."""
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


class TabManager:
    """Keeps one filled tab open per job waiting for review."""

    def __init__(self, cfg: Config, db: Database, llm: LLM, pw, log: Callable[[str], None], headless: bool = False):
        self.cfg, self.db, self.llm, self.pw, self.log = cfg, db, llm, pw, log
        self.headless = headless
        self.ctx = None
        self.open: dict[int, object] = {}
        self.baseline: dict[int, bool] = {}

    def _context(self):
        if self.ctx is None:
            self.ctx = launch_review_browser(self.pw, self.cfg, self.headless)
        return self.ctx

    def _new_page(self):
        for attempt in (1, 2):
            try:
                ctx = self._context()
                blank = next((p for p in ctx.pages if p.url == "about:blank" and p not in self.open.values()), None)
                return blank or ctx.new_page()
            except PlaywrightError:
                if attempt == 2:
                    raise
                self.ctx = None  # Chrome was quit; start it again

    def user_is_active(self) -> bool:
        """True while you're working in the review window (don't open tabs under you)."""
        if self.headless:
            return False
        for page in self.open.values():
            try:
                if not page.is_closed() and page.evaluate("() => document.hasFocus()"):
                    return True
            except PlaywrightError:
                continue
        return False

    def waiting_jobs(self) -> list:
        return [
            j
            for j in self.db.jobs_with_status(Status.TAILORED, Status.AWAITING_REVIEW)
            if j["tab_state"] is None and j["id"] not in self.open
        ]

    def open_ready(self) -> int:
        """Open and fill tabs for jobs waiting for review. Returns how many were opened."""
        room = self.cfg.settings.max_tabs - len(self.open)
        jobs = self.waiting_jobs()[: max(room, 0)]
        if not jobs or self.user_is_active():
            return 0
        try:
            profile = self.cfg.load_profile()
        except Exception as e:  # noqa: BLE001
            self.log(f"can't open tabs: config/profile.yaml problem: {str(e).splitlines()[0]}")
            return 0
        opened = 0
        for job in jobs:
            page = self._new_page()
            try:
                outcome = fill_one(page, job, profile, self.llm, self.db)
            except LLMError as e:
                if e.fatal:
                    raise
                outcome = record_fill_error(self.db, job, e)
            except Exception as e:  # noqa: BLE001 - logged; the tab stays open for manual use
                outcome = record_fill_error(self.db, job, e)
            self.log(f"tab #{job['id']} {job['company']} – {job['role']}: {outcome}")
            if page.is_closed():  # e.g. the posting turned out to be closed
                continue
            self.open[job["id"]] = page
            self.db.update_job(job["id"], tab_state="open")
            try:
                self.baseline[job["id"]] = bool(CONFIRM_RE.search(page.evaluate(BODY_TEXT_JS)))
            except PlaywrightError:
                self.baseline[job["id"]] = False
            opened += 1
        return opened

    def watch(self) -> list[int]:
        """Notice closed tabs and confirmation pages. Returns ids of jobs detected as submitted."""
        submitted = []
        for jid, page in list(self.open.items()):
            if page.is_closed():
                del self.open[jid]
                job = self.db.get_job(jid)
                if job and job["status"] in (Status.TAILORED, Status.AWAITING_REVIEW):
                    self.db.update_job(jid, tab_state="dismissed")
                    self.log(f"tab #{jid} closed without submitting; `autopilot open` brings it back")
                continue
            try:
                text = page.evaluate(BODY_TEXT_JS)
            except PlaywrightError:
                continue
            if not self.baseline.get(jid) and CONFIRM_RE.search(text):
                job = self.db.get_job(jid)
                if job and job["status"] != Status.SUBMITTED:
                    self.db.set_status(jid, Status.SUBMITTED, "confirmation page detected after you submitted")
                self.db.update_job(jid, tab_state=None)
                del self.open[jid]
                submitted.append(jid)
                self.log(f"job #{jid} submitted (confirmation page seen)")
        return submitted

    def idle(self, seconds: float) -> None:
        """Wait while letting Playwright process browser events (tab closes, navigation)."""
        page = next((p for p in self.open.values() if not p.is_closed()), None)
        if page is not None:
            try:
                page.wait_for_timeout(seconds * 1000)
                return
            except PlaywrightError:
                pass
        time.sleep(seconds)

    def close(self) -> None:
        if self.ctx is not None:
            try:
                self.ctx.close()
            except PlaywrightError:
                pass
            self.ctx = None


class Worker:
    def __init__(self, cfg: Config, tabs: bool = True):
        self.cfg = cfg
        self.tabs_enabled = tabs
        self.stopping = False
        self._notified_auth: date | None = None

    def log(self, message: str) -> None:
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {message}", flush=True)

    def _stop(self, *_):
        self.stopping = True

    def run(self) -> None:
        cfg = self.cfg
        cfg.ensure_dirs()
        pid_file(cfg).write_text(str(os.getpid()))
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        db = Database(cfg.db_path)
        db.conn.execute("UPDATE jobs SET tab_state = NULL WHERE tab_state IS NOT NULL")  # earlier tabs are gone
        db.conn.commit()
        llm = ClaudeLLM.from_config(cfg)
        http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}, timeout=25, follow_redirects=True
        )
        self.log(
            f"started (pid {os.getpid()}); checking {cfg.capture_address} every {cfg.settings.poll_minutes} min"
            f"{'' if self.tabs_enabled else '; tabs disabled'}"
        )
        try:
            with sync_playwright() as pw:
                headless = HeadlessBrowser(cfg.settings.browser_channel, pw=pw)
                tabs = TabManager(cfg, db, llm, pw, self.log) if self.tabs_enabled else None
                services = Services(
                    cfg=cfg,
                    db=db,
                    llm=llm,
                    fetcher=JDFetcher(http, headless),
                    lookup=ATSLookup(http),
                    renderer=make_renderer(cfg.settings.resume_format, headless),
                    http=http,
                    log=self.log,
                )
                next_mail = next_tabs = 0.0
                try:
                    while not self.stopping:
                        if time.time() >= next_mail:
                            self.cycle(services)
                            next_mail = time.time() + cfg.settings.poll_minutes * 60
                        if tabs is not None:
                            if time.time() >= next_tabs:
                                self.refresh_tabs(tabs)
                                next_tabs = time.time() + TAB_CHECK_SECONDS
                            tabs.watch()
                            tabs.idle(3)
                        else:
                            time.sleep(3)
                finally:
                    if tabs is not None:
                        tabs.close()
                    headless.close()
        finally:
            http.close()
            db.close()
            pid_file(cfg).unlink(missing_ok=True)
            self.log("stopped")

    def cycle(self, services: Services) -> None:
        """Check Gmail, then extract/score/tailor everything waiting."""
        pipeline = Pipeline(services)
        try:
            n = pipeline.ingest(GmailClient.from_config(self.cfg))
            if n:
                self.log(f"read {n} new email(s)")
        except GmailAuthError as e:
            self.log(f"Gmail: {e}")
            if self._notified_auth != date.today():
                notify("Gmail needs re-authorizing: run `autopilot gmail-auth`.")
                self._notified_auth = date.today()
        except Exception as e:  # noqa: BLE001 - network hiccups: try again next cycle
            self.log(f"Gmail check failed: {type(e).__name__}: {e}")
        try:
            summary = pipeline.run()
            if summary.captured or summary.outcomes:
                self.log(summary.line())
        except SetupError as e:
            self.log(f"waiting on setup: {e}")
        except (LLMError, BrowserUnavailable) as e:
            self.log(f"stopped this round: {e}")

    def refresh_tabs(self, tabs: TabManager) -> None:
        try:
            opened = tabs.open_ready()
        except (LLMError, BrowserUnavailable) as e:
            self.log(f"couldn't open tabs: {e}")
            return
        if opened:
            waiting = len(tabs.open)
            notify(f"{opened} new application(s) filled. {waiting} waiting for your review in Chrome.")


def spawn(cfg: Config, tabs: bool = True) -> subprocess.Popen:
    """Start the worker as a detached background process that survives closing the terminal."""
    cfg.ensure_dirs()
    log = open(log_file(cfg), "a", encoding="utf-8")  # noqa: SIM115 - handed to the child process
    cmd = [sys.executable, "-m", "autopilot.cli", "worker"] + ([] if tabs else ["--no-tabs"])
    env = {**os.environ, "AUTOPILOT_HOME": str(cfg.home), "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(
        cmd,
        cwd=cfg.home,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
