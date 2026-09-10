"""
Stage 3 — ACTION.

Executes a locked plan's steps in order. Runs as a background worker job
(see worker.py), not inside a short-lived HTTP request. Every step's
execution + evaluation result is persisted as it happens (row-per-attempt
in action_results), which is what enables crash-recovery: on resume, the
worker can see exactly which steps/attempts already ran and avoid
double-firing a side-effecting call.

Per-step flow:
1. Execute the step's tool.
2. Evaluate the result against the step's `intent` (code check or cheap
   model judgment — decided by the tool/profession, not globally).
3. Pass -> next step.
4. Fail -> retry ladder:
   - Attempt 1 (already just ran) doesn't count as a "retry" on its own;
     what's labeled `logic_retry` below is attempt 2's retry-without-a-model
     step when the error is classified as transient. If not retryable,
     skip straight to marking the step failed.
   - Attempts 2-3 (`model_retry`): cheap model gets the error + original
     args + intent, may use up to `max_diagnostic_calls_per_attempt` tool
     calls (web search / reading the tool's skill_doc) to diagnose, then
     proposes adjusted args before retrying. Hard ceiling of
     `max_diagnostic_calls_per_step` diagnostic calls total, enforced here.
5. Still failing after max_attempts_per_step -> mark failed.
   - non-blocking -> continue to next step, recorded as a known gap.
   - blocking -> escalate to replan (handled by the orchestrator, not this
     module directly — see worker.py's run_action_stage).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.infra.config import cfg
from app.infra.db import get_service_client
from app.infra.llm import StructuredCallError, call_structured
from app.infra.models import (
    ActionOutcome,
    PlanStepRow,
    RetryAttemptType,
    RetryProposal,
    StepEvaluationResult,
    ToolExecutionResult,
)
from app.tools.registry import get_tool, validate_tool_args

logger = logging.getLogger("thea.action")

# Error types considered transient/retryable at attempt-1 (logic-only,
# no model call). Per-tool refinement is an explicit OPEN item in the
# handoff ("build per-tool as professions are added, not globally") — this
# is the sane global default until then.
_RETRYABLE_ERROR_TYPES = {"transient", "rate_limit", "timeout", "network"}
_TERMINAL_ERROR_TYPES = {"auth_failure", "invalid_args", "not_found", "not_configured", "not_implemented"}


class BlockingStepFailed(Exception):
    """Raised when a blocking step exhausts all retry attempts. Carries
    everything the orchestrator needs to trigger a scoped replan, including
    the outcomes of every step in this run that passed *before* the
    failure — the orchestrator needs these to build replan context and to
    keep an accurate all_outcomes list, since raising unwinds past
    run_action_stage's own local list."""

    def __init__(
        self,
        step: PlanStepRow,
        tool_error: str | None,
        evaluation_note: str | None,
        partial_outcomes: list["StepOutcome"],
    ):
        self.step = step
        self.tool_error = tool_error
        self.evaluation_note = evaluation_note
        self.partial_outcomes = partial_outcomes
        super().__init__(f"Blocking step {step.step_number} ({step.tool_name}) failed after all retries")


@dataclass
class StepOutcome:
    step: PlanStepRow
    passed: bool
    final_result: dict[str, Any] | None
    final_error: str | None
    attempts_used: int


@dataclass
class ActionRunResult:
    outcomes: list[StepOutcome] = field(default_factory=list)
    any_non_blocking_failed: bool = False


def _classify_error_type(execution: ToolExecutionResult) -> str:
    if execution.error_type:
        return execution.error_type
    return "transient"  # unknown errors default to retryable, not terminal


def _is_retryable(error_type: str) -> bool:
    if error_type in _TERMINAL_ERROR_TYPES:
        return False
    return error_type in _RETRYABLE_ERROR_TYPES or error_type not in _TERMINAL_ERROR_TYPES


