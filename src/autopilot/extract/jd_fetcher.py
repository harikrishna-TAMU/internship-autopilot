"""Fetch a job description. Public ATS APIs first (Greenhouse, Lever, Ashby, Workday), then the page itself."""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import trafilatura

from autopilot.extract.links import ATSIds, ats_ids, detect_ats
from autopilot.headless import HeadlessBrowser
from autopilot.textutil import html_to_text

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0 Safari/537.36"
)

CLOSED_RE = re.compile(
    r"no longer accepting applications"
    r"|(?:job|position|posting|role|requisition)\s+(?:is|has been)\s+(?:closed|filled|expired|removed)"
    r"|no longer (?:available|open|active|posted)"
    r"|this job (?:posting )?has expired"
    r"|(?:job|page) you(?:'|’| a)re looking for (?:is|was|could|does|has)"
    r"|could not find (?:the|that|this) job"
    r"|applications? (?:are|is) (?:now )?closed",
    re.I,
)

KNOWN_ATS = {
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "icims",
    "smartrecruiters",
    "jobvite",
    "taleo",
    "successfactors",
}


class FetchError(RuntimeError):
    pass


@dataclass
class Page:
    url: str
    title: str
    text: str
    ats: str
    closed: bool = False
    closed_reason: str | None = None
    form_url: str | None = None


def looks_like_prose(text: str) -> bool:
    """False for page code (JSON, scripts) that some career sites embed instead of a readable description."""
    sample = (text or "")[:20000]
    if len(sample) < 200:
        return False
    junk = sum(sample.count(c) for c in '{}[]":;=<>\\') / len(sample)
    letters = sum(ch.isalpha() or ch.isspace() for ch in sample) / len(sample)
    return junk < 0.03 and letters > 0.75


def closed_signal(text: str) -> str | None:
    """Wording that says the posting is closed, if it appears near the top or the page is short."""
    m = CLOSED_RE.search(text)
    if m and (m.start() < 1500 or len(text) < 2500):
        return m.group(0)
    return None


