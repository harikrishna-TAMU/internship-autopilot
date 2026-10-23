# ApplyPilot

Turns internship links you email yourself (for example from @zero2sudo's Instagram Stories) into a tracked job list, a tailored one-page resume per job, and pre-filled applications in Chrome.

**It never submits anything.** It fills the form and stops. You review each tab and click Submit yourself.

```
phone:  story → copy link / screenshot → email you+jobs@gmail.com
laptop: autopilot start    → in the background: reads Gmail, extracts each job, scores fit,
                             tailors a resume, and opens a filled application tab in Chrome
you:    review each tab → Submit (the job is marked submitted when the site confirms)
```

**Highlights**
- **Capture by email.** Links or screenshots, no Instagram scraping. Screenshots are read with Claude vision, and missing links are found on Greenhouse, Lever and Ashby.
- **Honest tailoring.** Every resume bullet traces to a bullet in your profile. Invented skills or numbers are dropped, and a second Claude pass reverts overstated rewrites. Output uses your own LaTeX template.
- **Safe form filling.** All clicks go through a submit-proof safety layer. Consent and self-ID questions are always left for you, and drafted answers are highlighted for review.
- **Nothing drops silently.** Duplicates, expired or closed postings, ineligible and low-fit roles are logged with a reason and can be restored.

| Doc | What's in it |
|---|---|
| [`COMMANDS.md`](COMMANDS.md) | Every command, plus the setup checklist |
| [`PROJECT_SPEC.md`](PROJECT_SPEC.md) | Design and decisions |
| [`LICENSE`](LICENSE) | MIT |

## Set it up for yourself

This repo has no one's personal data in it. Everything that's yours stays on your laptop in gitignored files: `.env`, `config/profile.yaml`, `credentials.json`, `token.json` and `data/`. Nothing is shared with the repo owner or anyone else. Each person uses their own Claude API key, their own Gmail, and their own Google Cloud project.

**You need:** a Mac (tested on macOS; Linux should work; Windows is untested), Google Chrome, [Homebrew](https://brew.sh), a personal Gmail account, and a Claude API key with some credit (about $0.30 per job).

```bash
git clone https://github.com/harikrishna-TAMU/ApplyPilot.git
cd ApplyPilot
```

(If the repo is private, ask the owner to add you as a collaborator first.)

Then follow **Setup** below, steps 1–6 (about 30 minutes). What you make your own:

| What | Where | Notes |
|---|---|---|
| Your resume content | `config/profile.yaml` | The only source used for resumes and forms. Easiest: open the repo in Claude Code or Cursor and ask it to fill this file from your resume, then check every line |
| Resume file name | `resume_filename` in the profile | e.g. `Jane_Doe_Resume.pdf` |
| Links shown on the resume | `resume_links` in the profile | All links are still used to fill applications |
| Visa / sponsorship answers | `work_authorization` in the profile | Used only in applications, never printed on the resume |
| Fit threshold, locations, tabs | `config/settings.yaml` | e.g. `min_fit_score: 55`, `locations: ["Texas", "Remote"]` |
| Resume look (optional) | `src/autopilot/resume/templates/resume.tex.j2` | A LaTeX template; edit it to match your own format |
| Capture address | `GMAIL_ADDRESS` in `.env` | You email links to it with `+jobs` added |

Then run `uv run autopilot doctor` until every line is ✓, and `uv run autopilot start`. [`COMMANDS.md`](COMMANDS.md) is the day-to-day reference.

## Setup (one time, about 20 minutes)

### 1. Install

Needs Google Chrome. From this folder:

```bash
brew install uv tectonic # Python manager + LaTeX compiler for the resume PDFs
uv sync                  # installs Python 3.12 + dependencies into .venv
uv run autopilot init    # creates .env, config/profile.yaml, data/ and the database
```

Without Tectonic, resumes use a simpler built-in HTML template instead (`resume_format` in `config/settings.yaml`).

Run commands as `uv run autopilot …`, or activate the env once per terminal (`source .venv/bin/activate`) and just type `autopilot …`.

### 2. Claude API key

Create a key at <https://console.anthropic.com> and put it in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-opus-5-5
GMAIL_ADDRESS=yourname@gmail.com
```

If `autopilot doctor` says your key isn't tied to a workspace, add `ANTHROPIC_WORKSPACE_ID=wrkspc_...` to `.env`; the ID is under Console → Settings → Workspaces.

Cost: roughly $0.30 per job on Claude Opus 5.5 (extract + score + tailor + verify + form mapping). Jobs skipped as ineligible or low-fit stop early and cost less. You can change `CLAUDE_MODEL` (for example `claude-sonnet-5-5`, about half the price).

### 3. Your profile

Fill in `config/profile.yaml` from your resume (it's gitignored). This is the **only** source the tailoring and form filling may use:

- Give every entry and bullet a unique id (`exp1`, `exp1.b2`, `proj3.b1`).
- Add every true bullet you have, more than fits on one page. Tailoring picks and reorders; it never invents. Numbers must already be in a bullet to appear on a resume.
- `work_authorization`: answers to "authorized to work?" and "need sponsorship?" questions. Leave `null` to always answer those yourself.
- `eeo`: optional. `null` leaves gender/race/veteran/disability questions for you.
- `answers`: saved answers for common questions (`how_did_you_hear`, `availability`, …).

The pipeline refuses to score or tailor while the profile still has `TODO` placeholders.

### 4. Gmail (the capture inbox)

Use a **personal** Gmail account, not a university one: universities control API access to their accounts, and you lose them after graduating.

1. Go to <https://console.cloud.google.com>, create a project (e.g. "applypilot").
2. **APIs & Services → Library → Gmail API → Enable.**
3. **APIs & Services → OAuth consent screen**: User type *External*. Fill the app name and your email. Under *Test users*, add your Gmail address.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app.** Download the JSON and save it in this folder as `credentials.json` (gitignored).
5. Run:
   ```bash
   uv run autopilot gmail-auth
   ```
   A browser opens; sign in with that Gmail and allow access. (Google warns the app is unverified because it's your own; that's expected.) This also creates the `Jobs` and `Jobs/Processed` labels.
6. In Gmail: **Settings → See all settings → Filters → Create a new filter**. To: `yourname+jobs@gmail.com` → *Create filter* → tick **Skip the Inbox** and **Apply the label: Jobs** → *Create filter*.

If ingest later says Gmail isn't authorized, run `autopilot gmail-auth` again. Google expires tokens for apps in *Testing* mode after 7 days. To stop that, set the consent screen's publishing status to *In production*. It stays a private app that only you use.

### 5. Review browser (optional, recommended)

```bash
uv run autopilot browser-setup
```

ApplyPilot fills applications in its own Chrome window (a separate profile in `data/chrome-profile`; your everyday Chrome can't be automated, by Chrome's design). This opens that window as a **normal** Chrome once so you can:
- sign into Chrome and turn on sync (keep **Passwords** on), so Chrome offers your saved passwords on job-site login pages
- log into job portals you use (Workday tenants etc.) so they stay signed in.

Quit it with ⌘Q when done. ApplyPilot never reads or types passwords: Chrome offers them and you click.

### 6. Check everything

```bash
uv run autopilot doctor
```

## Daily use

**Capture (phone, about 5 seconds):** when a story has a job link, open the link sticker, copy the URL and email it to `yourname+jobs@gmail.com`. If the story is just a screenshot of a posting, email the screenshot. Several links per email are fine; the subject is an optional note.

### Background mode (recommended): one command

```bash
uv run autopilot start
```

Run it once, for example after turning on the laptop. Then close the terminal; it keeps running in the background:

- Every 15 minutes it checks Gmail, extracts each job, scores it, and tailors your resume.
- For every ready job, it opens a tab in a separate Chrome window ("review window") and fills the application. New tabs open only while you aren't using that window, so it never changes a page you're reading. On a Mac you get a notification.
- **When you're free:** switch to the review window, go through the tabs, and click **Submit** on each one you like. When the site shows its "thank you for applying" page, the job is marked submitted automatically.
- Closing a tab without submitting means "later": it won't reopen on its own. `autopilot open` brings closed tabs back (already filled, no extra Claude cost).

```bash
uv run autopilot status            # running? how many waiting? recent log
uv run autopilot open              # reopen tabs you closed for later
uv run autopilot stop              # stops it and closes the review window
uv run autopilot start --no-tabs   # only tailor resumes; fill later with `autopilot fill`
```

It stops when the laptop shuts down or restarts. Run `uv run autopilot start` again after that. Tabs for jobs you haven't submitted come back filled. The log is in `data/autopilot.log`.

### Manual mode: step by step

```bash
uv run autopilot ingest            # check Gmail → extract → score → tailor
uv run autopilot add <url-or-screenshot>   # skip email, e.g. from the laptop
uv run autopilot fill              # opens a Chrome tab per tailored job and fills it
uv run autopilot fill --ids 3,5    # just these
uv run autopilot mark 12 submitted # after you click Submit yourself
```

### Either mode

```bash
uv run autopilot list              # by deadline; red = due within 3 days
uv run autopilot show 12           # fit notes, resume changes, drafted answers, history
uv run autopilot mark 14 skipped --note "not interested"
```

**Resumes** land in `data/resumes/<Company Name>/` under the name set by `resume_filename` in your profile (e.g. `Jane_Doe_Resume.pdf`), plus its `.tex` source, with `tailor.json` beside it explaining every change. A second role at the same company gets its own folder, `<Company Name> - <Role>`.

In each filled tab:
- **Orange dashed outline**: an answer Claude drafted. Read and edit it.
- **Red outline**: a required field left for you (consent boxes, self-ID questions, anything not in your profile).
- A banner at the top summarizes what was filled. Nothing is submitted.
- **Sites that need a login** (Workday, company portals): the tab opens on the sign-in page and you get a notification. Sign in (or create the account) yourself, and the form is filled as soon as it appears. On multi-step forms, click **Next** yourself; each new page is filled too, and anything already entered is kept.
- LinkedIn tabs are only opened, never filled (LinkedIn bans automation).

The review window uses its own Chrome profile in `data/chrome-profile`, separate from your everyday Chrome. In manual mode, the `fill` command waits until you close that window.

**Fix links:** screenshots without a findable posting show as `needs_link`:

```bash
uv run autopilot set-link 7 https://jobs.lever.co/acme/...
```

### Rejection log

Every job the pipeline drops is logged with a readable reason. Nothing disappears silently.

```bash
uv run autopilot rejected                    # newest first
uv run autopilot rejected --reason low_fit --since 7d
uv run autopilot restore 9                   # it was wrong: put job 9 back in the pipeline
```

| Reason | Meaning |
|---|---|
| `duplicate` | Same link, or same company + role + term, as an earlier job |
| `expired` | The deadline has passed |
| `closed` | The posting says it's closed, or it's gone (404) |
| `ineligible` | A hard requirement excludes you (grad year, sponsorship, degree), compared with your profile |
| `low_fit` | Fit score below `min_fit_score` (55) in `config/settings.yaml` |
| `user_skipped` | You ran `mark <id> skipped` |
| `error` | A step failed; `restore` retries it |

The log is also written to `data/rejected.csv` after every run (opens in Excel or Google Sheets). `autopilot export` writes `data/jobs.csv` too.

If Gmail access expires (see Setup step 4), the background worker sends a notification and keeps the tabs it already has. Run `uv run autopilot gmail-auth`, and the next check picks it up; no restart is needed.

## Safety rules (enforced in code)

1. **Never submits.** All clicks go through `fill/safety.py`, which refuses anything that looks like submit/apply/next/confirm. A guard cancels form submission while filling, and a test fails if any other module clicks, presses keys or calls `submit()`.
2. **No Instagram scraping.** Capture is manual.
3. **No invented experience.** Resume bullets must cite a profile bullet ID. A validator drops unknown skills and IDs and reverts any rewrite that adds numbers. A second Claude pass reverts rewrites that add claims. Every change is listed in `tailor.json` next to the PDF.
4. **No CAPTCHAs, accounts or passwords.** Sign-in pages are never filled and ApplyPilot never reads or types passwords; you sign in (Chrome can offer your saved passwords), then it fills the form.
5. **No LinkedIn automation.** LinkedIn tabs are only opened.
6. **Secrets stay out of git:** `.env`, `credentials.json`, `token.json`, `config/profile.yaml`, `data/`.

## Development

```bash
uv run pytest            # Claude, Gmail and the network are faked; browser tests use headless Chrome
uv run ruff check src tests && uv run ruff format --check src tests
```

Layout: `src/autopilot/` has `ingest/` (Gmail), `extract/` (links, ATS APIs, JD fetch, screenshots), `match/` (fit score), `resume/` (tailor, validate, render), `fill/` (Chrome, safety, adapters), `pipeline.py` (stage orchestration), `rejections.py` and `cli.py`. Claude prompts are in `src/autopilot/llm/prompts/`.
