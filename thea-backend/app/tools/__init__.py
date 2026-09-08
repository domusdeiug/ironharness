"""
Importing this package registers every known tool (shared pool + each
profession's own tools) into the global registry in app.tools.registry.

Call `app.tools.load_all_tools()` once at process startup (API server and
worker both need this, since both may need to validate/execute tools).
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

    # Shared tools pool
    from app.tools import shared

    for _, module_name, _ in pkgutil.iter_modules(shared.__path__):
        importlib.import_module(f"app.tools.shared.{module_name}")

    # Profession-owned tools
    from app import professions

    for _, module_name, is_pkg in pkgutil.iter_modules(professions.__path__):
        if not is_pkg:
            continue
        try:
            tools_module = importlib.import_module(f"app.professions.{module_name}.tools")
        except ModuleNotFoundError:
            continue
        else:
            logger.info("Loaded tools from profession module: %s", tools_module.__name__)

    _LOADED = True
