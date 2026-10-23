# Autopilot: command reference

Every command, what it does, and when to use it. `README.md` has the background; this file is for quick lookup.

## How to run a command

1. In VS Code: **Terminal → New Terminal**. It opens in the project folder.
   (Or in the macOS Terminal app: `cd ~/Documents/"Application pipeline"`.)
2. Type the command with `uv run` in front:
   ```bash
   uv run autopilot status
   ```
3. Shortcut: run `source .venv/bin/activate` once per terminal, then you can drop `uv run` and type `autopilot status`.

`uv run autopilot --help` lists every command. `uv run autopilot <command> --help` explains one.

---

## One-time setup

Do these once, in order. The "What you need to do" section at the bottom has the detailed steps.

| Step | Command / action | What it does |
|---|---|---|
| 1 | Put your Claude API key in `.env` | Lets the pipeline call Claude |
| 2 | Fill in `config/profile.yaml` | Your resume content; the only source used for tailoring and filling |
| 3 | Save Google's OAuth file as `credentials.json` in the project folder | Lets the pipeline read your Gmail |
| 4 | `uv run autopilot gmail-auth` | Opens a browser once to authorize Gmail; creates the `Jobs` and `Jobs/Processed` labels |
| 5 | Create the Gmail filter | Sends emails to your `+jobs` address to the `Jobs` label, skipping the inbox |
| 6 | `uv run autopilot doctor` | Checks everything; all lines should show ✓ |

Other setup commands:

| Command | What it does |
|---|---|
| `uv run autopilot init` | Creates config files, the `data/` folder and the database. Already done; safe to run again |
| `uv run autopilot doctor` | Checks the API key, Gmail address, Gmail authorization, profile and Chrome. Shows the capture address and the email that goes on applications |

---

## Everyday workflow (background mode)

1. **Start it once**, for example after turning on the laptop. Then you can close the terminal:
   ```bash
   uv run autopilot start
   ```
2. **On your phone**: when a story has a job, open the link sticker, copy the link and email it to your capture address (your Gmail with `+jobs`, which `start` and `doctor` print). If the story is only a screenshot, email the screenshot.
3. **In the background**, every 15 minutes it reads new emails, extracts each job, scores your fit and tailors your resume. Then it opens a filled application tab in a separate Chrome window (the "review window"), and you get a Mac notification.
4. **When you're free**: go to the review window, check each tab, and click **Submit**. When the site shows "thank you for applying", the job is marked submitted automatically.

### Background worker commands

| Command | What it does |
|---|---|
| `uv run autopilot start` | Starts the background worker: Gmail check → tailoring → filled tabs. Keeps running after you close the terminal |
| `uv run autopilot start --no-tabs` | Same, but only tailors resumes. No tabs open; use `autopilot fill` when you're ready |
| `uv run autopilot status` | Whether it's running, how many jobs are waiting for review (open tabs vs. closed for later), how many are in progress or need a link, plus the recent log |
| `uv run autopilot open` | Reopens every tab you closed for later, already filled, with no extra Claude cost |
| `uv run autopilot open --ids 4,9` | Reopens just those jobs |
| `uv run autopilot stop` | Stops the worker and closes the review window. Asks first, because anything typed in unsubmitted tabs is lost |
| `uv run autopilot stop --yes` | Stops without asking |

Good to know:
- New tabs only open while you're **not** using the review window.
- Closing a tab without submitting means "later". It won't reopen until `autopilot open` or a restart.
- At most 8 tabs are open at once (`max_tabs` in `config/settings.yaml`). More jobs wait until you submit or close some.
- After a shutdown or restart, run `uv run autopilot start` again. Unsubmitted jobs come back as filled tabs.
- While the worker runs, `ingest` and `fill` refuse to run, because they would compete for Gmail and Chrome. Use `open`, or `stop` first.

---

## Manual mode (if you don't use the background worker)

