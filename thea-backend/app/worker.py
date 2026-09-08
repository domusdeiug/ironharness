"""
Background worker entry point (RQ). The full pipeline — not just ACTION —
runs as a queued job: classification and planning are also model calls with
real latency, and the handoff's requirement is that ACTION specifically
"runs as a background worker/job, not inside a single short-lived HTTP
request." Running the whole pipeline as one job keeps the status-transition
logic in one place (orchestrator.py) and means the HTTP layer only ever
does a fast enqueue + row read.

Run with:  rq worker thea-pipeline --url $REDIS_URL
(after `python -m app.worker` has registered tool imports — see below).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.tools import load_all_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("thea.worker")

# Tools must be registered before any job runs (validate_tool_args /
# execute_tool both require the registry to be populated). Doing this at
# module import time means it happens once, whether this module is
# imported by the RQ worker process or directly for local testing.
load_all_tools()


def run_pipeline_job(request_id_str: str) -> None:
    """Synchronous entry point RQ calls. Bridges to the async orchestrator."""
    from app.orchestrator import process_request

    request_id = uuid.UUID(request_id_str)
    logger.info("Starting pipeline for request_id=%s", request_id)
    try:
        asyncio.run(process_request(request_id=request_id))
    except Exception:
        logger.exception("Unhandled error processing request_id=%s", request_id)
        _mark_failed_on_crash(request_id)
        raise


def _mark_failed_on_crash(request_id: uuid.UUID) -> None:
    """Last-resort safety net: if process_request raises something not
    already handled internally (a genuine bug, not an expected
    ClassificationError/PlanningError which are caught in orchestrator.py),
    make sure the request doesn't sit stuck in an in-progress status
    forever with no explanation to the user."""
    try:
        from app.db import get_service_client

        get_service_client().table("requests").update(
            {
                "status": "failed",
                "final_response": "An unexpected internal error occurred. This has been logged.",
                "incomplete": True,
            }
        ).eq("id", str(request_id)).execute()
    except Exception:
        logger.exception("Failed to write crash-recovery status for request_id=%s", request_id)


def enqueue_pipeline_job(request_id: uuid.UUID):
    """Called from the API layer after inserting a `requests` row."""
    from redis import Redis
    from rq import Queue

    from app.config import get_settings

    settings = get_settings()
    redis_conn = Redis.from_url(settings.redis_url)
    queue = Queue("thea-pipeline", connection=redis_conn)
    return queue.enqueue(run_pipeline_job, str(request_id), job_timeout="20m")
