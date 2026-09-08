"""
Stage 2 — PLAN.

Produces one locked, ordered plan for the request's selected profession(s).
Multi-profession sequencing is decided here (which steps use which
profession's tools), not in Stage 1.

Validation checkpoint: right after the plan-generation call, every step's
tool_args is validated against that tool's registered Pydantic schema
("the form") — before the plan is persisted as locked and before any
worker job starts. This is distinct from Stage 3's intent-evaluation:
schema validation catches malformed args; intent-evaluation catches
well-formed-but-wrong results.

On validation failure: re-prompt PLAN with the validation error attached,
capped at `plan.plan_validation_max_attempts` (default 3, separate counter
from ACTION's retry ladder). Every attempt is logged to
plan_validation_attempts regardless of outcome. If still invalid after the
cap, the request fails at planning time with an explicit reason — ACTION is
never handed a step known to be malformed.

Also supports "replan": called again with prior successful steps' results
passed in as fixed context, scoped to only the failed step onward.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.config import cfg
from app.db import get_service_client
from app.llm import StructuredCallError, call_structured
from app.models import PlanOutput, PlanStepRow, ProfessionRow
from app.tools.registry import ToolNotFoundError, validate_tool_args

logger = logging.getLogger("thea.plan")


class PlanningError(Exception):
    pass


class NeedsClarification(Exception):
    def __init__(self, question: str):
        self.question = question
        super().__init__(question)


@dataclass
class ValidatedStep:
    step_number: int
    profession_id: uuid.UUID | None
    tool_name: str
    tool_args: dict[str, Any]
    intent: str
    blocking: bool


_PLAN_SYSTEM_PROMPT = """You are the planning stage of an agent harness. Given a request and the
selected profession(s) with their available tools, produce ONE ordered plan:
a numbered list of steps, each calling exactly one tool.

Rules:
- Every step MUST include an `intent`: a short statement of what calling
  this tool is meant to achieve. This is what later evaluation checks
  results against.
- step_number must start at 1 and increase sequentially with no gaps.
- If multiple professions were selected, decide explicitly which steps use
  which profession's tools — sequence them in whatever order the request
  actually requires (e.g. fix the spreadsheet, THEN email it).
- Mark a step `blocking: true` if the rest of the plan depends on it
  succeeding; `blocking: false` if the plan can reasonably continue without
  it (partial success is acceptable for that step).
- tool_args must be a JSON object matching the named tool's schema exactly
  — correct field names and types, all required fields present. Do not
  include fields the tool doesn't define.
- If the request is ambiguous enough that you cannot commit to a plan, set
  `clarifying_question` and leave `steps` empty.