| Command | What it does |
|---|---|
| `uv run autopilot ingest` | Checks Gmail once, then extracts, scores and tailors every new job |
| `uv run autopilot ingest --watch` | Same, every 15 minutes, while the terminal stays open (Ctrl+C stops it) |
| `uv run autopilot run` | Processes jobs already captured, without checking Gmail (e.g. after `restore` or after finishing your profile) |
| `uv run autopilot add <link>` | Adds a job link directly, without emailing it, and processes it |
| `uv run autopilot add <screenshot.png>` | Same for a screenshot file |
| `uv run autopilot add <link> --note "Google SWE"` | Adds a note, like an email subject |
| `uv run autopilot fill` | Opens Chrome with a filled tab for every tailored job. Waits until you close the window |
| `uv run autopilot fill --ids 3,5,8` | Fills just those jobs |
| `uv run autopilot fill --no-wait` | Closes Chrome as soon as filling finishes (rarely useful) |
| `uv run autopilot mark <id> submitted` | Records that you submitted. In background mode this happens automatically |

---

## Looking at your jobs

| Command | What it does |
|---|---|
| `uv run autopilot list` | Active jobs sorted by deadline, soonest first. **Red** = due within 3 days |
| `uv run autopilot list --all` | Includes submitted, skipped and errored jobs |
| `uv run autopilot list --status needs_link` | Only one status. Statuses: `new`, `extracted`, `needs_link`, `scored`, `tailored`, `awaiting_review`, `submitted`, `skipped`, `error` |
| `uv run autopilot show <id>` | Everything about one job: details, requirements, fit score and notes, resume changes, drafted answers, fields left for you, and its history |

What the statuses mean:

| Status | Meaning |
|---|---|
| `new` → `extracted` → `scored` | Being processed |
| `needs_link` | The posting couldn't be found (e.g. a screenshot without a link). Waiting on you |
| `tailored` | Resume ready; waiting for a tab |
| `awaiting_review` | Tab filled; waiting for you to review and submit |
| `submitted` | Done |
| `skipped` / `error` | Dropped; `autopilot rejected` says why |

---

## Recording and fixing jobs

| Command | What it does |
|---|---|
| `uv run autopilot mark <id> submitted` | You submitted it (automatic in background mode) |
| `uv run autopilot mark <id> skipped --note "not interested"` | You don't want it. It goes in the rejection log with your note |
| `uv run autopilot mark <id> needs_link` | Flags a job whose link is wrong |
| `uv run autopilot set-link <id> <url>` | Gives a `needs_link` (or wrong) job its real posting link and processes it right away |

---

## Rejection log (jobs the pipeline dropped)

| Command | What it does |
|---|---|
| `uv run autopilot rejected` | Every dropped job, newest first, with the reason and an explanation |
| `uv run autopilot rejected --reason low_fit` | Only one reason |
| `uv run autopilot rejected --since 7d` | Only recent ones. Also `12h`, `2w`, or a date like `2026-10-01` |
| `uv run autopilot restore <id>` | The pipeline was wrong: put the job back, skipping the check that dropped it. For `error`, it retries. Then run `autopilot run` (or wait for the worker) |

