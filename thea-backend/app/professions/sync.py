"""
Sync in-code profession metadata into the `professions` table.

This is the single place that turns PROFESSION_META declarations (see
app.professions._registry) into DB rows. Both the CLI
(scripts/seed_professions.py, for manual/CI runs) and the running app
(worker.py and main.py, at process startup) call `sync_professions_to_db()`
so the `professions` table can never drift out of sync with what's
actually registered in code -- there is no separate manual step to
remember.

Upserts are idempotent and cheap, so calling this on every process start
is fine even though most starts will be no-ops against an already-synced
table.
"""

from __future__ import annotations

import logging

from app.infra.db import get_service_client
from app.professions._registry import all_professions
from app.tools import load_all_tools
from app.tools.registry import all_tools

logger = logging.getLogger("thea.professions.sync")


class ProfessionSyncError(Exception):
    pass


def sync_professions_to_db() -> list[str]:
    """Upsert every registered profession's metadata into Supabase.
    Returns the list of profession names synced.

    Raises ProfessionSyncError (not caught here -- this is meant to fail
    loud at startup) if any profession references a tool that isn't
    registered, so a typo in a PROFESSION_META never silently ships a
    broken profession instead of failing the deploy.
    """
    load_all_tools()  # populates both the tool registry and this one, as a side effect

    registered_tools = all_tools()
    professions = all_professions()

    if not professions:
        raise ProfessionSyncError(
            "No professions registered. Every profession must declare a "
            "PROFESSION_META in its app/professions/<name>/__init__.py -- "
            "see app/professions/xlsx/__init__.py for the pattern."
        )

    client = get_service_client()
    synced: list[str] = []

    for name, meta in professions.items():
        missing = [t for t in meta.tools if t not in registered_tools]
        if missing:
            raise ProfessionSyncError(
                f"Profession '{name}' references unregistered tools {missing}. "
                "Register the tool(s) in code first -- only professions built "
                "from *already hardened* tools can be added as a pure data row."
            )

        row = {
            "name": meta.name,
            "description": meta.description,
            "example_queries": meta.example_queries,
            "skill_prompt": meta.skill_prompt,
            "tools": meta.tools,
            "active": meta.active,
        }
        client.table("professions").upsert(row, on_conflict="name").execute()
        synced.append(name)
        logger.info("Synced profession: %s", name)

    return synced
