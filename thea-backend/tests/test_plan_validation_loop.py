"""
Tests Stage 2's schema-validation-and-reprompt loop directly (not through
the full orchestrator): PLAN returns invalid tool_args on the first
attempt, valid args on the second, and the plan is persisted successfully
within the configured attempt cap. Also verifies plan_validation_attempts
is written for both the failed and the resolved attempt.

Run with: python -m tests.test_plan_validation_loop
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock, patch

from app.infra.models import PlanOutput, PlannedStep, ProfessionRow

ECHO_PROFESSION_ID = uuid.uuid4()
REQUEST_ID = uuid.uuid4()


class FakeTable:
    def __init__(self, name, store):
        self.name = name
        self.store = store
        self._pending_insert = None

    def insert(self, row):
        self._pending_insert = row if isinstance(row, list) else [row]
        return self

    def execute(self):
        result = MagicMock()
        rows = self._pending_insert or []
        self.store.setdefault(self.name, []).extend(rows)
        out = [dict(r, id=r.get("id", str(uuid.uuid4()))) for r in rows]
        result.data = out
        self._pending_insert = None
        return result


class FakeSupabaseClient:
    def __init__(self):
        self.store: dict = {}

    def table(self, name):
        return FakeTable(name, self.store)


async def run_test() -> None:
    from app.tools import load_all_tools
    from tests.fixtures.echo import tools as _echo_tools  # noqa: F401 -- registers echo.say/echo.always_fail for this test

    load_all_tools()

    fake_client = FakeSupabaseClient()
    profession = ProfessionRow(
        id=ECHO_PROFESSION_ID,
        name="echo",
        description="Echo profession",
        example_queries=[],
        skill_prompt="Call echo.say.",
        tools=["echo.say"],
        active=True,
    )

    # Attempt 1: model omits the required 'message' field -> schema validation fails.
    bad_plan = PlanOutput(
        steps=[
            PlannedStep(
                step_number=1,
                profession_name="echo",
                tool_name="echo.say",
                tool_args={},  # missing required 'message'
                intent="Echo something",
                blocking=True,
            )
        ]
    )
    # Attempt 2: corrected.
    good_plan = PlanOutput(
        steps=[
            PlannedStep(
                step_number=1,
                profession_name="echo",
                tool_name="echo.say",
                tool_args={"message": "fixed on retry"},
                intent="Echo something",
                blocking=True,
            )
        ]
    )

    calls = {"n": 0}

    async def fake_call_structured(*, model, schema, system_prompt, user_prompt, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            assert "invalid tool_args" not in user_prompt  # first attempt has no validation_error yet
            return bad_plan
        else:
            assert "invalid tool_args" in user_prompt  # second attempt must include the re-prompt
            return good_plan

    with patch("app.core.stages.plan.get_service_client", return_value=fake_client), \
         patch("app.core.stages.plan.call_structured", side_effect=fake_call_structured):

        from app.core.stages.plan import create_plan

        steps = await create_plan(
            request_id=REQUEST_ID,
            request_text="echo something",
            professions=[profession],
            professions_by_name={"echo": profession},
        )

    assert calls["n"] == 2, f"expected exactly 2 model calls (1 fail + 1 success), got {calls['n']}"
    assert len(steps) == 1
    assert steps[0].tool_args == {"message": "fixed on retry"}

    validation_attempts = fake_client.store.get("plan_validation_attempts", [])
    assert len(validation_attempts) == 2, validation_attempts
    assert validation_attempts[0]["resolved"] is False
    assert validation_attempts[1]["resolved"] is True

    plan_steps = fake_client.store.get("plan_steps", [])
    assert len(plan_steps) == 1
    assert plan_steps[0]["tool_args"] == {"message": "fixed on retry"}

    print("ALL ASSERTIONS PASSED — plan validation reprompt loop correct")
    print(f"Model calls: {calls['n']}, validation_attempts logged: {len(validation_attempts)}")


if __name__ == "__main__":
    asyncio.run(run_test())
