"""Render a tailored selection to a one-page PDF.

Default: your own LaTeX format (templates/resume.tex.j2), compiled with Tectonic; the .tex is saved
next to the PDF so you can still edit it in Overleaf. Fallback when Tectonic isn't installed:
an HTML template printed by headless Chrome.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from pypdf import PdfReader

from autopilot.headless import HeadlessBrowser
from autopilot.models import Profile, TailorSelection

TEMPLATES = Path(__file__).parent / "templates"
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


class RenderError(RuntimeError):
    pass


# -- text helpers ------------------------------------------------------------------


def month_year(value: str | None) -> str:
    """'2028-05' → 'May 2028'. Anything else is shown as written."""
    if not value:
        return ""
    for fmt in ("%Y-%m", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt).strftime("%b %Y")
        except ValueError:
            continue
    return str(value)


def bare_url(url: str | None) -> str:
    if not url:
        return ""
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix) :]
    return url.removeprefix("www.").rstrip("/")


def url_handle(url: str | None) -> str:
    """'https://www.linkedin.com/in/jane-doe/' → 'jane-doe'."""
    path = [p for p in urlsplit(url or "").path.split("/") if p]
    return path[-1] if path else bare_url(url)


TEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
TEX_UNICODE = {
    "–": "--",
    "—": "---",
    "→": r"$\rightarrow$",
    "←": r"$\leftarrow$",
    "’": "'",
    "‘": "`",
    "“": "``",
    "”": "''",
    "…": r"\ldots{}",
    "×": r"$\times$",
    "≥": r"$\geq$",
    "≤": r"$\leq$",
    "\u00a0": "~",
}


def tex(value) -> str:
    """Escape plain text for LaTeX."""
    s = "".join(TEX_SPECIAL.get(ch, ch) for ch in str(value or ""))
    for k, v in TEX_UNICODE.items():
        s = s.replace(k, v)
    return s


def texb(value) -> str:
    """Escape for LaTeX and turn **bold** markup into \\textbf{}."""
    return BOLD_RE.sub(r"\\textbf{\1}", tex(value))


def html_bold(value) -> Markup:
    return Markup(BOLD_RE.sub(r"<b>\1</b>", str(escape(value or ""))))


def contact_line(profile: Profile) -> str:
    b = profile.basics
    parts = []
    if b.phone:
        parts.append(rf"\faPhone* \ {tex(b.phone)}")
    parts.append(rf"\faEnvelope \ \href{{mailto:{b.email}}}{{{tex(b.email)}}}")
    shown = set(b.resume_links)
    if b.links.linkedin and "linkedin" in shown:
        parts.append(rf"\faLinkedin \ \href{{{b.links.linkedin}}}{{{tex(url_handle(b.links.linkedin))}}}")
    if b.links.github and "github" in shown:
        parts.append(rf"\faGithub \ \href{{{b.links.github}}}{{{tex(url_handle(b.links.github))}}}")
    if b.links.portfolio and "portfolio" in shown:
        parts.append(rf"\faGlobe \ \href{{{b.links.portfolio}}}{{{tex(bare_url(b.links.portfolio))}}}")
    if b.location:
        parts.append(rf"\faMapMarker* \ {tex(b.location)}")
    return " \\ \\textbar \\\n    ".join(parts)


def build_context(profile: Profile, sel: TailorSelection, target: str = "") -> dict:
    edu = {e.id: e for e in profile.education}
    exp = {e.id: e for e in profile.experience}
    proj = {p.id: p for p in profile.projects}
    return {
        "basics": profile.basics,
        "target": target,
        "summary": sel.summary,
        "education": [(edu[p.source_id], p.coursework) for p in sel.education if p.source_id in edu],
        "experience": [(exp[e.source_id], e.bullets) for e in sel.experience if e.source_id in exp],
        "projects": [(proj[e.source_id], e.bullets) for e in sel.projects if e.source_id in proj],
        "leadership": sel.leadership,
        "leadership_title": profile.leadership_title,
        "skills": sel.skills,
    }


def _html_env() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    env.filters.update(month_year=month_year, bare_url=bare_url, bold=html_bold)
    return env


def _tex_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=False,
        block_start_string="<%",
        block_end_string="%>",
        variable_start_string="<<",
        variable_end_string=">>",
        comment_start_string="<#",
        comment_end_string="#>",
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(month_year=month_year, tex=tex, texb=texb)
    return env


def render_html(profile: Profile, sel: TailorSelection) -> str:
    return _html_env().get_template("resume.html.j2").render(**build_context(profile, sel))


def render_tex(profile: Profile, sel: TailorSelection, target: str = "") -> str:
    ctx = build_context(profile, sel, target)
    ctx["contact"] = contact_line(profile)
    return _tex_env().get_template("resume.tex.j2").render(**ctx)


# -- fitting to one page -------------------------------------------------------------


def drop_one(sel: TailorSelection, optional_ids: frozenset[str] = frozenset()) -> str | None:
    """Remove the least important item to shorten the page. Returns what was dropped, or None."""
    projs, exps = sel.projects, sel.experience
    for entry in [*projs, *exps]:  # bullets marked optional go first
        for b in reversed(entry.bullets):
            if b.source_id in optional_ids and len(entry.bullets) > 1:
                entry.bullets.remove(b)
                return b.source_id
    multi = [p for p in projs if len(p.bullets) > 1]
    if multi:  # trim a bullet from the least relevant project that has several, before dropping a project
        return multi[-1].bullets.pop().source_id
    if len(sel.leadership) > 1:
        return sel.leadership.pop().source_id
    if len(projs) > 2:
        return projs.pop().source_id
    if exps and max(len(e.bullets) for e in exps) > 3:
        longest = max(reversed(exps), key=lambda e: len(e.bullets))
        return longest.bullets.pop().source_id
    if len(projs) > 1:
        return projs.pop().source_id
    for pick in reversed(sel.education):
        if pick.coursework:
            pick.coursework = []
            return f"{pick.source_id} coursework"
    if exps and max(len(e.bullets) for e in exps) > 1:
        longest = max(reversed(exps), key=lambda e: len(e.bullets))
        return longest.bullets.pop().source_id
    return None


def optional_bullet_ids(profile: Profile) -> frozenset[str]:
    return frozenset(i for i, (_, b) in profile.bullet_index().items() if b.optional)


def page_count(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


@dataclass
class RenderResult:
    path: Path
    pages: int
    dropped: list[str] = field(default_factory=list)


class ResumeRenderer:
    """HTML template → PDF with headless Chrome (fallback when Tectonic isn't installed)."""

    def __init__(self, headless: HeadlessBrowser):
        self.headless = headless

    def render(
        self, profile: Profile, sel: TailorSelection, out_pdf: Path, max_pages: int = 1, target: str = ""
    ) -> RenderResult:
        dropped: list[str] = []
        while True:
            pdf = self.headless.pdf(render_html(profile, sel))
            pages = page_count(pdf)
            if pages <= max_pages or (what := drop_one(sel, optional_bullet_ids(profile))) is None:
                break
            dropped.append(what)
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(pdf)
        return RenderResult(out_pdf, pages, dropped)


class LatexRenderer:
    """Your LaTeX format → PDF with Tectonic. Also saves the .tex next to the PDF."""

    def __init__(self, tectonic: str | None = None):
        self.bin = tectonic or shutil.which("tectonic")
        if not self.bin:
            raise RenderError("Tectonic isn't installed (brew install tectonic).")

    def compile(self, source: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resume.tex"
            path.write_text(source, encoding="utf-8")
            proc = subprocess.run(
                [self.bin, "--chatter", "minimal", "--outdir", tmp, str(path)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            pdf = Path(tmp) / "resume.pdf"
            if proc.returncode != 0 or not pdf.exists():
                errors = [
                    line
                    for line in (proc.stdout + proc.stderr).splitlines()
                    if "error" in line.lower() or line.startswith("!")
                ]
                raise RenderError("LaTeX compile failed: " + (" | ".join(errors[-5:]) or proc.stderr[-500:]))
            return pdf.read_bytes()

    def render(
        self, profile: Profile, sel: TailorSelection, out_pdf: Path, max_pages: int = 1, target: str = ""
    ) -> RenderResult:
        dropped: list[str] = []
        while True:
            source = render_tex(profile, sel, target)
            pdf = self.compile(source)
            pages = page_count(pdf)
            if pages <= max_pages or (what := drop_one(sel, optional_bullet_ids(profile))) is None:
                break
            dropped.append(what)
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(pdf)
        out_pdf.with_suffix(".tex").write_text(source, encoding="utf-8")
        return RenderResult(out_pdf, pages, dropped)


def make_renderer(resume_format: str, headless: HeadlessBrowser):
    """LaTeX (your format) when Tectonic is available, else the HTML fallback."""
    if resume_format == "latex" and shutil.which("tectonic"):
        return LatexRenderer()
    return ResumeRenderer(headless)
