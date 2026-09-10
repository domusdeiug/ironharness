-- Agent Harness — Supabase Schema (v1, with RLS from creation)
-- Requires: pgvector extension

create extension if not exists vector;

-- ============================================================
-- PROFESSIONS
-- System config, not user data. RLS enabled, NO policies for
-- anon/authenticated -> frontend gets zero access by default.
-- Only service_role (used by the backend/worker, bypasses RLS)
-- can read/write this table. Do NOT "fix" the lack of a policy
-- here by adding an open one — that's the point.
-- ============================================================
create table professions (
    id              uuid primary key default gen_random_uuid(),
    name            text not null unique,
    description     text not null,
    example_queries text[] not null default '{}',
    skill_prompt    text not null,
    tools           text[] not null default '{}',
    active          boolean not null default true,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

alter table professions enable row level security;
comment on table professions is 'Service-role only. Intentionally no anon/authenticated policies — system config, not user data.';

-- ============================================================
-- REQUESTS — top-level state machine, owner column is user_id
-- ============================================================
create type request_status as enum (
    'classifying',
    'planning',
    'acting',
    'replanning',
    'awaiting_user',
    'synthesizing',
    'done',
    'failed'
);

create table requests (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid references auth.users(id),  -- nullable until required; FK to auth.users since Supabase Auth is in use
    raw_text        text not null,
    status          request_status not null default 'classifying',
    final_response  text,
    incomplete      boolean not null default false,
    replan_count    integer not null default 0,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index requests_status_idx on requests (status);
create index requests_user_id_idx on requests (user_id);

alter table requests enable row level security;

create policy "requests_select_own"
    on requests for select
    to authenticated
    using ( (select auth.uid()) = user_id );

create policy "requests_insert_own"
    on requests for insert
    to authenticated
    with check ( (select auth.uid()) = user_id );

create policy "requests_delete_own"
    on requests for delete
    to authenticated
    using ( (select auth.uid()) = user_id );

-- No update policy for end users: requests are mutated only by the
-- backend (service_role), which bypasses RLS — pipeline state
-- transitions must stay backend-owned to preserve ACTION's
-- idempotency/crash-recovery guarantees. Users can read, create,
-- and delete their own requests, not edit pipeline state directly.

-- ============================================================
-- CLASSIFICATIONS — child of requests, no own user_id column,
-- so policy joins back to requests.user_id
-- ============================================================
create table classifications (
    id              uuid primary key default gen_random_uuid(),
    request_id      uuid not null references requests(id) on delete cascade,
    profession_id   uuid not null references professions(id),
    confidence      real,
    rationale       text not null,
    created_at      timestamptz not null default now()
);

create index classifications_request_id_idx on classifications (request_id);

alter table classifications enable row level security;

create policy "classifications_select_via_request_owner"
    on classifications for select
    to authenticated
    using (
        exists (
            select 1 from requests
            where requests.id = classifications.request_id
              and requests.user_id = (select auth.uid())
        )
    );

-- No insert/update/delete for end users — written only by the
-- backend (service_role) during Stage 1.

-- ============================================================
-- PLAN_STEPS — child of requests, same join pattern
-- ============================================================
create table plan_steps (
    id                 uuid primary key default gen_random_uuid(),
    request_id         uuid not null references requests(id) on delete cascade,
    replan_generation  integer not null default 0,
    step_number        integer not null,
    profession_id      uuid references professions(id),
    tool_name          text not null,
    tool_args          jsonb not null default '{}',
    intent             text not null,
    blocking           boolean not null default true,
    created_at         timestamptz not null default now(),

    unique (request_id, replan_generation, step_number)
);

create index plan_steps_request_id_idx on plan_steps (request_id, replan_generation, step_number);

alter table plan_steps enable row level security;

create policy "plan_steps_select_via_request_owner"
    on plan_steps for select
    to authenticated
    using (
        exists (
            select 1 from requests
            where requests.id = plan_steps.request_id
              and requests.user_id = (select auth.uid())
        )
    );

-- ============================================================
-- PLAN_VALIDATION_ATTEMPTS — internal diagnostics, child of requests
-- ============================================================
create table plan_validation_attempts (
    id                 uuid primary key default gen_random_uuid(),
    request_id         uuid not null references requests(id) on delete cascade,
    replan_generation  integer not null default 0,
    attempt_number     integer not null default 1,
    validation_errors  jsonb not null default '[]',
    resolved           boolean not null default false,
    created_at         timestamptz not null default now()
);

create index plan_validation_attempts_request_id_idx on plan_validation_attempts (request_id, replan_generation);

alter table plan_validation_attempts enable row level security;
comment on table plan_validation_attempts is 'Internal diagnostics, service-role only. No end-user policies — not useful/appropriate to expose to the frontend.';

-- ============================================================
-- ACTION_RESULTS — child of plan_steps -> requests
-- ============================================================
create type action_outcome as enum ('pass', 'fail', 'pending');
create type retry_attempt_type as enum ('initial', 'logic_retry', 'model_retry');

create table action_results (
    id                     uuid primary key default gen_random_uuid(),
    request_id             uuid not null references requests(id) on delete cascade,
    plan_step_id           uuid not null references plan_steps(id) on delete cascade,
    attempt_number         integer not null default 1,
    attempt_type           retry_attempt_type not null default 'initial',
    tool_result            jsonb,
    tool_error             text,
    diagnostic_calls       jsonb not null default '[]',
    diagnostic_call_count  integer not null default 0,
    evaluation             action_outcome not null default 'pending',
    evaluation_note        text,
    created_at             timestamptz not null default now(),

    unique (plan_step_id, attempt_number)
);

create index action_results_request_id_idx on action_results (request_id);
create index action_results_plan_step_id_idx on action_results (plan_step_id);

alter table action_results enable row level security;

create policy "action_results_select_via_request_owner"
    on action_results for select
    to authenticated
    using (
        exists (
            select 1 from requests
            where requests.id = action_results.request_id
              and requests.user_id = (select auth.uid())
        )
    );

-- ============================================================
-- EPISODIC MEMORY — per-user. user_id is denormalized here
-- (not just derivable via request_id join) because Stage 5's
-- retrieval queries filter/search memory directly and constantly;
-- forcing a join to requests on every retrieval call is both a
-- performance cost and an easy place to forget the scope check.
-- request_id is kept for traceability back to the originating run.
-- ============================================================
create type feedback_signal_type as enum ('explicit', 'implicit', 'none');

create table episodic_memory (
    id                  uuid primary key default gen_random_uuid(),
    request_id          uuid not null references requests(id) on delete cascade,
    user_id             uuid references auth.users(id),
    request_text        text not null,
    request_embedding   vector(1024) not null,
    profession_ids      uuid[] not null default '{}',
    compressed_trace     jsonb not null default '[]',
    outcome              text not null,
    feedback_signal      feedback_signal_type not null default 'none',
    feedback_value       boolean,
    created_at           timestamptz not null default now()
);

create index episodic_memory_embedding_idx on episodic_memory
    using hnsw (request_embedding vector_cosine_ops);
create index episodic_memory_request_id_idx on episodic_memory (request_id);
create index episodic_memory_user_id_idx on episodic_memory (user_id);

alter table episodic_memory enable row level security;

create policy "episodic_memory_select_own"
    on episodic_memory for select
    to authenticated
    using ( (select auth.uid()) = user_id );

-- No insert/update/delete policy for end users. Episodic rows are
-- written by the backend (service_role) at Stage 5. Feedback
-- (thumbs up/down) goes through a backend endpoint that writes via
-- service_role rather than a direct frontend table update — RLS
-- can't restrict which *columns* an update touches, only which
-- rows, so a user-facing UPDATE policy on this table would let a
-- client overwrite compressed_trace/outcome too, not just feedback.
-- Routing through the backend keeps that column boundary real.

-- ============================================================
-- SEMANTIC MEMORY — per-user distilled patterns. Distillation
-- (Stage 5's nightly batch job) clusters each user's own episodic
-- memories only — never across users — and retrieval (top-K) is
-- scoped to the requesting user's own semantic_memory rows.
-- ============================================================
create table semantic_memory (
    id                  uuid primary key default gen_random_uuid(),
    user_id              uuid references auth.users(id),
    pattern_text         text not null,
    pattern_embedding    vector(1024) not null,
    source_episode_ids   uuid[] not null default '{}',
    profession_ids       uuid[] not null default '{}',
    confidence           real not null default 0.5,
    created_at            timestamptz not null default now(),
    superseded_by         uuid references semantic_memory(id)
);

create index semantic_memory_embedding_idx on semantic_memory
    using hnsw (pattern_embedding vector_cosine_ops);
create index semantic_memory_user_id_idx on semantic_memory (user_id);

alter table semantic_memory enable row level security;

create policy "semantic_memory_select_own"
    on semantic_memory for select
    to authenticated
    using ( (select auth.uid()) = user_id );

-- No insert/update/delete policy for end users — written only by
-- the backend's nightly distillation job (service_role).

-- ============================================================
-- updated_at triggers
-- ============================================================
create or replace function set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql
set search_path = '';

create trigger requests_set_updated_at
    before update on requests
    for each row execute function set_updated_at();

create trigger professions_set_updated_at
    before update on professions
    for each row execute function set_updated_at();