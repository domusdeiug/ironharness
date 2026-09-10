"""
Minimal FastAPI app. Per the handoff: "perfect the backend first... the
frontend should be the simplest possible thing that lets a human trigger
the pipeline and see output." This API is sized to match — three
endpoints, no auth middleware beyond passing through a Supabase user JWT
for identifying the caller (real auth verification is Supabase Auth's job
on the frontend side; the backend trusts a verified user_id passed in by
a frontend that has already authenticated the user via Supabase Auth).

Feedback writes go through POST /requests/{id}/feedback (service_role),
never a direct frontend table update — see stages/evaluate.py's
record_explicit_feedback docstring for why.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.infra.db import get_service_client
from app.core.stages.evaluate import record_explicit_feedback
from app.worker import enqueue_pipeline_job

# Importing app.worker above already triggers load_all_tools() and
# sync_professions_to_db() as import-time side effects (see app/worker.py)
# -- no need to call them again here. Both processes end up in sync
# regardless of which one starts first, since upserts are idempotent.

app = FastAPI(title="Thea Harness API", version="0.1.0")


class SubmitRequestBody(BaseModel):
    raw_text: str
    user_id: uuid.UUID | None = None


class SubmitRequestResponse(BaseModel):
    request_id: uuid.UUID
    status: str


@app.post("/requests", response_model=SubmitRequestResponse)
def submit_request(body: SubmitRequestBody) -> SubmitRequestResponse:
    if not body.raw_text.strip():
        raise HTTPException(status_code=422, detail="raw_text must not be empty")

    client = get_service_client()
    resp = (
        client.table("requests")
        .insert({"raw_text": body.raw_text, "user_id": str(body.user_id) if body.user_id else None})
        .execute()
    )
    request_row = resp.data[0]
    request_id = uuid.UUID(request_row["id"])

    enqueue_pipeline_job(request_id)

    return SubmitRequestResponse(request_id=request_id, status=request_row["status"])


class RequestStatusResponse(BaseModel):
    request_id: uuid.UUID
    status: str
    final_response: str | None
    incomplete: bool
    replan_count: int


@app.get("/requests/{request_id}", response_model=RequestStatusResponse)
def get_request(request_id: uuid.UUID) -> RequestStatusResponse:
    client = get_service_client()
    resp = client.table("requests").select("*").eq("id", str(request_id)).single().execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Request not found")
    row = resp.data
    return RequestStatusResponse(
        request_id=request_id,
        status=row["status"],
        final_response=row.get("final_response"),
        incomplete=row.get("incomplete", False),
        replan_count=row.get("replan_count", 0),
    )


class FeedbackBody(BaseModel):
    episodic_memory_id: uuid.UUID
    feedback_value: bool


@app.post("/requests/{request_id}/feedback")
def submit_feedback(request_id: uuid.UUID, body: FeedbackBody) -> dict:
    # request_id in the path is accepted for a RESTful shape / future
    # cross-checking, but the actual write targets episodic_memory_id
    # directly since that's the row feedback attaches to (one request can,
    # in principle, map to more than one episode only in edge cases like a
    # retried job — episodic_memory_id is the unambiguous key).
    record_explicit_feedback(episodic_memory_id=body.episodic_memory_id, feedback_value=body.feedback_value)
    return {"ok": True}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
