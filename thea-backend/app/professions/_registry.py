"""
Profession metadata registry.

Mirrors app/tools/registry.py's pattern, one level up: instead of a tool
registering itself in code (a schema + handler), a *profession* registers
itself as a `ProfessionMeta` -- the data row that used to live hand-typed
in scripts/seed_professions.py. Adding a profession is meant to be
entirely local to its own folder: write `tools.py` (registers tools) and
declare a `PROFESSION_META` in `__init__.py` (registers the profession
row). Nothing outside `app/professions/<name>/` needs to change.

Registration happens on import, same as tools -- `app.tools.load_all_tools()`
imports every profession package under app/professions/, which runs each
one's `__init__.py` and registers its `PROFESSION_META` here as a side
effect. `scripts/seed_professions.py` then reads `all_professions()` and
upserts each into Supabase, instead of hand-maintaining a list.

A profession with no tools of its own (e.g. one built purely from shared
tools like `web_search`/`web_extract`/`vision_analyze`) still gets a
folder with just an `__init__.py` declaring its `PROFESSION_META` --
there is no requirement that a profession folder contain a `tools.py`.

Leading-underscore packages under app/professions/ (e.g. `_shared_tools`)
are infrastructure, not professions, and are never scanned for either
tools or metadata by the profession-discovery loop -- see
app/tools/__init__.py's `load_all_tools()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProfessionMeta:
    name: str
    description: str
    skill_prompt: str
    tools: list[str]
    example_queries: list[str] = field(default_factory=list)
    active: bool = True


class ProfessionCollisionError(Exception):
    pass


_REGISTRY: dict[str, ProfessionMeta] = {}


def register_profession(meta: ProfessionMeta) -> None:
    existing = _REGISTRY.get(meta.name)
    if existing is not None:
        if existing == meta:
            # Re-registering the identical metadata (e.g. module imported
            # twice) is a no-op, not a collision.
            return
        raise ProfessionCollisionError(
            f"Profession '{meta.name}' is already registered with different "
            "metadata. Profession names are enforced-unique across "
            "app/professions/ -- rename one of them."
        )
    _REGISTRY[meta.name] = meta


def all_professions() -> dict[str, ProfessionMeta]:
    return dict(_REGISTRY)
