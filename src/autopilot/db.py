"""SQLite storage (data/jobs.db): emails, attachments, jobs, events, rejections."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from autopilot.models import Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    message_id   TEXT PRIMARY KEY,
    received_at  TEXT,
    subject      TEXT,
    body         TEXT,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL REFERENCES emails(message_id),
    path       TEXT NOT NULL,
    mime       TEXT,
    UNIQUE (message_id, path)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        TEXT REFERENCES emails(message_id),
    source_key        TEXT NOT NULL UNIQUE,      -- one captured link/image; makes capture idempotent
    source_kind       TEXT NOT NULL,             -- link | image | note
    source_value      TEXT,                      -- the URL, or the screenshot path
    hints_json        TEXT,                      -- details read from a screenshot
    company           TEXT,
    role              TEXT,
    term              TEXT,
    location          TEXT,
    remote            TEXT,
    deadline          TEXT,
    apply_url         TEXT,
    apply_url_norm    TEXT UNIQUE,               -- NULL for duplicates
    form_url          TEXT,                      -- where the application form is, if different
    ats               TEXT,
    jd_text           TEXT,
    requirements_json TEXT,
    eligibility_json  TEXT,
    fit_score         INTEGER,
    fit_json          TEXT,
    status            TEXT NOT NULL,
    resume_path       TEXT,
    tailor_json       TEXT,
    answers_json      TEXT,
    notes             TEXT,
    overrides         TEXT NOT NULL DEFAULT '',  -- reason codes the user overrode with `restore`
    duplicate_of      INTEGER REFERENCES jobs(id),
    tab_state         TEXT,                      -- background worker: open | dismissed | NULL
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    at          TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS rejections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER REFERENCES jobs(id),
    message_id    TEXT REFERENCES emails(message_id),
    at            TEXT NOT NULL,
    stage         TEXT NOT NULL,
    reason_code   TEXT NOT NULL,
    detail        TEXT NOT NULL,
    company       TEXT,
    role          TEXT,
    apply_url     TEXT,
    duplicate_of  INTEGER REFERENCES jobs(id),
    restore_to    TEXT,                          -- status `restore` returns the job to
    overridden_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_rejections_job ON rejections(job_id);
"""

JOB_COLUMNS = {
    "message_id",
    "source_key",
    "source_kind",
    "source_value",
    "hints_json",
    "company",
    "role",
    "term",
    "location",
    "remote",
    "deadline",
    "apply_url",
    "apply_url_norm",
    "form_url",
    "ats",
    "jd_text",
    "requirements_json",
    "eligibility_json",
    "fit_score",
    "fit_json",
    "status",
    "resume_path",
    "tailor_json",
    "answers_json",
    "notes",
    "overrides",
    "duplicate_of",
    "tab_state",
}