async def _evaluate_step(step: PlanStepRow, execution: ToolExecutionResult) -> StepEvaluationResult:
    """Code check for structurally verifiable output; falls back to a cheap
    model judgment otherwise. Which one a given tool needs is a per-profession
    decision (OPEN in handoff) — the code-check path here covers the
    generic case (tool reported success/failure itself), and the model
    path is the default judgment call for anything murkier."""
    if not execution.success:
        return StepEvaluationResult(passed=False, note=execution.error or "Tool execution failed.")

    if execution.result is not None and "success" not in execution.result:
        # Structurally verifiable: the tool already told us success=True and
        # returned a result payload. Trust the code-level signal.
        return StepEvaluationResult(passed=True, note="Tool reported success; result payload present.")

    # Fall back to a cheap model judgment against the step's intent.
    model = cfg("evaluation.step_eval_model")
    try:
        judged = await call_structured(
            model=model,
            schema=StepEvaluationResult,
            system_prompt=(
                "Judge whether a tool's result satisfies the stated intent. "
                "Be strict: partial or ambiguous results should fail."
            ),
            user_prompt=f"Intent: {step.intent}\nTool: {step.tool_name}\nResult: {execution.result}",
        )
        return judged
    except StructuredCallError as exc:
        logger.warning("Step evaluation model call failed for step %s: %s", step.step_number, exc)
        # Evaluation infrastructure failing is itself a fail, not a silent pass.
        return StepEvaluationResult(passed=False, note=f"Evaluation call failed: {exc}")


def _persist_action_result(
    *,
    request_id: uuid.UUID,
    step: PlanStepRow,
    attempt_number: int,
    attempt_type: RetryAttemptType,
    execution: ToolExecutionResult | None,
    evaluation: StepEvaluationResult | None,
    diagnostic_calls: list[dict[str, Any]],
) -> None:
    client = get_service_client()
    client.table("action_results").insert(
        {
            "request_id": str(request_id),
            "plan_step_id": str(step.id),
            "attempt_number": attempt_number,
            "attempt_type": attempt_type.value,
            "tool_result": execution.result if execution else None,
            "tool_error": execution.error if execution else None,
            "diagnostic_calls": diagnostic_calls,
            "diagnostic_call_count": len(diagnostic_calls),
            "evaluation": (ActionOutcome.pass_.value if evaluation and evaluation.passed else ActionOutcome.fail.value)
            if evaluation
            else ActionOutcome.pending.value,
            "evaluation_note": evaluation.note if evaluation else None,
        }
    ).execute()


async def _run_diagnostics_and_propose_args(
    *,
    step: PlanStepRow,
    tool_error: str,
    remaining_diagnostic_budget: int,
) -> tuple[RetryProposal | None, list[dict[str, Any]]]:
    """Attempts 2-3: cheap model proposes adjusted args, optionally using a
    bounded number of diagnostic tool calls first (web search / skill_doc
    read). Returns (proposal_or_None, diagnostic_calls_made)."""
    model = cfg("action.retry_model")
    tool = get_tool(step.tool_name)
    diagnostic_calls: list[dict[str, Any]] = []

    per_attempt_cap = min(cfg("action.max_diagnostic_calls_per_attempt", 3), remaining_diagnostic_budget)

    # Diagnostic reads are simple here: always surface the tool's own
    # skill_doc for free (no budget cost — it's a local lookup, not an
    # external call), then allow up to `per_attempt_cap` web_search calls
    # if the model asks for them via its proposal reasoning. A fuller
    # tool-calling loop (letting the model actually decide per-call) is
    # the natural next step once a first real profession defines what
    # "the tool's skill doc" should contain beyond this string — flagged
    # as OPEN in the handoff.
    skill_doc_context = f"Tool skill doc: {tool.skill_doc}"

    try:
        proposal = await call_structured(
            model=model,
            schema=RetryProposal,
            system_prompt=(
                "A tool call failed. Propose adjusted arguments that are likely to "
                "succeed, given the error, the original args, the step's intent, and "
                "the tool's own documentation. Only change what's necessary."
            ),
            user_prompt=(
                f"Tool: {step.tool_name}\n"
                f"Original args: {step.tool_args}\n"
                f"Intent: {step.intent}\n"
                f"Error: {tool_error}\n"
                f"{skill_doc_context}\n"
                f"(Diagnostic budget available but not auto-invoked in this build: {per_attempt_cap} calls)"
            ),
        )
    except StructuredCallError as exc:
        logger.warning("Retry-proposal model call failed for step %s: %s", step.step_number, exc)
        return None, diagnostic_calls

    return proposal, diagnostic_calls


