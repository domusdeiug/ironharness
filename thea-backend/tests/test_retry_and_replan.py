"""
Tests the retry ladder (attempt 1 logic-retry -> attempts 2-3 model-assisted)
and blocking-step replan escalation, using the deliberately-always-failing
echo.always_fail tool. Mirrors test_pipeline_mocked.py's mocking approach.

Run with: python -m tests.test_retry_and_replan
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock, patch

from app.infra.models import (
    ClassificationOutput,
    PlanOutput,
    PlannedStep,
    ProfessionRow,
    ProfessionSelection,
    RetryProposal,
)

ECHO_PROFESSION_ID = uuid.uuid4()
REQUEST_ID = uuid.uuid4()


def make_fake_profession() -> ProfessionRow:
    return ProfessionRow(
        id=ECHO_PROFESSION_ID,
        name="echo",
        description="Echoes messages back, or always fails for testing.",
        example_queries=["fail on purpose"],
        skill_prompt="Call echo.always_fail to test retry handling.",
        tools=["echo.say", "echo.always_fail"],
        active=True,
    )


class FakeTable:
    def __init__(self, name: str, store: dict):
        self.name = name
        self.store = store
        self._pending_insert = None
        self._pending_update = None

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def single(self):
        return self

    def not_(self):
        return self

    def is_(self, *_a, **_k):
        return self

    def insert(self, row):
        self._pending_insert = row if isinstance(row, list) else [row]
        return self

    def update(self, fields):
        self._pending_update = fields
        return self

    def execute(self):
        result = MagicMock()
        if self._pending_insert is not None:
            self.store.setdefault(self.name, []).extend(self._pending_insert)
            out = []
            for row in self._pending_insert:
                r = dict(row)
                r.setdefault("id", str(uuid.uuid4()))
                out.append(r)
            result.data = out
            self._pending_insert = None
            return result
        if self._pending_update is not None:
            self.store.setdefault(f"{self.name}__updates", []).append(self._pending_update)
            result.data = [self._pending_update]
            self._pending_update = None
            return result

        if self.name == "requests":
            result.data = {"id": str(REQUEST_ID), "user_id": None, "raw_text": "test the failure ladder", "status": "classifying"}
        elif self.name == "professions":
            result.data = [make_fake_profession().model_dump(mode="json")]
        else:
            result.data = []
        return result


class FakeSupabaseClient:
    def __init__(self):
        self.store: dict = {}

    def table(self, name: str):
        return FakeTable(name, self.store)


async def run_test() -> None:
    from app.tools import load_all_tools
    from tests.fixtures.echo import tools as _echo_tools  # noqa: F401 -- registers echo.say/echo.always_fail for this test

    load_all_tools()

    fake_client = FakeSupabaseClient()

    classification_output = ClassificationOutput(
        professions=[ProfessionSelection(profession_name="echo", confidence=0.9, rationale="testing failure")],
        previous_turn_feedback=None,
    )

    # First plan: one blocking step that always fails.
    first_plan = PlanOutput(
        steps=[
            PlannedStep(
                step_number=1,
                profession_name="echo",
                tool_name="echo.always_fail",
                tool_args={"confirm_failure": True},
                intent="Deliberately fail to exercise the retry ladder",
                blocking=True,
            )
        ]
    )

    # Replan (generation 1): after the blocking failure, PLAN is called
    # again and this time produces a working step instead.
    replan_output = PlanOutput(
        steps=[
            PlannedStep(
                step_number=1,  # PLAN always numbers from 1; orchestrator.create_replan renumbers with an offset afterward
                profession_name="echo",
                tool_name="echo.say",
                tool_args={"message": "recovered after replan"},
                intent="Recover by echoing instead",
                blocking=True,
            )
        ]
    )

    plan_call_count = {"n": 0}

    async def fake_call_structured(*, model, schema, system_prompt, user_prompt, **kwargs):
        if schema is ClassificationOutput:
            return classification_output
        if schema is PlanOutput:
            plan_call_count["n"] += 1
            if plan_call_count["n"] == 1:
                return first_plan
            return replan_output
        if schema is RetryProposal:
            # Model-assisted retry attempts (2-3): propose identical args
            # since this tool always fails regardless of args, just to
            # prove the retry path executes and consumes a call.
            return RetryProposal(adjusted_args={"confirm_failure": True}, reasoning="retry with same args")
        raise AssertionError(f"Unexpected schema: {schema}")

    async def fake_call_text(*, model, system_prompt, user_prompt, **kwargs):
        return "I hit a snag with the first approach, but recovered: recovered after replan"

    async def fake_embed_text(text: str):
        return [0.0] * 1024

    with patch("app.core.stages.classify.get_service_client", return_value=fake_client), \
         patch("app.core.stages.plan.get_service_client", return_value=fake_client), \
         patch("app.core.stages.action.get_service_client", return_value=fake_client), \
         patch("app.core.stages.synthesize.get_service_client", return_value=fake_client), \
         patch("app.core.stages.evaluate.get_service_client", return_value=fake_client), \
         patch("app.core.orchestrator.get_service_client", return_value=fake_client), \
         patch("app.core.stages.classify.call_structured", side_effect=fake_call_structured), \
         patch("app.core.stages.plan.call_structured", side_effect=fake_call_structured), \
         patch("app.core.stages.action.call_structured", side_effect=fake_call_structured), \
         patch("app.core.stages.synthesize.call_text", side_effect=fake_call_text), \
         patch("app.core.stages.evaluate.embed_text", side_effect=fake_embed_text):

        from app.core.orchestrator import process_request

        await process_request(request_id=REQUEST_ID)

    # ---- Assertions ----
    updates = fake_client.store.get("requests__updates", [])
    statuses_seen = [u["status"] for u in updates if "status" in u]
    print("Status transitions:", statuses_seen)
    assert statuses_seen == [
        "classifying", "planning", "acting", "replanning", "acting", "synthesizing", "done"
    ], statuses_seen

    replan_count_updates = [u["replan_count"] for u in updates if "replan_count" in u]
    assert replan_count_updates == [1], replan_count_updates

    action_results = fake_client.store.get("action_results", [])
    print(f"action_results rows: {len(action_results)}")
    # First step (always_fail) should have been attempted max_attempts_per_step times (3)
    failed_step_attempts = [r for r in action_results if r["tool_error"] is not None]
    assert len(failed_step_attempts) == 3, f"expected 3 attempts on the failing step, got {len(failed_step_attempts)}"
    attempt_types = [r["attempt_type"] for r in failed_step_attempts]
    print("Attempt types on failing step:", attempt_types)
    assert attempt_types == ["initial", "logic_retry", "model_retry"], attempt_types

    # Replan's step (echo.say) should have succeeded on first try.
    succeeded = [r for r in action_results if r["evaluation"] == "pass"]
    assert len(succeeded) == 1, succeeded

    plan_steps = fake_client.store.get("plan_steps", [])
    generations = {s["replan_generation"] for s in plan_steps}
    assert generations == {0, 1}, generations

    final_update = updates[-1]
    assert final_update["incomplete"] is False, final_update
    assert "recovered" in final_update["final_response"], final_update

    print("ALL ASSERTIONS PASSED — retry ladder + replan scoping both correct")


if __name__ == "__main__":
    asyncio.run(run_test())
