"""
Smoke checks for the tools ported in from Hermes Agent's built-in list:
web_search (finished stub), web_extract, the files.* profession, and
vision_analyze. Mirrors the pattern already used for xlsx/email/calendar:
validate a deliberately malformed args dict is rejected, and exercise at
least one real handler call per tool where that's possible without
network access (this sandbox's egress allowlist blocks the providers
web_search/web_extract/vision_analyze would actually call — see
test_pipeline_mocked.py's docstring for the same constraint — so those
three are checked for not_configured/validation behavior rather than a
live network round trip).

Run with: python -m tests.test_new_tools_smoke
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from pydantic import ValidationError

from app.tools import load_all_tools
from app.tools.registry import execute_tool, get_tool, validate_tool_args


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_web_search_malformed_args_rejected() -> None:
    try:
        validate_tool_args("web_search", {"max_results": 999})  # missing query, out-of-range max_results
        raise AssertionError("Expected ValidationError for malformed web_search args")
    except ValidationError:
        pass


def test_web_search_not_configured_without_key() -> None:
    os.environ.pop("WEB_SEARCH_API_KEY", None)
    os.environ.setdefault("FILES_SANDBOX_ROOT", tempfile.mkdtemp())
    from app.config import get_settings

    get_settings.cache_clear()
    args = validate_tool_args("web_search", {"query": "thea harness"})
    result = asyncio.run(execute_tool("web_search", args))
    _assert(result.success is False, "web_search should fail without a configured API key")
    _assert(result.error_type == "not_configured", f"expected not_configured, got {result.error_type}")


def test_web_extract_malformed_args_rejected() -> None:
    try:
        validate_tool_args("web_extract", {"url": "", "max_chars": 1})  # empty url, max_chars below floor
        raise AssertionError("Expected ValidationError for malformed web_extract args")
    except ValidationError:
        pass


def test_files_round_trip() -> None:
    with tempfile.TemporaryDirectory() as sandbox_root:
        os.environ["FILES_SANDBOX_ROOT"] = sandbox_root
        from app.config import get_settings

        get_settings.cache_clear()

        write_args = validate_tool_args("files.write_file", {"path": "notes/todo.txt", "content": "buy milk\nwalk dog\n"})
        write_result = asyncio.run(execute_tool("files.write_file", write_args))
        _assert(write_result.success, f"files.write_file failed: {write_result.error}")
        _assert(write_result.result["path"] == "notes/todo.txt", "unexpected path in write result")

        read_args = validate_tool_args("files.read_file", {"path": "notes/todo.txt"})
        read_result = asyncio.run(execute_tool("files.read_file", read_args))
        _assert(read_result.success, f"files.read_file failed: {read_result.error}")
        _assert(read_result.result["content"] == "buy milk\nwalk dog\n", "read-back content mismatch")
        _assert(read_result.result["total_lines"] == 2, "expected 2 lines")

        patch_args = validate_tool_args(
            "files.patch", {"path": "notes/todo.txt", "old_text": "buy milk", "new_text": "buy oat milk"}
        )
        patch_result = asyncio.run(execute_tool("files.patch", patch_args))
        _assert(patch_result.success, f"files.patch failed: {patch_result.error}")

        reread_args = validate_tool_args("files.read_file", {"path": "notes/todo.txt"})
        reread_result = asyncio.run(execute_tool("files.read_file", reread_args))
        _assert("buy oat milk" in reread_result.result["content"], "patch did not apply")

        search_args = validate_tool_args("files.search_files", {"query": "walk", "path": "."})
        search_result = asyncio.run(execute_tool("files.search_files", search_args))
        _assert(search_result.success, f"files.search_files failed: {search_result.error}")
        _assert(search_result.result["match_count"] == 1, "expected exactly one match for 'walk'")
        _assert(search_result.result["matches"][0]["path"] == "notes/todo.txt", "unexpected match path")


def test_files_not_configured_without_sandbox_root() -> None:
    os.environ.pop("FILES_SANDBOX_ROOT", None)
    from app.config import get_settings

    get_settings.cache_clear()

    args = validate_tool_args("files.read_file", {"path": "foo.txt"})
    result = asyncio.run(execute_tool("files.read_file", args))
    _assert(result.success is False, "files.read_file should fail without FILES_SANDBOX_ROOT set")
    _assert(result.error_type == "not_configured", f"expected not_configured, got {result.error_type}")


def test_app_boots_without_files_sandbox_root() -> None:
    """The regression this test guards against: files_sandbox_root must be
    optional at the Settings level, so a deployment that never touches the
    files profession isn't blocked from booting by it — same as smtp_host
    being optional. Only supabase_url/openrouter_api_key/etc should be
    whole-app-blocking required fields."""
    os.environ.pop("FILES_SANDBOX_ROOT", None)
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()  # must not raise
    _assert(settings.files_sandbox_root is None, "expected files_sandbox_root to default to None when unset")


def test_files_read_file_not_found() -> None:
    with tempfile.TemporaryDirectory() as sandbox_root:
        os.environ["FILES_SANDBOX_ROOT"] = sandbox_root
        from app.config import get_settings

        get_settings.cache_clear()

        args = validate_tool_args("files.read_file", {"path": "does/not/exist.txt"})
        result = asyncio.run(execute_tool("files.read_file", args))
        _assert(result.success is False, "expected failure for missing file")
        _assert(result.error_type == "not_found", f"expected not_found, got {result.error_type}")


def test_files_sandbox_escape_rejected() -> None:
    with tempfile.TemporaryDirectory() as sandbox_root:
        os.environ["FILES_SANDBOX_ROOT"] = sandbox_root
        from app.config import get_settings

        get_settings.cache_clear()

        args = validate_tool_args("files.write_file", {"path": "../../etc/escape.txt", "content": "nope"})
        result = asyncio.run(execute_tool("files.write_file", args))
        _assert(result.success is False, "sandbox escape should be rejected")
        _assert(result.error_type == "invalid_args", f"expected invalid_args, got {result.error_type}")

        abs_escape_args = validate_tool_args("files.read_file", {"path": "/etc/passwd"})
        abs_result = asyncio.run(execute_tool("files.read_file", abs_escape_args))
        _assert(abs_result.success is False, "absolute-path sandbox escape should be rejected")
        _assert(abs_result.error_type == "invalid_args", f"expected invalid_args, got {abs_result.error_type}")


def test_files_patch_no_match_is_retryable() -> None:
    with tempfile.TemporaryDirectory() as sandbox_root:
        os.environ["FILES_SANDBOX_ROOT"] = sandbox_root
        from app.config import get_settings

        get_settings.cache_clear()

        write_args = validate_tool_args("files.write_file", {"path": "f.txt", "content": "hello world"})
        asyncio.run(execute_tool("files.write_file", write_args))

        patch_args = validate_tool_args("files.patch", {"path": "f.txt", "old_text": "goodbye", "new_text": "hi"})
        result = asyncio.run(execute_tool("files.patch", patch_args))
        _assert(result.success is False, "expected patch failure for absent old_text")
        _assert(result.error_type is None, f"expected unset (transient/retryable) error_type, got {result.error_type}")


def test_vision_analyze_malformed_args_rejected() -> None:
    try:
        validate_tool_args("vision_analyze", {})  # missing required image_url
        raise AssertionError("Expected ValidationError for malformed vision_analyze args")
    except ValidationError:
        pass


def test_vision_analyze_sandbox_escape_rejected() -> None:
    with tempfile.TemporaryDirectory() as sandbox_root:
        os.environ["FILES_SANDBOX_ROOT"] = sandbox_root
        from app.config import get_settings

        get_settings.cache_clear()

        args = validate_tool_args("vision_analyze", {"image_url": "../../etc/passwd"})
        result = asyncio.run(execute_tool("vision_analyze", args))
        _assert(result.success is False, "sandbox escape should be rejected")
        _assert(result.error_type == "invalid_args", f"expected invalid_args, got {result.error_type}")


def test_vision_analyze_local_not_found() -> None:
    with tempfile.TemporaryDirectory() as sandbox_root:
        os.environ["FILES_SANDBOX_ROOT"] = sandbox_root
        from app.config import get_settings

        get_settings.cache_clear()

        args = validate_tool_args("vision_analyze", {"image_url": "missing.png"})
        result = asyncio.run(execute_tool("vision_analyze", args))
        _assert(result.success is False, "expected failure for missing local image")
        _assert(result.error_type == "not_found", f"expected not_found, got {result.error_type}")


def main() -> None:
    load_all_tools()

    # Registry sanity: every new tool actually registered, no surprises.
    for name in (
        "web_search",
        "web_extract",
        "files.read_file",
        "files.write_file",
        "files.patch",
        "files.search_files",
        "vision_analyze",
    ):
        get_tool(name)  # raises ToolNotFoundError if missing

    test_web_search_malformed_args_rejected()
    test_web_search_not_configured_without_key()
    test_web_extract_malformed_args_rejected()
    test_app_boots_without_files_sandbox_root()
    test_files_not_configured_without_sandbox_root()
    test_files_round_trip()
    test_files_read_file_not_found()
    test_files_sandbox_escape_rejected()
    test_files_patch_no_match_is_retryable()
    test_vision_analyze_malformed_args_rejected()
    test_vision_analyze_sandbox_escape_rejected()
    test_vision_analyze_local_not_found()

    print("ALL ASSERTIONS PASSED — new tools registered, validated, and round-tripped correctly")


if __name__ == "__main__":
    main()
