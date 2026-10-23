"""Fill one application page: discover fields, map them, type values, mark drafts. Never submits."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from autopilot.fill.field_mapper import (
    RESUME_UPLOAD,
    TEXT_KINDS,
    FormField,
    discover_fields,
    is_resume_field,
    map_fields,
    read_combobox_options,
)
from autopilot.fill.safety import (
    SubmitBlocked,
    dismiss_popup,
    install_submit_guard,
    remove_submit_guard,
    safe_check,
    safe_click,
)
from autopilot.llm.claude import LLM
from autopilot.models import FieldAction, Profile

# Checkbox/radio questions like these are always left for you, whatever the mapping says.
CONSENT_RE = re.compile(
    r"agree|certify|acknowledge|consent|terms|privacy|attest|signature|accurate|true and complete", re.I
)

MARK_CSS = """
[data-autopilot-mark="draft"] { outline: 3px dashed #d97706 !important; outline-offset: 3px !important; }
[data-autopilot-mark="needs-you"] { outline: 3px solid #dc2626 !important; outline-offset: 3px !important; }
#autopilot-banner { position: fixed; top: 0; right: 0; z-index: 2147483647; pointer-events: none;
  max-width: 440px; padding: 10px 14px; background: #111827; color: #f9fafb; opacity: .94;
  font: 13px/1.45 -apple-system, system-ui, sans-serif; border-bottom-left-radius: 10px; }
#autopilot-banner b { color: #fbbf24; }
"""

MARK_JS = """
([id, mark]) => {
  const els = Array.from(document.querySelectorAll('[data-autopilot-id="' + id + '"]'));
  if (!els.length) return;
  let target = els[0];
  if (els.length > 1 || ['radio', 'checkbox', 'file'].includes(els[0].type)) {
    target = els[0].parentElement;
    while (target && !els.every(e => target.contains(e))) target = target.parentElement;
    if (target && els.length === 1) target = target.parentElement || target;
  }
  if (target) target.setAttribute('data-autopilot-mark', mark);
}
"""

BANNER_JS = """
(text) => {
  let b = document.getElementById('autopilot-banner');
  if (!b) { b = document.createElement('div'); b.id = 'autopilot-banner'; document.body.appendChild(b); }
  b.innerHTML = text;
}
"""


@dataclass
class FillReport:
    filled: list[dict] = field(default_factory=list)
    drafted: list[dict] = field(default_factory=list)
    left_for_you: list[dict] = field(default_factory=list)
    resume_uploaded: bool = False
    open_only: str | None = None
    actions_by_label: dict = field(default_factory=dict)

    def summary(self) -> str:
        if self.open_only:
            return f"opened only: {self.open_only}"
        req = sum(1 for x in self.left_for_you if x.get("required"))
        return (
            f"filled {len(self.filled)}, drafted {len(self.drafted)}, left {len(self.left_for_you)} for you "
            f"({req} required){'' if self.resume_uploaded else ', resume NOT uploaded'}"
        )

    def to_dict(self) -> dict:
        return {
            "filled": self.filled,
            "drafted": self.drafted,
            "left_for_you": self.left_for_you,
            "resume_uploaded": self.resume_uploaded,
            "open_only": self.open_only,
            "actions_by_label": self.actions_by_label,
        }


def best_option(value: str | None, options: list[str]) -> int | None:
    """Index of the option matching `value`: exact, then case-insensitive, then a close match."""
    if not value or not options:
        return None
    v = value.strip().casefold()
    for i, o in enumerate(options):
        if o.strip() == value.strip():
            return i
    for i, o in enumerate(options):
        if o.strip().casefold() == v:
            return i
    # "Texas A&M University" vs "Texas A&M University - College Station": accept a unique containment.
    contains = [
        i
        for i, o in enumerate(options)
        if v in o.strip().casefold() or (len(o.strip()) > 3 and o.strip().casefold() in v)
    ]
    if len(contains) == 1:
        return contains[0]
    scores = [SequenceMatcher(None, v, o.strip().casefold()).ratio() for o in options]
    i = max(range(len(options)), key=lambda k: scores[k])
    return i if scores[i] >= 0.85 else None


def banner(page: Page, html: str) -> None:
    try:
        page.add_style_tag(content=MARK_CSS)
        page.evaluate(BANNER_JS, html)
    except PlaywrightError:
        pass


def mark(page: Page, field_id: str, kind: str) -> None:
    try:
        page.evaluate(MARK_JS, [field_id, kind])
    except PlaywrightError:
        pass


class Filler:
    def __init__(self, page: Page, resume: Path | None):
        self.page = page
        self.resume = resume

    def loc(self, f: FormField, opt: int | None = None):
        sel = f'[data-autopilot-id="{f.id}"]'
        if opt is not None:
            sel += f'[data-autopilot-opt="{opt}"]'
        return self.page.locator(sel).first

    def apply(self, f: FormField, a: FieldAction) -> str:
        """Returns 'filled', 'drafted', 'uploaded' or the reason it was left for you."""
        if a.action == "leave_for_user":
            return a.reason or "left for you"
        if f.kind in ("checkbox", "radio", "checkbox_group", "select", "combobox") and CONSENT_RE.search(f.label):
            return "consent/attestation question: answer it yourself"
        value = (a.value or "").strip()
        if f.kind == "file":
            if value != RESUME_UPLOAD or not self.resume or not self.resume.exists():
                return "file upload"
            self.loc(f).set_input_files(str(self.resume))
            return "uploaded"
        if f.kind in TEXT_KINDS:
            if not value:
                return "no value"
            if f.max_length and len(value) > f.max_length:
                value = value[: f.max_length]
            self.loc(f).fill(value)
            return "drafted" if a.is_draft else "filled"
        if f.kind == "select":
            idx = best_option(value, f.options)
            if idx is None:
                return f"no matching option for '{value}'"
            self.loc(f).select_option(label=f.options[idx])
            return "filled"
        if f.kind == "radio":
            idx = best_option(value, f.options)
            if idx is None:
                return f"no matching option for '{value}'"
            safe_check(self.loc(f, idx))
            return "filled"
        if f.kind == "checkbox_group":
            picks = [best_option(v, f.options) for v in value.split("|")]
            if not picks or any(p is None for p in picks):
                return f"no matching option for '{value}'"
            for idx in picks:
                safe_check(self.loc(f, idx))
            return "filled"
        if f.kind == "checkbox":
            safe_check(self.loc(f), a.action != "uncheck")
            return "filled"
        if f.kind == "combobox":
            if f.options and not f.searchable and best_option(value, f.options) is None:
                return f"no matching option for '{value}'"
            return self._combobox(f, value)
        return f"unsupported field type {f.kind}"

    def _combobox(self, f: FormField, value: str) -> str:
        if not value:
            return "no value"
        box = self.loc(f)
        safe_click(box)
        box.fill(value)
        self.page.wait_for_timeout(700)
        options = self.page.locator('[role="option"]:visible')
        texts = [t.strip() for t in options.all_inner_texts()]
        idx = best_option(value, texts)
        if idx is None and len(texts) == 1:
            idx = 0
        if idx is None:
            dismiss_popup(self.page)
            return f"no matching option for '{value}'"
        safe_click(options.nth(idx))
        return "filled"


def fill_page(
    page: Page,
    job,
    profile: Profile,
    llm: LLM,
    resume: Path | None,
    upload_first: bool = False,
    cache: dict | None = None,
) -> FillReport:
    """Fill the application form on `page`. Stops before submit; the submit guard is on throughout."""
    report = FillReport()
    install_submit_guard(page)
    try:
        fields = discover_fields(page)
        if any(f.kind == "password" for f in fields):
            report.open_only = "the page asks you to sign in or create an account"
            return report
        if not fields:
            report.open_only = "no application form found on this page; use the site's own Apply button"
            return report

        filler = Filler(page, resume)
        if upload_first and resume and resume.exists():
            # Some ATSes (Lever) parse the resume into the form; upload first, then fill over it.
            files = [f for f in fields if f.kind == "file"]
            target = next((f for f in files if is_resume_field(f)), files[0] if len(files) == 1 else None)
            if target:
                filler.loc(target).set_input_files(str(resume))
                report.resume_uploaded = True
                page.wait_for_timeout(3000)
                fields = discover_fields(page)

        read_combobox_options(page, fields)
        actions = map_fields(llm, profile, job, fields, cache)
        for f in fields:
            a = actions[f.id]
            if f.kind != "file":
                report.actions_by_label[f.label] = {
                    "kind": f.kind,
                    "action": a.action,
                    "value": a.value,
                    "is_draft": a.is_draft,
                    "reason": a.reason,
                }
            if f.kind == "file" and report.resume_uploaded and a.value == RESUME_UPLOAD:
                continue
            try:
                outcome = filler.apply(f, a)
            except SubmitBlocked as e:
                outcome = f"safety: {e}"
            except PlaywrightError as e:
                outcome = f"couldn't fill: {str(e).splitlines()[0][:120]}"
            entry = {"label": f.label, "kind": f.kind, "required": f.required}
            if outcome == "uploaded":
                report.resume_uploaded = True
            elif outcome == "filled":
                report.filled.append({**entry, "value": a.value})
            elif outcome == "drafted":
                report.drafted.append({**entry, "value": a.value})
                mark(page, f.id, "draft")
            else:
                report.left_for_you.append({**entry, "reason": outcome})
                if f.required:
                    mark(page, f.id, "needs-you")
    finally:
        remove_submit_guard(page)

    banner(
        page,
        (
            f"<b>Autopilot</b>: filled {len(report.filled)} fields"
            f"{', uploaded your resume' if report.resume_uploaded else ', <b>resume not uploaded</b>'}.<br>"
            f"<b>Orange dashed</b> = AI-drafted answer, check it ({len(report.drafted)}). "
            f"<b>Red</b> = required, needs you ({sum(1 for x in report.left_for_you if x['required'])}).<br>"
            "Nothing has been submitted. Review everything, then click Submit yourself."
        ),
    )
    return report
