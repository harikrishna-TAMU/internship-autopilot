"""ClaudeLLM against a mocked API: the request carries the schema, effort and fallback opt-in; responses parse."""

from __future__ import annotations

import json

import httpx2
import pytest
from anthropic import Anthropic, DefaultHttpxClient

from autopilot.llm.claude import ClaudeLLM, LLMError
from autopilot.models import FitResult


def make_llm(handler) -> tuple[ClaudeLLM, list]:
    seen: list = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    llm = ClaudeLLM("sk-test", "claude-opus-5-5", {"fit": "medium"})
    llm.client = Anthropic(
        api_key="sk-test", max_retries=0, http_client=DefaultHttpxClient(transport=httpx2.MockTransport(record))
    )
    return llm, seen


def message(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


FIT = {"score": 72, "eligible": True, "blockers": [], "matched_skills": ["Python"], "missing_skills": [], "notes": "ok"}


def test_request_shape_and_parsing():
    llm, seen = make_llm(lambda r: httpx2.Response(200, json=message(json.dumps(FIT))))
    out = llm.ask("fit", FitResult, "<profile>...</profile>")
    assert isinstance(out, FitResult) and out.score == 72
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]
    assert "fit score" in body["system"].lower() or "fit" in body["system"].lower()


def test_refusal_raises():
    llm, _ = make_llm(lambda r: httpx2.Response(200, json=message("", stop_reason="refusal")))
    with pytest.raises(LLMError, match="declined"):
        llm.ask("fit", FitResult, "x")


def test_workspace_header_and_unscoped_key_error():
    seen = []

    def handler(request):
        seen.append(request)
        err = {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header",
            },
        }
        return httpx2.Response(400, json=err)

    llm = ClaudeLLM("sk-test", "claude-opus-5-5", {}, workspace_id="wrkspc_123")
    llm.client = llm.client.with_options(
        http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)), max_retries=0
    )
    with pytest.raises(LLMError) as e:
        llm.ask("fit", FitResult, "x")
    assert e.value.fatal and "ANTHROPIC_WORKSPACE_ID" in str(e.value)
    assert seen[0].headers["anthropic-workspace-id"] == "wrkspc_123"
    assert "ANTHROPIC_WORKSPACE_ID" in llm.verify_access()


def test_bad_key_is_fatal():
    err = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    llm, _ = make_llm(lambda r: httpx2.Response(401, json=err))
    with pytest.raises(LLMError) as e:
        llm.ask("fit", FitResult, "x")
    assert e.value.fatal


def test_low_credit_balance_pauses_instead_of_failing_jobs():
    err = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing.",
        },
    }
    llm, _ = make_llm(lambda r: httpx2.Response(400, json=err))
    with pytest.raises(LLMError) as e:
        llm.ask("fit", FitResult, "x")
    assert e.value.fatal and "Plans & Billing" in str(e.value)
