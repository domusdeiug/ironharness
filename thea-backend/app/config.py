"""
Central configuration for the harness.

Two layers, deliberately kept separate:

1. `Settings` (env vars, via pydantic-settings) — secrets and per-deployment
   values: Supabase URL/keys, OpenRouter API key, Redis URL, etc. These never
   belong in version control.

2. `config.yaml` (checked into the repo) — tunable pipeline behavior that a
   human should be able to change without touching code: retry caps, model
   IDs, the combined_call_mode flag, retrieval K, etc. Loaded once at import
   time into `PIPELINE_CONFIG` and re-readable via `reload_pipeline_config()`
   for tests.

Domain data (professions, tool schemas) is NOT here — that lives in the
`professions` table (service-role managed) and each profession's own module.
This file is the config/domain-data split called out in the handoff.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_YAML_PATH = REPO_ROOT / "config.yaml"


class Settings(BaseSettings):
    """Secrets and per-deployment values, sourced from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase
    supabase_url: str = Field(..., alias="SUPABASE_URL")
    supabase_service_role_key: str = Field(..., alias="SUPABASE_SERVICE_ROLE_KEY")
    # anon key isn't used server-side for privileged writes, but is kept
    # available for any endpoint that wants to mint a user-scoped client
    # (e.g. to double check RLS behavior in tests) rather than always
    # trusting service_role.
    supabase_anon_key: str | None = Field(default=None, alias="SUPABASE_ANON_KEY")

    # OpenRouter
    openrouter_api_key: str = Field(..., alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL")
    openrouter_site_url: str | None = Field(default=None, alias="OPENROUTER_SITE_URL")
    openrouter_app_name: str = Field(default="thea-harness", alias="OPENROUTER_APP_NAME")

    # Web search (used by the model-retry diagnostic step, and by any
    # profession that lists web_search among its tools). Provider: Brave
    # Search API — see app/tools/shared/web_search.py.
    web_search_api_key: str | None = Field(default=None, alias="WEB_SEARCH_API_KEY")

    # web_extract (shared pool) — no key required for the fetch itself
    # (it's a plain HTTP GET), but a per-deployment timeout is exposed
    # here so it can be tuned without a code change.
    web_extract_timeout_seconds: float = Field(default=15.0, alias="WEB_EXTRACT_TIMEOUT_SECONDS")

    # Email profession (SMTP-only send today; search/delete need an
    # IMAP or provider-API client, not yet implemented)
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str | None = Field(default=None, alias="SMTP_USERNAME")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
    smtp_from_address: str | None = Field(default=None, alias="SMTP_FROM_ADDRESS")

    # `files` profession — sandbox root every files.* tool resolves paths
    # against. Optional at the Settings level (unlike supabase_url/
    # openrouter_api_key) so a deployment that never enables `files` isn't
    # blocked from booting by it — same pattern as smtp_host being
    # optional for a deployment that doesn't use `email`. Each files.*
    # handler checks for this being unset itself and fails with a clean
    # error_type=not_configured rather than the whole app refusing to
    # start. See app/professions/files/tools.py for the resolution/escape
    # check and the not-configured guard.
    files_sandbox_root: str | None = Field(default=None, alias="FILES_SANDBOX_ROOT")

    # Calendar profession — no provider implemented yet; this just names
    # which one *should* be wired up, so the "not configured" error is
    # informative rather than generic.
    calendar_provider: str | None = Field(default=None, alias="CALENDAR_PROVIDER")

    # Worker / queue
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # App
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {path}. Copy config.example.yaml to config.yaml "
            "and adjust as needed."
        )
    with path.open("r") as f:
        return yaml.safe_load(f) or {}


_PIPELINE_CONFIG: dict[str, Any] | None = None


def get_pipeline_config() -> dict[str, Any]:
    """Lazily loaded, cached config.yaml contents. Call reload_pipeline_config()
    to force a re-read (e.g. between tests)."""
    global _PIPELINE_CONFIG
    if _PIPELINE_CONFIG is None:
        _PIPELINE_CONFIG = _load_yaml(CONFIG_YAML_PATH)
    return _PIPELINE_CONFIG


def reload_pipeline_config() -> dict[str, Any]:
    global _PIPELINE_CONFIG
    _PIPELINE_CONFIG = _load_yaml(CONFIG_YAML_PATH)
    return _PIPELINE_CONFIG


def cfg(dotted_path: str, default: Any = ...) -> Any:
    """Convenience accessor: cfg('classification.combined_call_mode')."""
    node: Any = get_pipeline_config()
    for part in dotted_path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            if default is not ...:
                return default
            raise KeyError(f"Missing required config key: {dotted_path}")
    return node
