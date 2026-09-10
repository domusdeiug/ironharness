"""
Importing this package registers every known tool (shared pool + each
profession's own tools) into the global registry in app.tools.registry --
and, as a side effect of importing each profession's package, every
profession's PROFESSION_META into app.professions._registry.

Call `app.tools.load_all_tools()` once at process startup (API server and
worker both need this, since both may need to validate/execute tools).
`scripts/seed_professions.py` also calls it before reading
`app.professions._registry.all_professions()` to sync the DB.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

logger = logging.getLogger("thea.tools")

_LOADED = False


def load_all_tools() -> None:
    global _LOADED
    if _LOADED:
        return

    # Shared tools pool -- lives under app/professions/_shared_tools since
    # these are tools, not a profession, but are owned by the professions
    # namespace rather than the (mechanism-only) app.tools package.
    from app.professions import _shared_tools

    for _, module_name, _ in pkgutil.iter_modules(_shared_tools.__path__):
        importlib.import_module(f"app.professions._shared_tools.{module_name}")

    # Profession-owned tools (and metadata -- see below)
    from app import professions

    for _, module_name, is_pkg in pkgutil.iter_modules(professions.__path__):
        # Leading-underscore packages (e.g. _shared_tools) are infrastructure
        # living under professions/, not a profession folder -- never scan
        # them here. Test-only fixtures (tests/fixtures/echo/) are outside
        # app/professions/ entirely and are registered explicitly by the
        # tests that need them, not by this scan.
        if not is_pkg or module_name.startswith("_"):
            continue

        package_name = f"app.professions.{module_name}"
        # Import the package itself first. This runs the profession's
        # __init__.py, which is where PROFESSION_META registers itself
        # (see app.professions._registry) -- covers professions built
        # purely from shared tools that have no tools.py of their own.
        importlib.import_module(package_name)

        try:
            tools_module = importlib.import_module(f"{package_name}.tools")
        except ModuleNotFoundError:
            continue
        else:
            logger.info("Loaded tools from profession module: %s", tools_module.__name__)

    _LOADED = True