# Columns added after the first release, for databases created before them.
MIGRATIONS = {"tab_state": "ALTER TABLE jobs ADD COLUMN tab_state TEXT"}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def norm_text(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        # The background worker and CLI commands share the file; wait for each other's writes.
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for col, sql in MIGRATIONS.items():
            if col not in cols:
                self.conn.execute(sql)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- emails ------------------------------------------------------------

    def add_email(self, message_id: str, received_at: str, subject: str, body: str) -> bool:
        """Returns True if this email is new."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO emails (message_id, received_at, subject, body) VALUES (?, ?, ?, ?)",
            (message_id, received_at, subject, body),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def email_processed(self, message_id: str) -> bool:
        row = self.conn.execute("SELECT processed_at FROM emails WHERE message_id = ?", (message_id,)).fetchone()
        return bool(row and row["processed_at"])

    def mark_email_processed(self, message_id: str) -> None:
        self.conn.execute("UPDATE emails SET processed_at = ? WHERE message_id = ?", (now(), message_id))
        self.conn.commit()

    def add_attachment(self, message_id: str, path: str, mime: str | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO attachments (message_id, path, mime) VALUES (?, ?, ?)",
            (message_id, path, mime),
        )
        self.conn.commit()

    # -- jobs --------------------------------------------------------------

    def create_job(
        self, *, source_key: str, source_kind: str, status: Status = Status.NEW, **fields: Any
    ) -> int | None:
        """Insert a job. Returns its id, or None if this source_key was already captured."""
        unknown = set(fields) - JOB_COLUMNS
        if unknown:
            raise ValueError(f"unknown job columns: {unknown}")
        ts = now()
        cols = {
            "source_key": source_key,
            "source_kind": source_kind,
            "status": str(status),
            "created_at": ts,
            "updated_at": ts,
            **fields,
        }
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = self.conn.execute(f"INSERT OR IGNORE INTO jobs ({names}) VALUES ({marks})", list(cols.values()))
        if cur.rowcount != 1:
            self.conn.commit()
            return None
        job_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO events (job_id, at, from_status, to_status, detail) VALUES (?, ?, NULL, ?, ?)",
            (job_id, ts, str(status), f"captured ({source_kind})"),
        )
        self.conn.commit()
        return job_id

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - JOB_COLUMNS
        if unknown:
            raise ValueError(f"unknown job columns: {unknown}")
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), job_id])
        self.conn.commit()

    def set_status(self, job_id: int, status: Status | str, detail: str | None = None) -> None:
        row = self.get_job(job_id)
        if row is None:
            raise KeyError(f"job {job_id} not found")
        ts = now()
        self.conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (str(status), ts, job_id))
        self.conn.execute(
            "INSERT INTO events (job_id, at, from_status, to_status, detail) VALUES (?, ?, ?, ?, ?)",
            (job_id, ts, row["status"], str(status), detail),
        )
        self.conn.commit()

    def jobs_with_status(self, *statuses: Status | str) -> list[sqlite3.Row]:
        marks = ", ".join("?" for _ in statuses)
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id", [str(s) for s in statuses]
        ).fetchall()

    def find_by_url(self, url_norm: str, exclude_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE apply_url_norm = ? AND id != ? AND duplicate_of IS NULL",
            (url_norm, exclude_id),
        ).fetchone()

    def find_by_identity(
        self, company: str | None, role: str | None, term: str | None, exclude_id: int
    ) -> sqlite3.Row | None:
        """Same (company, role, term), ignoring case and spacing. Only canonical (non-duplicate) jobs."""
        if not company or not role:
            return None
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE id != ? AND duplicate_of IS NULL AND company IS NOT NULL AND role IS NOT NULL",
            (exclude_id,),
        ).fetchall()
        key = (norm_text(company), norm_text(role), norm_text(term))
        for r in rows:
            if (norm_text(r["company"]), norm_text(r["role"]), norm_text(r["term"])) == key:
                return r
        return None

    def list_jobs(self, statuses: Iterable[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM jobs"
        args: list[Any] = []
        if statuses:
            statuses = list(statuses)
            sql += f" WHERE status IN ({', '.join('?' for _ in statuses)})"
            args = statuses
        sql += " ORDER BY CASE WHEN deadline IS NULL OR deadline = '' THEN 1 ELSE 0 END, deadline, id"
        return self.conn.execute(sql, args).fetchall()

    def events_for(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()

    def last_event(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()

    # -- rejections ----------------------------------------------------------

    def add_rejection(self, **fields: Any) -> int:
        fields.setdefault("at", now())
        names = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(f"INSERT INTO rejections ({names}) VALUES ({marks})", list(fields.values()))
        self.conn.commit()
        return int(cur.lastrowid)

    def rejections(
        self, reason: str | None = None, since: str | None = None, include_overridden: bool = True
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM rejections WHERE 1=1"
        args: list[Any] = []
        if reason:
            sql += " AND reason_code = ?"
            args.append(reason)
        if since:
            sql += " AND at >= ?"
            args.append(since)
        if not include_overridden:
            sql += " AND overridden_at IS NULL"
        sql += " ORDER BY at DESC, id DESC"
        return self.conn.execute(sql, args).fetchall()

    def open_rejection(self, job_id: int) -> sqlite3.Row | None:
        """The latest rejection for this job that hasn't been overridden."""
        return self.conn.execute(
            "SELECT * FROM rejections WHERE job_id = ? AND overridden_at IS NULL ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()

    def mark_overridden(self, rejection_id: int) -> None:
        self.conn.execute("UPDATE rejections SET overridden_at = ? WHERE id = ?", (now(), rejection_id))
        self.conn.commit()


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
