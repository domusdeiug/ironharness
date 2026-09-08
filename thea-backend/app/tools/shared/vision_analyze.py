"""
Shared `vision_analyze` tool. Registered bare (shared pool), not owned by
any profession — any profession, or a future multimodal profession, can
reasonably want "describe this image" or "answer a question about this
image" as a step.

Accepts either a plain URL (passed straight through to the model call) or
a path already inside the `files` profession's sandbox root — the latter
matters because a step that just wrote or downloaded an image via
`files.write_file` has no public URL for it, only a local path. Local
paths are read and re-encoded as a base64 data: URI before being handed
to the model; nothing is ever uploaded anywhere else.

Reuses `app.llm.call_vision` for the actual model call rather than
opening a second HTTP client — see that function's docstring. If
`app/llm.py` grows a structured-output multimodal path later, this tool
should move to it, but today's ask (a description, optionally answering
a question) doesn't need structured output.

error_type choices (see app/stages/action.py's _RETRYABLE_ERROR_TYPES /
_TERMINAL_ERROR_TYPES):
  - "not_found"      — image_url is a 404 (remote) or the sandboxed path
                        doesn't exist (local). Not retryable.
  - "invalid_args"   — image_url looks like a local path but resolves
                        outside the files sandbox root. Same reasoning as
                        files.py's own sandbox-escape case: not something
                        a retried arg guess should paper over.
  - "not_configured" — vision_model isn't set in config.yaml, or (for a
                        local path) FILES_SANDBOX_ROOT isn't set at all.
  - unset (defaults to "transient") — network errors, timeouts, or the
    model call itself failing outright (StructuredCallError-equivalent
    for the plain-text path). Worth a retry.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from app.config import cfg
from app.llm import StructuredCallError, call_vision
from app.models import ToolExecutionResult
from app.tools.registry import register_tool

_LOCAL_SCHEMES = ("http://", "https://", "data:")


class VisionAnalyzeArgs(BaseModel):
    image_url: str = Field(
        ...,
        description=(
            "A fetchable http(s) URL, or a path (relative or absolute) already "
            "inside the files profession's sandbox root."
        ),
    )
    question: str | None = Field(default=None, description="Optional question to answer about the image.")


def _is_remote(image_url: str) -> bool:
    return image_url.lower().startswith(("http://", "https://", "data:"))


def _read_local_image_as_data_uri_sync(image_url: str) -> tuple[str | None, str]:
    """Returns (data_uri_or_None, error_message). Resolves image_url against
    FILES_SANDBOX_ROOT the same way app.professions.files.tools does, so a
    path outside that root is rejected the same way it would be there."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.files_sandbox_root:
        return None, "not_configured:FILES_SANDBOX_ROOT is unset — cannot resolve a local image path."

    root = Path(settings.files_sandbox_root).resolve()
    candidate = Path(image_url)
    combined = candidate if candidate.is_absolute() else root / candidate
    resolved = combined.resolve()

    if not (resolved == root or resolved.is_relative_to(root)):
        return None, f"invalid_args:Path '{image_url}' resolves outside the files sandbox root."

    if not resolved.is_file():
        return None, f"not_found:Image file not found: {image_url}"

    mime_type, _ = mimetypes.guess_type(str(resolved))
    mime_type = mime_type or "application/octet-stream"
    encoded = base64.b64encode(resolved.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}", ""


async def _check_remote_url_exists(image_url: str) -> str | None:
    """Returns an error message if the URL is unreachable/404, else None.
    A cheap HEAD (falling back to a ranged GET if HEAD isn't supported) so
    we fail fast with error_type=not_found instead of paying for a full
    model call against a dead link."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.head(image_url)
            if resp.status_code == 405:  # HEAD not allowed — fall back to GET
                resp = await client.get(image_url, headers={"Range": "bytes=0-0"})
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return f"transient:Could not reach image_url: {exc}"

    if resp.status_code == 404:
        return f"not_found:image_url returned 404: {image_url}"
    if resp.status_code >= 400:
        return f"transient:image_url returned status {resp.status_code}: {image_url}"
    return None


async def _vision_analyze(args: VisionAnalyzeArgs) -> ToolExecutionResult:
    model = cfg("tools.vision_model", None)
    if not model:
        return ToolExecutionResult(
            success=False,
            error="vision_analyze has no model configured (tools.vision_model unset in config.yaml).",
            error_type="not_configured",
        )

    if _is_remote(args.image_url):
        check_error = await _check_remote_url_exists(args.image_url)
        if check_error:
            kind, _, message = check_error.partition(":")
            return ToolExecutionResult(success=False, error=message, error_type=None if kind == "transient" else kind)
        resolved_image_url = args.image_url
    else:
        data_uri, err = await asyncio.to_thread(_read_local_image_as_data_uri_sync, args.image_url)
        if data_uri is None:
            kind, _, message = err.partition(":")
            return ToolExecutionResult(success=False, error=message, error_type=kind)
        resolved_image_url = data_uri

    try:
        description = await call_vision(model=model, image_url=resolved_image_url, question=args.question)
    except StructuredCallError as exc:
        return ToolExecutionResult(success=False, error=f"vision_analyze model call failed: {exc}")

    return ToolExecutionResult(
        success=True,
        result={"image_url": args.image_url, "question": args.question, "description": description},
    )


register_tool(
    name="vision_analyze",
    args_schema=VisionAnalyzeArgs,
    handler=_vision_analyze,
    description="Analyze an image (by URL, or by a path inside the files sandbox) and answer an optional question about it.",
    skill_doc=(
        "vision_analyze(image_url, question=None) -> {image_url, question, description}. "
        "image_url can be a public http(s) URL or a path already inside the files "
        "profession's sandbox root (that file is read and sent directly, never "
        "uploaded anywhere else). Omit question for a general description; provide "
        "one to have it answered about the image specifically. A remote 404 or a "
        "missing/out-of-sandbox local path fails with error_type=not_found or "
        "invalid_args respectively (not retryable)."
    ),
)
