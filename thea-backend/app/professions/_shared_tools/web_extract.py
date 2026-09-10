"""
Shared `web_extract` tool. Lives next to web_search.py in the shared pool
for the same reason: any profession, or ACTION's model-retry diagnostic
step, can reasonably want "fetch this URL and give me its text" rather
than just a search-result snippet.

Fetches a URL and returns its content as plain text. HTML pages are
stripped of markup (script/style/nav/footer removed, then whitespace
collapsed) via BeautifulSoup; a PDF URL (detected by Content-Type, with a
`.pdf` extension as a fallback signal) is extracted page-by-page via
pypdf. Output is capped at `max_chars` — mirrors how web_search caps
`max_results` — so a large page can't blow out a step's context.

error_type choices (see app/stages/action.py's _RETRYABLE_ERROR_TYPES /
_TERMINAL_ERROR_TYPES):
  - "not_found"      — the server returned 404. No plausible arg
                        adjustment fixes this within the step.
  - "not_configured" — reserved for a future provider that needs a key
                        (e.g. a headless-render service for JS-heavy
                        pages); unused by the plain-httpx path today, kept
                        here so callers don't need a second lookup later.
  - unset (defaults to "transient") — network errors, timeouts, non-404
    non-2xx statuses, or a page that fails to parse (corrupt PDF,
    unexpected content type). These are worth a retry — a flaky fetch or
    a wrong-but-fixable URL guess are both plausible causes.
"""

from __future__ import annotations

import asyncio
import re

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.infra.models import ToolExecutionResult
from app.tools.registry import register_tool

_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "noscript")


class WebExtractArgs(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)
    max_chars: int = Field(default=5000, ge=200, le=50000)


def _looks_like_pdf(content_type: str, url: str) -> bool:
    return "application/pdf" in content_type.lower() or url.lower().split("?")[0].endswith(".pdf")


def _html_to_text(html_bytes: bytes) -> str:
    soup = BeautifulSoup(html_bytes, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse runs of blank lines / repeated whitespace left over from
    # stripped layout markup.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _pdf_to_text_sync(pdf_bytes: bytes) -> str:
    import io

    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages_text = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages_text).strip()


async def _extract(args: WebExtractArgs) -> ToolExecutionResult:
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(args.url)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return ToolExecutionResult(success=False, error=f"web_extract network error: {exc}")

    if resp.status_code == 404:
        return ToolExecutionResult(success=False, error=f"URL not found (404): {args.url}", error_type="not_found")
    if resp.status_code >= 400:
        return ToolExecutionResult(
            success=False, error=f"web_extract fetch failed with status {resp.status_code}: {args.url}"
        )

    content_type = resp.headers.get("content-type", "")

    try:
        if _looks_like_pdf(content_type, args.url):
            text = await asyncio.to_thread(_pdf_to_text_sync, resp.content)
            content_kind = "pdf"
        else:
            text = await asyncio.to_thread(_html_to_text, resp.content)
            content_kind = "html"
    except PdfReadError as exc:
        return ToolExecutionResult(success=False, error=f"Could not parse PDF at {args.url}: {exc}")
    except Exception as exc:  # noqa: BLE001 — tool boundary, must not raise
        return ToolExecutionResult(success=False, error=f"Could not parse content at {args.url}: {exc}")

    truncated = len(text) > args.max_chars
    text = text[: args.max_chars]

    return ToolExecutionResult(
        success=True,
        result={
            "url": args.url,
            "content_type": content_kind,
            "text": text,
            "truncated": truncated,
        },
    )


register_tool(
    name="web_extract",
    args_schema=WebExtractArgs,
    handler=_extract,
    description="Fetch a URL (webpage or PDF) and return its content as plain text.",
    skill_doc=(
        "web_extract(url, max_chars=5000) -> {url, content_type: 'html'|'pdf', "
        "text, truncated: bool}. Converts the page to plain text (markup stripped) "
        "or extracts text from a PDF. Result is capped at max_chars — truncated=true "
        "means more content exists beyond what was returned; re-call with a higher "
        "max_chars if the whole document is needed. A 404 fails with "
        "error_type=not_found (not retryable); other failures are retryable."
    ),
)
