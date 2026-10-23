"""Milestone M4: a one-page PDF per job, trimmed to fit."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from autopilot.headless import BrowserUnavailable, HeadlessBrowser
from autopilot.models import Bullet, Experience, TailoredBullet, TailoredEntry
from autopilot.resume.render import ResumeRenderer, render_html
from tests.conftest import selection

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def renderer():
    headless = HeadlessBrowser("chrome")
    try:
        headless.pdf("<p>probe</p>")
    except BrowserUnavailable as e:
        pytest.skip(str(e))
    yield ResumeRenderer(headless)
    headless.close()


def test_renders_one_page_pdf(renderer, profile, tmp_path):
    result = renderer.render(profile, selection(), tmp_path / "r.pdf")
    reader = PdfReader(io.BytesIO(result.path.read_bytes()))
    text = reader.pages[0].extract_text()
    assert result.pages == 1 and result.dropped == []
    assert "student@tamu.edu" in text and "Aggie Labs" in text  # the name is small caps, so it extracts letter-spaced


def test_overflow_is_trimmed_to_one_page(renderer, profile, tmp_path):
    long = (
        "Implemented and documented a feature in the internal tools used by the operations team every single week " * 2
    )
    for i in range(2, 9):
        profile.experience.append(
            Experience(
                id=f"exp{i}",
                org=f"Org {i}",
                title="Intern",
                dates="2025",
                bullets=[Bullet(id=f"exp{i}.b{k}", text=long) for k in range(1, 5)],
            )
        )
    sel = selection()
    sel.experience += [
        TailoredEntry(
            source_id=f"exp{i}", bullets=[TailoredBullet(source_id=f"exp{i}.b{k}", text=long) for k in range(1, 5)]
        )
        for i in range(2, 9)
    ]
    result = renderer.render(profile, sel, tmp_path / "long.pdf")
    assert result.pages == 1
    assert result.dropped


def test_html_escapes_content(profile):
    sel = selection()
    sel.experience[0].bullets[0].text = "<script>alert(1)</script>"
    assert "<script>alert" not in render_html(profile, sel)
