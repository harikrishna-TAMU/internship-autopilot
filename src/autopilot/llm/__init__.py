"""Pick how ApplyPilot talks to Claude: the API (pay per use) or Claude Code (your Claude subscription)."""

from __future__ import annotations


def make_llm(cfg):
    if (cfg.env.llm_backend or "api").strip().lower() in ("claude-code", "claude_code", "subscription"):
        from autopilot.llm.claude_code import ClaudeCodeLLM

        return ClaudeCodeLLM.from_config(cfg)
    from autopilot.llm.claude import ClaudeLLM

    return ClaudeLLM.from_config(cfg)
