"""
Shared `web_search` tool. Lives in the shared pool (not owned by any single
profession) since both real professions and ACTION's model-retry diagnostic
step can use it.

Provider: Tavily Search API (`Settings.web_search_api_key`). Tavily is a
single JSON POST with a bearer token, keeping this file's only new
dependency `httpx`, which is already a project dependency (see
app/llm.py). Swapping providers later only means rewriting `_search`'s
request/parse logic — the args schema, tool name, and registration are
provider-agnostic on purpose.

Requests Tavily's raw (full-page) content per result via
`include_raw_content`, not just the short snippet — this is deliberate:
it means most open-ended research requests can be answered from a single
web_search call, without a follow-up web_extract step that would need a
real URL to target, which the planner cannot know before web_search has
actually run (see the profession's skill_prompt for the resulting rule:
web_extract is for a URL already in hand, not for "reading more" after a
search).

error_type choices (see app/stages/action.py's _RETRYABLE_ERROR_TYPES /
_TERMINAL_ERROR_TYPES):
  - "not_configured" — WEB_SEARCH_API_KEY unset. Not fixable by adjusting
    args; needs an operator/config change.
  - "invalid_args"   — Tavily rejected the request itself (400), which
    given our own schema validation already passed means the query
    string tripped some provider-side constraint no arg tweak within our
    schema would fix. Terminal.
  - "rate_limit"     — Tavily returned 429. Retryable (attempt-1 logic
    retry, no model call needed) per the fixed vocabulary.
  - unset (defaults to "transient") — network errors, timeouts, or any
    other non-2xx status. Worth a retry before escalating.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from app.infra.models import ToolExecutionResult
from app.tools.registry import register_tool

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class WebSearchArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)


def _parse_tavily_results(payload: dict, max_results: int) -> list[dict]:
    web_results = payload.get("results") or []
    parsed = []
    for item in web_results[:max_results]:
        parsed.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
                # Full page content, requested via include_raw_content below.
                # Present so most requests are answerable from web_search
                # alone, without a follow-up web_extract step guessing at
                # which result's URL is worth reading in full.
                "content": item.get("raw_content", ""),
            }
        )
    return parsed


async def _search(args: WebSearchArgs) -> ToolExecutionResult:
    from app.infra.config import get_settings

    settings = get_settings()
    if not settings.web_search_api_key:
        return ToolExecutionResult(
            success=False,
            error=(
                "web_search tool has no provider configured (WEB_SEARCH_API_KEY unset). "
                "Set WEB_SEARCH_API_KEY (a Tavily API key) before relying on this in "
                "production."
            ),
            error_type="not_configured",
        )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.web_search_api_key}",
    }
    body = {"query": args.query, "max_results": args.max_results, "include_raw_content": True}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_TAVILY_SEARCH_URL, headers=headers, json=body)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return ToolExecutionResult(success=False, error=f"web_search network error: {exc}")

    if resp.status_code == 429:
        return ToolExecutionResult(success=False, error="web_search rate limited by provider.", error_type="rate_limit")
    if resp.status_code == 400:
        return ToolExecutionResult(
            success=False,
            error=f"web_search provider rejected the request: {resp.text[:500]}",
            error_type="invalid_args",
        )
    if resp.status_code >= 400:
        return ToolExecutionResult(
            success=False, error=f"web_search provider error {resp.status_code}: {resp.text[:500]}"
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        return ToolExecutionResult(success=False, error=f"web_search provider returned unparseable JSON: {exc}")

    results = _parse_tavily_results(payload, args.max_results)
    return ToolExecutionResult(success=True, result={"query": args.query, "results": results})


register_tool(
    name="web_search",
    args_schema=WebSearchArgs,
    handler=_search,
    description=(
        "Search the web for current information relevant to a query. Each "
        "result includes the page's real content, not just a snippet -- "
        "this is almost always sufficient on its own; do not follow up "
        "with web_extract unless a specific URL was already given to you "
        "directly or there is need to check a url from these results."
    ),
    skill_doc=(
        "web_search(query, max_results=5): returns a list of {title, url, snippet, "
        "content}. `content` is the real page content (may be empty for a small "
        "number of results the provider couldn't fetch in full -- the snippet still "
        "covers those). Use short, specific queries (3-6 words). This is the "
        "default and usually only tool needed for open-ended research -- see "
        "web_extract's own doc for the narrow case where that tool is actually "
        "appropriate."
    ),
)
