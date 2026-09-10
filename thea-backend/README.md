# Thea Harness — Backend

A 5-stage agent harness: **Classify → Plan → Act → Synthesize → Evaluate/Memorize**.
Backend for the Thea Supabase project (`cyudkegnsibqlgjkvjxs`). See
`docs/handoff.md`-equivalent context in prior planning — this README covers
what's actually built and verified.

## Status: backend scaffolding complete and tested (mocked); not yet run against live services

Everything below has been verified to *work correctly* via mocked
integration tests (`tests/`). It has **not** been run against the real
Supabase project or OpenRouter yet — this sandbox's network egress only
allows package registries, not `supabase.co` or `openrouter.ai`. Schema
changes (the one new RPC function) *were* applied live via the Supabase
MCP tools and confirmed against the security advisor. See "Next steps"
below for what running this for real requires.

## What's here

```
app/
  config.py          # Settings (env) + config.yaml (tunables) loader
  db.py              # Supabase client (service_role, bypasses RLS)
  models.py          # Every DB row shape + every stage's structured-output schema
  llm.py             # OpenRouter client (structured output + plain text calls)
  embeddings.py      # 1024-dim embeddings (matches schema's vector(1024))
  orchestrator.py    # Wires all 5 stages together; owns status transitions
  worker.py          # RQ job entry point; whole pipeline runs as a background job
  main.py            # Minimal FastAPI: POST /requests, GET /requests/{id}, POST .../feedback
  tools/
    registry.py      # Tool registration with enforced-unique names (no collisions)
    shared/          # Tools available to any profession (web_search, ...)
  professions/
    echo/            # Trivial smoke-test profession (echo.say, echo.always_fail)
  stages/
    classify.py      # Stage 1: profession routing + implicit feedback inference
    plan.py           # Stage 2: locked plan + schema-validation reprompt loop
    action.py         # Stage 3: per-step retry ladder + replan escalation
    synthesize.py      # Stage 4: honest final answer from the full trace
    evaluate.py        # Stage 5: episodic/semantic memory + retrieval policy
scripts/
  seed_professions.py # Add/update a profession row (no deploy needed)
tests/
  test_pipeline_mocked.py      # Full happy-path pipeline, mocked DB+LLM
  test_retry_and_replan.py     # Retry ladder + blocking-step replan escalation
  test_plan_validation_loop.py # Schema-validation reprompt loop
config.example.yaml  # Copy to config.yaml
.env.example          # Copy to .env
Dockerfile
docker-compose.yml    # api + worker + redis for local dev
```

## What's verified (via `tests/`, all passing)

- **Tool registry**: registration, collision detection, strict schema
  validation (Pydantic v2 default mode — no silent int→str coercion).
- **Full happy-path pipeline**: classify → plan → act → synthesize →
  evaluate, correct status transitions on the `requests` row at every
  stage, correct rows written to `classifications`, `plan_steps`,
  `action_results`, `episodic_memory`.
- **Retry ladder**: attempt 1 (initial) → attempt 2 (logic-only retry for
  transient errors, no model call) → attempt 3 (model-assisted arg
  adjustment) → exhausted → **blocking step correctly escalates to a
  scoped replan** → replan succeeds → final response honestly reflects the
  recovery. `plan_steps` correctly spans both `replan_generation` 0 and 1.
- **PLAN's schema-validation reprompt loop**: invalid `tool_args` on
  attempt 1 (missing required field) → reprompted with the validation
  error → valid on attempt 2 → both attempts logged to
  `plan_validation_attempts` with correct `resolved` flags.
- **Error classification**: transient/timeout → retryable; auth_failure →
  terminal (no wasted retries); unknown error_type → defaults retryable
  (safer default).
- **FastAPI app**: actually served over HTTP in this sandbox; `/health`
  returns 200, empty `raw_text` correctly rejected with 422, and
  `GET /requests/{id}` correctly attempts (and fails, since this sandbox
  has no route to Supabase) a real service_role query rather than any
  code-path bug.
- **Live Supabase migration**: added `match_semantic_memory` RPC for
  pgvector search; a real security-advisor finding (mutable `search_path`)
  was caught and fixed before moving on.

## Key design decisions made while building (not just following the handoff verbatim)