- You may only use tools that were explicitly listed as available below.
  Do not invent tool names."""


def _tools_prompt_block(professions: list[ProfessionRow]) -> str:
    from app.tools.registry import get_tool

    lines = []
    for prof in professions:
        lines.append(f"\nProfession: {prof.name}\n{prof.skill_prompt}")
        for tool_name in prof.tools:
            try:
                tool = get_tool(tool_name)
            except ToolNotFoundError:
                logger.error("Profession %s lists unregistered tool %s", prof.name, tool_name)
                continue
            schema = tool.args_schema.model_json_schema()
            lines.append(f"  - {tool_name}: {tool.description}\n    args schema: {schema}")
    return "\n".join(lines)


def _build_user_prompt(
    *,
    request_text: str,
    professions: list[ProfessionRow],
    prior_context: str | None = None,
    failure_context: str | None = None,
    validation_error: str | None = None,
) -> str:
    parts = [f"Request: {request_text}", "", "Available professions and tools:", _tools_prompt_block(professions)]
    if prior_context:
        parts += ["", "Prior successful steps (already executed — do NOT redo these, plan only from here forward):", prior_context]
    if failure_context:
        parts += ["", "The step that needs replanning failed with:", failure_context]
    if validation_error:
        parts += [
            "",
            "Your previous plan attempt had invalid tool_args. Fix this and "
            "resubmit a complete, corrected plan:",
            validation_error,
        ]
    return "\n".join(parts)


async def _generate_plan_with_validation(
    *,
    request_id: uuid.UUID,
    replan_generation: int,
    request_text: str,
    professions: list[ProfessionRow],
    professions_by_name: dict[str, ProfessionRow],
    prior_context: str | None = None,
    failure_context: str | None = None,
) -> list[ValidatedStep]:
    model = cfg("plan.model")
    max_attempts = cfg("plan.plan_validation_max_attempts", 3)
    client = get_service_client()

    validation_error_text: str | None = None
    last_output: PlanOutput | None = None

    for attempt in range(1, max_attempts + 1):
        user_prompt = _build_user_prompt(
            request_text=request_text,
            professions=professions,
            prior_context=prior_context,
            failure_context=failure_context,
            validation_error=validation_error_text,
        )
        try:
            output = await call_structured(
                model=model,
                schema=PlanOutput,
                system_prompt=_PLAN_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=4000,
            )
        except StructuredCallError as exc:
            validation_error_text = f"Model call/parse failure: {exc}"
            _log_validation_attempt(client, request_id, replan_generation, attempt, [{"error": validation_error_text}], False)
            continue

        last_output = output
        if output.clarifying_question and not output.steps:
            raise NeedsClarification(output.clarifying_question)

        errors: list[dict[str, Any]] = []
        validated: list[ValidatedStep] = []

        for step in output.steps:
            profession = professions_by_name.get(step.profession_name)
            if profession is None:
                errors.append({"step": step.step_number, "error": f"Unknown profession '{step.profession_name}'"})
                continue
            if step.tool_name not in profession.tools:
                errors.append(
                    {
                        "step": step.step_number,
                        "error": f"Tool '{step.tool_name}' is not in profession '{profession.name}''s tool list",
                    }
                )
                continue
            try:
                validated_args = validate_tool_args(step.tool_name, step.tool_args)
            except ToolNotFoundError as exc:
                errors.append({"step": step.step_number, "error": str(exc)})
                continue
            except ValidationError as exc:
                errors.append({"step": step.step_number, "tool": step.tool_name, "error": exc.errors()})
                continue

            validated.append(
                ValidatedStep(
                    step_number=step.step_number,
                    profession_id=profession.id,
                    tool_name=step.tool_name,
                    tool_args=validated_args.model_dump(mode="json"),
                    intent=step.intent,
                    blocking=step.blocking,
                )
            )

        if not errors:
            _log_validation_attempt(client, request_id, replan_generation, attempt, [], True)
            return validated

        validation_error_text = str(errors)
        _log_validation_attempt(client, request_id, replan_generation, attempt, errors, False)
        logger.warning("Plan validation attempt %d/%d failed: %s", attempt, max_attempts, errors)

    raise PlanningError(
        f"PLAN could not produce valid args after {max_attempts} attempts. "
        f"Last errors: {validation_error_text}"
    )


def _log_validation_attempt(
    client, request_id: uuid.UUID, replan_generation: int, attempt: int, errors: list[dict], resolved: bool
) -> None:
    client.table("plan_validation_attempts").insert(
        {
            "request_id": str(request_id),
            "replan_generation": replan_generation,
            "attempt_number": attempt,
            "validation_errors": errors,
            "resolved": resolved,
        }
    ).execute()


def _persist_plan_steps(request_id: uuid.UUID, replan_generation: int, steps: list[ValidatedStep]) -> list[PlanStepRow]:
    client = get_service_client()
    rows = [
        {
            "request_id": str(request_id),
            "replan_generation": replan_generation,
            "step_number": s.step_number,
            "profession_id": str(s.profession_id) if s.profession_id else None,
            "tool_name": s.tool_name,
            "tool_args": s.tool_args,
            "intent": s.intent,
            "blocking": s.blocking,
        }
        for s in steps
    ]
    resp = client.table("plan_steps").insert(rows).execute()
    return [PlanStepRow.model_validate(r) for r in resp.data]


async def create_plan(
    *,
    request_id: uuid.UUID,
    request_text: str,
    professions: list[ProfessionRow],
    professions_by_name: dict[str, ProfessionRow],
) -> list[PlanStepRow]:
    """Initial plan (replan_generation=0)."""
    validated = await _generate_plan_with_validation(
        request_id=request_id,
        replan_generation=0,
        request_text=request_text,
        professions=professions,
        professions_by_name=professions_by_name,
    )
    return _persist_plan_steps(request_id, 0, validated)


async def create_replan(
    *,
    request_id: uuid.UUID,
    replan_generation: int,
    request_text: str,
    professions: list[ProfessionRow],
    professions_by_name: dict[str, ProfessionRow],
    prior_successful_steps: list[dict[str, Any]],
    failed_step_intent: str,
    tool_error: str,
    evaluation_note: str | None,
) -> list[PlanStepRow]:
    """Replan scoped to only the failed step onward. `prior_successful_steps`
    is fixed context (already-executed results), never redone."""
    prior_context = "\n".join(
        f"  step {s['step_number']} ({s['tool_name']}): intent='{s['intent']}' -> {s['result_summary']}"
        for s in prior_successful_steps
    )
    failure_context = f"intent='{failed_step_intent}', tool_error={tool_error!r}, evaluation_note={evaluation_note!r}"

    validated = await _generate_plan_with_validation(
        request_id=request_id,
        replan_generation=replan_generation,
        request_text=request_text,
        professions=professions,
        professions_by_name=professions_by_name,
        prior_context=prior_context,
        failure_context=failure_context,
    )

    # Renumber steps to continue after the prior successful steps, so
    # step_number stays globally meaningful within this replan_generation
    # while the (request_id, replan_generation, step_number) uniqueness
    # constraint is respected.
    offset = len(prior_successful_steps)
    for i, step in enumerate(validated, start=1):
        step.step_number = offset + i

    return _persist_plan_steps(request_id, replan_generation, validated)
