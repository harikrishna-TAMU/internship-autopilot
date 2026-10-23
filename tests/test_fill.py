"""Milestone M5: a form is filled in a real browser and nothing is ever submitted."""

from __future__ import annotations

import json
import re

import pytest

from autopilot.fill.filler import best_option, fill_page
from autopilot.models import FieldAction, FieldMapping
from tests.conftest import FIXTURES, FakeLLM

pytestmark = pytest.mark.browser

DRAFT = "I enjoy building backend services like the FastAPI report tool I built at Aggie Labs."
JOB = {
    "company": "Acme",
    "role": "Software Engineering Intern",
    "term": "Summer 2027",
    "location": "Austin, TX",
    "remote": "hybrid",
    "deadline": None,
    "apply_url": "https://example.invalid",
    "requirements_json": "[]",
    "eligibility_json": "{}",
}


def fake_mapping(user, images):
    fields = json.loads(re.search(r"<fields>\n(.*)\n</fields>", user, re.S).group(1))
    actions = []
    for f in fields:
        label = f["label"].lower()
        if "school" in label:
            a = ("select", "Texas A&M University", False)
        elif "authorized" in label:
            a = ("select", "Yes", False)
        elif "sponsorship" in label:
            a = ("select", "Yes", False)
        elif "why" in label:
            a = ("fill", DRAFT, True)
        elif "certify" in label:
            a = ("check", "checked", False)  # the filler must refuse this anyway
        else:
            a = ("leave_for_user", None, False)
        actions.append(FieldAction(field_id=f["id"], action=a[0], value=a[1], is_draft=a[2], reason="test"))
    return FieldMapping(actions=actions)


def test_fill_greenhouse_like_form_without_submitting(page, profile, tmp_path):
    resume = tmp_path / "Test_Student_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    page.goto((FIXTURES / "greenhouse_form.html").as_uri())
    url_before = page.url
    fake = FakeLLM(map_fields=fake_mapping)

    report = fill_page(page, JOB, profile, fake, resume)

    val = lambda sel: page.locator(sel).input_value()  # noqa: E731
    assert val("#first_name") == "Test" and val("#last_name") == "Student"
    assert val("#email") == "student@tamu.edu" and val("#phone") == "+1 979 555 0100"
    assert val("#linkedin") == "https://www.linkedin.com/in/test-student"
    assert page.evaluate("document.getElementById('resume').files[0].name") == "Test_Student_Resume.pdf"
    assert page.evaluate("document.getElementById('cover_letter').files.length") == 0
    assert val("#school") == "Texas A&M University"
    assert page.locator("input[name=authorized][value='1']").is_checked()
    assert page.locator("input[name=sponsorship][value='1']").is_checked()
    assert val("#why") == DRAFT
    assert page.locator("#why").get_attribute("data-autopilot-mark") == "draft"
    assert val("#gender") == ""
    assert not page.locator("#consent").is_checked()

    # Basics were filled from the profile without asking Claude about them.
    asked = json.loads(re.search(r"<fields>\n(.*)\n</fields>", fake.calls[0][1], re.S).group(1))
    assert not {"First Name *", "Email *", "Resume/CV *"} & {f["label"] for f in asked}

    assert report.resume_uploaded and len(report.drafted) == 1
    left = {x["label"]: x for x in report.left_for_you}
    assert "consent" in left["I certify that the information provided is accurate *"]["reason"]
    assert "Gender" in left and "Cover Letter" in left

    # Nothing was submitted, the page didn't navigate, and the guard is gone for the user's own submit.
    assert not page.evaluate("window.__submitted")
    assert page.url == url_before
    assert page.evaluate("window.__autopilotGuard === undefined")
    assert "Nothing has been submitted" in page.locator("#autopilot-banner").inner_text()


