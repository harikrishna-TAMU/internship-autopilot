"""`autopilot` command line (Typer)."""

from __future__ import annotations

import csv
import os
import shutil
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import httpx
import typer
import yaml
from rich.console import Console
from rich.table import Table

from autopilot.config import AppSettings, Config, load_config
from autopilot.db import Database, loads
from autopilot.extract.ats_lookup import ATSLookup
from autopilot.extract.jd_fetcher import USER_AGENT, JDFetcher
from autopilot.headless import BrowserUnavailable, HeadlessBrowser
from autopilot.ingest.gmail_client import GmailAuthError, GmailClient
from autopilot.llm import make_llm as _make_llm
from autopilot.llm.claude import LLMError
from autopilot.models import Reason, Stage, Status
from autopilot.pipeline import Pipeline, Services, SetupError
from autopilot.rejections import export_csv, parse_since, reject, restore
from autopilot.resume.render import make_renderer

app = typer.Typer(
    help="ApplyPilot: emailed job links → job list → tailored resumes → pre-filled Chrome tabs. Never submits.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

ACTIVE = [Status.NEW, Status.EXTRACTED, Status.NEEDS_LINK, Status.SCORED, Status.TAILORED, Status.AWAITING_REVIEW]
STATUS_STYLE = {
    Status.TAILORED: "green",
    Status.AWAITING_REVIEW: "bold green",
    Status.SUBMITTED: "cyan",
    Status.NEEDS_LINK: "yellow",
    Status.SKIPPED: "dim",
    Status.ERROR: "red",
}


def fail(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(1)


@contextmanager
def opened() -> Iterator[tuple[Config, Database]]:
    cfg = load_config()
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        yield cfg, db
    finally:
        db.close()


def make_llm(cfg: Config):
    return _make_llm(cfg)


def uses_subscription(cfg: Config) -> bool:
    return (cfg.env.llm_backend or "api").strip().lower() in ("claude-code", "claude_code", "subscription")


@contextmanager
def pipeline_for(cfg: Config, db: Database) -> Iterator[Pipeline]:
    http = httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}, timeout=25, follow_redirects=True
    )
    headless = HeadlessBrowser(cfg.settings.browser_channel)
    services = Services(
        cfg=cfg,
        db=db,
        llm=make_llm(cfg),
        fetcher=JDFetcher(http, headless),
        lookup=ATSLookup(http),
        renderer=make_renderer(cfg.settings.resume_format, headless),
        http=http,
        log=lambda m: console.print(m),
    )
    try:
        yield Pipeline(services)
    finally:
        headless.close()
        http.close()


def run_pipeline(pipeline: Pipeline) -> None:
    """Process pending jobs and print the one-line summary; stop cleanly on setup/fatal errors."""
    try:
        summary = pipeline.run()
    except SetupError as e:
        console.print(f"[yellow]{e}[/yellow]\nJobs were saved; they'll continue on the next run.")
        console.print(pipeline.summary.line())
        return
    except (LLMError, BrowserUnavailable) as e:
        console.print(f"[red]{e}[/red]\nStopped early; the next run continues from here.")
        console.print(pipeline.summary.line())
        return
    console.print(f"[bold]{summary.line()}[/bold]")


def gmail_client(cfg: Config) -> GmailClient:
    return GmailClient.from_config(cfg)


# --------------------------------------------------------------------------- setup


@app.command()
def init() -> None:
    """Create config files, data folders and the database."""
    cfg = load_config()
    cfg.ensure_dirs()
    created = []
    settings = cfg.config_dir / "settings.yaml"
    if not settings.exists():
        settings.write_text(yaml.safe_dump(AppSettings().model_dump(), sort_keys=False), encoding="utf-8")
        created.append(str(settings.relative_to(cfg.home)))
    if not cfg.profile_path.exists():
        shutil.copyfile(cfg.config_dir / "profile.example.yaml", cfg.profile_path)
        created.append(str(cfg.profile_path.relative_to(cfg.home)))
    env = cfg.home / ".env"
    if not env.exists() and (cfg.home / ".env.example").exists():
        shutil.copyfile(cfg.home / ".env.example", env)
        created.append(".env")
    Database(cfg.db_path).close()
    for c in created:
        console.print(f"created {c}")
    console.print("Ready. Next: fill in config/profile.yaml and .env, then run `autopilot doctor`.")


