"""
`files` profession — general-purpose text file read/write/patch/search,
sandboxed to a single configured root directory (`Settings.files_sandbox_root`).

This is deliberately not the same job as `xlsx`'s spreadsheet-specific I/O:
xlsx tools only ever open a workbook with openpyxl, so the "path" is
always an argument to a library call that either succeeds or reports a
clean file-not-found. Here the model is choosing an arbitrary filesystem
path for raw read/write/search, so — unlike every other profession in
this repo — path safety is this profession's central concern, not an
edge case.

Every tool resolves its `path` argument against `files_sandbox_root` via
`_resolve_sandboxed_path()` before touching the filesystem, and rejects
(error_type="invalid_args", terminal) anything that would escape the
root — via `..` traversal, an absolute path outside the root, or a
symlink whose real target lands outside the root. Symlinks are resolved
with `Path.resolve()` (which follows them) specifically so a symlink
planted inside the sandbox that points outside it doesn't become an
escape hatch.

error_type choices (see app/stages/action.py's _RETRYABLE_ERROR_TYPES /
_TERMINAL_ERROR_TYPES):
  - "not_configured" — FILES_SANDBOX_ROOT isn't set at all. Not fixable
    by adjusting args; needs an operator/config change. Settings.
    files_sandbox_root is optional (unlike supabase_url/openrouter_api_key)
    specifically so a deployment that never enables `files` isn't blocked
    from booting by this — same reasoning as `email`'s SMTP settings
    being optional. Every handler below checks for this before doing
    anything else, mirroring email.py's _get_client()/_require_config()
    pattern.
  - "invalid_args" — a path-escape attempt. Not fixable by retrying with
    adjusted args in the useful sense: the model asked for something out
    of bounds, full stop, and no plausible "corrected" path is something
    ACTION should be proposing on the model's behalf. This is one of the
    few cases in the whole harness where "invalid_args" (terminal, no
    model-retry) is the right call, unlike e.g. xlsx's cell-address-typo
    cases, which are exactly what the retry ladder exists for.
  - "not_found" — files.read_file's resolved path doesn't exist. Not
    retryable (see xlsx's identical reasoning for its own not_found case).
  - unset (defaults to "transient") — files.patch's "old_text not found
    exactly once" case is deliberately NOT terminal: it's a well-formed
    request with a wrong detail (stale old_text), exactly the case the
    model-retry ladder (re-read the file, propose corrected old_text)
    exists for.

All four handlers do blocking file I/O and are wrapped in
asyncio.to_thread, matching xlsx's pattern for openpyxl.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.infra.models import ToolExecutionResult
from app.tools.registry import register_tool


class _SandboxEscapeError(Exception):
    def __init__(self, requested_path: str, root: Path):
        self.requested_path = requested_path
        self.root = root
        super().__init__(f"Path '{requested_path}' resolves outside the sandbox root '{root}'.")


class _NotConfiguredError(Exception):
    pass


def _sandbox_root() -> Path:
    from app.infra.config import get_settings

    root = get_settings().files_sandbox_root
    if not root:
        raise _NotConfiguredError(
            "FILES_SANDBOX_ROOT is not set. The files profession needs a sandbox "
            "root configured before any files.* tool can run — set it in the "
            "environment (see .env.example)."
        )
    return Path(root).resolve()


def _resolve_sandboxed_path(raw_path: str) -> Path:
    """Resolve `raw_path` (relative or absolute) against the sandbox root
    and verify the result doesn't escape it. Resolution happens via
    Path.resolve(), which normalizes '..' segments and follows symlinks —
    both traversal styles are caught by the same is_relative_to check.
    Raises _NotConfiguredError (via _sandbox_root()) if no root is set,
    or _SandboxEscapeError if the resolved path escapes it — callers catch
    both and map them to ToolExecutionResult."""
    root = _sandbox_root()
    candidate = Path(raw_path)
    combined = candidate if candidate.is_absolute() else root / candidate
    resolved = combined.resolve()

    if not (resolved == root or resolved.is_relative_to(root)):
        raise _SandboxEscapeError(raw_path, root)

    return resolved


def _escape_result(exc: _SandboxEscapeError) -> ToolExecutionResult:
    return ToolExecutionResult(
        success=False,
        error=f"Refusing to operate outside the sandbox root: {exc}",
        error_type="invalid_args",
    )


def _not_configured_result(exc: _NotConfiguredError) -> ToolExecutionResult:
    return ToolExecutionResult(success=False, error=str(exc), error_type="not_configured")


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class ReadFileArgs(BaseModel):
    path: str
    offset: int = Field(default=0, ge=0, description="0-indexed line number to start reading from.")
    limit: int = Field(default=2000, ge=1, le=20000, description="Maximum number of lines to return.")


def _read_file_sync(args: ReadFileArgs) -> ToolExecutionResult:
    try:
        resolved = _resolve_sandboxed_path(args.path)
    except _NotConfiguredError as exc:
        return _not_configured_result(exc)
    except _SandboxEscapeError as exc:
        return _escape_result(exc)

    if not resolved.is_file():
        return ToolExecutionResult(success=False, error=f"File not found: {args.path}", error_type="not_found")

    try:
        with resolved.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as exc:
        return ToolExecutionResult(success=False, error=f"Could not read file: {exc}")

    window = all_lines[args.offset : args.offset + args.limit]
    return ToolExecutionResult(
        success=True,
        result={
            "path": args.path,
            "offset": args.offset,
            "lines_returned": len(window),
            "total_lines": len(all_lines),
            "content": "".join(window),
        },
    )


async def _read_file(args: ReadFileArgs) -> ToolExecutionResult:
    return await asyncio.to_thread(_read_file_sync, args)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class WriteFileArgs(BaseModel):
    path: str
    content: str


def _write_file_sync(args: WriteFileArgs) -> ToolExecutionResult:
    try:
        resolved = _resolve_sandboxed_path(args.path)
    except _NotConfiguredError as exc:
        return _not_configured_result(exc)
    except _SandboxEscapeError as exc:
        return _escape_result(exc)

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(args.content, encoding="utf-8")
    except OSError as exc:
        return ToolExecutionResult(success=False, error=f"Could not write file: {exc}")

    return ToolExecutionResult(
        success=True,
        result={"path": args.path, "bytes_written": len(args.content.encode("utf-8"))},
    )


async def _write_file(args: WriteFileArgs) -> ToolExecutionResult:
    return await asyncio.to_thread(_write_file_sync, args)


# ---------------------------------------------------------------------------
# patch — exact string match only (v1)
# ---------------------------------------------------------------------------


class PatchArgs(BaseModel):
    path: str
    old_text: str = Field(..., min_length=1)
    new_text: str


def _patch_sync(args: PatchArgs) -> ToolExecutionResult:
    try:
        resolved = _resolve_sandboxed_path(args.path)
    except _NotConfiguredError as exc:
        return _not_configured_result(exc)
    except _SandboxEscapeError as exc:
        return _escape_result(exc)

    if not resolved.is_file():
        return ToolExecutionResult(success=False, error=f"File not found: {args.path}", error_type="not_found")

    try:
        original = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolExecutionResult(success=False, error=f"Could not read file: {exc}")

    occurrences = original.count(args.old_text)
    if occurrences == 0:
        return ToolExecutionResult(
            success=False,
            error=(
                f"old_text was not found in {args.path}. Re-read the file with "
                "files.read_file and retry with old_text copied exactly from its "
                "current content."
            ),
        )
    if occurrences > 1:
        return ToolExecutionResult(
            success=False,
            error=(
                f"old_text matches {occurrences} locations in {args.path}, but patch "
                "requires exactly one match. Include more surrounding context in "
                "old_text to make it unique."
            ),
        )

    updated = original.replace(args.old_text, args.new_text, 1)
    try:
        resolved.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return ToolExecutionResult(success=False, error=f"Could not write file: {exc}")

    return ToolExecutionResult(success=True, result={"path": args.path, "replaced": True})


async def _patch(args: PatchArgs) -> ToolExecutionResult:
    return await asyncio.to_thread(_patch_sync, args)


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------


class SearchFilesArgs(BaseModel):
    query: str = Field(..., min_length=1)
    path: str = Field(default=".", description="Directory (relative to the sandbox root) to search within.")
    regex: bool = Field(default=False, description="Treat `query` as a regular expression instead of a literal string.")


def _search_files_sync(args: SearchFilesArgs) -> ToolExecutionResult:
    try:
        resolved_dir = _resolve_sandboxed_path(args.path)
    except _NotConfiguredError as exc:
        return _not_configured_result(exc)
    except _SandboxEscapeError as exc:
        return _escape_result(exc)

    if not resolved_dir.is_dir():
        return ToolExecutionResult(success=False, error=f"Directory not found: {args.path}", error_type="not_found")

    if args.regex:
        try:
            pattern = re.compile(args.query)
        except re.error as exc:
            return ToolExecutionResult(success=False, error=f"Invalid regex '{args.query}': {exc}")
        matcher = pattern.search
    else:
        needle = args.query
        matcher = lambda line: needle in line  # noqa: E731

    # Safe to call again unguarded: _resolve_sandboxed_path already
    # succeeded above, which means files_sandbox_root was set and
    # resolvable at that point in this same call.
    root = _sandbox_root()
    matches: list[dict] = []
    for file_path in resolved_dir.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as f:
                for line_number, line in enumerate(f, start=1):
                    if matcher(line):
                        matches.append(
                            {
                                "path": str(file_path.relative_to(root)),
                                "line_number": line_number,
                                "line": line.rstrip("\n"),
                            }
                        )
        except OSError:
            continue  # unreadable file (permissions, binary, etc.) — skip, don't fail the whole search

    return ToolExecutionResult(success=True, result={"query": args.query, "matches": matches, "match_count": len(matches)})


async def _search_files(args: SearchFilesArgs) -> ToolExecutionResult:
    return await asyncio.to_thread(_search_files_sync, args)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register_tool(
    name="files.read_file",
    args_schema=ReadFileArgs,
    handler=_read_file,
    description="Read a text file's content, paginated by line, from within the sandboxed root.",
    skill_doc=(
        "files.read_file(path, offset=0, limit=2000) -> {path, offset, lines_returned, "
        "total_lines, content}. path is relative to the sandbox root (or an absolute "
        "path inside it) — anything that resolves outside the root fails with "
        "error_type=invalid_args and is not retryable. Use offset/limit to page "
        "through files longer than `limit` lines."
    ),
)

register_tool(
    name="files.write_file",
    args_schema=WriteFileArgs,
    handler=_write_file,
    description="Write (fully overwrite) a text file's content within the sandboxed root, creating parent directories as needed.",
    skill_doc=(
        "files.write_file(path, content) -> {path, bytes_written}. Fully overwrites "
        "any existing file at path; creates parent directories automatically. Use "
        "files.patch instead if you only want to change part of an existing file."
    ),
)

register_tool(
    name="files.patch",
    args_schema=PatchArgs,
    handler=_patch,
    description="Replace one exact occurrence of old_text with new_text in an existing file within the sandboxed root.",
    skill_doc=(
        "files.patch(path, old_text, new_text) -> {path, replaced: true}. old_text "
        "must match the file's current content exactly and appear exactly once — "
        "no fuzzy matching. If old_text isn't found (or matches more than once), "
        "the failure is retryable: re-read the file with files.read_file and retry "
        "with old_text copied exactly (and, if it matched multiple times, with more "
        "surrounding context to make it unique)."
    ),
)

register_tool(
    name="files.search_files",
    args_schema=SearchFilesArgs,
    handler=_search_files,
    description="Search text files under a directory (within the sandboxed root) for a literal string or regex, returning matching file paths and line numbers.",
    skill_doc=(
        "files.search_files(query, path='.', regex=False) -> {query, matches: "
        "[{path, line_number, line}], match_count}. path is a directory relative to "
        "the sandbox root, searched recursively. Set regex=true to treat query as a "
        "Python regular expression instead of a literal substring."
    ),
)
