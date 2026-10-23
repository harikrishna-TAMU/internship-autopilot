"""Find the form's fields and decide what goes in each: profile basics directly, the rest via Claude."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from autopilot.fill.safety import SubmitBlocked, dismiss_popup, safe_click
from autopilot.llm.claude import LLM
from autopilot.match.fit_scorer import job_summary
from autopilot.models import FieldAction, FieldMapping, Profile

TEXT_KINDS = {"text", "email", "tel", "url", "number", "date", "textarea"}
RESUME_UPLOAD = "__resume__"

# Tags every field with data-autopilot-id (radios/checkbox groups share one id + data-autopilot-opt).
DISCOVER_JS = r"""
() => {
  document.querySelectorAll('[data-autopilot-id]').forEach(e => { e.removeAttribute('data-autopilot-id'); e.removeAttribute('data-autopilot-opt'); });
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const textOf = id => { const n = document.getElementById(id); return n ? clean(n.innerText) : ''; };
  const visible = el => {
    if (el.type === 'file') return true;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    if (r.width > 0 && r.height > 0) return true;
    return (el.type === 'radio' || el.type === 'checkbox') && el.closest('label') !== null;
  };
  const ownLabel = el => {
    let t = '';
    const lb = el.getAttribute('aria-labelledby');
    if (lb) t = lb.split(/\s+/).map(textOf).join(' ');
    if (!t && el.labels && el.labels.length) t = Array.from(el.labels).map(l => clean(l.innerText)).join(' ');
    if (!t) t = el.getAttribute('aria-label') || '';
    return clean(t);
  };
  // Button words that label upload widgets instead of the field ("Attach", "Upload").
  const GENERIC = /^(attach|upload|browse|choose( a)? file|select( a)? file|enter manually|select\.\.\.)$/i;
  const nearbyLabel = el => {
    let p = el.parentElement;
    for (let i = 0; p && i < 5; i++, p = p.parentElement) {
      const cands = p.querySelectorAll('label, legend, .application-label, [class*="label"], [class*="question"]');
      for (const c of cands) {
        const t = clean(c.innerText);
        if (!c.contains(el) && t && !GENERIC.test(t)) return t;
      }
    }
    return '';
  };
  const humanize = s => clean(String(s || '').replace(/[_\-\[\]]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2'));
  const labelFor = el => {
    let own = ownLabel(el);
    if (GENERIC.test(own)) own = '';
    return clean(own || nearbyLabel(el) || el.getAttribute('placeholder') || humanize(el.getAttribute('name') || el.id)).slice(0, 300);
  };
  const groupLabel = els => {
    const optionTexts = els.map(e => ownLabel(e)).filter(Boolean);
    const fs = els[0].closest('fieldset');
    if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText).slice(0, 300);
    const rg = els[0].closest('[role=radiogroup], [role=group]');
    if (rg) {
      const lb = rg.getAttribute('aria-labelledby');
      if (lb) return clean(lb.split(/\s+/).map(textOf).join(' ')).slice(0, 300);
      if (rg.getAttribute('aria-label')) return clean(rg.getAttribute('aria-label')).slice(0, 300);
    }
    let p = els[0].parentElement;
    while (p && !els.every(e => p.contains(e))) p = p.parentElement;
    for (let i = 0; p && i < 3; i++, p = p.parentElement) {
      let t = clean(p.innerText);
      for (const o of optionTexts) t = t.replace(o, ' ');
      t = clean(t);
      if (t) return t.slice(0, 300);
    }
    return '';
  };
  const required = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label);

  const out = [];
  const seen = new Set();
  let n = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = String(el.type || '').toLowerCase();
    const isCombo = el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete');
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (el.disabled || (el.readOnly && !isCombo)) continue;
    // Decoy inputs that dropdown widgets add for validation (aria-hidden, not focusable).
    if (el.getAttribute('aria-hidden') === 'true' && type !== 'file') continue;
    if (!visible(el)) continue;
    if (type === 'password') { out.push({ id: '', kind: 'password', label: '', name: '', required: false, options: [], max_length: null, current_value: '' }); continue; }

    if (type === 'radio' || type === 'checkbox') {
      const key = type + ':' + (el.name || ('#' + n));
      if (el.name && seen.has(key)) continue;
      const group = el.name ? Array.from(document.querySelectorAll('input[type="' + type + '"]')).filter(g => g.name === el.name) : [el];
      if (group.length > 1) {
        seen.add(key);
        const id = 'f' + (++n);
        group.forEach((g, i) => { g.setAttribute('data-autopilot-id', id); g.setAttribute('data-autopilot-opt', String(i)); });
        const label = groupLabel(group);
        out.push({ id, kind: type === 'radio' ? 'radio' : 'checkbox_group', label, name: el.name || '',
                   required: group.some(g => g.required) || /\*\s*$/.test(label),
                   options: group.map(g => ownLabel(g) || g.value), max_length: null,
                   current_value: group.filter(g => g.checked).map(g => ownLabel(g) || g.value).join(' | ') });
        continue;
      }
    }
    const id = 'f' + (++n);
    el.setAttribute('data-autopilot-id', id);
    const label = labelFor(el);
    let kind = type || 'text';
    let options = [];
    if (el.tagName === 'SELECT') {
      kind = 'select';
      options = Array.from(el.options).map(o => clean(o.text)).filter((t, i) => t && !(i === 0 && /^(select|choose|please|--)/i.test(t)));
    } else if (el.tagName === 'TEXTAREA') {
      kind = 'textarea';
    } else if (isCombo) {
      kind = 'combobox';
    } else if (!['email', 'tel', 'url', 'number', 'date', 'file', 'checkbox', 'radio'].includes(kind)) {
      kind = 'text';
    }
    out.push({ id, kind, label, name: el.getAttribute('name') || el.id || '', required: required(el, label), options,
               max_length: el.maxLength > 0 ? el.maxLength : null,
               current_value: (kind === 'file' || kind === 'checkbox') ? (el.checked ? 'checked' : '') : String(el.value || '').slice(0, 200),
               accept: el.getAttribute('accept') || '' });
  }
  return out;
}
"""


@dataclass
class FormField:
    id: str
    kind: str
    label: str
    name: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    max_length: int | None = None
    current_value: str = ""
    accept: str = ""
    searchable: bool = False  # a type-to-search dropdown whose option list was cut off

    def for_prompt(self) -> dict:
        d = asdict(self)
        for k in ("accept", "name", "searchable"):
            d.pop(k)
        if self.searchable:
            d["note"] = (
                "searchable dropdown: only the first options are listed. Use `select` with the exact value "
                "you want (e.g. the school name); it will be typed in and searched."
            )
        return d


def discover_fields(page: Page) -> list[FormField]:
    raw = page.evaluate(DISCOVER_JS)
    return [FormField(**{k: v for k, v in r.items() if k in FormField.__dataclass_fields__}) for r in raw]


def read_combobox_options(page: Page, fields: list[FormField], limit: int = 80) -> None:
    """Open each dropdown-style combobox briefly to read its choices, so Claude can pick an exact one."""
    for f in fields:
        if f.kind != "combobox" or f.options:
            continue
        loc = page.locator(f'[data-autopilot-id="{f.id}"]').first
        try:
            safe_click(loc)
            page.wait_for_timeout(400)
            found = [t.strip() for t in page.locator('[role="option"]:visible').all_inner_texts() if t.strip()]
            f.options = found[:limit]
            f.searchable = len(found) >= limit
        except (SubmitBlocked, PlaywrightError):
            f.options = []
        finally:
            try:
                dismiss_popup(page)
            except PlaywrightError:
                pass


# -- deterministic mapping for the basics --------------------------------------------


def _basics_rules(profile: Profile) -> list[tuple[re.Pattern, str | None]]:
    b = profile.basics
    return [
        (re.compile(r"preferred\s*(first\s*)?name|nick\s*name", re.I), b.preferred_name),
        (re.compile(r"first\s*name|given\s*name|^first$", re.I), b.first_name),
        (re.compile(r"last\s*name|family\s*name|surname|^last$", re.I), b.last_name),
        (re.compile(r"^(full\s*|legal\s*|your\s*)?name\s*\*?$", re.I), b.name),
        (re.compile(r"e-?mail", re.I), b.email),
        (re.compile(r"phone|mobile|cell", re.I), b.phone),
        (re.compile(r"linked\s*in", re.I), b.links.linkedin),
        (re.compile(r"git\s*hub", re.I), b.links.github),
        (re.compile(r"portfolio|personal\s*(web)?site|^website", re.I), b.links.portfolio),
    ]


def basic_value(f: FormField, profile: Profile) -> str | None:
    if f.kind not in {"text", "email", "tel", "url"}:
        return None
    for text in (f.label, f.name):
        text = re.sub(r"[\s*✱]+$", "", text or "")  # required markers: "Full name ✱", "Email *"
        # Long labels are real questions ("How did you hear… email newsletter?"), not basics fields.
        if not text or len(text) > 60:
            continue
        # Someone else's contact details, or a second number: not your basics.
        if re.search(r"alternate|alternative|secondary|other|emergency|reference|referr|recruiter|manager", text, re.I):
            return None
        for pattern, value in _basics_rules(profile):
            if pattern.search(text):
                return value or None
    return None


def is_resume_field(f: FormField) -> bool:
    text = f"{f.label} {f.name}".lower()
    return f.kind == "file" and ("resume" in text or "cv" in text.split() or "résumé" in text) and "cover" not in text


LONG_LIST = 50


def profile_keywords(profile: Profile) -> set[str]:
    """Words likely to appear in the right option of a very long dropdown (schools, majors, places)."""
    words: set[str] = set()
    texts = [profile.basics.location or ""]
    for e in profile.education:
        texts += [e.school, e.degree, e.major or "", e.location or ""]
    texts += ["United States", "USA", "Decline", "Prefer not"]
    for t in texts:
        words.update(
            w
            for w in re.findall(r"[A-Za-z&]{3,}", t.casefold())
            if w not in {"the", "and", "university", "college", "school", "bachelor", "master", "science", "arts", "of"}
        )
    return words


def shortlist(options: list[str], keywords: set[str], keep: int = 40) -> list[str]:
    """For huge dropdowns (every university in the world), send Claude only plausible options."""
    scored = []
    for i, o in enumerate(options):
        hits = sum(1 for w in re.findall(r"[A-Za-z&]{3,}", o.casefold()) if w in keywords)
        if hits:
            scored.append((-hits, i, o))
    picked = [o for _, _, o in sorted(scored)[:keep]]
    head = [o for o in options[:8] if o not in picked]
    return head + picked


def leave(field_id: str, reason: str) -> FieldAction:
    return FieldAction(field_id=field_id, action="leave_for_user", value=None, is_draft=False, reason=reason)


def map_fields(
    llm: LLM, profile: Profile, job, fields: list[FormField], cache: dict | None = None
) -> dict[str, FieldAction]:
    """`cache` (label → saved action from an earlier fill of this job) avoids asking Claude again on reopen."""
    actions: dict[str, FieldAction] = {}
    rest: list[FormField] = []
    resume_done = False
    for f in fields:
        if f.kind == "file":
            if is_resume_field(f) and not resume_done:
                actions[f.id] = FieldAction(
                    field_id=f.id, action="fill", value=RESUME_UPLOAD, is_draft=False, reason="tailored resume"
                )
                resume_done = True
            else:
                actions[f.id] = leave(f.id, "file upload other than the resume")
            continue
        value = basic_value(f, profile)
        if value:
            actions[f.id] = FieldAction(
                field_id=f.id, action="fill", value=value, is_draft=False, reason="profile basics"
            )
        else:
            rest.append(f)
    # If no file field mentions a resume but there's exactly one file input, it's the resume.
    files = [f for f in fields if f.kind == "file"]
    if not resume_done and len(files) == 1:
        actions[files[0].id] = FieldAction(
            field_id=files[0].id, action="fill", value=RESUME_UPLOAD, is_draft=False, reason="tailored resume"
        )

    if rest and cache:
        still = []
        for f in rest:
            saved = cache.get(f.label)
            if saved and saved.get("kind") == f.kind:
                actions[f.id] = FieldAction(
                    field_id=f.id,
                    action=saved["action"],
                    value=saved.get("value"),
                    is_draft=bool(saved.get("is_draft")),
                    reason=saved.get("reason"),
                )
            else:
                still.append(f)
        rest = still

    if rest:
        keywords = profile_keywords(profile)
        prompt_fields = []
        for f in rest:
            d = f.for_prompt()
            if len(f.options) > LONG_LIST:
                d["options"] = shortlist(f.options, keywords)
                d.setdefault(
                    "note", f"long list ({len(f.options)} options); only likely ones shown. Leave it if none fit."
                )
            prompt_fields.append(d)
        user = (
            f"<profile>\n{profile.to_yaml()}\n</profile>\n"
            f"<job>\n{job_summary(job)}\n</job>\n"
            f"<fields>\n{json.dumps(prompt_fields, indent=1)}\n</fields>"
        )
        ids = {f.id for f in rest}
        for a in llm.ask("map_fields", FieldMapping, user).actions:
            if a.field_id in ids:
                actions[a.field_id] = a
    for f in fields:
        actions.setdefault(f.id, leave(f.id, "no answer in your profile"))
    return actions