@app.command()
def doctor() -> None:
    """Check that everything the pipeline needs is in place."""
    cfg = load_config()
    ok = True

    def check(passed: bool, label: str, fix: str = "") -> None:
        nonlocal ok
        ok &= passed
        console.print(
            f"{'[green]✓[/green]' if passed else '[red]✗[/red]'} {label}"
            + ("" if passed or not fix else f"  [dim]→ {fix}[/dim]")
        )

    if uses_subscription(cfg):
        llm = make_llm(cfg)
        problem = llm.verify_access()
        check(problem is None, "Claude via Claude Code (your Claude plan, no API credits)", problem or "")
        console.print(f"  account: {llm.describe()}")
    elif cfg.env.anthropic_api_key:
        problem = make_llm(cfg).verify_access()
        check(problem is None, f"Claude API key works (model: {cfg.env.claude_model})", problem or "")
    else:
        check(
            False, "ANTHROPIC_API_KEY set in .env", "get one at console.anthropic.com, or set LLM_BACKEND=claude-code"
        )
    check(
        bool(cfg.capture_address),
        f"GMAIL_ADDRESS set (capture address: {cfg.capture_address or '-'})",
        "add GMAIL_ADDRESS to .env",
    )
    check(cfg.credentials_path.exists(), "credentials.json present", "README → Gmail setup")
    check(cfg.token_path.exists(), "Gmail authorized (token.json)", "run `autopilot gmail-auth`")
    try:
        profile = cfg.load_profile()
        problems = profile.placeholder_problems()
        check(
            not problems,
            f"config/profile.yaml valid ({len(profile.experience)} experience, {len(profile.projects)} projects, "
            f"{len(profile.bullet_index())} bullets)",
            "; ".join(problems),
        )
        console.print(f"  email on applications: {profile.basics.email}")
    except Exception as e:  # noqa: BLE001
        check(False, "config/profile.yaml valid", str(e).splitlines()[0])
    try:
        h = HeadlessBrowser(cfg.settings.browser_channel)
        h.pdf("<p>ok</p>")
        h.close()
        check(True, f"Chrome available (channel: {cfg.settings.browser_channel})")
    except Exception as e:  # noqa: BLE001
        check(False, "Chrome available", str(e).splitlines()[0])
    raise typer.Exit(0 if ok else 1)


