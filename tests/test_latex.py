"""Your LaTeX resume format: escaping, bold markup, and a real Tectonic compile (skipped without Tectonic)."""

from __future__ import annotations

import shutil

import pytest

from autopilot.resume.render import LatexRenderer, render_tex, tex, texb, url_handle
from tests.conftest import selection


def test_tex_escaping_and_bold():
    assert tex("50% of C# & R&D_x $5 {a}") == r"50\% of C\# \& R\&D\_x \$5 \{a\}"
    assert texb("Cut time by **50%** – fast") == r"Cut time by \textbf{50\%} -- fast"
    assert tex("A → B") == r"A $\rightarrow$ B"
    assert url_handle("https://www.linkedin.com/in/jane-doe/") == "jane-doe"


def test_template_has_your_sections(profile):
    source = render_tex(profile, selection(), target="Acme – SWE Intern")
    for needle in (
        r"\section{Summary}",
        r"\section{Core Skills}",
        r"\section{Education}",
        r"\section{Experience}",
        r"\section{Projects}",
        r"\resumeSubheading",
        r"\resumeProjectHeading",
        r"\faEnvelope",
        r"\textbf{Python}",
        "Tailored for: Acme – SWE Intern",
    ):
        assert needle in source, needle
    assert "**" not in source  # all bold markup converted


@pytest.mark.skipif(not shutil.which("tectonic"), reason="Tectonic not installed")
def test_compiles_to_one_page_and_keeps_tex(profile, tmp_path):
    result = LatexRenderer().render(profile, selection(), tmp_path / "Hari_Krishna_Resume.pdf")
    assert result.pages == 1
    assert result.path.read_bytes().startswith(b"%PDF")
    assert (tmp_path / "Hari_Krishna_Resume.tex").exists()


def test_links_hidden_from_resume_still_exist_for_applications(profile):
    profile.basics.resume_links = ["linkedin"]
    source = render_tex(profile, selection())
    assert "test-student" in source and "teststudent" not in source and r"\faGithub" not in source
    assert profile.basics.links.github == "https://github.com/teststudent"  # still there for forms