- **Tool name collisions are enforced-unique at registration time**,
  raising `ToolCollisionError` on any second registration under the same
  name — resolves the handoff's open question about profession-folder vs.
  shared-pool naming conflicts.
- **OpenRouter fallback-model behavior is deliberately narrow**: a
  configured `fallback_model` only retries transport-level failures
  (network/non-2xx/timeout), never schema-validation failures. A model
  responding with malformed structured output is usually a prompting
  problem, and blindly retrying it against a second model risks masking a
  real bug as "flaky provider." See `app/infra/llm.py`'s module docstring.
- **`BlockingStepFailed` carries `partial_outcomes`**: I initially wrote
  the orchestrator with a bug where successful-steps-before-a-blocking-
  failure would be silently dropped from `all_outcomes` (and therefore
  from the replan's prior-context and from the final synthesis trace).
  Caught it, fixed it by having the exception carry the run's outcomes so
  far, and this is now covered by `test_retry_and_replan.py`'s assertion
  on `plan_steps` spanning both replan generations.
- **Embeddings pinned to a real 1024-dim model** (`mxbai-embed-large-v1`)
  to match the schema's `vector(1024)` columns exactly, with an explicit
  runtime check that raises if a swapped-in provider returns the wrong
  dimension.
- **`match_semantic_memory` RPC uses `set search_path = public`**
  explicitly, matching the same defense-in-depth the handoff described for
  `rls_auto_enable()`, and was verified clean against the live security
  advisor after the fix.

## OPEN items carried over from the handoff (still genuinely unresolved)

These are flagged in `config.example.yaml` comments too:

1. **Which OpenRouter model IDs to actually pin** for classify/plan/retry/
   evaluation/distillation. Current `config.yaml` has placeholder models
   (`gpt-4.1-mini`, `claude-sonnet-4.5`, etc.) — needs the structured-output
   verification pass the handoff called for before trusting these in
   production.
2. **`combined_call_mode` comparison test** — defaults to `true` per the
   handoff, but the actual quality comparison between one combined call vs.
   two separate calls hasn't been run.
3. **Diagnostic tools for the retry step** — `action.py`'s
   `_run_diagnostics_and_propose_args` currently surfaces the failing
   tool's `skill_doc` for free and *mentions* a web_search budget in the
   prompt, but doesn't yet give the retry model a real tool-calling loop to
   actually invoke `web_search` mid-diagnosis. The budget accounting
   (`max_diagnostic_calls_per_step`) is enforced in code and ready for this;
   wiring the actual tool-use turn is the remaining piece.
4. **`web_search` has no real provider wired up** — `app/professions/_shared_tools/web_search.py`
   is a structured stub that returns `not_implemented` until a provider
   (Bing/Brave/Serper/etc.) is chosen and `WEB_SEARCH_API_KEY` means
   something concrete.
5. **`max_plan_steps` cap** — left as `null` (no cap) pending a first real
   (non-`echo`) profession to get a realistic sense of typical step counts.
6. **Per-tool retry classification** — the global `_RETRYABLE_ERROR_TYPES` /
   `_TERMINAL_ERROR_TYPES` sets in `action.py` are a reasonable default but
   the handoff called for building this out per-tool as real professions
   are added.
7. **Distillation cadence** — `config.yaml` says `nightly` but nothing
   schedules `run_distillation_all_users()` yet; needs a Railway cron (or
   APScheduler) wired up.

## Next steps to actually run this

1. `cp .env.example .env` and fill in the real `SUPABASE_SERVICE_ROLE_KEY`
   and `OPENROUTER_API_KEY` (the Supabase project ref/URL are already
   correct in `.env.example`).
2. `docker compose up` — starts Redis, the API, and the worker together.
3. `python -m scripts.seed_professions` (from inside the `api` container,
   or locally with the same `.env`) to seed the `echo` profession so
   there's something to route to.
4. `curl -X POST localhost:8000/requests -d '{"raw_text": "say hello back to me"}' -H 'Content-Type: application/json'`
   and poll `GET /requests/{id}` — should move through
   classifying → planning → acting → synthesizing → done.
5. Resolve OPEN items 1–2 above (model IDs, combined-call test) before
   trusting this beyond the `echo` smoke test.
6. Build the first real (non-`echo`) profession — this will surface the
   right defaults for `max_plan_steps` and per-tool retry classification
   (OPEN items 5–6).
