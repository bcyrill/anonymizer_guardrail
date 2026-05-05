"""
Fake OpenAI-compatible Chat Completions server for guardrail testing.

Reads a YAML rules file at startup. Each POST /v1/chat/completions
request is matched against the rules in order; the first matching rule
decides the response. Without any match, the `default:` block applies
(empty entities by default).

Designed for the LLMAnonymizer guardrail's LLM detector: rule outputs
default to OpenAI Chat Completions JSON containing
`{"entities": [...]}`, which is what the detector parses. Escape hatches
(`raw_content`, `status_code`, `delay_s`) let you exercise the parser's
error paths and timeout/LLM_FAIL_CLOSED handling.

Configuration:
  RULES_PATH   path to the rules YAML (default /app/rules.yaml)
  PORT         listen port (default 4000)
  MODEL_NAME   value echoed back as `model` in the response (default `fake`)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("fake-llm")

RULES_PATH = os.environ.get("RULES_PATH", "/app/rules.yaml")
DEFAULT_MODEL = os.environ.get("MODEL_NAME", "fake")


class Rule:
    """One matching rule.

    Matchers (a rule fires when EVERY set matcher succeeds):
      - `match`               substring of the user message
      - `match_regex`         Python regex on the user message (.search semantics)
      - `match_model`         substring of the request's `model` field — useful for
                              verifying that callers' llm_model overrides reach us
      - `match_system_prompt` substring of the system message — useful for
                              verifying that llm_prompt overrides reach us

    At least one matcher must be set (otherwise the rule is unfireable).
    Within the text-side matchers (`match` and `match_regex`) the rule
    fires when *either* hits (OR); the model + system-prompt matchers
    AND-combine with that result.

    Response controls (mutually-compatible, applied in this order):
      - `delay_s`     sleep before responding (float seconds)
      - `status_code` non-200 → returned with a stub error body
      - `raw_content` arbitrary string returned as the assistant's
                      message content (use to test parser error paths)
      - `entities`    default path; serialized as JSON
                      `{"entities": [...]}` in the assistant content
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self.description = str(raw.get("description", ""))
        self.match = raw.get("match")
        match_regex = raw.get("match_regex")
        self.match_regex = re.compile(match_regex) if match_regex else None
        self.match_model = raw.get("match_model")
        self.match_system_prompt = raw.get("match_system_prompt")
        if (
            self.match is None
            and self.match_regex is None
            and self.match_model is None
            and self.match_system_prompt is None
        ):
            raise ValueError(
                f"rule {self.description!r} has none of `match`, `match_regex`, "
                f"`match_model`, or `match_system_prompt`"
            )
        self.entities = raw.get("entities", [])
        self.raw_content: str | None = raw.get("raw_content")
        self.status_code = int(raw.get("status_code", 200))
        self.delay_s = float(raw.get("delay_s", 0))
        # When true, the rule responds with the user's last message verbatim.
        # Useful for round-trip guardrail tests where we want to see how the
        # streamed-back surrogate is restored on the deanonymize side without
        # baking a fixed surrogate value into the rules file.
        self.echo = bool(raw.get("echo", False))
        # When set on a streaming response, override the default
        # whitespace-boundary token splitting and emit fixed-size
        # character chunks instead. Lets tests deliberately split a
        # surrogate token across multiple upstream chunks so a
        # chunk-aware downstream consumer (e.g. UnifiedLLMGuardrails
        # transform-mode) gets exercised on the straddling case.
        # Ignored for non-streaming responses.
        chunk_chars_raw = raw.get("chunk_chars")
        self.chunk_chars: int | None = (
            int(chunk_chars_raw) if chunk_chars_raw is not None else None
        )
        if self.chunk_chars is not None and self.chunk_chars < 1:
            raise ValueError(
                f"rule {self.description!r}: chunk_chars must be >= 1 (got "
                f"{self.chunk_chars})"
            )

    def matches(self, text: str, model: str, system_prompt: str) -> bool:
        # Model matcher (when set) must succeed.
        if self.match_model is not None:
            if not isinstance(self.match_model, str) or self.match_model not in model:
                return False
        # System-prompt matcher (when set) must succeed.
        if self.match_system_prompt is not None:
            if (not isinstance(self.match_system_prompt, str)
                    or self.match_system_prompt not in system_prompt):
                return False
        # Text matchers (when any are set) must have at least one hit.
        has_text = self.match is not None or self.match_regex is not None
        if has_text:
            text_hit = False
            if isinstance(self.match, str) and self.match and self.match in text:
                text_hit = True
            elif self.match_regex is not None and self.match_regex.search(text):
                text_hit = True
            if not text_hit:
                return False
        return True


