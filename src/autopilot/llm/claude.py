"""Claude wrapper: one structured-output call per task, validated against a Pydantic model.

Task instructions live in llm/prompts/<task>.md and are sent as the system prompt.
The per-job data (job description, profile, form fields) goes in the user message.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

PROMPTS_DIR = Path(__file__).parent / "prompts"
T = TypeVar("T", bound=BaseModel)

SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class LLMError(RuntimeError):
    """`fatal` errors (bad key, outage, rate limit) stop the whole run instead of failing one job."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class LLM(Protocol):
    def ask(self, task: str, schema: type[T], user: str, images: list[Path] | None = None) -> T: ...


def load_prompt(task: str) -> str:
    path = PROMPTS_DIR / f"{task}.md"
    if not path.exists():
        raise LLMError(f"missing prompt file {path}")
    return path.read_text(encoding="utf-8")


def image_block(path: Path) -> dict:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if mime not in SUPPORTED_IMAGE_TYPES:
        raise LLMError(f"{path.name}: {mime} images aren't supported. Send screenshots as PNG or JPG.")
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise LLMError(f"{path.name} is over 5 MB. Send a smaller screenshot.")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime, "data": base64.standard_b64encode(data).decode()},
    }


WORKSPACE_HINT = (
    "Your API key isn't tied to a Console workspace. Either add ANTHROPIC_WORKSPACE_ID=wrkspc_... to .env, "
    "or create a new key inside a workspace (Console → API keys) and use that."
)


BILLING_HINT = (
    "Your Claude API credit balance is too low. Add credits at console.anthropic.com → Plans & Billing; "
    "waiting jobs continue on the next run."
)


def is_billing_error(e: anthropic.APIStatusError) -> bool:
    msg = str(e.message).lower()
    return e.status_code == 402 or "credit balance" in msg or "billing" in msg


class ClaudeLLM:
    def __init__(self, api_key: str | None, model: str, efforts: dict[str, str], workspace_id: str | None = None):
        # With no key, the SDK falls back to ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
        kwargs: dict = {"max_retries": 3}
        if api_key:
            kwargs["api_key"] = api_key
        if workspace_id:
            kwargs["default_headers"] = {"anthropic-workspace-id": workspace_id}
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.efforts = efforts

    @classmethod
    def from_config(cls, cfg) -> ClaudeLLM:
        return cls(
            cfg.env.anthropic_api_key,
            cfg.env.claude_model,
            cfg.settings.effort.model_dump(),
            cfg.env.anthropic_workspace_id,
        )

    def verify_access(self) -> str | None:
        """Free check that the key works and can use the model (no tokens). Returns a problem, or None."""
        try:
            self.client.models.retrieve(self.model)
            return None
        except anthropic.AuthenticationError:
            return "Claude rejected the API key (ANTHROPIC_API_KEY in .env)."
        except anthropic.NotFoundError:
            return f"Model '{self.model}' isn't available to this key (CLAUDE_MODEL in .env)."
        except anthropic.APIStatusError as e:
            return (
                WORKSPACE_HINT
                if "workspace" in str(e.message).lower()
                else f"Claude API error {e.status_code}: {e.message}"
            )
        except anthropic.APIConnectionError:
            return "Couldn't reach the Claude API (internet connection?)."
        except TypeError:
            return "No Claude API key found (ANTHROPIC_API_KEY in .env)."

    def ask(self, task: str, schema: type[T], user: str, images: list[Path] | None = None) -> T:
        content: list[dict] = [image_block(p) for p in (images or [])]
        content.append({"type": "text", "text": user})
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=load_prompt(task),
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": self.efforts.get(task, "medium"),
                    "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
                },
                # On a policy decline, retry server-side on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Claude rejected the API key. Check ANTHROPIC_API_KEY in .env.", fatal=True) from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"The API key can't use model '{self.model}': {e.message}", fatal=True) from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Model '{self.model}' not found. Check CLAUDE_MODEL in .env.", fatal=True) from e
        except anthropic.RateLimitError as e:
            raise LLMError(
                "Claude rate limit hit after retries. The next run will pick up where this stopped.", fatal=True
            ) from e
        except anthropic.APIStatusError as e:
            if "workspace" in str(e.message).lower():
                raise LLMError(WORKSPACE_HINT, fatal=True) from e
            if is_billing_error(e):
                raise LLMError(BILLING_HINT, fatal=True) from e
            raise LLMError(f"Claude API error {e.status_code}: {e.message}", fatal=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Couldn't reach the Claude API. Check your internet connection.", fatal=True) from e
        except TypeError as e:
            if "authentication" in str(e).lower():
                raise LLMError(
                    "No Claude API key found. Add ANTHROPIC_API_KEY to .env (see README).", fatal=True
                ) from e
            raise

        # Check why Claude stopped before parsing: a refusal or cut-off answer isn't valid JSON.
        if response.stop_reason == "refusal":
            raise LLMError(f"Claude declined the {task} request.")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Claude's {task} answer was cut off (max_tokens).")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            raise LLMError(f"Claude's {task} answer didn't match the expected format: {str(e).splitlines()[0]}") from e
