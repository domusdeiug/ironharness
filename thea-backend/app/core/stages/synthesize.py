"""
Stage 4 — SYNTHESIZATION.

Reads back everything ACTION produced (intent, result, pass/fail for every
step across all replan generations) and answers the user, or confirms the
task is done. If any steps failed (partial outcome), this stage is
responsible for honestly conveying what was and wasn't accomplished —
never silently presenting a partial result as a full success.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.infra.config import cfg
from app.infra.db import get_service_client
from app.infra.llm import call_text
from app.core.stages.action import StepOutcome

logger = logging.getLogger("thea.synthesize")

_SYSTEM_PROMPT = """You are the synthesis stage of an agent harness. You are given the original
user request and a full trace of what the pipeline did: each step's intent,
whether it passed or failed, and its result. Write a direct, honest answer
to the user.

Rules:
- If everything succeeded, just answer normally using the results.
- If anything failed (even non-blocking steps), say plainly what wasn't
  accomplished. Do not imply full success when the outcome was partial.
- Don't narrate internal pipeline mechanics (retries, replans, tool names)
  unless the user would find that genuinely useful context for what to do
  next.
- Be concise. Answer the request; don't restate it."""


def _trace_block(outcomes: list[StepOutcome]) -> str:
    lines = []
    for o in outcomes:
        status = "PASS" if o.passed else "FAIL"
        lines.append(
            f"- step {o.step.step_number} [{status}] tool={o.step.tool_name} "
            f"intent='{o.step.intent}' result={o.final_result} error={o.final_error}"
        )
    return "\n".join(lines)


async def synthesize_response(
    *,
    request_text: str,
    all_outcomes: list[StepOutcome],
    incomplete: bool,
) -> str:
    model = cfg("synthesis.model")
    trace = _trace_block(all_outcomes)
    user_prompt = (
        f"Original request: {request_text}\n\n"
        f"Execution trace:\n{trace}\n\n"
        f"Overall incomplete flag: {incomplete}"
    )
    return await call_text(
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.4,
    )


def finalize_request(
    *, request_id: uuid.UUID, final_response: str, incomplete: bool, status: str = "done"
) -> None:
    client = get_service_client()
    client.table("requests").update(
        {"final_response": final_response, "incomplete": incomplete, "status": status}
    ).eq("id", str(request_id)).execute()
