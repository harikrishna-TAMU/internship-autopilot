from autopilot.extract.links import ats_ids, detect_ats, find_urls, normalize_url, not_a_posting_reason, unwrap


def test_find_urls_strips_punctuation_and_dedupes():
    text = (
        "Apply here: https://jobs.lever.co/acme/123. Also (https://jobs.lever.co/acme/123) and www.example.com/jobs/9!"
    )
    assert find_urls(text) == ["https://jobs.lever.co/acme/123", "https://www.example.com/jobs/9"]


def test_unwrap_redirect_wrappers():
    assert (
        unwrap("https://l.instagram.com/?u=https%3A%2F%2Fjobs.lever.co%2Facme%2F123&e=AT0")
        == "https://jobs.lever.co/acme/123"
    )
    assert (
        unwrap("https://www.google.com/url?q=https://boards.greenhouse.io/acme/jobs/1&sa=D")
        == "https://boards.greenhouse.io/acme/jobs/1"
    )
    safelink = "https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2Fjobs.ashbyhq.com%2Facme%2Fabc&data=x"
    assert unwrap(safelink) == "https://jobs.ashbyhq.com/acme/abc"


def test_normalize_url_drops_tracking_and_unifies_variants():
    a = normalize_url("https://job-boards.greenhouse.io/acme/jobs/123?gh_src=insta&utm_source=ig#app")
    b = normalize_url("http://boards.greenhouse.io/acme/jobs/123/")
    assert a == b == "https://boards.greenhouse.io/acme/jobs/123"
    assert (
        normalize_url("https://jobs.lever.co/acme/abc/apply?lever-source=Instagram") == "https://jobs.lever.co/acme/abc"
    )
    # meaningful params are kept
    assert "gh_jid=55" in normalize_url("https://acme.com/careers?gh_jid=55&utm_medium=x")


def test_detect_ats_and_ids():
    assert detect_ats("https://jobs.lever.co/acme/1") == "lever"
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Austin/Intern_R1") == "workday"
    assert detect_ats("https://acme.com/careers?gh_jid=9") == "greenhouse"
    ids = ats_ids("https://boards.greenhouse.io/embed/job_app?for=acme&token=77")
    assert (ids.board, ids.job_id) == ("acme", "77")
    ids = ats_ids("https://jobs.ashbyhq.com/acme/0b1c/application")
    assert (ids.ats, ids.board, ids.job_id) == ("ashby", "acme", "0b1c")


def test_not_a_posting():
    assert not_a_posting_reason("https://www.instagram.com/stories/zero2sudo/123") == "an Instagram link"
    assert not_a_posting_reason("https://linktr.ee/someone") == "a Linktree page"
    assert not_a_posting_reason("https://www.linkedin.com/in/someone") is not None
    assert not_a_posting_reason("https://www.linkedin.com/jobs/view/123") is None
    assert not_a_posting_reason("https://jobs.lever.co/acme/1") is None
