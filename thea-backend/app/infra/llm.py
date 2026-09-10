"""
Thin OpenRouter client used by every stage that needs a model call.

Design notes:
- All structured-output calls go through `call_structured`, which asks the
  model for JSON matching a Pydantic schema (via response_format /
  json_schema where the model supports it) and validates the response
  before handing it back. A malformed response raises `StructuredCallError`
  rather than silently returning something half-parsed — callers (Stage 1,
  Stage 2's validation loop, etc.) decide what to do with that.
- Fallback model/provider strategy is an explicit OPEN item in the handoff.
  What's implemented here is the minimal honest version: a single optional
  `fallback_model` that gets one retry if the primary model call fails
  outright (network error, non-2xx, timeout) — NOT if the model responds
  but with content that fails schema validation, since that's usually a
  prompting problem, not a flaky-provider problem, and retrying blindly
  against a schema-validation failure risks masking a genuine bug in the
  step's own logic (e.g. a bad intent needing a real replan) as intermittent
  server flakiness. Escalate schema failures back to the caller.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.infra.config import get_settings

logger = logging.getLogger("thea.llm")

T = TypeVar("T", bound=BaseModel)


class StructuredCallError(Exception):
    """Raised when a model call fails outright, or returns content that
    cannot be parsed/validated against the requested schema."""


def _headers() -> dict[str, str]:
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_app_name:
        headers["X-Title"] = settings.openrouter_app_name
    return headers


def _extract_text(response_json: dict[str, Any]) -> str:
    try:
        return response_json["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise StructuredCallError(f"Unexpected OpenRouter response shape: {response_json}") from exc


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


async def _post_chat_completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    response_schema: dict[str, Any] | None,
    schema_name: str | None,
    tools: list[dict[str, Any]] | None,
    timeout: float,
) -> dict[str, Any]:
    """`messages` content can be either a plain string (existing text-only
    callers) or a list of OpenAI-style content blocks (used by
    `call_vision` below to attach an image_url block) — both pass through
    to OpenRouter unchanged, since it accepts the same message shape
    either way for multimodal-capable models."""
    settings = get_settings()
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name or "structured_output",
                "strict": True,
                "schema": response_schema,
            },
        }
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{settings.openrouter_base_url}/chat/completions",
            headers=_headers(),
            json=payload,
        )
    if resp.status_code >= 400:
        raise StructuredCallError(f"OpenRouter {resp.status_code} error: {resp.text[:2000]}")
    return resp.json()


async def call_structured(
    *,
    model: str,
    schema: type[T],
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    fallback_model: str | None = None,
    timeout: float = 60.0,
) -> T:
    """Call an OpenRouter model and validate its JSON response against
    `schema`. Raises StructuredCallError on outright failure or on a
    response that doesn't validate (after the single fallback attempt for
    transport-level failures only — see module docstring)."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    json_schema = schema.model_json_schema()

    async def _attempt(m: str) -> dict[str, Any]:
        return await _post_chat_completion(
            model=m,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_schema=json_schema,
            schema_name=schema.__name__,
            tools=None,
            timeout=timeout,
        )

    try:
        raw = await _attempt(model)
    except StructuredCallError as exc:
        if not fallback_model:
            raise
        logger.warning("Primary model %s failed (%s); falling back to %s", model, exc, fallback_model)
        raw = await _attempt(fallback_model)

    text = _strip_code_fences(_extract_text(raw))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredCallError(f"Model did not return valid JSON: {text[:500]}") from exc

    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise StructuredCallError(f"Model output failed schema validation: {exc}") from exc


async def call_text(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.4,
    max_tokens: int = 2000,
    timeout: float = 60.0,
) -> str:
    """Plain (non-structured) call, used by Synthesization."""
    raw = await _post_chat_completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        response_schema=None,
        schema_name=None,
        tools=None,
        timeout=timeout,
    )
    return _extract_text(raw)


async def call_vision(
    *,
    model: str,
    image_url: str,
    question: str | None = None,
    system_prompt: str = "Describe the image accurately and concisely.",
    temperature: float = 0.2,
    max_tokens: int = 1000,
    timeout: float = 60.0,
) -> str:
    """Plain (non-structured) multimodal call: one image plus an optional
    question, against a vision-capable OpenRouter model. Shared
    infrastructure — used by the `vision_analyze` shared tool
    (app/tools/shared/vision_analyze.py) rather than that tool opening its
    own HTTP client, since a second ad-hoc client would duplicate the
    auth/header/error handling already centralized here.

    `image_url` is passed straight through as an `image_url` content
    block per the OpenAI-compatible multimodal message format OpenRouter
    expects; the caller is responsible for it being reachable by
    OpenRouter's servers (a data: URI also works if a caller ever needs
    to send bytes directly, though nothing in this repo does that yet)."""
    user_content: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": image_url}},
    ]
    user_content.append({"type": "text", "text": question or "Describe this image."})

    raw = await _post_chat_completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        response_schema=None,
        schema_name=None,
        tools=None,
        timeout=timeout,
    )
    return _extract_text(raw)
