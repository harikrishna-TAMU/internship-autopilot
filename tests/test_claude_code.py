"""LLM_BACKEND=claude-code: requests go through `claude -p` on your subscription (tested with a fake CLI)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from autopilot.llm.claude import LLMError
from autopilot.llm.claude_code import ClaudeCodeLLM
from autopilot.models import FitResult

FIT = {"score": 81, "eligible": True, "blockers": [], "matched_skills": ["C++"], "missing_skills": [], "notes": "ok"}


def fake_claude(tmp_path: Path, response: dict | str, returncode: int = 0) -> tuple[str, Path]:
    """A stand-in `claude` that records its args, stdin and env, then prints `response`."""
    record = tmp_path / "call.json"
    body = response if isinstance(response, str) else json.dumps(response)
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"json.dump({{'args': sys.argv[1:], 'stdin': sys.stdin.read(), 'env': dict(os.environ)}}, open({str(record)!r}, 'w'))\n"
        f"print({body!r})\n"
        f"sys.exit({returncode})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), record


def test_request_uses_subscription_login_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/x/.claude-work")
    binary, record = fake_claude(
        tmp_path,
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(FIT),
            "structured_output": FIT,
        },
    )
    out = ClaudeCodeLLM({"fit": "medium"}, binary=binary).ask("fit", FitResult, "<profile>…</profile>")
    assert out.score == 81
    call = json.loads(record.read_text())
    args = call["args"]
    assert args[0] == "-p" and "--json-schema" in args and "--system-prompt" in args
    assert args[args.index("--tools") + 1] == ""  # no tools at all
    assert args[args.index("--effort") + 1] == "medium"
    assert call["stdin"] == "<profile>…</profile>"
    assert "ANTHROPIC_API_KEY" not in call["env"]  # can't fall back to API billing
    assert "CLAUDE_CONFIG_DIR" not in call["env"]  # default (personal) login, not the work one


def test_screenshots_allow_only_the_read_tool(tmp_path):
    shot = tmp_path / "story.png"
    shot.write_bytes(b"png")
    binary, record = fake_claude(tmp_path, {"subtype": "success", "is_error": False, "structured_output": FIT})
    ClaudeCodeLLM({}, binary=binary).ask("fit", FitResult, "read it", images=[shot])
    call = json.loads(record.read_text())
    assert call["args"][call["args"].index("--tools") + 1] == "Read"
    assert str(shot.resolve()) in call["stdin"]


def test_usage_limit_pauses(tmp_path):
    binary, _ = fake_claude(
        tmp_path, {"subtype": "success", "is_error": True, "result": "Claude AI usage limit reached|1792900000"}
    )
    with pytest.raises(LLMError) as e:
        ClaudeCodeLLM({}, binary=binary).ask("fit", FitResult, "x")
    assert e.value.fatal and "usage limit" in str(e.value)


def test_not_logged_in_pauses(tmp_path):
    binary, _ = fake_claude(tmp_path, "Invalid API key · Please run /login", returncode=1)
    with pytest.raises(LLMError) as e:
        ClaudeCodeLLM({}, binary=binary).ask("fit", FitResult, "x")
    assert e.value.fatal


def test_bad_answer_fails_only_that_job(tmp_path):
    binary, _ = fake_claude(tmp_path, {"subtype": "success", "is_error": False, "structured_output": {"score": "high"}})
    with pytest.raises(LLMError) as e:
        ClaudeCodeLLM({}, binary=binary).ask("fit", FitResult, "x")
    assert not e.value.fatal