def _load_rules(path: str) -> tuple[list[Rule], dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        raise RuntimeError(
            f"RULES_PATH={path!r} does not exist. Mount your rules file at "
            f"this path, or override RULES_PATH."
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rules = [Rule(r) for r in raw.get("rules", [])]
    default = raw.get("default", {"entities": []})
    log.info("Loaded %d rule(s) from %s", len(rules), path)
    for i, r in enumerate(rules):
        log.info("  [%d] %s", i, r.description or "(no description)")
    return rules, default


_RULES, _DEFAULT = _load_rules(RULES_PATH)

app = FastAPI(title="fake-llm", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "rules": len(_RULES)}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """Some clients (LiteLLM included) probe /v1/models on startup."""
    return {
        "object": "list",
        "data": [{"id": DEFAULT_MODEL, "object": "model", "owned_by": "fake-llm"}],
    }


def _completion_envelope(content: str, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _split_for_streaming(content: str, chunk_chars: int | None = None) -> list[str]:
    """Split a content string into chunks for SSE streaming.

    Default mode: whitespace boundaries, with whitespace attached to
    the preceding token so concatenation of all deltas reconstructs
    the original content byte-for-byte. Long unbroken runs (e.g.
    `[EMAIL_ADDRESS_AABBCCDD]`) stay as single chunks under this
    mode — fine for the common token-aligned LLM streaming shape.

    Fixed-size mode (`chunk_chars=N`): split into N-character chunks
    regardless of word boundaries. Useful for tests that deliberately
    split a surrogate token across chunk boundaries to exercise
    chunk-straddling logic in downstream consumers.

    Empty input → []; the caller treats that as "nothing to stream"
    and emits only a terminal envelope.
    """
    if not content:
        return []
    if chunk_chars is not None and chunk_chars > 0:
        return [content[i : i + chunk_chars] for i in range(0, len(content), chunk_chars)]
    # Whitespace-boundary default. Match either a run of non-whitespace
    # optionally followed by whitespace, OR a run of whitespace (covers
    # leading whitespace in the response).
    return re.findall(r"\S+\s*|\s+", content)


async def _stream_completion(
    content: str,
    model: str,
    chunk_delay_s: float = 0.0,
    chunk_chars: int | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield OpenAI-style SSE chunks for the given content.

    Emits one `data:` event per token-ish substring from
    `_split_for_streaming` (so a multi-word response shows up as
    multiple chunks the way a real model would stream), then a
    finish-reason chunk, then `data: [DONE]`. Output bytes are UTF-8
    encoded for `StreamingResponse`.

    `chunk_chars` (>0) overrides the default whitespace-boundary split
    with fixed-size character chunks. Useful for tests that need a
    surrogate token to span chunk boundaries.

    `chunk_delay_s` (>0) sleeps between chunks. Useful when a test
    wants to exercise the proxy's sampling cadence with realistic
    inter-chunk gaps; default 0 ships everything as fast as the
    asyncio loop schedules.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    def envelope(delta: dict[str, Any], finish_reason: str | None) -> str:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    # Initial role chunk (matches OpenAI shape — first delta carries role,
    # subsequent deltas carry content only).
    yield envelope({"role": "assistant", "content": ""}, None).encode("utf-8")

    for piece in _split_for_streaming(content, chunk_chars=chunk_chars):
        if chunk_delay_s > 0:
            await asyncio.sleep(chunk_delay_s)
        yield envelope({"content": piece}, None).encode("utf-8")

    # Terminal chunk + sentinel — same shape OpenAI emits.
    yield envelope({}, "stop").encode("utf-8")
    yield b"data: [DONE]\n\n"


def _last_user_content(messages: list[dict[str, Any]]) -> str:
    """Pull the last user-role message; the guardrail sends one user
    message per detection call. We take the *last* so multi-turn
    payloads still match against the most recent input."""
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def _first_system_content(messages: list[dict[str, Any]]) -> str:
    """Pull the first system-role message — what the guardrail uses to
    carry its detection prompt. Empty when the caller didn't send one."""
    for m in messages:
        if m.get("role") == "system":
            return str(m.get("content", ""))
    return ""


@app.post("/v1/chat/completions")
async def chat_completions(req: Request) -> Response:
    body = await req.json()
    model = str(body.get("model", DEFAULT_MODEL))
    msgs = body.get("messages", [])
    user_msg = _last_user_content(msgs)
    system_msg = _first_system_content(msgs)
    stream = bool(body.get("stream", False))

    rule = next((r for r in _RULES if r.matches(user_msg, model, system_msg)), None)

    if rule is None:
        log.info("no-rule-match user=%r stream=%s", user_msg[:120], stream)
        content = json.dumps({"entities": _DEFAULT.get("entities", [])})
        if stream:
            return StreamingResponse(
                _stream_completion(content, model),
                media_type="text/event-stream",
            )
        return Response(
            content=json.dumps(_completion_envelope(content, model)),
            status_code=200,
            media_type="application/json",
        )

    log.info(
        "matched rule=%r user=%r stream=%s",
        rule.description or "(unnamed)",
        user_msg[:120],
        stream,
    )

    if rule.delay_s > 0:
        await asyncio.sleep(rule.delay_s)

    if rule.status_code != 200:
        return Response(
            content=json.dumps({"error": {"message": f"forced {rule.status_code}"}}),
            status_code=rule.status_code,
            media_type="application/json",
        )

    # Resolve response content: `echo` wins over `raw_content` wins over
    # `entities` (default). `echo` returns the user's last message
    # verbatim — useful for round-trip guardrail tests where the
    # surrogate value isn't known ahead of time.
    if rule.echo:
        content = user_msg
    elif rule.raw_content is not None:
        content = rule.raw_content
    else:
        content = json.dumps({"entities": rule.entities})

    if stream:
        return StreamingResponse(
            _stream_completion(content, model, chunk_chars=rule.chunk_chars),
            media_type="text/event-stream",
        )
    return Response(
        content=json.dumps(_completion_envelope(content, model)),
        status_code=200,
        media_type="application/json",
    )