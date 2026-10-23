"""Paths, settings (.env + config/settings.yaml) and the profile loader."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from autopilot.models import Profile

DEFAULT_MODEL = "claude-opus-5-5"


class Env(BaseSettings):
    """Secrets and personal values from `.env` (never committed)."""

    model_config = SettingsConfigDict(extra="ignore")

    llm_backend: str = "api"  # api (API credits) | claude-code (your Claude Pro/Max plan via Claude Code)
    claude_code_model: str | None = None  # claude-code only; empty = your plan's default model
    claude_code_config_dir: str | None = None  # claude-code only; empty = Claude Code's default login
    anthropic_api_key: str | None = None
    anthropic_workspace_id: str | None = None  # only for keys not tied to a Console workspace
    claude_model: str = DEFAULT_MODEL
    gmail_address: str | None = None


class Effort(BaseModel):
    """Claude effort level per task (low | medium | high | xhigh | max)."""

    extract: str = "low"
    screenshot: str = "low"
    fit: str = "medium"
    tailor: str = "high"
    verify: str = "medium"
    map_fields: str = "medium"


class AppSettings(BaseModel):
    """Values from config/settings.yaml. Safe to commit: no personal data here."""

    min_fit_score: int = 55
    max_tabs: int = 8
    poll_minutes: int = 15
    gmail_label: str = "Jobs"
    processed_label: str = "Jobs/Processed"
    capture_tag: str = "jobs"
    locations: list[str] = Field(default_factory=list)
    browser_channel: str = "chrome"
    max_resume_pages: int = 1
    resume_format: str = "latex"  # latex (your template, via Tectonic) | html
    effort: Effort = Field(default_factory=Effort)


@dataclass
class Config:
    home: Path
    env: Env
    settings: AppSettings

    @property
    def config_dir(self) -> Path:
        return self.home / "config"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.db"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"

    @property
    def chrome_profile_dir(self) -> Path:
        return self.data_dir / "chrome-profile"

    @property
    def rejected_csv(self) -> Path:
        return self.data_dir / "rejected.csv"

    @property
    def profile_path(self) -> Path:
        return self.config_dir / "profile.yaml"

    @property
    def credentials_path(self) -> Path:
        return self.home / "credentials.json"

    @property
    def token_path(self) -> Path:
        return self.home / "token.json"

    @property
    def capture_address(self) -> str | None:
        """`name@gmail.com` → `name+jobs@gmail.com`."""
        if not self.env.gmail_address or "@" not in self.env.gmail_address:
            return None
        local, domain = self.env.gmail_address.split("@", 1)
        return f"{local}+{self.settings.capture_tag}@{domain}"

    def ensure_dirs(self) -> None:
        for d in (self.config_dir, self.data_dir, self.attachments_dir, self.resumes_dir):
            d.mkdir(parents=True, exist_ok=True)

    def load_profile(self) -> Profile:
        return load_profile(self.profile_path)


def find_home(start: Path | None = None) -> Path:
    """Project root: $AUTOPILOT_HOME, else the nearest folder (upwards) with pyproject.toml or config/."""
    if os.environ.get("AUTOPILOT_HOME"):
        return Path(os.environ["AUTOPILOT_HOME"]).expanduser().resolve()
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "pyproject.toml").exists() or (d / "config" / "settings.yaml").exists():
            return d
    return here


def load_config(home: Path | None = None) -> Config:
    home = home or find_home()
    env = Env(_env_file=home / ".env", _env_file_encoding="utf-8")  # type: ignore[call-arg]
    settings_path = home / "config" / "settings.yaml"
    raw = {}
    if settings_path.exists():
        raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    return Config(home=home, env=env, settings=AppSettings(**raw))


def load_profile(path: Path) -> Profile:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run `autopilot init`, then fill it in from your resume.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Profile(**data)
