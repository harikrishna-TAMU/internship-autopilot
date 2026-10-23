"""Gmail API ingest: read emails sent to you+jobs@gmail.com, save screenshots, label them processed."""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from autopilot.textutil import html_to_text

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailAuthError(RuntimeError):
    pass


@dataclass
class AttachmentRef:
    filename: str
    mime: str
    attachment_id: str | None = None
    data: bytes | None = None


@dataclass
class ParsedEmail:
    message_id: str
    received_at: str
    subject: str
    sender: str
    text: str
    html: str
    attachments: list[AttachmentRef] = field(default_factory=list)

    @property
    def body(self) -> str:
        return self.text.strip() or html_to_text(self.html)

    @property
    def images(self) -> list[AttachmentRef]:
        return [a for a in self.attachments if a.mime.startswith("image/")]


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def parse_message(msg: dict) -> ParsedEmail:
    """Turn a Gmail API `format=full` message into text, HTML and attachment references."""
    payload = msg.get("payload", {}) or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", []) or []}
    texts: list[str] = []
    htmls: list[str] = []
    attachments: list[AttachmentRef] = []

    def walk(part: dict) -> None:
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        filename = part.get("filename") or ""
        is_file = bool(filename) or (mime.startswith("image/") and (body.get("attachmentId") or body.get("data")))
        if is_file:
            if not filename:
                ext = mimetypes.guess_extension(mime) or ""
                filename = f"image{len(attachments) + 1}{ext}"
            attachments.append(
                AttachmentRef(
                    filename=filename,
                    mime=mime,
                    attachment_id=body.get("attachmentId"),
                    data=_b64(body["data"]) if body.get("data") and not body.get("attachmentId") else None,
                )
            )
        elif mime == "text/plain" and body.get("data"):
            texts.append(_b64(body["data"]).decode("utf-8", "replace"))
        elif mime == "text/html" and body.get("data"):
            htmls.append(_b64(body["data"]).decode("utf-8", "replace"))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    # Two screenshots called "image.png" must not overwrite each other on disk.
    seen: dict[str, int] = {}
    for a in attachments:
        key = a.filename.lower()
        if key in seen:
            seen[key] += 1
            stem, dot, ext = a.filename.rpartition(".")
            a.filename = f"{stem}-{seen[key]}.{ext}" if dot else f"{a.filename}-{seen[key]}"
        else:
            seen[key] = 1
    ms = int(msg.get("internalDate") or 0)
    received = datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone().isoformat(timespec="seconds")
    return ParsedEmail(
        message_id=msg["id"],
        received_at=received,
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        text="\n".join(texts),
        html="\n".join(htmls),
        attachments=attachments,
    )


def safe_filename(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "attachment"
    return base[:120]


def search_label(name: str) -> str:
    """Gmail search syntax for a label name: 'Jobs/Processed' → 'jobs-processed'."""
    return re.sub(r"[\s/]+", "-", name.strip().lower())


class GmailClient:
    def __init__(
        self, credentials_path: Path, token_path: Path, label: str, processed_label: str, capture_address: str | None
    ):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.label = label
        self.processed_label = processed_label
        self.capture_address = capture_address
        self.service = None
        self.processed_label_id: str | None = None

    @classmethod
    def from_config(cls, cfg) -> GmailClient:
        return cls(
            cfg.credentials_path,
            cfg.token_path,
            cfg.settings.gmail_label,
            cfg.settings.processed_label,
            cfg.capture_address,
        )

    # -- auth ----------------------------------------------------------------

    def connect(self, interactive: bool = False) -> str:
        """Authorize (opening a browser the first time if interactive). Returns the account's address."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if creds and not creds.valid and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                creds = None
        if not creds or not creds.valid:
            if not interactive:
                raise GmailAuthError("Gmail isn't authorized yet. Run `autopilot gmail-auth`.")
            if not self.credentials_path.exists():
                raise GmailAuthError(
                    f"{self.credentials_path.name} not found in the project folder. "
                    "See README → 'Gmail setup' to create it in Google Cloud."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        os.chmod(self.token_path, 0o600)
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self.service.users().getProfile(userId="me").execute()["emailAddress"]

    def ensure_labels(self) -> None:
        """Create the 'Jobs' and 'Jobs/Processed' labels if they don't exist."""
        labels = self.service.users().labels().list(userId="me").execute().get("labels", [])
        by_name = {lab["name"]: lab["id"] for lab in labels}
        for name in (self.label, self.processed_label):
            if name not in by_name:
                created = (
                    self.service.users()
                    .labels()
                    .create(
                        userId="me",
                        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
                    )
                    .execute()
                )
                by_name[name] = created["id"]
        self.processed_label_id = by_name[self.processed_label]

    # -- reading -------------------------------------------------------------

    def query(self) -> str:
        sources = [f"label:{search_label(self.label)}"]
        if self.capture_address:
            sources.append(f"to:{self.capture_address}")
        return f"({' OR '.join(sources)}) -label:{search_label(self.processed_label)}"

    def fetch_unprocessed(self, already_done: Callable[[str], bool], limit: int = 200) -> list[ParsedEmail]:
        """New capture emails, oldest first. `already_done(id)` skips ones the local DB has finished."""
        if self.processed_label_id is None:
            self.ensure_labels()
        ids: list[str] = []
        page_token = None
        while len(ids) < limit:
            resp = (
                self.service.users()
                .messages()
                .list(userId="me", q=self.query(), maxResults=min(100, limit - len(ids)), pageToken=page_token)
                .execute()
            )
            ids += [m["id"] for m in resp.get("messages", [])]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        out: list[ParsedEmail] = []
        for mid in ids:
            if already_done(mid):
                self.mark_processed(mid)  # local DB says done; make sure Gmail agrees
                continue
            full = self.service.users().messages().get(userId="me", id=mid, format="full").execute()
            if self.processed_label_id in (full.get("labelIds") or []):
                continue
            out.append(parse_message(full))
        out.sort(key=lambda e: e.received_at)
        return out

    def download_attachment(self, message_id: str, ref: AttachmentRef, dest_dir: Path) -> Path:
        data = ref.data
        if data is None:
            resp = (
                self.service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=ref.attachment_id)
                .execute()
            )
            data = _b64(resp["data"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / safe_filename(ref.filename)
        path.write_bytes(data)
        return path

    def mark_processed(self, message_id: str) -> None:
        self.service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [self.processed_label_id]}
        ).execute()
