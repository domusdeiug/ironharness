"""
Tool registry.

Every tool is registered exactly once, globally, keyed by name. This is
where the handoff's open "tool-name collision rule" is resolved: rather
than leaving it ambiguous which of a profession-folder tool or a
shared-pool tool wins, registration is enforced-unique — attempting to
register two different callables under the same name raises at import
time. There is no shadowing/override behavior. If two professions
legitimately need "the same kind of thing" with different behavior, they
must use different tool names (e.g. `xlsx.write_cell` vs
`gsheets.write_cell`), not rely on last-registered-wins.

Each registered tool carries:
- `name`               — the string used in `plan_steps.tool_name` and in a
                          profession's `tools` list.
- `args_schema`         — a Pydantic model ("the form"). This is what Stage 2
                          validates `tool_args` against before a plan is
                          locked.
- `handler`             — async callable(args_model_instance) -> ToolExecutionResult.
- `skill_doc`           — short reference text the model-retry diagnostic
                          step can read when a call to this tool fails. Per
                          the handoff, exactly what counts as "the tool's
                          skill doc" is still open pending a first real
                          profession; the mechanism here is deliberately
                          simple (a string on the registration) so it can be
                          swapped for a richer doc-lookup later without
                          changing callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import BaseModel

from app.models import ToolExecutionResult


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    args_schema: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[ToolExecutionResult]]
    skill_doc: str
    description: str


class ToolCollisionError(Exception):
    pass


class ToolNotFoundError(Exception):
    pass


_REGISTRY: dict[str, RegisteredTool] = {}


def register_tool(
    *,
    name: str,
    args_schema: type[BaseModel],
    handler: Callable[[BaseModel], Awaitable[ToolExecutionResult]],
    skill_doc: str,
    description: str,
) -> None:
    if name in _REGISTRY:
        existing = _REGISTRY[name]
        if existing.handler is handler and existing.args_schema is args_schema:
            # Re-registering the identical tool (e.g. module imported twice)
            # is a no-op, not a collision.
            return
        raise ToolCollisionError(
            f"Tool '{name}' is already registered (handler={existing.handler!r}). "
            "Tool names are enforced-unique across profession folders and the "
            "shared tools/ pool — rename one of them."
        )
    _REGISTRY[name] = RegisteredTool(
        name=name,
        args_schema=args_schema,
        handler=handler,
        skill_doc=skill_doc,
        description=description,
    )


def get_tool(name: str) -> RegisteredTool:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ToolNotFoundError(f"No tool registered under name '{name}'") from exc


def all_tools() -> dict[str, RegisteredTool]:
    return dict(_REGISTRY)


def validate_tool_args(tool_name: str, raw_args: dict) -> BaseModel:
    """Validate raw args against the tool's schema. Raises pydantic's
    ValidationError on failure — callers (Stage 2) catch this to build the
    validation_errors payload for plan_validation_attempts."""
    tool = get_tool(tool_name)
    return tool.args_schema.model_validate(raw_args)


async def execute_tool(tool_name: str, validated_args: BaseModel) -> ToolExecutionResult:
    tool = get_tool(tool_name)
    return await tool.handler(validated_args)
