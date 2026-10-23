# ApplyPilot: project spec

A personal tool that turns internship links (captured from @zero2sudo's Instagram Stories) into a tracked job list, a tailored resume per job, and pre-filled applications in Chrome. **Nothing is ever submitted automatically. A human reviews and clicks submit.**

This file holds the final design decisions, including options that were considered and rejected (Instagram API, Telegram, WhatsApp) in favor of email capture.

---

## 1. Non-negotiable rules

These apply to every module. Coding agents must follow them.

1. **Never submit an application.** No code path may click a submit, apply or send button, or post a final application. Filling stops at "ready for review".
2. **Never scrape Instagram.** Stories aren't available via Meta's API, and logged-in scraping risks an account ban. Capture is manual (one email per job).
3. **Never invent experience.** Resume tailoring may only select, reorder and rephrase content that exists in `profile.yaml`. Every output bullet must trace back to a source bullet ID.
4. **Never bypass CAPTCHAs, create accounts, or handle passwords.** Stop and leave those for the user.
5. **No automation on LinkedIn.** Its user agreement bans bots. For LinkedIn or Easy Apply links, only open the tab.
6. **Secrets and personal data stay out of git:** `.env`, `credentials.json`, `token.json`, `config/profile.yaml`, `data/`.
7. **Nothing drops out silently.** Any job that leaves the pipeline before `awaiting_review` (skipped, duplicate, expired, closed or errored) must get a row in the rejection log with a reason code and a readable explanation. See §3.7.

---

## 2. Final architecture

```
 Phone                         Laptop / scheduler                                  You
 ─────                         ─────────────────────────────────────────────       ───
 Story notification
   → copy link or screenshot
   → email to you+jobs@gmail.com
                               [ingest]  Gmail API: label "Jobs", unprocessed
                                   ↓
                               [extract] links / screenshot → job record (Claude)
                                   ↓      fetch job description (JD)
                               [store]   SQLite jobs.db (dedupe, status)
                                   ↓
                               [match]   fit score + eligibility (Claude)
                                   ↓
                               [tailor]  profile.yaml → tailored resume PDF
                                   ↓
                               [fill]    Playwright, visible Chrome, one tab per job
                                         fill + upload, STOP before submit
                                                                                  review each tab
                                                                                  click submit
                                                                                  `autopilot mark <id> submitted`
```

### Capture (manual, ~5 seconds per job)

- Capture inbox: a **personal Gmail account**, not the university account. The address is `GMAIL_ADDRESS` in `.env`; the capture address is the same with `+jobs` added (`capture_tag` in `settings.yaml`).
  - Why not the university address: it's a university-managed Google account, so university IT controls whether apps can read it through the Gmail API, and access ends after graduation.
  - The university (TAMU) address is the contact email on resumes and applications: `basics.email` in `profile.yaml`.
  - Both real addresses live only in those two gitignored files, so this repo can be public.
- Turn on story notifications for @zero2sudo.
- When a story contains a job link, open the link sticker, copy the URL, and email it to the capture address.
- If the story is only a screenshot of a posting, email the screenshot.
- Gmail filter: `to:(<capture address>)` → apply label `Jobs`, skip inbox.
- Multiple links per email are allowed. The subject line is free text (optional note).

### Job status lifecycle

```
new → extracted → (needs_link) → scored → tailored → awaiting_review → submitted

Exits before awaiting_review (each one writes a rejection log row, §3.7):
  skipped   reason: duplicate | expired | closed | ineligible | low_fit | user_skipped
  error     reason: error (stage + message recorded)
```

`autopilot restore <id>` moves a skipped or errored job back into the pipeline (§3.7).

---

## 3. Modules

### 3.1 `ingest`: Gmail API

- Google Cloud project → enable Gmail API → OAuth client ID (Desktop app) → `credentials.json`.
- Scope: `https://www.googleapis.com/auth/gmail.modify` (needed to add a "processed" label).
- Query: `(label:jobs OR to:<capture address>) -label:jobs-processed`, plus a local check against `emails.processed_at` (so a missing filter or label never causes reprocessing).
- `autopilot gmail-auth` runs the OAuth flow once and creates the `Jobs` and `Jobs/Processed` labels.
- For each message: store `message_id`, `received_at`, `subject`, plain-text body, and image attachments (saved to `data/attachments/<message_id>/`).
- Each link or screenshot becomes one `jobs` row keyed by `source_key` (`<message_id>:<url or file>`), so capture is idempotent. After that, add label `Jobs/Processed`.
- An email with no usable link or image (or only an Instagram/Linktree link) becomes a `needs_link` job with an explanation, never silently dropped.
- Run every 15 minutes (cron or Windows Task Scheduler), or via `autopilot ingest --watch`.
- Optional later variant: a Google Apps Script time-driven trigger that writes to a Google Sheet (runs in the cloud with the laptop off).

### 3.2 `extract`: job records

- Pull URLs from the body with a regex; unwrap redirect/shortener links (follow redirects with httpx). Drop Instagram and Linktree links after resolving them where possible.
- Screenshots: send the image to Claude (vision) and extract `company`, `role`, `term`, `location`, `deadline`, and any visible URL.
- Screenshot with no URL: try public ATS board APIs to find the posting:
  - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{company_token}/jobs`
  - Lever: `https://api.lever.co/v0/postings/{company}?mode=json`
  - If not found → status `needs_link` (shown in the review list for the user to fix).
  - Ashby: `https://api.ashbyhq.com/posting-api/job-board/{org}`
- Fetch the job description from public ATS APIs where possible (Greenhouse, Lever, Ashby, Workday `wday/cxs`), which also give the application form URL. A company page with `?gh_jid=` finds its Greenhouse board from the page or the domain. Otherwise httpx + trafilatura, falling back to headless Chrome when the text is thin (JS-rendered pages).
- Claude call with a strict JSON schema (`JobExtraction` in `models.py`; `ats` comes from the URL and `jd_text` from the fetch, not from Claude):

```json
{
  "company": "string",
  "role": "string",
  "term": "Summer 2027 | Fall 2026 | ...",
  "location": "string",
  "remote": "onsite | hybrid | remote | unknown",
  "deadline": "YYYY-MM-DD | null",
  "apply_url": "string",
  "ats": "greenhouse | lever | ashby | workday | linkedin | other",
  "requirements": ["string"],
  "eligibility": {"grad_years": ["2027", "2028"], "work_authorization": "string | null", "degree": "string | null"},
  "jd_text": "string"
}
```

- Dedupe on normalized `apply_url` (no tracking params, `boards.`/`job-boards.` Greenhouse unified, Lever `/apply` stripped), then on `(company, role, term)`. The duplicate's row is marked `skipped` with `duplicate_of` set and logged as `duplicate` with a pointer to the original job.
- Deadline already passed → `skipped`, reason `expired`.
- The posting page says it's no longer accepting applications, or a known ATS returns 404 → `skipped`, reason `closed`.

### 3.3 `match`: fit score

- Claude compares the JD against `profile.yaml` and returns `{score: 0-100, eligible: bool, blockers: [], matched_skills: [], missing_skills: [], notes}`.
- Eligibility blockers (grad year, work authorization, degree level) → `skipped`, reason `ineligible`. The detail names each blocker against the profile, e.g. "Requires grad year 2026; you graduate 2028-05". A deterministic grad-year check backs up Claude when the posting lists explicit years.
- `score < settings.min_fit_score` (default 55) → `skipped`, reason `low_fit`. The detail includes the score, the threshold, missing skills and Claude's notes.
- Both can be overridden with `autopilot restore <id>`.

### 3.4 `tailor`: resume

- `config/profile.yaml` is the master source of truth. Every bullet has a stable ID:

```yaml
basics: {name, email, phone, location, links: {github, linkedin, portfolio}}
education:
  - id: edu1
    school: ...
    degree: ...
    grad: 2028-05
    gpa: 3.8
    coursework: [...]
experience:
  - id: exp1
    org: ...
    title: ...
    dates: ...
    bullets:
      - {id: exp1.b1, text: "...", skills: [python, fastapi]}
projects:
  - id: proj1
    name: ...
    link: ...
    bullets:
      - {id: proj1.b1, text: "...", skills: [react, aws]}
skills: {languages: [...], frameworks: [...], tools: [...]}
work_authorization: ...
answers:   # reusable answers for common application questions
  why_internship: "..."
  pronouns: "..."
```

- Claude returns a selection, not free text: the sections and bullets to include (by `source_id`), each with an optionally rephrased `text`, plus an ordered skills list drawn only from `profile.yaml`.
- **Validation (`resume/validate.py`), required before rendering:**
  - every `source_id` exists in the profile
  - every skill in the output exists in the profile
  - no numbers or metrics appear that aren't in the source bullet
  - a second Claude pass checks each rephrased bullet against its source, rejecting any new claims
  - fits on one page (re-render with fewer bullets if it overflows)
- Validation failures are retried once with the problems fed back; anything still wrong gets the safe fix (drop the item, or revert the bullet to its source wording), never passed through. Unverified rewrites are reverted too.
- Render: Jinja2 HTML template → PDF via headless Chrome (Playwright; no system libraries needed), page count via `pypdf`. Output: `data/resumes/<Company Name>/<resume_filename>` (`basics.resume_filename` in the profile, e.g. `Hari_Krishna_Resume.pdf`; default `First_Last_Resume.pdf`), plus `tailor.json` in the same folder listing what was changed, fixed, reverted, or dropped to fit, and why. A second role at the same company goes in `<Company Name> - <Role>`; re-tailoring a job overwrites its own folder.

### 3.5 `fill`: Chrome form filling

- Playwright `launch_persistent_context(user_data_dir="data/chrome-profile", channel="chrome", headless=False)`. Use a dedicated profile, not the everyday one.
- `autopilot fill` opens a tab for every job in status `tailored` (or a chosen subset), up to `settings.max_tabs`.
- ATS adapters: `greenhouse`, `lever`, `ashby` first, then `generic` (label-based field discovery). For `linkedin`, **open the tab only**. Workday and other portals with logins open on their sign-in page and are filled after you sign in (see FollowUp below).
- Field mapping: collect each field (label, name, type, required, options; dropdown options are read by opening them) → name/email/phone/links/resume filled directly from the profile → Claude maps the rest to profile values / reusable answers → fill. Upload the tailored PDF via `set_input_files`. Very long dropdowns are shortlisted before prompting; searchable dropdowns are typed into and the best option picked.
- Free-text questions: Claude drafts an answer from the profile + JD (same `map_fields` call) and it's stored in `answers_json` for review. Drafts get an orange dashed outline; required fields left for the user get a red outline; a banner summarizes.
- Always left for the user, whatever Claude says: consent/attestation/terms/privacy questions, file uploads other than the resume, and sign-in pages (ApplyPilot never reads or types passwords).
- Only pages that look like application forms are filled (at least two inputs including a name or email field); a job page with only an upload box (e.g. a "match your resume to jobs" widget) is left alone.
- **After sign-in and multi-step forms (`FollowUp`):** each tab is watched; when its set of fields changes (you signed in, or clicked Next), the new page is filled with `keep_existing` (values you or the site entered are kept) and the resume is uploaded at most once. Pages already filled are never refilled. LinkedIn is open-only and never followed.
- **Review window profile:** a dedicated Chrome profile (`data/chrome-profile`); Chrome blocks automating the everyday profile. It launches without Playwright's `--disable-sync`, `--password-store=basic` and `--use-mock-keychain`, so Chrome sync can offer saved passwords and sessions persist. `autopilot browser-setup` opens it as normal Chrome once to sign in.
- **Safety (`fill/safety.py`):** all clicks and checks go through `safe_click()` / `safe_check()`, which refuse `type=submit|image|reset` and any button/link whose text, value, aria-label, title, id, name, href or label matches `/submit|apply|send|finish|complete|confirm|next|continue|review|proceed|sign up|sign in|log in|register|create account|save and|pay|purchase/i` (clicking into a text box is allowed). While filling, a guard cancels every form submission and `form.submit()` / `requestSubmit()`; it's removed afterwards so the user's own Submit works. `tests/test_safety.py` fails if any other module clicks, checks, presses keys, uses the keyboard/mouse, dispatches events, or calls `submit()` from page JavaScript.
- If the form page shows the posting is closed → `skipped`, reason `closed`, and close the tab.
- After filling: status `awaiting_review`. Leave the tab open.
- Alternative to building this module: the **Simplify Copilot** Chrome extension (free) autofills Workday, Greenhouse, Lever, iCIMS, Taleo and more. If using it, keep "Continuously autofill multipage forms" **off**.

### 3.6 `review`: CLI

```
autopilot start [--no-tabs]         # background worker (§3.8): Gmail → tailor → filled tabs, survives closing the terminal
autopilot status | open [--ids] | stop   # worker status; reopen tabs closed for later; stop (closes the review window)
autopilot init                      # config files, data/, database
autopilot doctor                    # checks key, Gmail, profile, Chrome
autopilot gmail-auth                # one-time OAuth + labels
autopilot ingest [--watch]          # pull new emails → extract → score → tailor
autopilot run                       # process already-captured jobs (no Gmail)
autopilot add <url-or-screenshot>   # capture without email
autopilot list [--status STATUS] [--all]   # table: id, company, role, deadline, score, status
autopilot show <id>                 # JD summary, fit notes, resume changes, drafted answers
autopilot fill [--ids 3,5,8]        # open + fill tabs (stops before submit)
autopilot mark <id> submitted|skipped|needs_link --note "..."   # skipped → logged as user_skipped
autopilot set-link <id> <url>       # fix needs_link items
autopilot rejected [--reason CODE] [--since 7d]   # rejection log (§3.7)
autopilot restore <id>              # override a skip / retry an error
autopilot export --csv              # optional: data/jobs.csv or a Google Sheet
```

Sort `list` by deadline, soonest first. Highlight jobs due within 3 days.

### 3.7 Rejection log

Every job that doesn't reach `awaiting_review` is recorded with the reason, so you can check what the pipeline dropped and override it if it was wrong.

| Reason code | Stage | When | Detail recorded |
|---|---|---|---|
| `duplicate` | extract | Same normalized `apply_url`, or same `(company, role, term)`, as an existing job | ID of the original job, and which key matched |
| `expired` | extract | Deadline is before today | The deadline |
| `closed` | extract / fill | Posting says it's no longer accepting applications, or the ATS returns 404 | Page text snippet or HTTP status |
| `ineligible` | match | Eligibility blocker: grad year, work authorization, degree level | Each blocker, compared with your profile |
| `low_fit` | match | `score < min_fit_score` | Score, threshold, missing skills, Claude's notes |
| `user_skipped` | review | You ran `autopilot mark <id> skipped` | Your `--note` |
| `error` | any | A step failed after retries (JD fetch, tailoring validation, etc.) | Stage and error message |

`needs_link` is not a rejection. Those jobs are waiting on you and appear in `autopilot list --status needs_link`.

**Where it shows up:**
- `rejections` table in `jobs.db`: the source of truth (§4).
- `autopilot rejected`: table of date, job id, company, role, stage, reason and detail, newest first. Filter with `--reason` and `--since`.
- `data/rejected.csv`: rewritten after every run, so it opens in Excel or Google Sheets without the CLI.
- End of each `ingest` run: a one-line summary, e.g. `5 captured: 2 tailored, 1 low_fit, 1 ineligible, 1 duplicate`.

**Overriding (`autopilot restore <id>`):**
- `low_fit`, `ineligible`, `expired`, `closed`, `user_skipped`: the job goes back into the pipeline after the stage that skipped it, so the check that rejected it doesn't run again.
- `error`: the failed stage is retried.
- `duplicate`: can't be restored. Use the original job instead.
- The log row is kept, with `overridden_at` set, so the history stays complete.

### 3.8 Background worker (`autopilot start`)

The intended daily workflow: start once, email links from the phone, and review/submit whenever free.

- `autopilot start` spawns a detached process (`autopilot worker`, PID in `data/autopilot.pid`, log in `data/autopilot.log`) that survives closing the terminal and stops on `autopilot stop` or shutdown.
- Every `poll_minutes`: Gmail ingest, then extract → match → tailor (same pipeline as `ingest`).
- About every 20 s: for each job in `tailored`/`awaiting_review` with no tab, open a tab in the review window (persistent Chrome profile) and fill it, up to `max_tabs`. New tabs are only opened while the user isn't focused on that window. One Playwright instance is shared by the headless renderer and the review window.
- `jobs.tab_state`: `open` (tab exists), `dismissed` (the user closed it without submitting: "later"; `autopilot open` clears it), NULL (needs a tab). It's reset on worker start, since earlier tabs are gone.
- Submission is detected, never performed: when a tab shows confirmation wording ("Thank you for applying", "Application submitted", …) that wasn't on the page when it was filled, the job becomes `submitted`.
- Reopening a tab reuses the saved field answers (`answers_json.actions_by_label`), so it costs no new Claude call.
- A macOS notification announces new filled tabs and expired Gmail access. Fatal errors (bad key, outage) skip the round and retry later; the worker keeps running.
- `fill` and `ingest` refuse to run while the worker is running, since they would compete for the Chrome profile and Gmail.

---

## 4. Data model (SQLite `data/jobs.db`)

```
emails(message_id PK, received_at, subject, body, processed_at)
attachments(id PK, message_id FK, path, mime)
jobs(id PK, message_id FK, source_key UNIQUE, source_kind, source_value, hints_json,
     company, role, term, location, remote, deadline,
     apply_url, apply_url_norm UNIQUE, form_url, ats, jd_text, requirements_json, eligibility_json,
     fit_score, fit_json, status, resume_path, tailor_json, answers_json,
     notes, overrides, duplicate_of FK NULL, tab_state, created_at, updated_at)
events(id PK, job_id FK, at, from_status, to_status, detail)
rejections(id PK, job_id FK NULL, message_id FK, at, stage, reason_code, detail,
           company, role, apply_url, duplicate_of FK NULL,
           restore_to,                         -- status `restore` returns the job to
           overridden_at NULL)
```

`jobs.status` values: `new, extracted, needs_link, scored, tailored, awaiting_review, submitted, skipped, error`. The reason for a `skipped` or `error` status is always in `rejections`. `jobs.overrides` lists reason codes the user overrode with `restore`, so those checks are skipped for that job.

---

## 5. Repo layout

```
ApplyPilot/
├── README.md
├── PROJECT_SPEC.md
├── COMMANDS.md
├── LICENSE
├── pyproject.toml
├── .env.example            # ANTHROPIC_API_KEY=, CLAUDE_MODEL=, GMAIL_ADDRESS=
├── .gitignore              # .env, credentials.json, token.json, config/profile.yaml, data/
├── config/
│   ├── settings.yaml       # min_fit_score, max_tabs, poll_minutes, labels, locations, effort per step
│   ├── profile.example.yaml
│   └── profile.yaml        # gitignored: your real profile
├── data/                   # gitignored: jobs.db, attachments/, resumes/, chrome-profile/, rejected.csv
├── src/autopilot/
│   ├── cli.py              # Typer
│   ├── config.py           # pydantic-settings
│   ├── db.py
│   ├── models.py           # profile, statuses, Claude output schemas
│   ├── pipeline.py         # capture → extract → match → tailor orchestration
│   ├── worker.py           # background worker: polling, review-window tabs, submit detection
│   ├── rejections.py       # reject(), restore(), rejected.csv export, run summary
│   ├── headless.py         # shared headless Chrome (JD rendering, PDFs)
│   ├── textutil.py
│   ├── llm/claude.py       # Anthropic SDK wrapper, structured outputs, fallback, error handling
│   ├── llm/prompts/        # extract.md, screenshot.md, fit.md, tailor.md, verify.md, map_fields.md
│   ├── ingest/gmail_client.py
│   ├── extract/{links.py, screenshot.py, jd_fetcher.py, ats_lookup.py, job_extractor.py}
│   ├── match/fit_scorer.py
│   ├── resume/{tailor.py, validate.py, render.py, templates/resume.html.j2}
│   └── fill/{browser.py, filler.py, safety.py, field_mapper.py, adapters/{greenhouse,lever,ashby,generic}.py}
└── tests/                  # fakes for Claude/Gmail/network; fixture form HTML; safety, rejection-log, validator tests
```

Stack: Python 3.11+ (3.12 via `uv`), `anthropic`, `google-api-python-client`, `google-auth-oauthlib`, `httpx`, `trafilatura`, `playwright`, `jinja2`, `pypdf`, `typer`, `pydantic`, `pydantic-settings`, `pyyaml`, `rich`, `pytest`, `ruff`.

Claude model: `CLAUDE_MODEL` in `.env`, default `claude-opus-5-5`. Every call uses structured outputs (`output_config.format`), an effort level per step from `settings.yaml`, and server-side refusal fallback (`fallbacks: "default"`). A bad key, outage or rate limit stops the run without marking jobs as errors; the next run continues.

---

## 6. Milestones

| # | Milestone | Done when |
|---|-----------|-----------|
| M1 | Gmail ingest + SQLite | Emails under `Jobs` land in `emails`; reruns create no duplicates; processed label applied |
| M2 | Extraction + JD fetch | Link emails and screenshot emails both produce `jobs` rows; `needs_link` works; duplicates, expired and closed postings are logged in `rejections` |
| M3 | Fit scoring + rejection log | Ineligible and low-fit roles skipped and logged with readable reasons; `autopilot rejected` and `restore` work; a test checks every skip path writes exactly one `rejections` row; `autopilot list` shows scores |
| M4 | Tailoring + PDF | One-page PDF per job; validator rejects an injected fake skill (test) |
| M5 | Chrome filling | Greenhouse + Lever forms filled in a visible tab; safety test passes; nothing is ever submitted |
| M6 | Review CLI + scheduling | Full loop: email → filled tab → `mark submitted` |
| M7 (optional) | Apps Script / Google Sheet sync, deadline reminders | |

**Status (2026-10-22):** M1–M6 are implemented, including the Ashby adapter and the background worker (§3.8). They're covered by 72 offline tests: Claude, Gmail and the network are faked, and the form-fill and PDF tests use real headless Chrome. JD fetching and form-field discovery were also checked read-only against live Greenhouse, Lever and Ashby postings. A full live run is still pending the inputs in §7.

---

## 7. Inputs the user must provide

- `config/profile.yaml` filled from the resume, with as many real projects and bullets as possible (it starts with the TAMU email and school filled in; the remaining `TODO`s block tailoring until replaced).
- `ANTHROPIC_API_KEY` in `.env` (`GMAIL_ADDRESS` is already set there).
- Google Cloud OAuth `credentials.json` (Desktop app) for Gmail, created with the personal Gmail account, then `autopilot gmail-auth`.
- The Gmail filter (`to:` capture address → label `Jobs`, skip inbox). `gmail-auth` creates the labels.
- Decided: capture inbox = personal Gmail; applications and resumes use the TAMU email.

## 8. Sources

- Meta Business Discovery API (no Stories support): https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery
- Instagram API updates, Apr 2026: https://developers.facebook.com/blog/post/2026/04/22/instagram-api-updates-for-partnerships-metrics-collaboration-and-engagement/
- zero2sudo posts job links in Stories: https://www.tiktok.com/@zero2sudo/video/7617517663262199070
- Simplify Copilot: https://simplify.jobs/copilot
- Simplify autofill settings: https://help.simplify.jobs/articles/8686025-manage-autofill-settings-in-the-simplify-extension
