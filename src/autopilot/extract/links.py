"""URL handling: find links in an email, unwrap redirect wrappers, normalize for dedupe, detect the ATS."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(r"""(?i)\b((?:https?://|www\.)[^\s<>"'`]+)""")
TRAILING_PUNCT = ".,;:!?)]}>'\"»”’"

# Query parameters that only track where a click came from.
TRACKING_PARAMS = {
    "gh_src",
    "source",
    "src",
    "ref",
    "referrer",
    "referer",
    "lever-source",
    "lever-origin",
    "lever-source[]",
    "trk",
    "trackingid",
    "refid",
    "fbclid",
    "gclid",
    "igshid",
    "igsh",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
    "si",
    "share",
    "utm",
    "ashby_jid_source",
    "codes",
    "jobsource",
}

SHORTENER_HOSTS = {
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "lnkd.in",
    "buff.ly",
    "ow.ly",
    "rebrand.ly",
    "shorturl.at",
    "cutt.ly",
    "tiny.cc",
    "is.gd",
    "rb.gy",
    "t.ly",
    "s.id",
    "l.instagram.com",
    "goo.gl",
    "bitly.com",
}

# Links that are never a job posting. Captured alone, they become `needs_link` with an explanation.
NOT_A_POSTING_HOSTS = {
    "instagram.com": "an Instagram link",
    "linktr.ee": "a Linktree page",
    "beacons.ai": "a link-in-bio page",
    "stan.store": "a link-in-bio page",
    "linkin.bio": "a link-in-bio page",
    "tiktok.com": "a TikTok link",
    "youtube.com": "a YouTube link",
    "youtu.be": "a YouTube link",
    "facebook.com": "a Facebook link",
    "x.com": "an X/Twitter link",
    "twitter.com": "an X/Twitter link",
    "threads.net": "a Threads link",
    "threads.com": "a Threads link",
    "substack.com": "a Substack post",
}


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def find_urls(text: str) -> list[str]:
    """URLs in free text, in order, without duplicates or trailing punctuation."""
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_RE.finditer(text or ""):
        url = m.group(1).rstrip(TRAILING_PUNCT)
        if url.lower().startswith("www."):
            url = "https://" + url
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def unwrap(url: str) -> str:
    """Undo redirect wrappers (Instagram, Google, Facebook, LinkedIn, Outlook Safe Links)."""
    for _ in range(5):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        inner = None
        if host == "l.instagram.com" or host == "l.facebook.com":
            inner = params.get("u")
        elif host.endswith("google.com") and parts.path == "/url":
            inner = params.get("q") or params.get("url")
        elif host.endswith("safelinks.protection.outlook.com"):
            inner = params.get("url")
        elif host.endswith("linkedin.com") and parts.path.startswith("/redir/redirect"):
            inner = params.get("url")
        if not inner:
            return url
        url = unquote(inner) if "%" in inner and "://" not in inner else inner
    return url


def not_a_posting_reason(url: str) -> str | None:
    host = host_of(url)
    for domain, label in NOT_A_POSTING_HOSTS.items():
        if _host_matches(host, domain):
            return label
    if _host_matches(host, "linkedin.com") and not urlsplit(url).path.startswith("/jobs"):
        return "a LinkedIn page that isn't a job posting"
    return None


def is_shortener(url: str) -> bool:
    return host_of(url) in SHORTENER_HOSTS


def detect_ats(url: str) -> str:
    host = host_of(url)
    query = urlsplit(url).query.lower()
    if "greenhouse.io" in host or "gh_jid=" in query:
        return "greenhouse"
    if _host_matches(host, "lever.co"):
        return "lever"
    if _host_matches(host, "ashbyhq.com") or "ashby_jid=" in query:
        return "ashby"
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host or "workday.com" in host:
        return "workday"
    if _host_matches(host, "linkedin.com") or host == "lnkd.in":
        return "linkedin"
    if "icims.com" in host:
        return "icims"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "jobvite.com" in host:
        return "jobvite"
    if "taleo.net" in host or "oraclecloud.com" in host:
        return "taleo"
    if "successfactors" in host:
        return "successfactors"
    return "other"


def normalize_url(url: str) -> str:
    """Canonical form used for dedupe: https, no www, no tracking params or fragment, no trailing slash."""
    url = unwrap(url.strip())
    parts = urlsplit(url)
    host = host_of(url)
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    # Greenhouse serves the same job on boards.* and job-boards.*
    if host in (
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
        "boards.eu.greenhouse.io",
    ):
        host = "boards.greenhouse.io"
    # The apply page and the posting page are the same job.
    if host.endswith("lever.co") and path.endswith("/apply"):
        path = path[: -len("/apply")]
    if host == "jobs.ashbyhq.com" and path.endswith("/application"):
        path = path[: -len("/application")]
    if len(path) > 1:
        path = path.rstrip("/")
    params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    params.sort()
    return urlunsplit(("https", host, path, urlencode(params), ""))


@dataclass
class ATSIds:
    """Board/company slug and job id for ATSes with public posting APIs."""

    ats: str
    board: str | None = None
    job_id: str | None = None
    region: str = ""  # "eu" for EU-hosted Lever/Greenhouse


def ats_ids(url: str) -> ATSIds:
    ats = detect_ats(url)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    params = dict(parse_qsl(parts.query))
    region = "eu" if ".eu." in host else ""
    if ats == "greenhouse":
        if "embed" in segs and params.get("for"):
            return ATSIds(ats, params.get("for"), params.get("token"), region)
        if len(segs) >= 3 and segs[1] == "jobs" and "greenhouse.io" in host:
            return ATSIds(ats, segs[0], segs[2], region)
        return ATSIds(ats, None, params.get("gh_jid"), region)
    if ats == "lever" and len(segs) >= 2:
        return ATSIds(ats, segs[0], segs[1], region)
    if ats == "ashby" and host == "jobs.ashbyhq.com" and len(segs) >= 2:
        return ATSIds(ats, segs[0], segs[1])
    return ATSIds(ats)
