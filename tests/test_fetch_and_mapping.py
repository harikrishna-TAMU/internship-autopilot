"""JD fetching against mocked ATS APIs, and field-mapping helpers (no network, no browser)."""

from __future__ import annotations

import httpx

from autopilot.extract.ats_lookup import board_slugs, title_score
from autopilot.extract.jd_fetcher import JDFetcher, closed_signal
from autopilot.fill.field_mapper import FormField, basic_value, profile_keywords, shortlist
from autopilot.fill.filler import best_option


def client(routes: dict[str, tuple[int, object]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix, (status, body) in routes.items():
            if url.startswith(prefix):
                if isinstance(body, str):
                    return httpx.Response(status, text=body, headers={"content-type": "text/html"})
                return httpx.Response(status, json=body)
        return httpx.Response(404, text="not found")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_greenhouse_api_and_form_url():
    http = client(
        {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/42": (
                200,
                {
                    "title": "SWE Intern",
                    "location": {"name": "Austin"},
                    "content": "&lt;p&gt;Build things in Python.&lt;/p&gt;",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                },
            )
        }
    )
    page = JDFetcher(http).fetch("https://job-boards.greenhouse.io/acme/jobs/42?gh_src=x")
    assert not page.closed and "Build things in Python." in page.text
    assert page.form_url == "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=42"


def test_greenhouse_404_is_closed():
    page = JDFetcher(client({})).fetch("https://boards.greenhouse.io/acme/jobs/999")
    assert page.closed and "404" in page.closed_reason


def test_company_hosted_greenhouse_job_finds_board_from_domain():
    long_page = "<html><body><h1>Careers</h1>" + "<p>Intro text about the company.</p>" * 80 + "</body></html>"
    http = client(
        {
            "https://acme.com/jobs/search": (200, long_page),
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/77": (
                200,
                {
                    "title": "Data Intern",
                    "content": "SQL and Python",
                    "absolute_url": "https://acme.com/jobs/search?gh_jid=77",
                },
            ),
        }
    )
    page = JDFetcher(http).fetch("https://acme.com/jobs/search?gh_jid=77")
    assert page.ats == "greenhouse" and "SQL and Python" in page.text
    assert page.form_url.endswith("for=acme&token=77")


def test_lever_api():
    http = client(
        {
            "https://api.lever.co/v0/postings/acme/abc": (
                200,
                {
                    "text": "Backend Intern",
                    "categories": {"location": "Remote", "commitment": "Internship"},
                    "descriptionPlain": "Work on APIs.",
                    "lists": [{"text": "Requirements", "content": "<li>Go</li><li>SQL</li>"}],
                    "hostedUrl": "https://jobs.lever.co/acme/abc",
                },
            )
        }
    )
    page = JDFetcher(http).fetch("https://jobs.lever.co/acme/abc/apply")
    assert "Work on APIs." in page.text and "- Go" in page.text
    assert page.form_url == "https://jobs.lever.co/acme/abc/apply"


def test_closed_signal():
    assert closed_signal("Sorry, this job is no longer accepting applications.")
    assert closed_signal("We build rockets. " * 300 + "This position has been filled.") is None


def test_lookup_helpers():
    assert board_slugs("Jane Street Capital, LLC")[:2] == ["janestreetcapitalllc", "jane-street-capital-llc"]
    assert "janestreet" in board_slugs("Jane Street Capital, LLC")
    assert title_score("Software Engineering Intern", "Software Engineer Intern - Summer 2027") > 0.6
    assert title_score("Software Engineering Intern", "Senior Software Engineer") < 0.6


def test_basic_values_handle_required_markers(profile):
    f = FormField(id="f1", kind="text", label="Full name ✱")
    assert basic_value(f, profile) == "Test Student"
    assert basic_value(FormField(id="f2", kind="text", label="Legal Name"), profile) == "Test Student"
    long_q = FormField(
        id="f3", kind="text", label="How did you hear about us? For example an email newsletter or a friend"
    )
    assert basic_value(long_q, profile) is None


def test_shortlist_long_dropdowns(profile):
    options = (
        ["Click here"]
        + [f"University {i}" for i in range(500)]
        + ["Texas A&M University - College Station", "Texas Tech University"]
    )
    picked = shortlist(options, profile_keywords(profile))
    assert "Texas A&M University - College Station" in picked and len(picked) < 60
    assert best_option("Texas A&M University", options) == options.index("Texas A&M University - College Station")


def test_page_code_instead_of_prose_is_rendered_in_a_browser():
    from autopilot.extract.jd_fetcher import looks_like_prose

    blob = (
        '{"themeOptions": {"customTheme": {"varTheme": {"accordion-body-text-color": "#646464"}}}, "x": [1,2,3]}' * 60
    )
    prose = (
        "<html><body><h1>Security Research Intern</h1>"
        + "<p>Design and conduct security research.</p>" * 60
        + "</body></html>"
    )
    assert not looks_like_prose(blob) and looks_like_prose("Design and conduct security research. " * 20)

    class FakeHeadless:
        def render(self, url, wait_ms=2500):
            return url, prose, "Security Research Intern"

    http = client(
        {"https://careers.example.com/job/1": (200, f"<html><body><script>{blob}</script>{blob}</body></html>")}
    )
    page = JDFetcher(http, FakeHeadless()).fetch("https://careers.example.com/job/1")
    assert "Design and conduct security research." in page.text and "themeOptions" not in page.text


def test_alternate_or_other_peoples_contact_fields_are_not_your_basics(profile):
    assert basic_value(FormField(id="a", kind="tel", label="Alternate Phone Number"), profile) is None
    assert basic_value(FormField(id="b", kind="email", label="Reference email"), profile) is None
    assert basic_value(FormField(id="c", kind="tel", label="Phone number"), profile) == "+1 979 555 0100"
