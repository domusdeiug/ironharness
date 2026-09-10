"""
Stage 5 — EVALUATION AND MEMORIZATION.

Three distinct memory types, kept schema-separate (see handoff):

1. Episodic memory — one row per completed request, written here when the
   request finishes. `feedback_signal` starts as 'none' unless the frontend
   already posted explicit feedback by the time this runs (unlikely in
   practice since Stage 5 runs immediately after Synthesization, before the
   user has seen the answer — included for completeness). The row's
   feedback state is *not* considered final at write time: Stage 1's
   implicit-feedback inference on the *next* request may come back later
   and fill in feedback_signal='implicit' (see stages/classify.py). This
   function never blocks waiting for that.

2. Semantic memory — distilled per-user patterns, built by a periodic batch
   job (run_distillation_for_user / run_distillation_all_users), never
   per-request. Clusters only within one user's own episodes.

3. Retrieval policy — get_retrieval_context() bundles top-K semantic +
   optionally the latest raw episode, scoped to the requesting user, for
   injection into Stage 1/2's context. Implementation lives here since it
   reads the same tables Stage 5 writes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.infra.config import cfg
from app.infra.db import get_service_client
from app.infra.embeddings import embed_text
from app.infra.llm import StructuredCallError, call_structured
from app.infra.models import DistilledPattern
from app.core.stages.action import StepOutcome
from pydantic import BaseModel


class _DistillationOutput(BaseModel):
    patterns: list[DistilledPattern]

logger = logging.getLogger("thea.evaluate")


def _compress_trace(outcomes: list[StepOutcome]) -> list[dict[str, Any]]:
    """One line per step: intent + pass/fail, not verbatim tool payloads —
    per the handoff, compressed_trace is structured metadata, not a place
    to dump full tool results."""
    return [
        {
            "step_number": o.step.step_number,
            "tool_name": o.step.tool_name,
            "intent": o.step.intent,
            "passed": o.passed,
            "attempts_used": o.attempts_used,
        }
        for o in outcomes
    ]


async def write_episodic_memory(
    *,
    request_id: uuid.UUID,
    user_id: uuid.UUID | None,
    request_text: str,
    profession_ids: list[uuid.UUID],
    outcomes: list[StepOutcome],
    final_response: str,
    incomplete: bool,
    explicit_feedback_value: bool | None = None,
) -> uuid.UUID:
    client = get_service_client()
    embedding = await embed_text(request_text)

    outcome_summary = "incomplete" if incomplete else "complete"
    outcome_text = f"{outcome_summary}: {final_response[:500]}"

    row = {
        "request_id": str(request_id),
        "user_id": str(user_id) if user_id else None,
        "request_text": request_text,
        "request_embedding": embedding,
        "profession_ids": [str(p) for p in profession_ids],
        "compressed_trace": _compress_trace(outcomes),
        "outcome": outcome_text,
        "feedback_signal": "explicit" if explicit_feedback_value is not None else "none",
        "feedback_value": explicit_feedback_value,
    }
    resp = client.table("episodic_memory").insert(row).execute()
    episode_id = uuid.UUID(resp.data[0]["id"])
    logger.info("Wrote episodic_memory id=%s for request_id=%s", episode_id, request_id)
    return episode_id


def record_explicit_feedback(*, episodic_memory_id: uuid.UUID, feedback_value: bool) -> None:
    """The one legitimate write path for user-submitted thumbs up/down.
    Called from a backend endpoint (service_role), never a direct frontend
    table update — see handoff: RLS can't restrict which columns an UPDATE
    touches, only which rows, so a user-facing UPDATE policy on
    episodic_memory would let a client overwrite compressed_trace/outcome
    too. Explicit always overwrites/wins over any prior implicit value."""
    client = get_service_client()
    client.table("episodic_memory").update(
        {"feedback_signal": "explicit", "feedback_value": feedback_value}
    ).eq("id", str(episodic_memory_id)).execute()


# ---------------------------------------------------------------------------
# Retrieval policy
# ---------------------------------------------------------------------------


@dataclass
class RetrievalContext:
    semantic_patterns: list[dict[str, Any]]
    latest_episode: dict[str, Any] | None


async def get_retrieval_context(*, user_id: uuid.UUID | None, request_text: str) -> RetrievalContext:
    """Top-K semantic memories (default K=3) scoped to the requesting user,
    plus optionally that user's single most recent raw episode. Bounded by
    construction — this never returns more than K+1 items."""
    if user_id is None:
        return RetrievalContext(semantic_patterns=[], latest_episode=None)

    client = get_service_client()
    top_k = cfg("retrieval.semantic_top_k", 3)
    include_latest = cfg("retrieval.include_latest_episode", True)

    embedding = await embed_text(request_text)

    # pgvector cosine-distance search via RPC. Requires a matching RPC
    # function in the DB (see migrations/xxxx_retrieval_rpc.sql). Supabase
    # Python client doesn't do raw vector ORDER BY through PostgREST
    # filters, so an RPC wrapper is the standard pattern here.
    semantic_resp = client.rpc(
        "match_semantic_memory",
        {
            "query_embedding": embedding,
            "match_user_id": str(user_id),
            "match_count": top_k,
        },
    ).execute()
    semantic_patterns = semantic_resp.data or []

    latest_episode = None
    if include_latest:
        ep_resp = (
            client.table("episodic_memory")
            .select("id, request_text, outcome, compressed_trace, created_at")
            .eq("user_id", str(user_id))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        latest_episode = ep_resp.data[0] if ep_resp.data else None

    return RetrievalContext(semantic_patterns=semantic_patterns, latest_episode=latest_episode)


# ---------------------------------------------------------------------------
# Semantic memory distillation (periodic batch job — cadence OPEN per handoff)
# ---------------------------------------------------------------------------

_DISTILLATION_SYSTEM_PROMPT = """You review a single user's own past episodic memories (requests + outcomes)
and identify recurring patterns worth remembering as a durable preference,
e.g. "for requests like X, approach Y using [tools] worked well; Z didn't."
Only propose a pattern when you see genuine repetition or a clear signal
(explicit negative feedback especially) — do not force a pattern out of a
single, unrepresentative episode. It's fine to return zero patterns."""