SEARCHABLE = """
<form onsubmit="window.__submitted = true; return false;">
  <label for="fn">First Name</label><input id="fn">
  <label for="em">Email</label><input id="em" type="email">
  <label for="uni">University (search) *</label>
  <input id="uni" role="combobox" aria-autocomplete="list" required>
  <div id="lb" role="listbox" style="display:none"></div>
  <button type="submit">Submit</button>
</form>
<script>
  const all = Array.from({length: 120}, (_, i) => 'Aardvark University ' + i).concat(['Texas A&M University - College Station', 'Texas Tech University']);
  const inp = document.getElementById('uni'), lb = document.getElementById('lb');
  function show(q) {
    lb.innerHTML = '';
    all.filter(x => x.toLowerCase().includes(q.toLowerCase())).slice(0, 100).forEach(x => {
      const d = document.createElement('div'); d.setAttribute('role', 'option'); d.textContent = x;
      d.onclick = () => { inp.value = x; lb.style.display = 'none'; window.__picked = x; };
      lb.appendChild(d);
    });
    lb.style.display = 'block';
  }
  inp.addEventListener('click', () => show(inp.value));
  inp.addEventListener('input', () => show(inp.value));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') lb.style.display = 'none'; });
</script>
"""


def test_searchable_dropdown_types_and_picks(page, profile):
    page.set_content(SEARCHABLE)
    seen = {}

    def mapping(user, images):
        fields = json.loads(re.search(r"<fields>\n(.*)\n</fields>", user, re.S).group(1))
        uni = next(f for f in fields if "University" in f["label"])
        seen.update(uni)
        return FieldMapping(
            actions=[
                FieldAction(
                    field_id=uni["id"],
                    action="select",
                    value="Texas A&M University",
                    is_draft=False,
                    reason="profile education",
                )
            ]
        )

    report = fill_page(page, JOB, profile, FakeLLM(map_fields=mapping), None)
    assert "searchable" in seen["note"]
    assert page.evaluate("window.__picked") == "Texas A&M University - College Station"
    assert report.filled and not page.evaluate("window.__submitted")


def test_login_page_is_open_only(page, profile):
    page.set_content(
        "<form><label>Email<input name=email></label><label>Password<input type=password name=pw></label></form>"
    )
    report = fill_page(page, JOB, profile, FakeLLM(), None)
    assert report.open_only and "sign in" in report.open_only
    assert page.locator("input[name=email]").input_value() == ""


def test_best_option():
    assert best_option("yes", ["Yes", "No"]) == 0
    assert best_option("Texas A&M University", ["Other", "Texas A&M University"]) == 1
    assert best_option("Maybe", ["Yes", "No"]) is None


def test_job_page_with_only_an_upload_box_is_not_filled(page, profile, tmp_path):
    """A career page's 'match your resume to jobs' widget must never receive the resume."""
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF-1.4")
    page.set_content(
        "<h1>Security Research Intern</h1><button>Apply</button>"
        "<label for=up>Upload your resume to find matching jobs</label><input id=up type=file>"
        "<input type=text placeholder='Search jobs' aria-label='Search jobs'>"
    )
    report = fill_page(page, JOB, profile, FakeLLM(), resume)
    assert report.open_only and not report.resume_uploaded
    assert page.evaluate("document.getElementById('up').files.length") == 0


def test_banner_can_be_minimized_hidden_and_moved(page, profile):
    from autopilot.fill.filler import banner

    page.set_content("<h1>Apply</h1>")
    banner(page, "Filled 3 fields.", short="3 filled · 1 need you")
    b = page.locator("#autopilot-banner")
    assert "ApplyPilot · 3 filled · 1 need you" in b.inner_text()
    page.locator("#autopilot-banner .ap-toggle").click()  # you minimize it
    assert "ap-min" in b.get_attribute("class") and not page.locator("#autopilot-banner .ap-body").is_visible()
    bar = page.locator("#autopilot-banner .ap-bar").bounding_box()
    page.mouse.move(bar["x"] + 20, bar["y"] + 10)
    page.mouse.down()
    page.mouse.move(200, 400)  # you drag it somewhere else
    page.mouse.up()
    box = b.bounding_box()
    assert abs(box["y"] - 390) < 15
    page.locator("#autopilot-banner .ap-hide").click()
    assert not b.is_visible()
