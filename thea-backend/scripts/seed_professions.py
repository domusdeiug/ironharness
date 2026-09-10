"""
CLI entry point for syncing `professions` rows from code into Supabase.

The actual logic lives in app.professions.sync so it can also run
automatically at worker/API process startup (see app/worker.py) -- this
script exists for manual/CI runs where you want to sync without starting
the worker or API (e.g. right after adding a new profession, before
deploying).

Usage:
    python -m scripts.seed_professions
"""

from __future__ import annotations

from app.professions.sync import ProfessionSyncError, sync_professions_to_db


def main() -> None:
    try:
        synced = sync_professions_to_db()
    except ProfessionSyncError as exc:
        raise SystemExit(str(exc)) from exc

    for name in synced:
        print(f"Upserted profession: {name}")


if __name__ == "__main__":
    main()