async def execute_step_with_retries(*, request_id: uuid.UUID, step: PlanStepRow) -> StepOutcome:
    max_attempts = cfg("action.max_attempts_per_step", 3)
    max_diagnostic_total = cfg("action.max_diagnostic_calls_per_step", 6)
    diagnostic_budget_used = 0

    current_args = dict(step.tool_args)
    last_execution: ToolExecutionResult | None = None
    last_evaluation: StepEvaluationResult | None = None

    for attempt in range(1, max_attempts + 1):
        attempt_type = (
            RetryAttemptType.initial
            if attempt == 1
            else (RetryAttemptType.logic_retry if attempt == 2 and last_execution and _should_logic_only_retry(last_execution) else RetryAttemptType.model_retry)
        )

        try:
            validated_args = validate_tool_args(step.tool_name, current_args)
            execution = await get_tool(step.tool_name).handler(validated_args)
        except Exception as exc:  # noqa: BLE001 — tool boundary must not crash the worker
            execution = ToolExecutionResult(success=False, error=str(exc), error_type="transient")

        last_execution = execution
        evaluation = await _evaluate_step(step, execution)
        last_evaluation = evaluation

        _persist_action_result(
            request_id=request_id,
            step=step,
            attempt_number=attempt,
            attempt_type=attempt_type,
            execution=execution,
            evaluation=evaluation,
            diagnostic_calls=[],
        )

        if evaluation.passed:
            return StepOutcome(
                step=step, passed=True, final_result=execution.result, final_error=None, attempts_used=attempt
            )

        if attempt >= max_attempts:
            break

        error_type = _classify_error_type(execution)

        if attempt == 1:
            # Attempt-1 -> attempt-2 decision: pure logic, no model call.
            if not _is_retryable(error_type):
                logger.info(
                    "Step %s error_type=%s classified terminal; skipping remaining retries.",
                    step.step_number,
                    error_type,
                )
                break
            # Retryable and cheap: just retry with identical args once
            # (logic_retry) before spending a model call.
            continue

        # attempts 2..max_attempts-1: model-assisted arg adjustment
        remaining_budget = max_diagnostic_total - diagnostic_budget_used
        if remaining_budget <= 0:
            logger.info("Step %s hit diagnostic call ceiling; no further model-assisted retries.", step.step_number)
            break

        proposal, diag_calls = await _run_diagnostics_and_propose_args(
            step=step, tool_error=execution.error or "unknown error", remaining_diagnostic_budget=remaining_budget
        )
        diagnostic_budget_used += len(diag_calls)
        if proposal is None:
            break
        current_args = proposal.adjusted_args

    return StepOutcome(
        step=step,
        passed=False,
        final_result=last_execution.result if last_execution else None,
        final_error=last_execution.error if last_execution else "no execution attempted",
        attempts_used=max_attempts,
    )


def _should_logic_only_retry(execution: ToolExecutionResult) -> bool:
    return _classify_error_type(execution) in _RETRYABLE_ERROR_TYPES


async def run_action_stage(*, request_id: uuid.UUID, steps: list[PlanStepRow]) -> ActionRunResult:
    """Executes steps in order. Raises BlockingStepFailed as soon as a
    blocking step exhausts retries — the caller (worker.py) is responsible
    for triggering a scoped replan and re-invoking this function for the
    remainder of the plan. Non-blocking failures are recorded and execution
    continues."""
    result = ActionRunResult()

    for step in steps:
        outcome = await execute_step_with_retries(request_id=request_id, step=step)
        result.outcomes.append(outcome)

        if not outcome.passed:
            if step.blocking:
                raise BlockingStepFailed(
                    step=step,
                    tool_error=outcome.final_error,
                    evaluation_note=None,
                    partial_outcomes=list(result.outcomes),
                )
            else:
                result.any_non_blocking_failed = True
                logger.info(
                    "Non-blocking step %s (%s) failed after retries; continuing. error=%s",
                    step.step_number,
                    step.tool_name,
                    outcome.final_error,
                )

    return result
