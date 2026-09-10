"""
Pydantic models used across the pipeline.

Two categories, kept visually distinct:

- `*Row` models mirror actual DB rows (read/write shape for a table).
- Everything else is a structured-output schema for a specific model call
  (Stage 1's classification+feedback output, Stage 2's plan, etc.) or an
  in-memory pipeline object that never round-trips to a table directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums mirroring Postgres enum types
# ---------------------------------------------------------------------------


class RequestStatus(str, Enum):
    classifying = "classifying"
    planning = "planning"
    acting = "acting"
    replanning = "replanning"
    awaiting_user = "awaiting_user"
    synthesizing = "synthesizing"
    done = "done"
    failed = "failed"


class RetryAttemptType(str, Enum):
    initial = "initial"
    logic_retry = "logic_retry"
    model_retry = "model_retry"


class ActionOutcome(str, Enum):
    pass_ = "pass"
    fail = "fail"
    pending = "pending"


class FeedbackSignalType(str, Enum):
    explicit = "explicit"
    implicit = "implicit"
    none = "none"


class FeedbackSentiment(str, Enum):
    satisfied = "satisfied"
    unsatisfied = "unsatisfied"
    unclear = "unclear"


class FeedbackClosure(str, Enum):
    resolved = "resolved"
    continuation = "continuation"
    abandoned_or_pivoted = "abandoned_or_pivoted"


# ---------------------------------------------------------------------------
# DB row models
# ---------------------------------------------------------------------------


class ProfessionRow(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    example_queries: list[str] = Field(default_factory=list)
    skill_prompt: str
    tools: list[str] = Field(default_factory=list)
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RequestRow(BaseModel):
    id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    raw_text: str
    status: RequestStatus = RequestStatus.classifying
    final_response: str | None = None
    incomplete: bool = False
    replan_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ClassificationRow(BaseModel):
    id: uuid.UUID | None = None
    request_id: uuid.UUID
    profession_id: uuid.UUID
    confidence: float | None = None
    rationale: str
    created_at: datetime | None = None


class PlanStepRow(BaseModel):
    id: uuid.UUID | None = None
    request_id: uuid.UUID
    replan_generation: int = 0
    step_number: int
    profession_id: uuid.UUID | None = None
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    intent: str
    blocking: bool = True
    created_at: datetime | None = None


class PlanValidationAttemptRow(BaseModel):
    id: uuid.UUID | None = None
    request_id: uuid.UUID
    replan_generation: int = 0
    attempt_number: int = 1
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    resolved: bool = False
    created_at: datetime | None = None


class ActionResultRow(BaseModel):
    id: uuid.UUID | None = None
    request_id: uuid.UUID
    plan_step_id: uuid.UUID
    attempt_number: int = 1
    attempt_type: RetryAttemptType = RetryAttemptType.initial
    tool_result: dict[str, Any] | None = None
    tool_error: str | None = None
    diagnostic_calls: list[dict[str, Any]] = Field(default_factory=list)
    diagnostic_call_count: int = 0
    evaluation: ActionOutcome = ActionOutcome.pending
    evaluation_note: str | None = None
    created_at: datetime | None = None


class EpisodicMemoryRow(BaseModel):
    id: uuid.UUID | None = None
    request_id: uuid.UUID
    user_id: uuid.UUID | None = None
    request_text: str
    request_embedding: list[float] | None = None
    profession_ids: list[uuid.UUID] = Field(default_factory=list)
    compressed_trace: list[dict[str, Any]] = Field(default_factory=list)
    outcome: str
    feedback_signal: FeedbackSignalType = FeedbackSignalType.none
    feedback_value: bool | None = None
    feedback_analysis: str | None = None
    created_at: datetime | None = None


class SemanticMemoryRow(BaseModel):
    id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    pattern_text: str
    pattern_embedding: list[float] | None = None
    source_episode_ids: list[uuid.UUID] = Field(default_factory=list)
    profession_ids: list[uuid.UUID] = Field(default_factory=list)
    confidence: float = 0.5
    created_at: datetime | None = None
    superseded_by: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# Stage 1 — CLASSIFICATION structured output
# ---------------------------------------------------------------------------


class ProfessionSelection(BaseModel):
    profession_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class PreviousTurnFeedback(BaseModel):
    """Optional second field in Stage 1's structured output. Present only if
    a previous episodic_memory row exists for this user/session. The model
    infers this; code performs the actual write (see stages/classify.py)."""

    episodic_memory_id: uuid.UUID
    sentiment: FeedbackSentiment
    closure: FeedbackClosure
    analysis: str
    confidence: float = Field(ge=0.0, le=1.0)


class ClassificationOutput(BaseModel):
    """Top-level structured output of Stage 1. `previous_turn_feedback` is a
    schema-separate field, never blended into profession selection or its
    rationale, per the handoff."""

    professions: list[ProfessionSelection]
    previous_turn_feedback: PreviousTurnFeedback | None = None
    clarifying_question: str | None = None

    @field_validator("professions")
    @classmethod
    def _at_least_one_unless_clarifying(cls, v: list[ProfessionSelection]) -> list[ProfessionSelection]:
        # Structural validation only (non-empty is enforced by the caller
        # when there's no clarifying_question — see classify.py, since a
        # clarifying question legitimately has zero professions selected).
        return v


class FeedbackOnlyOutput(BaseModel):
    """Structured output for the two-call fallback's second call
    (combined_call_mode = false). Same semantic content as
    PreviousTurnFeedback but issued as its own top-level schema so the
    prompt for this call doesn't need to know about professions at all."""

    previous_turn_feedback: PreviousTurnFeedback | None = None


# ---------------------------------------------------------------------------
# Stage 2 — PLAN structured output
# ---------------------------------------------------------------------------


class PlannedStep(BaseModel):
    step_number: int
    profession_name: str
    tool_name: str
    tool_args: dict[str, Any]
    intent: str
    blocking: bool = True


class PlanOutput(BaseModel):
    steps: list[PlannedStep]
    clarifying_question: str | None = None

    @field_validator("steps")
    @classmethod
    def _steps_sequential(cls, v: list[PlannedStep]) -> list[PlannedStep]:
        for i, step in enumerate(v, start=1):
            if step.step_number != i:
                raise ValueError(
                    f"Plan steps must be sequential starting at 1; got step_number="
                    f"{step.step_number} at position {i}"
                )
        return v


# ---------------------------------------------------------------------------
# Stage 3 — ACTION in-memory objects
# ---------------------------------------------------------------------------


class ToolExecutionResult(BaseModel):
    """What a tool implementation returns to ACTION, before evaluation."""

    success: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    error_type: str | None = None  # used by the attempt-1 retryability check


class StepEvaluationResult(BaseModel):
    passed: bool
    note: str


class RetryProposal(BaseModel):
    """What the cheap arg-adjustment model returns during attempts 2-3."""

    adjusted_args: dict[str, Any]
    reasoning: str


# ---------------------------------------------------------------------------
# Stage 5 — evaluation/memorization structured outputs
# ---------------------------------------------------------------------------


class DistilledPattern(BaseModel):
    pattern_text: str
    source_episode_ids: list[uuid.UUID]
    profession_ids: list[uuid.UUID]
    confidence: float = Field(ge=0.0, le=1.0)
