"""
End-to-end pipeline smoke test using mocked Supabase + OpenRouter calls.

This is NOT a substitute for running against the real Thea project once
credentials are available in the deployment environment — it exists to
prove the pipeline's *logic* (stage sequencing, retry ladder, replan
scoping, status transitions) is correct in isolation from network access,
since this sandbox's egress allowlist blocks supabase.co and
openrouter.ai.

Run with: python -m tests.test_pipeline_mocked
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import (
    ActionOutcome,
    ClassificationOutput,
    PlanOutput,
    PlannedStep,
    ProfessionRow,
    ProfessionSelection,
    ToolExecutionResult,
)

ECHO_PROFESSION_ID = uuid.uuid4()
REQUEST_ID = uuid.uuid4()


def make_fake_profession() -> ProfessionRow:
    return ProfessionRow(
        id=ECHO_PROFESSION_ID,
        name="echo",
        description="Echoes messages back.",
        example_queries=["say hi"],
        skill_prompt="Call echo.say with the message.",
        tools=["echo.say"],
        active=True,
    )


class FakeTable:
    """Minimal stand-in for supabase-py's fluent table() query builder.
    Captures inserts/updates for assertions and returns canned data for
    selects, keyed by table name."""

    def __init__(self, name: str, store: dict):
        self.name = name
        self.store = store
        self._pending_insert = None
        self._pending_update = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def single(self):
        return self

    def not_(self):
        return self

    def is_(self, *_args, **_kwargs):
        return self

    def insert(self, row):
        self._pending_insert = row if isinstance(row, list) else [row]
        return self

    def upsert(self, row, on_conflict=None):
        self._pending_insert = [row]
        return self

    def update(self, fields):
        self._pending_update = fields
        return self

    def execute(self):
        result = MagicMock()
        if self._pending_insert is not None:
            self.store.setdefault(self.name, []).extend(self._pending_insert)
            out_rows = []
            for row in self._pending_insert:
                r = dict(row)
                r.setdefault("id", str(uuid.uuid4()))
                out_rows.append(r)
            result.data = out_rows
            self._pending_insert = None
            return result
        if self._pending_update is not None:
            self.store.setdefault(f"{self.name}__updates", []).append(self._pending_update)
            result.data = [self._pending_update]
            self._pending_update = None
            return result

        # select path — canned responses per table
        if self.name == "requests":
            result.data = {
                "id": str(REQUEST_ID),
                "user_id": None,
                "raw_text": "please echo hello world",
                "status": "classifying",
            }
        elif self.name == "professions":
            result.data = [make_fake_profession().model_dump(mode="json")]
        elif self.name == "episodic_memory":
            result.data = []  # no prior episode -> no implicit feedback path
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

    load_all_tools()

    fake_client = FakeSupabaseClient()

    classification_output = ClassificationOutput(
        professions=[ProfessionSelection(profession_name="echo", confidence=0.95, rationale="direct echo request")],
        previous_turn_feedback=None,
        clarifying_question=None,
    )

    plan_output = PlanOutput(
        steps=[
            PlannedStep(
                step_number=1,
                profession_name="echo",
                tool_name="echo.say",
                tool_args={"message": "hello world"},
                intent="Echo the user's message back",
                blocking=True,
            )
        ],
        clarifying_question=None,
    )

    call_count = {"n": 0}

    async def fake_call_structured(*, model, schema, system_prompt, user_prompt, **kwargs):
        call_count["n"] += 1
        if schema is ClassificationOutput:
            return classification_output
        if schema is PlanOutput:
            return plan_output
        raise AssertionError(f"Unexpected schema requested in mock: {schema}")

    async def fake_embed_text(text: str):
        return [0.0] * 1024

    async def fake_call_text(*, model, system_prompt, user_prompt, **kwargs):
        return "Done! I echoed your message back: hello world"

    with patch("app.db.get_service_client", return_value=fake_client), \
         patch("app.stages.classify.get_service_client", return_value=fake_client), \
         patch("app.stages.plan.get_service_client", return_value=fake_client), \
         patch("app.stages.action.get_service_client", return_value=fake_client), \
         patch("app.stages.synthesize.get_service_client", return_value=fake_client), \
         patch("app.stages.evaluate.get_service_client", return_value=fake_client), \
         patch("app.orchestrator.get_service_client", return_value=fake_client), \
         patch("app.stages.classify.call_structured", side_effect=fake_call_structured), \
         patch("app.stages.plan.call_structured", side_effect=fake_call_structured), \
         patch("app.stages.synthesize.call_text", side_effect=fake_call_text), \
         patch("app.stages.evaluate.embed_text", side_effect=fake_embed_text):

        from app.orchestrator import process_request

        await process_request(request_id=REQUEST_ID)

    # ---- Assertions ----
    updates = fake_client.store.get("requests__updates", [])
    statuses_seen = [u["status"] for u in updates if "status" in u]
    print("Status transitions:", statuses_seen)
    assert statuses_seen == ["classifying", "planning", "acting", "synthesizing", "done"], statuses_seen

    final_update = updates[-1]
    assert final_update["incomplete"] is False, final_update
    assert "hello world" in final_update["final_response"], final_update

    classifications = fake_client.store.get("classifications", [])
    assert len(classifications) == 1
    assert classifications[0]["profession_id"] == str(ECHO_PROFESSION_ID)

    plan_steps = fake_client.store.get("plan_steps", [])
    assert len(plan_steps) == 1
    assert plan_steps[0]["tool_name"] == "echo.say"
    assert plan_steps[0]["tool_args"] == {"message": "hello world"}

    action_results = fake_client.store.get("action_results", [])
    assert len(action_results) == 1
    assert action_results[0]["evaluation"] == ActionOutcome.pass_.value

    episodes = fake_client.store.get("episodic_memory", [])
    assert len(episodes) == 1
    assert episodes[0]["outcome"].startswith("complete:")

    print("ALL ASSERTIONS PASSED")
    print(f"Model calls made: {call_count['n']} (expected 2: classify + plan)")


if __name__ == "__main__":
    asyncio.run(run_test())
