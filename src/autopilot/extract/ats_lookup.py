"""Find a posting from just a company and role (a screenshot with no link), via public ATS job-board APIs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

import httpx

SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs|group|holdings)\b\.?", re.I
)


@dataclass
class FoundPosting:
    url: str
    title: str
    ats: str
    score: float


def board_slugs(company: str) -> list[str]:
    """Likely board names: 'Jane Street Capital, LLC' → janestreetcapital, jane-street-capital, janestreet, …"""
    base = re.sub(r"[^a-z0-9 ]+", " ", company.lower())
    words = base.split()
    trimmed = SUFFIXES.sub(" ", base).split()
    out: list[str] = []
    for ws in (words, trimmed, trimmed[:2], trimmed[:1]):
        if ws:
            for s in ("".join(ws), "-".join(ws)):
                if s and s not in out:
                    out.append(s)
    return out[:6]


def title_score(role: str, title: str) -> float:
    a, b = _norm_title(role), _norm_title(title)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    wa, wb = set(a.split()), set(b.split())
    overlap = len(wa & wb) / max(len(wa), 1)
    score = max(ratio, overlap)
    # An internship role should match an internship posting.
    if ("intern" in wa or "internship" in wa) != ("intern" in wb or "internship" in wb):
        score *= 0.5
    return score


def _norm_title(t: str) -> str:
    t = t.lower().replace("internship", "intern")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


class ATSLookup:
    def __init__(self, http: httpx.Client, min_score: float = 0.6):
        self.http = http
        self.min_score = min_score

    def find(self, company: str | None, role: str | None) -> FoundPosting | None:
        if not company or not role:
            return None
        best: FoundPosting | None = None
        for slug in board_slugs(company):
            for found in self._candidates(slug, role):
                if best is None or found.score > best.score:
                    best = found
            if best and best.score >= 0.9:
                break
        return best if best and best.score >= self.min_score else None

    def _candidates(self, slug: str, role: str) -> list[FoundPosting]:
        out: list[FoundPosting] = []
        gh = self._json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if isinstance(gh, dict):
            for j in gh.get("jobs", []):
                out.append(
                    FoundPosting(
                        j.get("absolute_url", ""),
                        j.get("title", ""),
                        "greenhouse",
                        title_score(role, j.get("title", "")),
                    )
                )
        lv = self._json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if isinstance(lv, list):
            for j in lv:
                out.append(
                    FoundPosting(
                        j.get("hostedUrl", ""), j.get("text", ""), "lever", title_score(role, j.get("text", ""))
                    )
                )
        ab = self._json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if isinstance(ab, dict):
            for j in ab.get("jobs", []):
                out.append(
                    FoundPosting(
                        j.get("jobUrl", ""), j.get("title", ""), "ashby", title_score(role, j.get("title", ""))
                    )
                )
        return [f for f in out if f.url]

    def _json(self, url: str):
        try:
            r = self.http.get(url, headers={"Accept": "application/json"}, timeout=15)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None