async def run_distillation_for_user(*, user_id: uuid.UUID, lookback_limit: int = 200) -> list[uuid.UUID]:
    """Clusters ONLY this user's own episodic memories — never across users,
    per the handoff. Returns the ids of newly created semantic_memory rows."""
    client = get_service_client()
    model = cfg("evaluation.distillation_model")

    episodes_resp = (
        client.table("episodic_memory")
        .select("id, request_text, outcome, compressed_trace, feedback_signal, feedback_value, profession_ids")
        .eq("user_id", str(user_id))
        .order("created_at", desc=True)
        .limit(lookback_limit)
        .execute()
    )
    episodes = episodes_resp.data or []
    if len(episodes) < 3:
        # Not enough signal yet to distill anything meaningful.
        return []

    episodes_block = "\n".join(
        f"- id={e['id']} feedback={e['feedback_signal']}/{e['feedback_value']}: "
        f"{e['request_text'][:200]} -> {e['outcome'][:200]}"
        for e in episodes
    )

    try:
        output = await call_structured(
            model=model,
            schema=_DistillationOutput,
            system_prompt=_DISTILLATION_SYSTEM_PROMPT,
            user_prompt=f"This user's episodes:\n{episodes_block}",
            max_tokens=3000,
        )
    except StructuredCallError as exc:
        logger.warning("Distillation call failed for user_id=%s: %s", user_id, exc)
        return []

    created_ids: list[uuid.UUID] = []
    for pattern in output.patterns:
        embedding = await embed_text(pattern.pattern_text)
        resp = (
            client.table("semantic_memory")
            .insert(
                {
                    "user_id": str(user_id),
                    "pattern_text": pattern.pattern_text,
                    "pattern_embedding": embedding,
                    "source_episode_ids": [str(i) for i in pattern.source_episode_ids],
                    "profession_ids": [str(i) for i in pattern.profession_ids],
                    "confidence": pattern.confidence,
                }
            )
            .execute()
        )
        created_ids.append(uuid.UUID(resp.data[0]["id"]))

    logger.info("Distilled %d semantic_memory rows for user_id=%s", len(created_ids), user_id)
    return created_ids


async def run_distillation_all_users() -> dict[str, list[uuid.UUID]]:
    """Nightly-batch entry point (cadence is OPEN per handoff — this just
    does one pass over all users with episodes; scheduling is external,
    e.g. a Railway cron or APScheduler trigger calling this)."""
    client = get_service_client()
    # Distinct user_ids with at least one episodic_memory row.
    resp = client.table("episodic_memory").select("user_id").not_.is_("user_id", "null").execute()
    user_ids = {row["user_id"] for row in resp.data or [] if row["user_id"]}

    results: dict[str, list[uuid.UUID]] = {}
    for uid in user_ids:
        results[uid] = await run_distillation_for_user(user_id=uuid.UUID(uid))
    return results
