"""Pipeline: capture → extract → match → tailor. Progress lives in SQLite, so every run picks up where the last stopped.

Every exit before `tailored` (other than `needs_link`, which waits on you) goes through
`rejections.reject()`, so it shows up in the rejection log.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
from pydantic import ValidationError

from autopilot.config import Config
from autopilot.db import Database, loads, now
from autopilot.extract.job_extractor import extract_job
from autopilot.extract.links import find_urls, normalize_url, not_a_posting_reason, unwrap
from autopilot.extract.screenshot import read_screenshot
from autopilot.headless import BrowserUnavailable
from autopilot.ingest.gmail_client import AttachmentRef, ParsedEmail, safe_filename
from autopilot.llm.claude import LLM, LLMError
from autopilot.match.fit_scorer import score_fit
from autopilot.models import Profile, Reason, Stage, Status
from autopilot.rejections import RunSummary, export_csv, is_overridden, reject
from autopilot.resume.tailor import tailor_resume
from autopilot.textutil import folder_name, hrefs, slug


class SetupError(RuntimeError):
    """Something the user has to fix before the run can continue (profile, keys)."""


@dataclass
class Services:
    cfg: Config
    db: Database
    llm: LLM
    fetcher: object  # .fetch(url) -> Page
    lookup: object  # .find(company, role) -> FoundPosting | None
    renderer: object  # .render(profile, selection, out_pdf, max_pages) -> RenderResult
    http: httpx.Client | None = None
    today: date = field(default_factory=date.today)
    log: Callable[[str], None] = print


def valid_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def with_scheme(url: str) -> str:
    url = url.strip()
    return url if url.lower().startswith(("http://", "https://")) else "https://" + url


class Pipeline:
    def __init__(self, services: Services):
        self.s = services
        self.db = services.db
        self.summary = RunSummary()
        self._profile: Profile | None = None

    # -- setup -----------------------------------------------------------------

    @property
    def profile(self) -> Profile:
        if self._profile is None:
            try:
                profile = self.s.cfg.load_profile()
            except FileNotFoundError as e:
                raise SetupError(str(e)) from e
            except ValidationError as e:
                raise SetupError(f"config/profile.yaml has errors:\n{e}") from e
            problems = profile.placeholder_problems()
            if problems:
                raise SetupError("Finish config/profile.yaml first: " + "; ".join(problems) + ".")
            self._profile = profile
        return self._profile

    # -- capture -----------------------------------------------------------------

    def capture_email(self, email: ParsedEmail, save_image: Callable[[AttachmentRef], Path]) -> int:
        """Create one job per link or screenshot in the email. Safe to repeat for the same email."""
        mid = email.message_id
        self.db.add_email(mid, email.received_at, email.subject, email.body)
        note = email.subject.strip() or None

        links: list[str] = []
        seen: set[str] = set()
        for url in [*find_urls(email.body), *hrefs(email.html)]:
            url = unwrap(url)
            key = normalize_url(url)  # the text and HTML parts usually repeat the same link
            if key not in seen:
                seen.add(key)
                links.append(url)
        candidates = [u for u in links if not not_a_posting_reason(u)]
        ignored = [u for u in links if not_a_posting_reason(u)]

        created = 0
        for url in candidates:
            if self.db.create_job(
                source_key=f"{mid}:{url}", source_kind="link", source_value=url, message_id=mid, notes=note
            ):
                created += 1
        for ref in email.images:
            path = save_image(ref)
            self.db.add_attachment(mid, str(path), ref.mime)
            if self.db.create_job(
                source_key=f"{mid}:{path.name}", source_kind="image", source_value=str(path), message_id=mid, notes=note
            ):
                created += 1

        if not candidates and not email.images:
            if ignored:
                why = (
                    f"Only {not_a_posting_reason(ignored[0])} was sent ({ignored[0]}). Open the story's link sticker "
                    "and send the job link instead"
                )
            else:
                why = "The email had no link or screenshot"
            jid = self.db.create_job(
                source_key=f"{mid}:note", source_kind="note", status=Status.NEEDS_LINK, message_id=mid, notes=note
            )
            if jid:
                self.db.set_status(jid, Status.NEEDS_LINK, f"{why}. Fix with `autopilot set-link {jid} <url>`.")
                created += 1
                self.summary.add("needs_link")

        self.db.mark_email_processed(mid)
        self.summary.captured += created
        return created

    def ingest(self, client) -> int:
        """Capture every unprocessed Gmail message, labelling each processed. Returns the number of emails."""
        client.connect(interactive=False)
        emails = client.fetch_unprocessed(already_done=self.db.email_processed)
        for email in emails:
            folder = self.s.cfg.attachments_dir / safe_filename(email.message_id)
            self.capture_email(
                email, save_image=lambda ref, mid=email.message_id, d=folder: client.download_attachment(mid, ref, d)
            )
            client.mark_processed(email.message_id)
        return len(emails)

    def capture_manual(self, value: str, note: str | None = None) -> int | None:
        """`autopilot add <url-or-screenshot>`: same as emailing it to yourself."""
        mid = f"manual-{uuid.uuid4().hex[:12]}"
        path = Path(value).expanduser()
        if path.exists() and path.is_file():
            dest = self.s.cfg.attachments_dir / mid / safe_filename(path.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            self.db.add_email(mid, now(), note or "", f"(screenshot {path.name})")
            jid = self.db.create_job(
                source_key=f"{mid}:{dest.name}", source_kind="image", source_value=str(dest), message_id=mid, notes=note
            )
        else:
            urls = find_urls(value) or ([value] if "." in value else [])
            if not urls:
                raise ValueError(f"'{value}' isn't a URL or an existing file.")
            self.db.add_email(mid, now(), note or "", value)
            url = unwrap(with_scheme(urls[0]))
            jid = self.db.create_job(
                source_key=f"{mid}:{url}", source_kind="link", source_value=url, message_id=mid, notes=note
            )
        self.db.mark_email_processed(mid)
        if jid:
            self.summary.captured += 1
        return jid

    # -- processing ----------------------------------------------------------------

    def run(self) -> RunSummary:
        """Process every job that is waiting: extract, then score, then tailor."""
        pending = self.db.jobs_with_status(Status.NEW, Status.EXTRACTED, Status.SCORED)
        if pending and not self._online():
            self.s.log("Offline: jobs will be processed on the next run.")
            return self.summary
        try:
            for _ in range(5):  # screenshots can add jobs; loop until none are new
                new = self.db.jobs_with_status(Status.NEW)
                if not new:
                    break
                for job in new:
                    self._guard(Stage.EXTRACT, job, self.extract)
            extracted = self.db.jobs_with_status(Status.EXTRACTED)
            if extracted:
                _ = self.profile  # stop here with a clear message if the profile isn't ready
            for job in extracted:
                self._guard(Stage.MATCH, job, self.match)
            for job in self.db.jobs_with_status(Status.SCORED):
                self._guard(Stage.TAILOR, job, self.tailor)
        finally:
            export_csv(self.db, self.s.cfg.rejected_csv)
        return self.summary

    def _online(self) -> bool:
        if self.s.http is None:
            return True
        try:
            self.s.http.head("https://www.google.com", timeout=6)
            return True
        except httpx.HTTPError:
            return False

    def _guard(self, stage: Stage, job, fn: Callable) -> None:
        """Run one stage for one job. A failure becomes a logged `error`, except fatal ones, which stop the run."""
        try:
            fn(job)
        except (SetupError, BrowserUnavailable):
            raise
        except LLMError as e:
            if e.fatal:
                raise
            self._error(stage, job, e)
        except Exception as e:  # noqa: BLE001 - any failure must be logged, never swallowed
            self._error(stage, job, e)

    def _error(self, stage: Stage, job, err: Exception) -> None:
        current = self.db.get_job(job["id"])
        if current["status"] in (Status.SKIPPED, Status.ERROR):
            return
        detail = f"{type(err).__name__}: {err}"[:600]
        reject(
            self.db, job["id"], stage=stage, reason=Reason.ERROR, detail=detail, restore_to=Status(current["status"])
        )
        self.summary.add(str(Reason.ERROR))
        self.s.log(f"  job {job['id']}: {stage} failed: {detail}")

    def _skip(
        self,
        job_id: int,
        stage: Stage,
        reason: Reason,
        detail: str,
        restore_to: Status | None,
        duplicate_of: int | None = None,
    ) -> None:
        reject(
            self.db, job_id, stage=stage, reason=reason, detail=detail, restore_to=restore_to, duplicate_of=duplicate_of
        )
        self.summary.add(str(reason))

    def _needs_link(self, job_id: int, why: str) -> None:
        self.db.set_status(job_id, Status.NEEDS_LINK, f"{why} Fix with `autopilot set-link {job_id} <url>`.")
        self.summary.add("needs_link")

    def _expired(self, deadline: str | None) -> bool:
        d = valid_date(deadline)
        return bool(d and date.fromisoformat(d) < self.s.today)

    # -- extract -----------------------------------------------------------------

    def extract(self, job) -> None:
        if job["source_kind"] == "image":
            self._extract_image(job)
        elif job["source_kind"] == "link":
            self._extract_link(job, job["source_value"])
        else:
            self._needs_link(job["id"], "Nothing to extract.")

    def _duplicate_by_url(self, job, url_norm: str) -> bool:
        dup = self.db.find_by_url(url_norm, exclude_id=job["id"])
        if dup is None:
            return False
        label = " – ".join(x for x in (dup["company"], dup["role"]) if x) or dup["apply_url"] or ""
        self._skip(
            job["id"],
            Stage.EXTRACT,
            Reason.DUPLICATE,
            f"Same link as job #{dup['id']} ({label})",
            None,
            duplicate_of=dup["id"],
        )
        return True

    def _extract_link(self, job, url: str) -> None:
        jid = job["id"]
        url = unwrap(with_scheme(url))
        if why := not_a_posting_reason(url):
            self._needs_link(jid, f"The link is {why}, not a job posting.")
            return
        if self._duplicate_by_url(job, normalize_url(url)):
            return

        page = self.s.fetcher.fetch(url)
        final = page.url or url
        if why := not_a_posting_reason(final):
            self._needs_link(jid, f"The link led to {why}, not a job posting.")
            return
        norm = normalize_url(final)
        if self._duplicate_by_url(job, norm):
            return
        self.db.update_job(
            jid, apply_url=final, apply_url_norm=norm, ats=page.ats, form_url=page.form_url, jd_text=page.text
        )
        job = self.db.get_job(jid)
        if page.closed and not is_overridden(job, Reason.CLOSED):
            self._skip(jid, Stage.EXTRACT, Reason.CLOSED, page.closed_reason or "The posting is closed", Status.NEW)
            return

        hints = loads(job["hints_json"], {})
        note = " | ".join(x for x in (job["notes"], hints.get("notes")) if x) or None
        data = extract_job(self.s.llm, final, page.text, note=note, today=self.s.today)
        if not data.is_job_posting:
            self._needs_link(jid, f"{final} doesn't look like a single job posting ({page.title or 'no title'}).")
            return
        self.db.update_job(
            jid,
            company=data.company or hints.get("company"),
            role=data.role or hints.get("role"),
            term=data.term or hints.get("term"),
            location=data.location or hints.get("location"),
            remote=data.remote,
            deadline=valid_date(data.deadline) or valid_date(hints.get("deadline")),
            requirements_json=json.dumps(data.requirements),
            eligibility_json=data.eligibility.model_dump_json(),
        )
        job = self.db.get_job(jid)
        if not data.accepting_applications and not is_overridden(job, Reason.CLOSED):
            self._skip(
                jid,
                Stage.EXTRACT,
                Reason.CLOSED,
                data.closed_reason or "The page says applications are closed",
                Status.NEW,
            )
            return
        if self._duplicate_by_identity(job):
            return
        if self._expired(job["deadline"]) and not is_overridden(job, Reason.EXPIRED):
            self._skip(jid, Stage.EXTRACT, Reason.EXPIRED, f"Deadline was {job['deadline']}", Status.EXTRACTED)
            return
        self.db.set_status(jid, Status.EXTRACTED, f"{job['company']} – {job['role']}")

    def _duplicate_by_identity(self, job) -> bool:
        dup = self.db.find_by_identity(job["company"], job["role"], job["term"], exclude_id=job["id"])
        if dup is None:
            return False
        self._skip(
            job["id"],
            Stage.EXTRACT,
            Reason.DUPLICATE,
            f"Same company, role and term as job #{dup['id']} ({dup['company']} – {dup['role']})",
            None,
            duplicate_of=dup["id"],
        )
        return True

    def _extract_image(self, job) -> None:
        jid = job["id"]
        hints = loads(job["hints_json"])
        if hints is None:
            shot = read_screenshot(self.s.llm, Path(job["source_value"]), note=job["notes"], today=self.s.today)
            if not shot.is_job_posting or not shot.jobs:
                self._needs_link(jid, "The screenshot doesn't show a job posting.")
                return
            first, *rest = shot.jobs
            for i, extra in enumerate(rest, start=2):
                self.db.create_job(
                    source_key=f"{job['source_key']}#{i}",
                    source_kind="image",
                    source_value=job["source_value"],
                    message_id=job["message_id"],
                    notes=job["notes"],
                    hints_json=extra.model_dump_json(),
                )
                self.summary.captured += 1
            self.db.update_job(jid, hints_json=first.model_dump_json())
            hints = first.model_dump()

        self.db.update_job(
            jid,
            company=hints.get("company"),
            role=hints.get("role"),
            term=hints.get("term"),
            location=hints.get("location"),
            deadline=valid_date(hints.get("deadline")),
        )
        url = next((with_scheme(u) for u in hints.get("urls") or [] if not not_a_posting_reason(with_scheme(u))), None)
        if url is None:
            found = self.s.lookup.find(hints.get("company"), hints.get("role"))
            if found:
                url = found.url
                self.db.update_job(
                    jid, notes=" | ".join(x for x in (job["notes"], f"found on {found.ats}: {found.title}") if x)
                )
        if url:
            self._extract_link(self.db.get_job(jid), url)
            return

        job = self.db.get_job(jid)
        if self._duplicate_by_identity(job):
            return
        if self._expired(job["deadline"]) and not is_overridden(job, Reason.EXPIRED):
            self._skip(jid, Stage.EXTRACT, Reason.EXPIRED, f"Deadline was {job['deadline']}", Status.NEEDS_LINK)
            return
        who = " – ".join(x for x in (job["company"], job["role"]) if x) or "this screenshot"
        self._needs_link(jid, f"Couldn't find the posting for {who}.")

    # -- match -------------------------------------------------------------------

    def match(self, job) -> None:
        jid = job["id"]
        fit = score_fit(self.s.llm, self.profile, job, self.s.cfg.settings.locations)
        self.db.update_job(jid, fit_score=fit.score, fit_json=fit.model_dump_json())
        job = self.db.get_job(jid)
        if (not fit.eligible or fit.blockers) and not is_overridden(job, Reason.INELIGIBLE):
            self._skip(jid, Stage.MATCH, Reason.INELIGIBLE, "; ".join(fit.blockers) or fit.notes, Status.SCORED)
            return
        threshold = self.s.cfg.settings.min_fit_score
        if fit.score < threshold and not is_overridden(job, Reason.LOW_FIT):
            missing = ", ".join(fit.missing_skills[:6]) or "none listed"
            self._skip(
                jid,
                Stage.MATCH,
                Reason.LOW_FIT,
                f"Score {fit.score} < {threshold}. Missing: {missing}. {fit.notes}",
                Status.SCORED,
            )
            return
        self.db.set_status(jid, Status.SCORED, f"fit {fit.score}")

    # -- tailor ------------------------------------------------------------------

    def _resume_folder(self, job) -> Path:
        """data/resumes/<Company>/. A second role at the same company gets <Company> - <Role>."""
        company = folder_name(job["company"], "Unknown Company")
        role = folder_name(job["role"], "Role")
        names = [company, f"{company} - {role}", f"{company} - {role} ({job['id']})"]
        for name in names:
            folder = self.s.cfg.resumes_dir / name
            if not folder.exists() or not any(folder.iterdir()):
                return folder
            owner = (
                loads((folder / "tailor.json").read_text(encoding="utf-8"), {})
                if (folder / "tailor.json").exists()
                else {}
            )
            if owner.get("job_id") == job["id"]:  # re-tailoring the same job overwrites its own folder
                return folder
        return self.s.cfg.resumes_dir / names[-1]

    def tailor(self, job) -> None:
        jid = job["id"]
        profile = self.profile
        outcome = tailor_resume(self.s.llm, profile, job)
        folder = self._resume_folder(job)
        filename = Path(profile.basics.resume_filename or f"{slug(profile.basics.name)}_Resume").name
        pdf = folder / (filename if filename.lower().endswith(".pdf") else filename + ".pdf")
        max_pages = self.s.cfg.settings.max_resume_pages
        target = " – ".join(x for x in (job["company"], job["role"], job["term"]) if x)
        result = self.s.renderer.render(profile, outcome.selection, pdf, max_pages, target=target)
        sidecar = {
            "job_id": jid,
            "company": job["company"],
            "role": job["role"],
            "generated_at": now(),
            "pages": result.pages,
            "selection": outcome.selection.model_dump(),
            "changes": [c.model_dump() for c in outcome.selection.changes],
            "first_attempt_problems": outcome.first_attempt_problems,
            "validator_fixes": outcome.fixes,
            "reverted_by_verify_pass": outcome.reverted,
            "dropped_to_fit_page": result.dropped,
        }
        (folder / "tailor.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
        self.db.update_job(jid, resume_path=str(pdf), tailor_json=json.dumps(sidecar))
        note = f"resume {pdf.name}, {result.pages} page(s)"
        if result.pages > max_pages:
            note += f" - still over {max_pages} page(s), check it"
        self.db.set_status(jid, Status.TAILORED, note)
        self.summary.add("tailored")
