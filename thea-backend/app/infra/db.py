"""
Supabase client for the backend/worker.

Per the handoff's access model: the backend uses the service_role key and
bypasses RLS entirely. It is the only writer for all pipeline-state tables
and the only reader/writer of `professions`, `plan_validation_attempts`,
and `semantic_memory`. The frontend talks to Supabase directly with the
anon/authenticated key for its own reads (subject to RLS) and calls a
backend endpoint for anything privileged (submitting a request, writing
feedback) — never a raw table write from the client for those.

This module intentionally exposes a single shared client. There is no
per-request user-scoped client here: user scoping for backend-authored rows
is done by setting `user_id` explicitly in the write, not by relying on
`auth.uid()` (which is only populated in a PostgREST request carrying the
user's own JWT, not in service_role calls).
"""

from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from app.infra.config import get_settings


@lru_cache
def get_service_client() -> Client:
    """The privileged client used by every pipeline stage. Bypasses RLS."""
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
