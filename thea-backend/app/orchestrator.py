"""
The orchestrator: drives one request through all 5 stages in order. This is
what the background worker (worker.py) invokes per job. Kept separate from
worker.py so the actual pipeline logic is testable without a queue.

Status transitions on the `requests` row happen here, at each stage
boundary, so a frontend polling/subscribing to the row always sees an
accurate `status` — this is the "always keep the user informed"
cross-cutting requirement from the handoff.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.config import cfg
from app.db import get_service_client
from app.models import RequestStatus
from app.stages import classify, evaluate, plan, synthesize
from app.stages.action import BlockingStepFailed, StepOutcome, run_action_stage

logger = logging.getLogger("thea.orchestrator")


class RequestFailed(Exception):
    def __init__(self, request_id: uuid.UUID, reason: str):
        self.request_id = request_id
        self.reason = reason
        super().__init__(reason)


def _set_status(request_id: uuid.UUID, status: RequestStatus, **extra: Any) -> None:
    client = get_service_client()
    client.table("requests").update({"status": status.value, **extra}).eq("id", str(request_id)).execute()


def _fail_request(request_id: uuid.UUID, reason: str) -> None:
    client = get_service_client()
    client.table("requests").update(
        {"status": RequestStatus.failed.value, "final_response": reason, "incomplete": True}
    ).eq("id", str(request_id)).execute()
    logger.error("Request %s failed: %s", request_id, reason)


def _persist_classifications(request_id: uuid.UUID, selections, professions_by_name) -> list[uuid.UUID]:
    client = get_service_client()
    profession_ids = []
    rows = []
    for sel in selections:
        prof = professions_by_name[sel.profession_name]
        profession_ids.append(prof.id)
        rows.append(
            {
                "request_id": str(request_id),
                "profession_id": str(prof.id),
                "confidence": sel.confidence,
                "rationale": sel.rationale,
            }
        )
    client.table("classifications").insert(rows).execute()
    return profession_ids


def _summarize_prior_steps_for_replan(outcomes: list[StepOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "step_number": o.step.step_number,
            "tool_name": o.step.tool_name,
            "intent": o.step.intent,
            "result_summary": o.final_result,
        }
        for o in outcomes
        if o.passed
    ]


async def process_request(*, request_id: uuid.UUID) -> None:
    """Main pipeline entry point. Idempotent-ish at the stage-status level:
    if this crashes and is re-invoked, it re-reads `requests.status` to
    avoid restarting from scratch — see the crash-recovery note in
    stages/action.py for how ACTION itself resumes at the step level."""
    client = get_service_client()
    req_resp = client.table("requests").select("*").eq("id", str(request_id)).single().execute()
    if not req_resp.data:
        raise RequestFailed(request_id, f"Request {request_id} not found")

    request_text = req_resp.data["raw_text"]
    user_id = uuid.UUID(req_resp.data["user_id"]) if req_resp.data.get("user_id") else None

    # ---- Stage 1: CLASSIFICATION ----
    _set_status(request_id, RequestStatus.classifying)
    try:
        classification = await classify.classify_request(request_text=request_text, user_id=user_id)
    except classify.NeedsClarification as exc:
        _set_status(request_id, RequestStatus.awaiting_user, final_response=exc.question)
        return
    except classify.ClassificationError as exc:
        _fail_request(request_id, f"Classification failed: {exc}")
        return

    profession_ids = _persist_classifications(request_id, classification.selections, classification.professions_by_name)
    professions = list(classification.professions_by_name.values())
    professions_by_name = classification.professions_by_name

    # ---- Stage 2: PLAN ----
    _set_status(request_id, RequestStatus.planning)
    try:
        steps = await plan.create_plan(
            request_id=request_id,
            request_text=request_text,
            professions=professions,
            professions_by_name=professions_by_name,
        )
    except plan.NeedsClarification as exc:
        _set_status(request_id, RequestStatus.awaiting_user, final_response=exc.question)
        return
    except plan.PlanningError as exc:
        _fail_request(request_id, str(exc))
        return

    # ---- Stage 3: ACTION (with bounded replan escalation) ----
    _set_status(request_id, RequestStatus.acting)
    all_outcomes: list[StepOutcome] = []
    remaining_steps = steps
    replan_generation = 0
    max_replans = cfg("action.max_replans_per_request", 3)
    incomplete = False

    while True:
        try:
            run_result = await run_action_stage(request_id=request_id, steps=remaining_steps)
            all_outcomes.extend(run_result.outcomes)
            if run_result.any_non_blocking_failed:
                incomplete = True
            break  # completed this generation's steps without a blocking failure
        except BlockingStepFailed as exc:
            # exc.partial_outcomes includes every step in this run, including
            # the failed blocking step itself (marked passed=False) — record
            # all of it so the final trace/synthesis sees the failure too.
            all_outcomes.extend(exc.partial_outcomes)

            replan_generation += 1
            if replan_generation > max_replans:
                incomplete = True
                logger.error(
                    "Request %s exhausted %d replans; falling through to synthesis with failure flag.",
                    request_id,
                    max_replans,
                )
                break

            _set_status(request_id, RequestStatus.replanning)
            client.table("requests").update({"replan_count": replan_generation}).eq("id", str(request_id)).execute()

            try:
                remaining_steps = await plan.create_replan(
                    request_id=request_id,
                    replan_generation=replan_generation,
                    request_text=request_text,
                    professions=professions,
                    professions_by_name=professions_by_name,
                    prior_successful_steps=_summarize_prior_steps_for_replan(all_outcomes),
                    failed_step_intent=exc.step.intent,
                    tool_error=exc.tool_error or "unknown",
                    evaluation_note=exc.evaluation_note,
                )
            except plan.PlanningError as replan_exc:
                _fail_request(request_id, f"Replan failed: {replan_exc}")
                return
            except plan.NeedsClarification as clarify_exc:
                _set_status(request_id, RequestStatus.awaiting_user, final_response=clarify_exc.question)
                return

            _set_status(request_id, RequestStatus.acting)
            continue

    # ---- Stage 4: SYNTHESIZATION ----
    _set_status(request_id, RequestStatus.synthesizing)
    final_response = await synthesize.synthesize_response(
        request_text=request_text, all_outcomes=all_outcomes, incomplete=incomplete
    )
    synthesize.finalize_request(request_id=request_id, final_response=final_response, incomplete=incomplete)

    # ---- Stage 5: EVALUATION AND MEMORIZATION ----
    await evaluate.write_episodic_memory(
        request_id=request_id,
        user_id=user_id,
        request_text=request_text,
        profession_ids=profession_ids,
        outcomes=all_outcomes,
        final_response=final_response,
        incomplete=incomplete,
    )
