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
#autopilot-banner { position: fixed; top: 12px; right: 12px; z-index: 2147483647; width: 360px;
  max-width: calc(100vw - 24px); background: #111827; color: #f9fafb; opacity: .96; border-radius: 10px;
  box-shadow: 0 6px 24px rgba(0,0,0,.35); font: 13px/1.45 -apple-system, system-ui, sans-serif; }
#autopilot-banner .ap-bar { display: flex; align-items: center; gap: 8px; padding: 6px 6px 6px 12px;
  cursor: move; user-select: none; border-bottom: 1px solid #374151; }
#autopilot-banner .ap-title { flex: 1; font-weight: 600; color: #fbbf24; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
#autopilot-banner button { all: unset; cursor: pointer; width: 24px; height: 22px; text-align: center;
  border-radius: 5px; color: #e5e7eb; font-size: 16px; line-height: 22px; }
#autopilot-banner button:hover { background: #374151; }
#autopilot-banner .ap-body { padding: 8px 12px 10px; }
#autopilot-banner .ap-body b { color: #fbbf24; }
#autopilot-banner.ap-min { width: auto; max-width: 320px; }
#autopilot-banner.ap-min .ap-body { display: none; }
#autopilot-banner.ap-min .ap-bar { border-bottom: 0; }
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

# A small panel you can drag by its title bar, minimize (–) or hide (×). Its position is remembered for the
# site in this tab, so it stays where you put it on the next page.
BANNER_JS = """
([html, short]) => {
  const KEY = '__applypilot_banner';
  let st = {};
  try { st = JSON.parse(sessionStorage.getItem(KEY) || '{}'); } catch (e) {}
  const save = () => { try { sessionStorage.setItem(KEY, JSON.stringify(st)); } catch (e) {} };
  let b = document.getElementById('autopilot-banner');
  if (!b) {
    b = document.createElement('div');
    b.id = 'autopilot-banner';
    b.innerHTML = '<div class="ap-bar"><span class="ap-title"></span>'
      + '<button type="button" class="ap-toggle" title="Minimize / expand">–</button>'
      + '<button type="button" class="ap-hide" title="Hide (it comes back on the next page)">×</button></div>'
      + '<div class="ap-body"></div>';
    document.body.appendChild(b);
    const place = () => {
      if (st.left != null) {
        b.style.left = st.left + 'px'; b.style.top = st.top + 'px'; b.style.right = 'auto';
      }
      b.classList.toggle('ap-min', !!st.min);
      b.querySelector('.ap-toggle').textContent = st.min ? '+' : '–';
    };
    b.__place = place;
    b.querySelector('.ap-toggle').addEventListener('click', e => { e.stopPropagation(); st.min = !st.min; save(); place(); });
    b.querySelector('.ap-hide').addEventListener('click', e => { e.stopPropagation(); b.style.display = 'none'; });
    b.querySelector('.ap-bar').addEventListener('mousedown', e => {
      if (e.target.closest('button')) return;
      const r = b.getBoundingClientRect();
      const dx = e.clientX - r.left, dy = e.clientY - r.top;
      const move = ev => {
        st.left = Math.max(0, Math.min(window.innerWidth - 80, ev.clientX - dx));
        st.top = Math.max(0, Math.min(window.innerHeight - 32, ev.clientY - dy));
        place();
      };
      const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); save(); };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
      e.preventDefault();
    });
  }
  b.querySelector('.ap-title').textContent = 'ApplyPilot' + (short ? ' · ' + short : '');
  b.querySelector('.ap-body').innerHTML = html;
  b.style.display = '';
  b.__place();
}
"""


@dataclass
class FillReport:
    filled: list[dict] = field(default_factory=list)
    drafted: list[dict] = field(default_factory=list)
    left_for_you: list[dict] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)
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
            "kept": self.kept,
            "resume_uploaded": self.resume_uploaded,
            "open_only": self.open_only,
            "actions_by_label": self.actions_by_label,
        }


IDENTITY_RE = re.compile(r"name|e-?mail", re.I)


