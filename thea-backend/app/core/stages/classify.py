"""
Stage 1 — CLASSIFICATION.

Two structurally separate jobs happen here (see handoff "Deltas" #3):

1. Profession selection — routes the incoming request to 1..N professions
   from the fixed, active `professions` list. Always runs.
2. Implicit feedback inference on the user's *previous* turn — runs only if
   a previous episodic_memory row exists for this user. Config-controlled
   whether this is folded into call #1's structured output
   (`combined_call_mode: true`) or issued as an independent second call
   (`combined_call_mode: false`). Either mode produces the same
   `PreviousTurnFeedback | None` shape, so nothing downstream needs to know
   which mode ran.

The model infers; this module writes. `write_implicit_feedback` is the only
place that touches `episodic_memory.feedback_*` columns for the implicit
path, and it explicitly never overwrites an existing explicit signal.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.infra.config import cfg
from app.infra.db import get_service_client
from app.infra.llm import StructuredCallError, call_structured
from app.infra.models import (
    ClassificationOutput,
    FeedbackOnlyOutput,
    FeedbackSignalType,
    PreviousTurnFeedback,
    ProfessionRow,
    ProfessionSelection,
)

logger = logging.getLogger("thea.classify")


class ClassificationError(Exception):
    pass


class NeedsClarification(Exception):
    """Raised when the model asks a clarifying question instead of
    committing to a profession list. Caller (the request-processing
    orchestrator) is expected to surface this to the user and set the
    request status to awaiting_user."""

    def __init__(self, question: str):
        self.question = question
        super().__init__(question)


@dataclass
class ClassificationResult:
    selections: list[ProfessionSelection]
    professions_by_name: dict[str, ProfessionRow]


# ---------------------------------------------------------------------------
# Profession catalog
# ---------------------------------------------------------------------------


def load_active_professions() -> list[ProfessionRow]:
    """The fixed, bounded, numbered profession list. Always loaded fresh
    (not cached across requests) so a new profession row added without a
    deploy is picked up immediately."""
    client = get_service_client()
    resp = client.table("professions").select("*").eq("active", True).execute()
    return [ProfessionRow.model_validate(row) for row in resp.data]


def _catalog_prompt_block(professions: list[ProfessionRow]) -> str:
    lines = []
    for p in professions:
        examples = "; ".join(p.example_queries[:3]) if p.example_queries else "(none provided)"
        lines.append(f"- {p.name}: {p.description}\n  Example queries: {examples}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Previous turn lookup
# ---------------------------------------------------------------------------


def _fetch_previous_episode(user_id: uuid.UUID | None) -> dict | None:
    if user_id is None:
        return None
    client = get_service_client()
    resp = (
        client.table("episodic_memory")
        .select("id, request_text, outcome, compressed_trace, feedback_signal, created_at")
        .eq("user_id", str(user_id))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_COMBINED_SYSTEM_PROMPT = """You are the classification stage of an agent harness.

You have two jobs, kept strictly separate in your output:

1. PROFESSION SELECTION: read the user's request and the list of available
   professions (specialized agents with their own tools). Select the FEWEST
   professions that genuinely cover the request — usually 1, sometimes 2,
   never more than {max_professions}. Do not invent professions not in the
   list. If the request is too ambiguous to route confidently, set
   `clarifying_question` instead and leave `professions` empty.

2. IMPLICIT FEEDBACK (only if previous-turn context is provided below):
   compare the user's NEW message against the PREVIOUS request/outcome.
   Infer whether the user seemed satisfied, and whether the previous turn
   was resolved, is being continued, or was abandoned/pivoted away from.
   Write a free-text `analysis` capturing anything the new message reveals
   about the previous answer — a misunderstanding, an unmet expectation, a
   correction — even subtle. If there is no previous-turn context provided,
   or nothing meaningful to infer, omit `previous_turn_feedback` entirely.

