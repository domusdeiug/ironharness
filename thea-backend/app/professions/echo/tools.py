"""
`echo` profession — a deliberately trivial, deterministic profession used
to exercise the full pipeline (classify -> plan -> act -> synthesize ->
evaluate) end to end without depending on any real external integration.
Seed this profession's row via `scripts/seed_professions.py`.

Not meant to ship to real users; useful as a smoke test and as a template
for the shape a real profession module takes: a `tools.py` that registers
its tools on import, args schemas with real validation, and a `seed.py`
(or entry in the seed script) describing the profession row.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models import ToolExecutionResult
from app.tools.registry import register_tool


class EchoArgs(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


async def _echo(args: EchoArgs) -> ToolExecutionResult:
    return ToolExecutionResult(success=True, result={"echoed": args.message})


class FailArgs(BaseModel):
    # Deliberately narrow field so "call this destructively" is structurally
    # hard to trigger by accident — mirrors the handoff's delete_email
    # example of using schema strictness to gate risky actions, just with a
    # harmless tool so it's safe to leave registered.
    confirm_failure: bool = Field(
        ...,
        description="Must be explicitly true. Exists to test the retry ladder on a tool that always fails.",
    )


async def _always_fail(args: FailArgs) -> ToolExecutionResult:
    return ToolExecutionResult(
        success=False,
        error="This tool always fails; it exists to exercise the retry/replan ladder.",
        error_type="transient",
    )


register_tool(
    name="echo.say",
    args_schema=EchoArgs,
    handler=_echo,
    description="Echoes back the given message. No side effects.",
    skill_doc="echo.say(message: str) -> {echoed: str}. Always succeeds.",
)

register_tool(
    name="echo.always_fail",
    args_schema=FailArgs,
    handler=_always_fail,
    description="Always fails with a transient error. For testing retries only.",
    skill_doc="echo.always_fail(confirm_failure: bool) -> always returns a transient failure.",
)