def looks_like_application(fields: list[FormField]) -> bool:
    """A real application form asks who you are. Upload boxes or a search field alone don't count."""
    inputs = [f for f in fields if f.kind not in ("file", "password")]
    has_identity = any(IDENTITY_RE.search(f"{f.label} {f.name}") for f in inputs if f.kind in ("text", "email"))
    return len(inputs) >= 2 and has_identity


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


def banner(page: Page, html: str, short: str = "") -> None:
    try:
        page.add_style_tag(content=MARK_CSS)
        page.evaluate(BANNER_JS, [html, short])
    except PlaywrightError:
        pass


def mark(page: Page, field_id: str, kind: str) -> None:
    try:
        page.evaluate(MARK_JS, [field_id, kind])
    except PlaywrightError:
        pass


KEPT = "kept"


class Filler:
    def __init__(self, page: Page, resume: Path | None, keep_existing: bool = False, upload_resume: bool = True):
        self.page = page
        self.resume = resume
        self.keep_existing = keep_existing  # after you sign in: never overwrite what you or the site entered
        self.upload_resume = upload_resume

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
        if self.keep_existing and f.kind != "file" and f.current_value.strip():
            return KEPT
        value = (a.value or "").strip()
        if f.kind == "file":
            if value != RESUME_UPLOAD or not self.resume or not self.resume.exists():
                return "file upload"
            if not self.upload_resume:
                return "resume already uploaded on an earlier page"
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
    keep_existing: bool = False,
    upload_resume: bool = True,
) -> FillReport:
    """Fill the application form on `page`. Stops before submit; the submit guard is on throughout.

    keep_existing: used when filling after you sign in, or on the next page of a multi-step form.
    Fields that already have a value (typed by you or pre-filled by the site) are left as they are.
    """
    report = FillReport()
    page.set_default_timeout(8000)  # a field that won't take input is left for you instead of stalling 30 s
    install_submit_guard(page)
    try:
        fields = discover_fields(page)
        if any(f.kind == "password" for f in fields):
            report.open_only = "the page asks you to sign in or create an account"
            return report
        if not looks_like_application(fields):
            # e.g. a job page whose only upload box is a "match your resume to jobs" widget:
            # attaching the resume there would send it to the site without an application.
            report.open_only = "no application form on this page yet: click Apply (and sign in if asked)"
            return report

        filler = Filler(page, resume, keep_existing=keep_existing, upload_resume=upload_resume)
        if upload_first and upload_resume and resume and resume.exists():
            # Some ATSes (Lever) parse the resume into the form; upload first, then fill over it.
            files = [f for f in fields if f.kind == "file"]
            target = next((f for f in files if is_resume_field(f)), files[0] if len(files) == 1 else None)
            if target:
                filler.loc(target).set_input_files(str(resume))
                report.resume_uploaded = True
                page.wait_for_timeout(3000)
                fields = discover_fields(page)

        to_map = [f for f in fields if not (keep_existing and f.kind != "file" and f.current_value.strip())]
        read_combobox_options(page, to_map)
        actions = map_fields(llm, profile, job, to_map, cache) if to_map else {}
        for f in fields:
            if f.id not in actions:
                report.kept.append({"label": f.label, "kind": f.kind, "required": f.required})
                continue
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
            elif outcome == KEPT:
                report.kept.append(entry)
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

    needs_you = sum(1 for x in report.left_for_you if x["required"])
    banner(
        page,
        (
            f"Filled {len(report.filled)} fields"
            f"{', uploaded your resume' if report.resume_uploaded else ', <b>resume not uploaded</b>'}"
            f"{f', kept {len(report.kept)} already filled' if report.kept else ''}.<br>"
            f"<b>Orange dashed</b> = AI-drafted answer, check it ({len(report.drafted)}). "
            f"<b>Red</b> = required, needs you ({needs_you}).<br>"
            "Nothing has been submitted. Review everything, then click Submit yourself."
        ),
        short=f"{len(report.filled)} filled · {needs_you} need you",
    )
    return report