These two jobs are independent. A low-confidence or unclear feedback
inference must never affect profession selection, and vice versa."""

_FEEDBACK_ONLY_SYSTEM_PROMPT = """You compare a user's new message against their previous request and its
outcome, to infer implicit feedback. You do NOT select professions or reason
about tools — that is handled elsewhere. Infer sentiment (satisfied /
unsatisfied / unclear), closure (resolved / continuation /
abandoned_or_pivoted), and a free-text analysis of anything the new message
reveals about the previous answer. If there's nothing meaningful to infer,
return null for previous_turn_feedback."""

_PROFESSION_ONLY_SYSTEM_PROMPT = """You are the classification stage of an agent harness. Read the user's
request and the list of available professions (specialized agents with
their own tools). Select the FEWEST professions that genuinely cover the
request — usually 1, sometimes 2, never more than {max_professions}. Do not
invent professions not in the list. If the request is too ambiguous to
route confidently, set `clarifying_question` instead and leave `professions`
empty."""


def _user_prompt(
    *,
    request_text: str,
    professions: list[ProfessionRow],
    previous_episode: dict | None,
    include_feedback_context: bool,
) -> str:
    parts = [
        "Available professions:",
        _catalog_prompt_block(professions),
        "",
        f"User's new request:\n{request_text}",
    ]
    if include_feedback_context and previous_episode is not None:
        parts += [
            "",
            "Previous turn context (for implicit feedback inference only — "
            "do not let this affect profession selection):",
            f"- previous request: {previous_episode['request_text']}",
            f"- previous outcome: {previous_episode['outcome']}",
            f"- episodic_memory_id: {previous_episode['id']}",
        ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def classify_request(*, request_text: str, user_id: uuid.UUID | None) -> ClassificationResult:
    professions = load_active_professions()
    if not professions:
        raise ClassificationError("No active professions configured — cannot classify any request.")

    max_professions = cfg("classification.max_professions_per_request", 3)
    model = cfg("classification.model")
    combined = cfg("classification.combined_call_mode", True)

    previous_episode = _fetch_previous_episode(user_id)
    has_previous = previous_episode is not None

    if combined:
        system = _COMBINED_SYSTEM_PROMPT.format(max_professions=max_professions)
        user = _user_prompt(
            request_text=request_text,
            professions=professions,
            previous_episode=previous_episode,
            include_feedback_context=has_previous,
        )
        try:
            output = await call_structured(
                model=model,
                schema=ClassificationOutput,
                system_prompt=system,
                user_prompt=user,
            )
        except StructuredCallError as exc:
            raise ClassificationError(str(exc)) from exc

        if output.clarifying_question and not output.professions:
            raise NeedsClarification(output.clarifying_question)
        if not output.professions:
            raise ClassificationError("Model returned no professions and no clarifying question.")

        _apply_previous_turn_feedback(output.previous_turn_feedback)
        selections = output.professions[:max_professions]

    else:
        # Two independent calls. Profession-selection prompt is untouched by
        # any feedback-analysis framing.
        system = _PROFESSION_ONLY_SYSTEM_PROMPT.format(max_professions=max_professions)
        user = _user_prompt(
            request_text=request_text,
            professions=professions,
            previous_episode=None,
            include_feedback_context=False,
        )
        try:
            prof_output = await call_structured(
                model=model,
                schema=ClassificationOutput,
                system_prompt=system,
                user_prompt=user,
            )
        except StructuredCallError as exc:
            raise ClassificationError(str(exc)) from exc

        if prof_output.clarifying_question and not prof_output.professions:
            raise NeedsClarification(prof_output.clarifying_question)
        if not prof_output.professions:
            raise ClassificationError("Model returned no professions and no clarifying question.")

        selections = prof_output.professions[:max_professions]

        if has_previous:
            feedback_model = cfg("classification.feedback_model", model)
            fb_user = _user_prompt(
                request_text=request_text,
                professions=[],
                previous_episode=previous_episode,
                include_feedback_context=True,
            )
            try:
                fb_output = await call_structured(
                    model=feedback_model,
                    schema=FeedbackOnlyOutput,
                    system_prompt=_FEEDBACK_ONLY_SYSTEM_PROMPT,
                    user_prompt=fb_user,
                )
                _apply_previous_turn_feedback(fb_output.previous_turn_feedback)
            except StructuredCallError as exc:
                # Feedback inference is a bonus signal, not load-bearing for
                # this request's own routing — log and continue rather than
                # failing the user's actual request over it.
                logger.warning("Implicit feedback inference call failed (non-fatal): %s", exc)

    professions_by_name = {p.name: p for p in professions}
    unknown = [s.profession_name for s in selections if s.profession_name not in professions_by_name]
    if unknown:
        raise ClassificationError(f"Model selected unknown profession(s): {unknown}")

    for sel in selections:
        logger.info(
            "classification profession=%s confidence=%.2f rationale=%s",
            sel.profession_name,
            sel.confidence,
            sel.rationale,
        )

    return ClassificationResult(selections=selections, professions_by_name=professions_by_name)


def _apply_previous_turn_feedback(feedback: PreviousTurnFeedback | None) -> None:
    """Code writes, model infers. Never overwrites an explicit signal."""
    if feedback is None:
        return

    client = get_service_client()
    existing = (
        client.table("episodic_memory")
        .select("id, feedback_signal")
        .eq("id", str(feedback.episodic_memory_id))
        .limit(1)
        .execute()
    )
    if not existing.data:
        logger.warning(
            "Implicit feedback referenced unknown episodic_memory_id=%s; skipping write.",
            feedback.episodic_memory_id,
        )
        return

    if existing.data[0]["feedback_signal"] == FeedbackSignalType.explicit.value:
        logger.info(
            "Skipping implicit feedback write for episodic_memory_id=%s: explicit signal already present.",
            feedback.episodic_memory_id,
        )
        return

    feedback_value = feedback.sentiment.value == "satisfied" if feedback.sentiment.value != "unclear" else None

    client.table("episodic_memory").update(
        {
            "feedback_signal": FeedbackSignalType.implicit.value,
            "feedback_value": feedback_value,
            "feedback_analysis": feedback.analysis,
        }
    ).eq("id", str(feedback.episodic_memory_id)).execute()

    logger.info(
        "Wrote implicit feedback for episodic_memory_id=%s sentiment=%s closure=%s",
        feedback.episodic_memory_id,
        feedback.sentiment.value,
        feedback.closure.value,
    )