class JDFetcher:
    def __init__(self, http: httpx.Client | None = None, headless: HeadlessBrowser | None = None):
        self.http = http or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=25,
            follow_redirects=True,
        )
        self.headless = headless

    def fetch(self, url: str) -> Page:
        ids = ats_ids(url)
        if ids.board and ids.job_id:
            if ids.ats == "greenhouse":
                return self._greenhouse(url, ids)
            if ids.ats == "lever":
                return self._lever(url, ids)
            if ids.ats == "ashby":
                return self._ashby(url, ids)
        if ids.ats == "workday":
            try:
                return self._workday(url)
            except FetchError:
                pass  # fall back to rendering the page
        return self._generic(url)

    # -- ATS APIs --------------------------------------------------------------

    def _get_json(self, api_url: str) -> dict | list | None:
        """JSON from a public API, or None on 404/410 (posting gone)."""
        try:
            r = self.http.get(api_url, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise FetchError(f"Couldn't reach {urlsplit(api_url).hostname}: {e}") from e
        if r.status_code in (404, 410):
            return None
        if r.status_code >= 400:
            raise FetchError(f"{urlsplit(api_url).hostname} returned HTTP {r.status_code}")
        return r.json()

    def _greenhouse(self, url: str, ids: ATSIds) -> Page:
        form_url = f"https://job-boards.greenhouse.io/embed/job_app?for={ids.board}&token={ids.job_id}"
        data = self._get_json(f"https://boards-api.greenhouse.io/v1/boards/{ids.board}/jobs/{ids.job_id}")
        if data is None:
            return Page(
                url, "", "", "greenhouse", closed=True, closed_reason="Greenhouse says this job no longer exists (404)"
            )
        body = html_to_text(htmllib.unescape(data.get("content") or ""))
        loc = (data.get("location") or {}).get("name") or ""
        text = f"{data.get('title', '')}\nLocation: {loc}\n\n{body}"
        return Page(data.get("absolute_url") or url, data.get("title") or "", text, "greenhouse", form_url=form_url)

    def _lever(self, url: str, ids: ATSIds) -> Page:
        api = "api.eu.lever.co" if ids.region == "eu" else "api.lever.co"
        jobs = "jobs.eu.lever.co" if ids.region == "eu" else "jobs.lever.co"
        data = self._get_json(f"https://{api}/v0/postings/{ids.board}/{ids.job_id}")
        if data is None:
            return Page(
                url, "", "", "lever", closed=True, closed_reason="Lever says this posting no longer exists (404)"
            )
        cats = data.get("categories") or {}
        parts = [
            data.get("text") or "",
            " | ".join(x for x in (cats.get("location"), cats.get("commitment"), cats.get("team")) if x),
            data.get("descriptionPlain") or html_to_text(data.get("description")),
        ]
        for lst in data.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
        parts.append(data.get("additionalPlain") or html_to_text(data.get("additional")))
        return Page(
            data.get("hostedUrl") or url,
            data.get("text") or "",
            "\n\n".join(p for p in parts if p),
            "lever",
            form_url=f"https://{jobs}/{ids.board}/{ids.job_id}/apply",
        )

    def _ashby(self, url: str, ids: ATSIds) -> Page:
        data = self._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{ids.board}?includeCompensation=true")
        jobs = (data or {}).get("jobs", []) if isinstance(data, dict) else []
        job = next((j for j in jobs if str(j.get("id")) == ids.job_id), None)
        if job is None:
            return Page(url, "", "", "ashby", closed=True, closed_reason="Ashby no longer lists this job")
        text = f"{job.get('title', '')}\nLocation: {job.get('location', '')}\n\n" + (
            job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml"))
        )
        return Page(
            job.get("jobUrl") or url,
            job.get("title") or "",
            text,
            "ashby",
            form_url=f"https://jobs.ashbyhq.com/{ids.board}/{ids.job_id}/application",
        )

    def _workday(self, url: str) -> Page:
        parts = urlsplit(url)
        host = parts.hostname or ""
        segs = [s for s in parts.path.split("/") if s]
        if segs and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]):
            segs = segs[1:]
        if "job" not in segs or segs.index("job") == 0:
            raise FetchError("not a Workday job URL")
        site = segs[0]
        rest = "/".join(segs[segs.index("job") + 1 :])
        tenant = host.split(".")[0]
        data = self._get_json(f"https://{host}/wday/cxs/{tenant}/{site}/job/{rest}")
        if data is None:
            return Page(
                url, "", "", "workday", closed=True, closed_reason="Workday says this job no longer exists (404)"
            )
        info = data.get("jobPostingInfo") or {}
        if not info:
            raise FetchError("Workday returned no job details")
        text = f"{info.get('title', '')}\nLocation: {info.get('location', '')}\n\n{html_to_text(info.get('jobDescription'))}"
        return Page(info.get("externalUrl") or url, info.get("title") or "", text, "workday")

    # -- any other page --------------------------------------------------------

    def _generic(self, url: str) -> Page:
        ats = detect_ats(url)
        final_url, markup, status = url, "", 0
        try:
            r = self.http.get(url)
            final_url, markup, status = str(r.url), r.text, r.status_code
        except httpx.HTTPError as e:
            if not self.headless:
                raise FetchError(f"Couldn't load {url}: {e}") from e

        # A shortener or company page may redirect to an ATS with a public API.
        final_ids = ats_ids(final_url)
        if (
            final_url != url
            and final_ids.board
            and final_ids.job_id
            and final_ids.ats in ("greenhouse", "lever", "ashby")
        ):
            return self.fetch(final_url)
        # Company page embedding a Greenhouse job (…?gh_jid=123): find the board, then use the API.
        if final_ids.ats == "greenhouse" and final_ids.job_id and not final_ids.board:
            board = self._greenhouse_board(final_url, markup, final_ids.job_id)
            if board:
                return self._greenhouse(final_url, ATSIds("greenhouse", board, final_ids.job_id))

        ats = detect_ats(final_url) if ats == "other" else ats
        if status in (404, 410) and ats in KNOWN_ATS:
            return Page(final_url, "", "", ats, closed=True, closed_reason=f"HTTP {status} from the job page")

        text = self._extract_text(markup, final_url) if status and status < 400 else ""
        title = _title(markup)
        good = looks_like_prose(text)
        if (len(text) < 1500 or not good) and self.headless:
            # Thin text, or page code instead of prose, means the description is rendered by JavaScript.
            r_url, r_markup, r_title = self.headless.render(url, wait_ms=4000)
            r_text = self._extract_text(r_markup, r_url)
            r_good = looks_like_prose(r_text)
            if (r_good and not good) or (r_good == good and len(r_text) > len(text)):
                final_url, text, title = r_url, r_text, r_title or title
        if status >= 400 and len(text) < 200:
            raise FetchError(f"{urlsplit(url).hostname} returned HTTP {status}")
        if len(text) < 200:
            raise FetchError("Couldn't read a job description on the page (it may need a login).")

        reason = closed_signal(text)
        return Page(final_url, title, text, detect_ats(final_url), closed=bool(reason), closed_reason=reason)

    def _greenhouse_board(self, url: str, markup: str, job_id: str) -> str | None:
        """The Greenhouse board token for a company-hosted job: from the page's embed script, else guessed from the domain."""
        m = re.search(r"greenhouse\.io/embed/job_(?:board|app)(?:/js)?\?for=([A-Za-z0-9_-]+)", markup or "")
        if m:
            return m.group(1)
        parts = [
            p
            for p in (urlsplit(url).hostname or "").lower().split(".")
            if p not in ("www", "careers", "jobs", "boards")
        ]
        guesses: list[str] = []
        for label in parts[:-1]:
            for g in (label, label.replace("-", "")):
                if g and g not in guesses:
                    guesses.append(g)
        for g in guesses[:4]:
            try:
                r = self.http.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{g}/jobs/{job_id}",
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                return g
        return None

    @staticmethod
    def _extract_text(markup: str, url: str) -> str:
        if not markup:
            return ""
        text = trafilatura.extract(markup, url=url, include_tables=True, include_comments=False, favor_recall=True)
        if not text or len(text) < 300:
            text = html_to_text(markup)
        return text or ""


def _title(markup: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", markup or "", re.I | re.S)
    return htmllib.unescape(" ".join(m.group(1).split())) if m else ""
