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
error paths and timeout/FAIL_CLOSED handling.

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
from typing import Any

import yaml
from fastapi import FastAPI, Request, Response

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("fake-llm")

RULES_PATH = os.environ.get("RULES_PATH", "/app/rules.yaml")
DEFAULT_MODEL = os.environ.get("MODEL_NAME", "fake")


class Rule:
    """One matching rule.

    `match` (substring) and `match_regex` (Python regex) are tried in
    that order; either or both may be set. `match_regex` is compiled
    once at startup. A rule fires if any configured matcher succeeds.

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
        if self.match is None and self.match_regex is None:
            raise ValueError(
                f"rule {self.description!r} has neither `match` nor `match_regex`"
            )
        self.entities = raw.get("entities", [])
        self.raw_content: str | None = raw.get("raw_content")
        self.status_code = int(raw.get("status_code", 200))
        self.delay_s = float(raw.get("delay_s", 0))

    def matches(self, text: str) -> bool:
        if isinstance(self.match, str) and self.match and self.match in text:
            return True
        if self.match_regex is not None and self.match_regex.search(text):
            return True
        return False


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


def _last_user_content(messages: list[dict[str, Any]]) -> str:
    """Pull the last user-role message; the guardrail sends one user
    message per detection call. We take the *last* so multi-turn
    payloads still match against the most recent input."""
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


@app.post("/v1/chat/completions")
async def chat_completions(req: Request) -> Response:
    body = await req.json()
    model = str(body.get("model", DEFAULT_MODEL))
    user_msg = _last_user_content(body.get("messages", []))

    rule = next((r for r in _RULES if r.matches(user_msg)), None)

    if rule is None:
        log.info("no-rule-match user=%r", user_msg[:120])
        content = json.dumps({"entities": _DEFAULT.get("entities", [])})
        return Response(
            content=json.dumps(_completion_envelope(content, model)),
            status_code=200,
            media_type="application/json",
        )

    log.info(
        "matched rule=%r user=%r",
        rule.description or "(unnamed)",
        user_msg[:120],
    )

    if rule.delay_s > 0:
        await asyncio.sleep(rule.delay_s)

    if rule.status_code != 200:
        return Response(
            content=json.dumps({"error": {"message": f"forced {rule.status_code}"}}),
            status_code=rule.status_code,
            media_type="application/json",
        )

    if rule.raw_content is not None:
        content = rule.raw_content
    else:
        content = json.dumps({"entities": rule.entities})

    return Response(
        content=json.dumps(_completion_envelope(content, model)),
        status_code=200,
        media_type="application/json",
    )