| Reason | Meaning |
|---|---|
| `duplicate` | Same link, or same company + role + term, as an earlier job (can't be restored; use the original) |
| `expired` | The deadline has passed |
| `closed` | The posting says it's closed, or it no longer exists |
| `ineligible` | A hard requirement excludes you (grad year, sponsorship, degree) |
| `low_fit` | Fit score below 55 (`min_fit_score` in `config/settings.yaml`) |
| `user_skipped` | You ran `mark <id> skipped` |
| `error` | A step failed; `restore` retries it |

---

## Files and exports

| Command / location | What it is |
|---|---|
| `uv run autopilot export` | Writes `data/jobs.csv` and `data/rejected.csv` (open in Excel or Google Sheets) |
| `data/resumes/<Company Name>/<resume_filename>` | The tailored resume for that company (e.g. `Jane_Doe_Resume.pdf`) |
| `data/resumes/<Company Name>/tailor.json` | What was changed in the resume and why |
| `data/resumes/<Company Name> - <Role>/` | A second role at the same company |
| `data/rejected.csv` | The rejection log, updated after every run |
| `data/autopilot.log` | The background worker's log |
| `config/profile.yaml` | Your profile (edit anytime; it's used from the next job on) |
| `config/settings.yaml` | Fit threshold, check interval, max tabs, preferred locations |
| `.env` | API key, model and Gmail address |

---

## In each filled tab

| You see | Meaning |
|---|---|
| **Orange dashed outline** | An answer Claude drafted. Read it and make it yours |
| **Red outline** | A required field left for you (consent boxes, self-ID questions, anything not in your profile) |
| Banner at the top right | Summary of what was filled. Nothing has been submitted |
| Workday / LinkedIn tab | Only opened, not filled: Workday needs an account, and LinkedIn bans automation. Fill these yourself |

The tool never clicks Submit. You always do.

---

## Troubleshooting

| Message or problem | Fix |
|---|---|
| "No Claude API key found" | Add `ANTHROPIC_API_KEY=...` to `.env` |
| "Gmail isn't authorized yet" / Mac notification about Gmail | `uv run autopilot gmail-auth` (Google expires test-mode access every 7 days; see setup step 3) |
| "Finish config/profile.yaml first … TODO placeholders" | Replace every `TODO` in `config/profile.yaml`, then `uv run autopilot run` |
| "The background worker is running and already opens tabs" | Use `autopilot open`, or `autopilot stop` before `fill` / `ingest` |
| No tabs appearing | `uv run autopilot status`: is it running? Are jobs waiting? Did you close them (then `autopilot open`)? Check `data/autopilot.log` |
| A job you wanted was skipped | `uv run autopilot rejected`, then `uv run autopilot restore <id>` |
| Laptop restarted | `uv run autopilot start` |

---

## What you need to do (setup, about 20–30 minutes)

### 1. Claude API key (about 3 min)
1. Go to <https://console.anthropic.com>, sign in, and add credits under **Billing** (it's prepay; about $0.30 per job).
2. **API Keys → Create Key**, and copy it.
3. Open `.env` in this folder and paste it after `ANTHROPIC_API_KEY=` (no spaces, no quotes).

### 2. Your profile (about 10–15 min, or send Claude your resume and it drafts this)
Open `config/profile.yaml` and replace every `TODO`:
- `name` (exactly as it should appear on the resume), `phone`, LinkedIn and GitHub links.
- Education: degree, major, start and graduation month (`2028-05` format), GPA (or `null`), and coursework.
- Experience and projects. Every true bullet you have, each with a unique `id`. Include numbers only if they're real.
- `skills`: languages, frameworks, tools.
- `work_authorization`: whether you're authorized to work in the US and whether you need sponsorship now or later. `null` = always leave those questions for you.
- `answers`: optional saved answers, like `how_did_you_hear: "Social media"`.

### 3. Google `credentials.json` (about 10 min)
1. Go to <https://console.cloud.google.com> and sign in with your **personal Gmail**.
2. Create a project (top bar → project picker → **New project**, e.g. "internship-autopilot").
3. **APIs & Services → Library**, search "Gmail API", **Enable**.
4. **APIs & Services → OAuth consent screen** (may be called **Google Auth Platform**): user type **External**, any app name, your email as the contact. Under **Test users / Audience**, add your personal Gmail.
5. **Credentials / Clients → Create → OAuth client ID → Desktop app**, then **Download JSON**.
6. Rename the downloaded file (`client_secret_….json`) to `credentials.json` and move it into this project folder.
7. Optional, to stop the 7-day re-authorization: on the consent screen, change the publishing status to **In production**. It's still a private app only you use. Google shows an "unverified app" warning; choose *Advanced → continue*.

### 4. Authorize Gmail (1 min)
```bash
uv run autopilot gmail-auth
```
A browser opens. Sign in with your personal Gmail and allow access. It prints your capture address.

### 5. Gmail filter (2 min)
In Gmail: **Settings (gear) → See all settings → Filters and Blocked Addresses → Create a new filter**.
- **To:** your capture address (your Gmail with `+jobs` before the `@`)
- **Create filter** → tick **Skip the Inbox** and **Apply the label: Jobs** → **Create filter**.

### 6. Instagram (1 min)
On the account that posts the jobs you want (e.g. @zero2sudo), tap the bell and turn on **Stories** notifications.

### 7. Check and start
```bash
uv run autopilot doctor     # every line should be ✓
uv run autopilot start
```
Send yourself one test link to check that a tab appears within about 15 minutes.