def chrome_launch_command(profile_dir: str, url: str) -> list[str] | None:
    """A normal (non-automated) Chrome on the review profile; it runs beside your everyday Chrome."""
    import platform
    import shutil as _shutil

    args = [f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check", url]
    if platform.system() == "Darwin" and os.path.exists("/Applications/Google Chrome.app"):
        return ["open", "-n", "-W", "-a", "Google Chrome", "--args", *args]
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium"):
        if found := _shutil.which(name):
            return [found, *args]
    return None


@app.command("browser-setup")
def browser_setup() -> None:
    """One time: open the review Chrome window normally to sign into Chrome and your job sites."""
    import subprocess

    from autopilot.worker import running_pid

    cfg = load_config()
    cfg.ensure_dirs()
    if running_pid(cfg):
        fail("Stop the background worker first (`autopilot stop`): Chrome can't open the review profile twice.")
    cmd = chrome_launch_command(str(cfg.chrome_profile_dir), "chrome://settings/people")
    if cmd is None:
        fail("Google Chrome wasn't found.")
    console.print(
        "[bold]A separate Chrome window is opening[/bold] (the review profile, not your everyday Chrome). In it:\n"
        "  1. Click the profile icon (top right) → [bold]Turn on sync[/bold] → sign in with your Google account,\n"
        "     and keep [bold]Passwords[/bold] on. Chrome can then offer your saved passwords on login pages.\n"
        "  2. Optional: log into job sites you use (Workday portals, etc.) so they stay signed in.\n"
        "  3. Quit that window with [bold]⌘Q[/bold] while it's in front (your everyday Chrome stays open).\n"
        "ApplyPilot never reads or types your passwords; Chrome offers them and you click to use them."
    )
    subprocess.run(cmd, check=False)
    console.print("Done. Start ApplyPilot with `uv run autopilot start`.")


@app.command("gmail-auth")
def gmail_auth() -> None:
    """Authorize Gmail (opens a browser once) and create the Jobs labels."""
    cfg = load_config()
    client = gmail_client(cfg)
    try:
        account = client.connect(interactive=True)
        client.ensure_labels()
    except GmailAuthError as e:
        fail(str(e))
    console.print(
        f"Authorized [bold]{account}[/bold]; labels '{cfg.settings.gmail_label}' and '{cfg.settings.processed_label}' are ready."
    )
    if cfg.env.gmail_address and account.lower() != cfg.env.gmail_address.lower():
        console.print(
            f"[yellow]Note: GMAIL_ADDRESS in .env is {cfg.env.gmail_address}, but you authorized {account}.[/yellow]"
        )
    console.print(f"Send job links and screenshots to: [bold]{cfg.capture_address}[/bold]")


# --------------------------------------------------------------------------- background worker


@app.command()
def start(
    tabs: bool = typer.Option(
        True, "--tabs/--no-tabs", help="Keep filled Chrome tabs open (default), or only tailor resumes."
    ),
) -> None:
    """Run everything in the background: check Gmail, tailor, and keep filled tabs open for review."""
    from autopilot.worker import log_file, running_pid, spawn

    cfg = load_config()
    cfg.ensure_dirs()
    if pid := running_pid(cfg):
        console.print(f"Already running (pid {pid}). `autopilot status` shows what it's doing.")
        return
    if not uses_subscription(cfg) and not cfg.env.anthropic_api_key:
        fail("Add ANTHROPIC_API_KEY to .env first, or set LLM_BACKEND=claude-code to use your Claude plan.")
    llm = make_llm(cfg)
    if problem := llm.verify_access():
        fail(problem)
    if uses_subscription(cfg):
        console.print(f"Claude: your plan via Claude Code ({llm.describe()})")
    if not cfg.token_path.exists():
        fail("Gmail isn't authorized yet. Run `autopilot gmail-auth` once, then `autopilot start`.")
    try:
        problems = cfg.load_profile().placeholder_problems()
    except Exception as e:  # noqa: BLE001
        problems = [str(e).splitlines()[0]]
    if problems:
        console.print(
            f"[yellow]Note: {'; '.join(problems)}. Jobs will be captured, but resumes wait until the profile is done.[/yellow]"
        )

    proc = spawn(cfg, tabs)
    time.sleep(3)
    if proc.poll() is not None:
        tail = log_file(cfg).read_text(encoding="utf-8").splitlines()[-15:]
        fail("The background worker stopped right away:\n" + "\n".join(tail))
    console.print(f"[green]Running in the background[/green] (pid {proc.pid}). You can close this terminal.")
    console.print(f"Send job links and screenshots to [bold]{cfg.capture_address}[/bold].")
    if tabs:
        console.print(
            "Filled applications open as tabs in a separate Chrome window, while you're not using that window.\n"
            "Review each tab and click Submit. The job is marked submitted when the site confirms it."
        )
    console.print("[dim]`autopilot status` · `autopilot stop` · log: data/autopilot.log[/dim]")


@app.command()
def stop(yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation.")) -> None:
    """Stop the background worker (closes its Chrome window)."""
    from autopilot.worker import running_pid

    cfg = load_config()
    pid = running_pid(cfg)
    if not pid:
        console.print("Not running.")
        return
    if not yes and not typer.confirm(
        "This closes the review Chrome window; anything typed in unsubmitted tabs is lost. Stop?"
    ):
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.5)
        if not running_pid(cfg):
            console.print("Stopped.")
            return
    os.kill(pid, signal.SIGKILL)
    console.print("Stopped (forced).")


@app.command()
def status() -> None:
    """Is the background worker running, and what's waiting for you."""
    from autopilot.worker import log_file, running_pid

    with opened() as (cfg, db):
        pid = running_pid(cfg)
        console.print(
            f"Worker: [green]running[/green] (pid {pid})"
            if pid
            else "Worker: [yellow]not running[/yellow] (`autopilot start`)"
        )
        counts = {
            s: len(db.jobs_with_status(s))
            for s in (
                Status.TAILORED,
                Status.AWAITING_REVIEW,
                Status.NEEDS_LINK,
                Status.NEW,
                Status.EXTRACTED,
                Status.SCORED,
            )
        }
        open_tabs = db.conn.execute("SELECT COUNT(*) FROM jobs WHERE tab_state = 'open'").fetchone()[0]
        later = db.conn.execute("SELECT COUNT(*) FROM jobs WHERE tab_state = 'dismissed'").fetchone()[0]
        console.print(
            f"Waiting for your review: {counts[Status.TAILORED] + counts[Status.AWAITING_REVIEW]} "
            f"({open_tabs} open in Chrome, {later} closed for later)"
        )
        console.print(
            f"In progress: {counts[Status.NEW] + counts[Status.EXTRACTED] + counts[Status.SCORED]}   "
            f"Need a link: {counts[Status.NEEDS_LINK]}"
        )
        if log_file(cfg).exists():
            console.print("[dim]Recent log:[/dim]")
            for line in log_file(cfg).read_text(encoding="utf-8").splitlines()[-8:]:
                console.print(f"  [dim]{line}[/dim]")


@app.command("open")
def open_tabs(
    ids: str | None = typer.Option(None, "--ids", help="Comma-separated job ids; default: all waiting."),
) -> None:
    """Bring back tabs you closed for later (the background worker reopens them within ~20 s)."""
    from autopilot.worker import running_pid

    with opened() as (cfg, db):
        if not running_pid(cfg):
            fail(
                "The background worker isn't running. Use `autopilot start`, or `autopilot fill` for a one-off session."
            )
        sql = "UPDATE jobs SET tab_state = NULL WHERE tab_state = 'dismissed' AND status IN ('tailored', 'awaiting_review')"
        args: list = []
        if ids:
            id_list = [int(x) for x in ids.split(",") if x.strip()]
            sql += f" AND id IN ({', '.join('?' for _ in id_list)})"
            args = id_list
        n = db.conn.execute(sql, args).rowcount
        db.conn.commit()
        console.print(f"{n} tab(s) will reopen shortly." if n else "No closed tabs to reopen.")


@app.command(hidden=True)
def worker(tabs: bool = typer.Option(True, "--tabs/--no-tabs")) -> None:
    """The background process behind `autopilot start` (runs in the foreground if called directly)."""
    from autopilot.worker import Worker, running_pid

    cfg = load_config()
    if (pid := running_pid(cfg)) and pid != os.getpid():
        fail(f"Already running (pid {pid}).")
    Worker(cfg, tabs=tabs).run()


# --------------------------------------------------------------------------- pipeline


def _ingest_once(cfg: Config, db: Database) -> None:
    with pipeline_for(cfg, db) as pipeline:
        pipeline.ingest(gmail_client(cfg))
        run_pipeline(pipeline)


@app.command()
def ingest(
    watch: bool = typer.Option(False, "--watch", help="Keep running; check Gmail every poll_minutes."),
) -> None:
    """Pull new emails → extract → score → tailor."""
    from autopilot.worker import running_pid

    with opened() as (cfg, db):
        if running_pid(cfg):
            fail("The background worker is already checking Gmail (`autopilot status`).")
        if not watch:
            try:
                _ingest_once(cfg, db)
            except GmailAuthError as e:
                fail(str(e))
            except Exception as e:  # noqa: BLE001 - e.g. no network or a Google API error
                fail(f"{type(e).__name__}: {e}")
            return
        minutes = cfg.settings.poll_minutes
        console.print(f"Watching {cfg.capture_address} every {minutes} min. Ctrl+C to stop.")
        while True:
            console.print(f"[dim]{time.strftime('%H:%M')} checking Gmail…[/dim]")
            try:
                _ingest_once(cfg, db)
            except GmailAuthError as e:
                fail(str(e))
            except Exception as e:  # noqa: BLE001 - keep watching through network hiccups
                console.print(f"[red]{type(e).__name__}: {e}[/red] (will retry)")
            time.sleep(minutes * 60)


@app.command()
def run() -> None:
    """Process jobs already captured (no Gmail check)."""
    with opened() as (cfg, db), pipeline_for(cfg, db) as pipeline:
        run_pipeline(pipeline)


@app.command()
def add(
    value: str = typer.Argument(..., help="A job URL or a screenshot file."),
    note: str | None = typer.Option(None, "--note", help="Optional note, like the email subject."),
) -> None:
    """Capture a link or screenshot directly (same as emailing it), then process it."""
    with opened() as (cfg, db), pipeline_for(cfg, db) as pipeline:
        try:
            jid = pipeline.capture_manual(value, note)
        except ValueError as e:
            fail(str(e))
        console.print(f"Captured as job #{jid}." if jid else "Already captured.")
        run_pipeline(pipeline)


# --------------------------------------------------------------------------- review


def _deadline_cell(deadline: str | None) -> str:
    if not deadline:
        return "-"
    try:
        days = (date.fromisoformat(deadline) - date.today()).days
    except ValueError:
        return deadline
    if days < 0:
        return f"[dim]{deadline}[/dim]"
    if days <= 3:
        return f"[bold red]{deadline} ({days}d)[/bold red]"
    return deadline


@app.command("list")
def list_jobs(
    status: str | None = typer.Option(None, "--status", help="Only this status."),
    all_: bool = typer.Option(False, "--all", help="Include submitted, skipped and errored jobs."),
) -> None:
    """Jobs sorted by deadline (soonest first). Red = due within 3 days."""
    with opened() as (cfg, db):
        if status and status not in {s.value for s in Status}:
            fail(f"Unknown status '{status}'. One of: {', '.join(s.value for s in Status)}")
        statuses = [status] if status else (None if all_ else [s.value for s in ACTIVE])
        rows = db.list_jobs(statuses)
        table = Table(show_lines=False)
        for col in ("id", "company", "role", "deadline", "score", "status", "note"):
            table.add_column(col, overflow="fold")
        for r in rows:
            note = ""
            if r["status"] in (Status.NEEDS_LINK, Status.SKIPPED, Status.ERROR, Status.AWAITING_REVIEW):
                ev = db.last_event(r["id"])
                note = (ev["detail"] or "")[:90] if ev else ""
            style = STATUS_STYLE.get(r["status"], "")
            table.add_row(
                str(r["id"]),
                r["company"] or "-",
                r["role"] or (r["source_value"] or "-")[:50],
                _deadline_cell(r["deadline"]),
                str(r["fit_score"]) if r["fit_score"] is not None else "-",
                f"[{style}]{r['status']}[/{style}]" if style else r["status"],
                note,
            )
        console.print(table if rows else "No active jobs.")
        if not status and not all_:
            hidden = len(db.list_jobs([Status.SUBMITTED, Status.SKIPPED, Status.ERROR]))
            if hidden:
                console.print(
                    f"[dim]{hidden} submitted/skipped/errored job(s) hidden: `autopilot list --all`, or `autopilot rejected` for reasons.[/dim]"
                )


@app.command()
def show(job_id: int) -> None:
    """Job details: JD summary, fit notes, resume changes, drafted answers, history."""
    with opened() as (cfg, db):
        j = db.get_job(job_id)
        if j is None:
            fail(f"No job #{job_id}.")
        console.rule(f"#{j['id']} {j['company'] or '?'} – {j['role'] or '?'}")
        for k in ("status", "term", "location", "remote", "deadline", "ats", "apply_url", "resume_path", "notes"):
            if j[k]:
                console.print(f"[bold]{k}:[/bold] {j[k]}")
        reqs = loads(j["requirements_json"], [])
        if reqs:
            console.print("[bold]requirements:[/bold] " + "; ".join(reqs[:8]))
        fit = loads(j["fit_json"])
        if fit:
            console.print(f"[bold]fit:[/bold] {fit['score']}  {fit['notes']}")
            if fit.get("blockers"):
                console.print(f"[bold]blockers:[/bold] {'; '.join(fit['blockers'])}")
            console.print(
                f"[bold]matched:[/bold] {', '.join(fit['matched_skills'])}  [bold]missing:[/bold] {', '.join(fit['missing_skills'])}"
            )
        tailor = loads(j["tailor_json"])
        if tailor:
            console.print("[bold]resume changes:[/bold]")
            for c in tailor.get("changes", []):
                console.print(f"  - {c['source_id']}: {c['why']}")
            for r in tailor.get("reverted_by_verify_pass", []):
                console.print(f"  [yellow]reverted {r['source_id']}[/yellow]: {r['problem']}")
            if tailor.get("dropped_to_fit_page"):
                console.print(f"  dropped to fit one page: {', '.join(tailor['dropped_to_fit_page'])}")
        answers = loads(j["answers_json"])
        if answers:
            console.print("[bold]application form[/bold] (what ApplyPilot did vs. what the site already had):")
            for f in answers.get("filled", []):
                console.print(f"  [green]filled by ApplyPilot[/green]  {f['label'][:70]}: {f.get('value')}")
            for d in answers.get("drafted", []):
                console.print(f"  [yellow]drafted, check it[/yellow]     {d['label'][:70]}:\n      {d['value']}")
            for left in answers.get("left_for_you", []):
                req = " (required)" if left.get("required") else ""
                console.print(f"  [red]left for you{req}[/red]  {left['label'][:70]}: {left['reason']}")
            for k in answers.get("kept", []):
                console.print(f"  [dim]already filled by the site/you, kept[/dim]  {k['label'][:70]}")
        console.print("[bold]history:[/bold]")
        for e in db.events_for(job_id):
            console.print(f"  {e['at']}  {e['from_status'] or '-'} → {e['to_status']}  {e['detail'] or ''}")


@app.command()
def fill(
    ids: str | None = typer.Option(None, "--ids", help="Comma-separated job ids, e.g. 3,5,8"),
    no_wait: bool = typer.Option(False, "--no-wait", help="Close Chrome when done instead of waiting."),
) -> None:
    """Open + fill tabs in Chrome for tailored jobs. Stops before submit; you submit."""
    from autopilot.fill.browser import fill_jobs, select_jobs
    from autopilot.worker import running_pid

    with opened() as (cfg, db):
        if running_pid(cfg):
            fail(
                "The background worker is running and already opens tabs for you. "
                "Use `autopilot open` to bring back tabs you closed, or `autopilot stop` first."
            )
        try:
            id_list = [int(x) for x in ids.split(",") if x.strip()] if ids else None
            jobs = select_jobs(db, id_list, cfg.settings.max_tabs)
        except ValueError as e:
            fail(str(e))
        if not jobs:
            console.print("Nothing to fill: no jobs are in status 'tailored'.")
            return
        try:
            results = fill_jobs(cfg, db, make_llm(cfg), jobs, log=lambda m: console.print(m), wait=not no_wait)
        except (BrowserUnavailable, LLMError) as e:
            fail(str(e))
        table = Table("id", "company", "role", "result")
        for r in results:
            table.add_row(str(r.job_id), r.company, r.role, r.outcome)
        console.print(table)


@app.command()
def mark(
    job_id: int,
    status: str = typer.Argument(..., help="submitted | skipped | needs_link"),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Record what you did with a job."""
    with opened() as (cfg, db):
        j = db.get_job(job_id)
        if j is None:
            fail(f"No job #{job_id}.")
        if status == "submitted":
            db.set_status(job_id, Status.SUBMITTED, note or "submitted by you")
        elif status == "skipped":
            if j["status"] in (Status.SKIPPED, Status.ERROR):
                fail(f"Job #{job_id} is already {j['status']}.")
            reject(
                db,
                job_id,
                stage=Stage.REVIEW,
                reason=Reason.USER_SKIPPED,
                detail=note or "skipped by you",
                restore_to=Status(j["status"]),
            )
            export_csv(db, cfg.rejected_csv)
        elif status == "needs_link":
            db.set_status(job_id, Status.NEEDS_LINK, note or "marked by you")
        else:
            fail("Status must be submitted, skipped or needs_link.")
        console.print(f"Job #{job_id} → {status}")


@app.command("set-link")
def set_link(job_id: int, url: str) -> None:
    """Give a needs_link (or any unfinished) job its posting URL and process it."""
    with opened() as (cfg, db):
        j = db.get_job(job_id)
        if j is None:
            fail(f"No job #{job_id}.")
        if j["status"] in (Status.SUBMITTED,):
            fail(f"Job #{job_id} is already submitted.")
        rej = db.open_rejection(job_id)
        if rej is not None:
            db.mark_overridden(rej["id"])  # the new link supersedes the old skip
        db.update_job(
            job_id,
            source_kind="link",
            source_value=url.strip(),
            apply_url=None,
            apply_url_norm=None,
            form_url=None,
            duplicate_of=None,
        )
        db.set_status(job_id, Status.NEW, f"link set: {url.strip()}")
        with pipeline_for(cfg, db) as pipeline:
            run_pipeline(pipeline)


@app.command()
def rejected(
    reason: str | None = typer.Option(
        None, "--reason", help="duplicate | expired | closed | ineligible | low_fit | user_skipped | error"
    ),
    since: str | None = typer.Option(None, "--since", help="7d, 12h, 2w or YYYY-MM-DD"),
) -> None:
    """The rejection log: every job the pipeline dropped, and why. Newest first."""
    with opened() as (cfg, db):
        if reason and reason not in {r.value for r in Reason}:
            fail(f"Unknown reason '{reason}'.")
        try:
            rows = db.rejections(reason=reason, since=parse_since(since))
        except ValueError as e:
            fail(str(e))
        table = Table("date", "job", "company", "role", "stage", "reason", "detail")
        for r in rows:
            reason_cell = r["reason_code"] + (" [dim](restored)[/dim]" if r["overridden_at"] else "")
            table.add_row(
                r["at"][:16].replace("T", " "),
                str(r["job_id"] or "-"),
                r["company"] or "-",
                r["role"] or "-",
                r["stage"],
                reason_cell,
                r["detail"],
            )
        console.print(table if rows else "Nothing rejected" + (" for that filter." if reason or since else " yet."))
        export_csv(db, cfg.rejected_csv)


@app.command("restore")
def restore_cmd(job_id: int) -> None:
    """Override a skip (or retry an error) and put the job back in the pipeline."""
    with opened() as (cfg, db):
        try:
            msg = restore(db, job_id)
        except ValueError as e:
            fail(str(e))
        export_csv(db, cfg.rejected_csv)
        console.print(msg + " Run `autopilot run` to continue it now.")


@app.command()
def export(csv_: bool = typer.Option(True, "--csv", help="Write data/jobs.csv and data/rejected.csv.")) -> None:
    """Export the job list and rejection log as CSV (opens in Excel or Google Sheets)."""
    with opened() as (cfg, db):
        rows = db.list_jobs()
        cols = [
            "id",
            "company",
            "role",
            "term",
            "location",
            "deadline",
            "fit_score",
            "status",
            "apply_url",
            "resume_path",
            "created_at",
        ]
        path = cfg.data_dir / "jobs.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
        n = export_csv(db, cfg.rejected_csv)
        console.print(f"Wrote {path} ({len(rows)} jobs) and {cfg.rejected_csv} ({n} rejections).")


if __name__ == "__main__":
    app()
