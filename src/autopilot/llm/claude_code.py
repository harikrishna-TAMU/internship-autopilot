"""Claude through the Claude Code CLI (`claude -p`), so usage comes from your Claude Pro/Max plan, not API credits.

Each request runs `claude -p` with the task prompt as the system prompt, every tool turned off (Read only, for
screenshots), no MCP servers or user settings, and a JSON schema for the answer. Requests never use an API key
from the environment, so they can't land on an API account by accident.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import anthropic
from pydantic import ValidationError

from autopilot.llm.claude import LLMError, T, load_prompt

LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|out of (?:extra )?usage|quota", re.I)
LOGIN_RE = re.compile(r"not logged in|please run /login|invalid api key|authentication|unauthorized|oauth", re.I)
TEAM_PLANS = {"team", "enterprise"}


class ClaudeCodeLLM:
    def __init__(
        self,
        efforts: dict[str, str],
        model: str | None = None,
        config_dir: str | None = None,
        binary: str | None = None,
        timeout: int = 900,
    ):
        self.bin = binary or shutil.which("claude")
        self.efforts = efforts
        self.model = model or None
        self.config_dir = str(Path(config_dir).expanduser()) if config_dir else None
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg) -> ClaudeCodeLLM:
        return cls(cfg.settings.effort.model_dump(), cfg.env.claude_code_model, cfg.env.claude_code_config_dir)

    def env(self) -> dict[str, str]:
        """Subscription login only: drop API credentials and any config-dir switch (e.g. VS Code's work account)."""
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL")
        }
        if self.config_dir:
            env["CLAUDE_CONFIG_DIR"] = self.config_dir
        return env

    def account(self) -> dict:
        """The Claude account this backend uses: {loggedIn, email, orgName, subscriptionType, ...}."""
        if not self.bin:
            return {"loggedIn": False, "error": "Claude Code isn't installed (https://claude.com/claude-code)."}
        proc = subprocess.run([self.bin, "auth", "status"], capture_output=True, text=True, env=self.env(), timeout=60)
        try:
            return json.loads(proc.stdout[proc.stdout.find("{") :])
        except ValueError:
            return {"loggedIn": False, "error": (proc.stdout + proc.stderr).strip()[:300]}

    def verify_access(self) -> str | None:
        acct = self.account()
        if not acct.get("loggedIn"):
            return acct.get("error") or "Claude Code isn't logged in. Run `claude` in a terminal, then /login."
        if acct.get("authMethod") not in (None, "claude.ai"):
            return f"Claude Code is using {acct.get('authMethod')}, not a Claude subscription login."
        return None

    def describe(self) -> str:
        acct = self.account()
        if not acct.get("loggedIn"):
            return "not logged in"
        plan = acct.get("subscriptionType") or "?"
        note = " (an organization's plan; use your personal login for job searching)" if plan in TEAM_PLANS else ""
        return f"{acct.get('email')} · {acct.get('orgName')} · {plan} plan{note}"

    def ask(self, task: str, schema: type[T], user: str, images: list[Path] | None = None) -> T:
        if not self.bin:
            raise LLMError("Claude Code isn't installed, so LLM_BACKEND=claude-code can't run.", fatal=True)
        cmd = [
            self.bin, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(anthropic.transform_schema(schema)),
            "--system-prompt", load_prompt(task),
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources", "",
        ]  # fmt: skip
        if self.model:
            cmd += ["--model", self.model]
        if effort := self.efforts.get(task):
            cmd += ["--effort", effort]
        prompt = user
        if images:
            files = [str(p.resolve()) for p in images]
            cmd += [
                "--tools",
                "Read",
                "--allowed-tools",
                "Read",
                "--add-dir",
                *sorted({str(Path(f).parent) for f in files}),
            ]
            prompt += "\n\nUse the Read tool to open these image files, then answer:\n" + "\n".join(files)
        else:
            cmd += ["--tools", ""]

        with tempfile.TemporaryDirectory() as cwd:  # neutral folder: no project files or CLAUDE.md
            try:
                proc = subprocess.run(
                    cmd, input=prompt, capture_output=True, text=True, env=self.env(), cwd=cwd, timeout=self.timeout
                )
            except subprocess.TimeoutExpired as e:
                raise LLMError(f"Claude Code took longer than {self.timeout}s on {task}.") from e

        out = proc.stdout.strip()
        try:
            data = json.loads(out[out.find("{") :]) if "{" in out else {}
        except ValueError:
            data = {}
        message = str(data.get("result") or proc.stderr or out)[:500]
        if not data or data.get("is_error") or data.get("subtype") != "success":
            if LIMIT_RE.search(message):
                raise LLMError(
                    f"Your Claude plan's usage limit is reached; jobs continue after it resets. ({message})", fatal=True
                )
            if LOGIN_RE.search(message):
                raise LLMError(f"Claude Code isn't logged in: run `claude`, then /login. ({message})", fatal=True)
            if proc.returncode != 0 and not data:
                raise LLMError(f"Claude Code failed: {message}", fatal=True)
            raise LLMError(f"Claude Code couldn't finish {task}: {message}")

        answer = data.get("structured_output")
        if answer is None:
            try:
                answer = json.loads(data.get("result") or "")
            except ValueError as e:
                raise LLMError(f"Claude Code's {task} answer wasn't JSON.") from e
        try:
            return schema.model_validate(answer)
        except ValidationError as e:
            raise LLMError(
                f"Claude Code's {task} answer didn't match the expected format: {str(e).splitlines()[0]}"
            ) from e
