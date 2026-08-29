CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS artefacts (
    artefact_id   text    NOT NULL,
    version       int     NOT NULL,
    content       text,
    content_bytes bytea,
    content_mime  text    NOT NULL DEFAULT 'text/plain',
    repo_pointer  text,
    content_hash  text    NOT NULL,
    source        text    NOT NULL DEFAULT 'seat',
    author        text    NOT NULL DEFAULT 'unknown',
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (artefact_id, version)
);
CREATE INDEX IF NOT EXISTS artefacts_id_idx ON artefacts (artefact_id);
ALTER TABLE artefacts DROP CONSTRAINT IF EXISTS artefacts_artefact_id_content_hash_key;
ALTER TABLE artefacts ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'seat';
ALTER TABLE artefacts ADD COLUMN IF NOT EXISTS author text NOT NULL DEFAULT 'unknown';

CREATE TABLE IF NOT EXISTS hints (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    text           text NOT NULL,
    embedding      vector(1536) NOT NULL,
    metadata       jsonb NOT NULL DEFAULT '{}',
    source         text NOT NULL DEFAULT 'seat',
    author         text NOT NULL DEFAULT 'unknown',
    artefact_id      text,
    artefact_version int,
    repo_pointer     text,
    content_hash   text NOT NULL,
    deleted_at     timestamptz,
    UNIQUE (content_hash),
    CONSTRAINT hints_artefact_pairing CHECK ((artefact_id IS NULL) = (artefact_version IS NULL)),
    FOREIGN KEY (artefact_id, artefact_version) REFERENCES artefacts (artefact_id, version)
);
CREATE INDEX IF NOT EXISTS hints_embedding_idx
    ON hints USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 256);
CREATE INDEX IF NOT EXISTS hints_tags_idx ON hints USING GIN ((metadata->'tags'));
CREATE INDEX IF NOT EXISTS hints_created_idx ON hints (created_at DESC);
ALTER TABLE hints ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple',
        coalesce(text,'') || ' ' || coalesce(metadata->>'tags',''))) STORED;
CREATE INDEX IF NOT EXISTS hints_tsv_idx ON hints USING GIN (search_tsv);

CREATE TABLE IF NOT EXISTS audit_events (
    id              bigserial PRIMARY KEY,
    run_id          text NOT NULL,
    seq             bigint NOT NULL,
    source          text NOT NULL,
    kind            text NOT NULL,
    ts              timestamptz NOT NULL DEFAULT now(),
    payload         jsonb NOT NULL DEFAULT '{}',
    stream_entry_id text,
    content_hash    text,
    raw_entry       jsonb,
    UNIQUE (run_id, seq)
);
-- Migrate DBs whose audit_events/audit_deadletter predate `kind` (CREATE TABLE IF NOT EXISTS won't add
-- it). Nullable on migrated rows; the consumer always supplies kind on insert. Slice 3 makes audit live
-- (first real emitters) → without this, a pre-kind audit_events silently fails every vote INSERT.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS kind text;
-- Structural one-verdict backstop for audit-close: non-verdict events remain unconstrained, but each
-- run_id can have at most one verdict. If an existing database already has duplicate verdicts, index
-- creation fails loudly for manual resolution; verified against the live database, which has zero
-- verdicts, so creation is clean.
CREATE UNIQUE INDEX IF NOT EXISTS audit_events_one_verdict
    ON audit_events (run_id) WHERE kind = 'verdict';

CREATE TABLE IF NOT EXISTS audit_deadletter (
    id               bigserial PRIMARY KEY,
    run_id           text,
    seq              bigint,
    source           text,
    kind             text,
    payload          jsonb,
    content_hash     text,
    conflicting_hash text,
    raw_entry        jsonb,
    error            text,
    ts               timestamptz DEFAULT now()
);
ALTER TABLE audit_deadletter ADD COLUMN IF NOT EXISTS kind text;
CREATE UNIQUE INDEX IF NOT EXISTS audit_deadletter_run_seq_content_hash_idx
    ON audit_deadletter (run_id, seq, content_hash);
ALTER TABLE audit_deadletter ADD COLUMN IF NOT EXISTS stream_entry_id text;
CREATE UNIQUE INDEX IF NOT EXISTS audit_deadletter_stream_entry_id_idx
    ON audit_deadletter (stream_entry_id);

CREATE TABLE IF NOT EXISTS audit_close_deadletter (
    id               bigserial PRIMARY KEY,
    request_id       text,
    run_id           text,
    raw_entry        jsonb NOT NULL,
    error            text NOT NULL,
    stream_entry_id  text NOT NULL UNIQUE,
    ts               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_event_raw (
    id              bigserial PRIMARY KEY,
    run_id          text NOT NULL,
    task_id         text NOT NULL,
    seat_id         text,
    orchestrator    text,
    event_type      text NOT NULL,
    schema_version  text NOT NULL DEFAULT '1',
    sent_at         timestamptz NOT NULL,
    payload         jsonb NOT NULL,           -- allowlisted metadata only; raw I/O excluded at the tee
    stream_entry_id text NOT NULL,
    inserted_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)
);
ALTER TABLE eval_event_raw ADD COLUMN IF NOT EXISTS schema_version text NOT NULL DEFAULT '1';
ALTER TABLE eval_event_raw ADD COLUMN IF NOT EXISTS orchestrator text;
CREATE INDEX IF NOT EXISTS eval_event_raw_run_idx ON eval_event_raw (run_id);
CREATE INDEX IF NOT EXISTS eval_event_raw_task_idx ON eval_event_raw (task_id);
CREATE INDEX IF NOT EXISTS eval_event_raw_inserted_at_idx ON eval_event_raw (inserted_at);
CREATE INDEX IF NOT EXISTS eval_event_raw_orchestrator_task_sent_idx
    ON eval_event_raw (orchestrator, task_id, sent_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS eval_deadletter (
    id              bigserial PRIMARY KEY,
    run_id          text,
    task_id         text,
    seat_id         text,
    event_type      text,
    schema_version  text,
    payload         jsonb,
    stream_entry_id text,
    raw_entry       jsonb,
    error           text,
    ts              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)   -- plan-panel M3 P1: PEL redelivery must not double-deadletter
);
ALTER TABLE eval_deadletter ADD COLUMN IF NOT EXISTS schema_version text;

CREATE TABLE IF NOT EXISTS transcript_io (
    id              bigserial PRIMARY KEY,
    run_id          text NOT NULL,
    task_id         text NOT NULL,
    seat_id         text,
    orchestrator    text,
    turn_index      integer NOT NULL DEFAULT 0,
    item_id         text NOT NULL,
    seq             bigint NOT NULL DEFAULT 0,
    kind            text NOT NULL,
    tool_name       text,
    content         text NOT NULL,
    meta            jsonb NOT NULL DEFAULT '{}',
    ts              timestamptz NOT NULL,
    stream_entry_id text NOT NULL,
    inserted_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)
);
ALTER TABLE transcript_io ADD COLUMN IF NOT EXISTS turn_index integer NOT NULL DEFAULT 0;
ALTER TABLE transcript_io ADD COLUMN IF NOT EXISTS tool_name text;
ALTER TABLE transcript_io ADD COLUMN IF NOT EXISTS meta jsonb NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS transcript_io_task_ts_idx ON transcript_io (task_id, ts);
CREATE INDEX IF NOT EXISTS transcript_io_ts_idx ON transcript_io (ts);
CREATE INDEX IF NOT EXISTS transcript_io_inserted_at_idx ON transcript_io (inserted_at);

CREATE TABLE IF NOT EXISTS transcript_deadletter (
    id              bigserial PRIMARY KEY,
    run_id          text,
    task_id         text,
    seat_id         text,
    kind            text,
    raw_entry       jsonb,
    error           text,
    stream_entry_id text,
    ts              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)
);
ALTER TABLE transcript_deadletter ADD COLUMN IF NOT EXISTS kind text;

CREATE TABLE IF NOT EXISTS eval_tool_call (
    id                 bigserial PRIMARY KEY,
    run_id             text NOT NULL,
    task_id            text NOT NULL,
    seat_id            text,
    orchestrator       text,
    attempt_epoch      integer NOT NULL,
    turn_index         integer NOT NULL,
    tool_call_id       text NOT NULL,
    tool_name          text,
    started_at         timestamptz,
    finished_at        timestamptz,
    latency_ms         bigint,
    latency_basis      text CHECK (latency_basis IN ('sent_at', 'event_ts')),
    exit_code          integer,
    ok                 boolean,
    outcome            text NOT NULL DEFAULT 'open'
                       CHECK (outcome IN ('open', 'finished', 'timeout', 'incomplete', 'clock_invalid')),
    started_stream_id  text,
    finished_stream_id text,
    inserted_at        timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, task_id, tool_call_id)
);

CREATE TABLE IF NOT EXISTS eval_turn (
    id                 bigserial PRIMARY KEY,
    run_id             text NOT NULL,
    task_id            text NOT NULL,
    seat_id            text,
    orchestrator       text,
    attempt_epoch      integer NOT NULL,
    turn_index         integer NOT NULL,
    started_at         timestamptz,
    completed_at       timestamptz,
    latency_ms         bigint,
    latency_basis      text CHECK (latency_basis IN ('sent_at', 'event_ts')),
    tool_call_count    integer NOT NULL DEFAULT 0,
    ok                 boolean,
    outcome            text NOT NULL DEFAULT 'open'
                       CHECK (outcome IN ('open', 'finished', 'timeout', 'incomplete', 'clock_invalid', 'recovered')),
    close_basis        text NOT NULL DEFAULT 'none'
                       CHECK (close_basis IN ('turn_completed', 'task_finish_derived', 'turn_timeout', 'turn_finalized', 'none')),
    finality_evidence  text CHECK (finality_evidence IN ('fd_quiescence', 'retracted')),
    inserted_at        timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, task_id, turn_index)
);

CREATE TABLE IF NOT EXISTS eval_task (
    id                 bigserial PRIMARY KEY,
    run_id             text NOT NULL,
    task_id            text NOT NULL,
    seat_id            text,
    orchestrator       text,
    attempt_epoch      integer NOT NULL,
    started_at         timestamptz,
    finished_at        timestamptz,
    duration_ms        bigint,
    turn_count         integer NOT NULL DEFAULT 0,
    tool_call_count    integer NOT NULL DEFAULT 0,
    ok                 boolean,
    outcome            text NOT NULL DEFAULT 'open'
                       CHECK (outcome IN ('open', 'finished', 'timeout', 'incomplete')),
    inserted_at        timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, task_id)
);

CREATE TABLE IF NOT EXISTS span_deadletter (
    id               bigserial PRIMARY KEY,
    run_id           text,
    task_id          text,
    event_type       text NOT NULL,
    raw_entry        jsonb NOT NULL,
    error            text NOT NULL,
    stream_entry_id  text NOT NULL UNIQUE,
    ts               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE idempotency_keys ADD COLUMN IF NOT EXISTS receipt jsonb;

CREATE TABLE IF NOT EXISTS write_deadletter (
    ulid       text NOT NULL,
    payload    jsonb NOT NULL,
    error      text NOT NULL,
    stream_entry_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE write_deadletter ADD COLUMN IF NOT EXISTS stream_entry_id text;
CREATE INDEX IF NOT EXISTS write_deadletter_ulid_idx ON write_deadletter (ulid);
CREATE UNIQUE INDEX IF NOT EXISTS write_deadletter_stream_entry_id_idx
    ON write_deadletter (stream_entry_id);

CREATE SCHEMA IF NOT EXISTS mcp_auth;

CREATE TABLE IF NOT EXISTS mcp_auth.oauth_clients (
    client_id          text PRIMARY KEY,
    client_secret_hash text,
    redirect_uris      jsonb NOT NULL DEFAULT '[]',
    metadata           jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    last_used_at       timestamptz
);

CREATE TABLE IF NOT EXISTS mcp_auth.auth_codes (
    code_hash      text PRIMARY KEY,
    client_id      text NOT NULL,
    redirect_uri   text NOT NULL,
    resource       text NOT NULL,
    code_challenge text NOT NULL,
    scopes         jsonb NOT NULL DEFAULT '[]',
    expires_at     timestamptz NOT NULL,
    used_at        timestamptz
);

CREATE TABLE IF NOT EXISTS mcp_auth.access_tokens (
    token_hash text PRIMARY KEY,
    client_id  text NOT NULL,
    resource   text NOT NULL,
    scopes     jsonb NOT NULL DEFAULT '[]',
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS mcp_auth.refresh_tokens (
    token_hash        text PRIMARY KEY,
    client_id         text NOT NULL,
    access_token_hash text,
    resource          text NOT NULL,
    scopes            jsonb NOT NULL DEFAULT '[]',
    expires_at        timestamptz NOT NULL,
    rotated_to        text,
    revoked_at        timestamptz
);

CREATE TABLE IF NOT EXISTS mcp_auth.login_sessions (
    session_id      text PRIMARY KEY,
    csrf_token      text NOT NULL,
    authorize_state jsonb NOT NULL,
    verified_at     timestamptz,
    expires_at      timestamptz NOT NULL,
    fail_count      int NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mcp_auth.login_attempts (
    client_id         text PRIMARY KEY,
    fail_count        int NOT NULL DEFAULT 0,
    window_started_at timestamptz NOT NULL DEFAULT now()
);

-- Bus-side gate (spec art-8742dfc1ca4b8be8 v6 §4). DDL only: privilege statements and the
-- reader role are applied out-of-band by src/arb_memory/mcp/grants.py, per test_schema.py.
CREATE TABLE IF NOT EXISTS claims (
    claim_id     text PRIMARY KEY,
    finding_ref  text NOT NULL,
    status       text NOT NULL DEFAULT 'unconfirmed'
                 CHECK (status IN ('unconfirmed','confirmed','retracted')),
    severity     text,                                  -- ORDERING ONLY (see spec §8)
    -- Author identity (F1): the cross-family MUST in spec §7.3 is uncheckable without it.
    author_seat   text NOT NULL,
    author_family text NOT NULL,
    author_family_provenance text NOT NULL
                 CHECK (author_family_provenance IN ('wire','configured')),
    probe_artefact_id text,
    probe_artefact_version int,
    review_by    timestamptz,                           -- accepted-risk expiry; NULL = none
    confirmed_at timestamptz,
    confirmed_by text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS attestations (
    claim_id        text NOT NULL REFERENCES claims(claim_id),
    verifier_seat   text NOT NULL,
    verifier_family text NOT NULL,
    family_provenance text NOT NULL
                    CHECK (family_provenance IN ('wire','configured')),
    restatement     text NOT NULL,          -- the claim in the verifier's own words
    mechanism       text NOT NULL,          -- which lines, which behaviour, why output entails defect
    falsifier       text NOT NULL,          -- what result would have falsified it
    falsifier_kind  text NOT NULL
                    CHECK (falsifier_kind IN ('command','prose')),
    -- Harness-produced record of the verifier's re-run (F2). NOT NULL at the COLUMN, not merely
    -- required by the view. Two halves, deliberately enforced at different strengths:
    --   EXISTENCE  -- column-strength, via the FK below. NOT NULL alone proved only that a pointer
    --                was supplied; the literal 'x' satisfied it (panel P2-3).
    --   AUTHORSHIP -- consumer-strength only, in gate_store.insert_attestation (F4). It is
    --                cross-artefact, so no column constraint can express it. NOTE: the author field
    --                it checks is writer-supplied and unbound (see gate_store.py) -- F4 does NOT
    --                today establish "harness-authored".
    rerun_artefact_id      text NOT NULL,
    rerun_artefact_version int  NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, verifier_seat)
);
-- DROP-then-ADD so schema.sql stays re-appliable over itself (test_schema.py:30); a bare
-- ADD CONSTRAINT fails on the second apply. (The in-file precedent for an FK INTO artefacts is
-- the inline declaration on `hints` above; artefacts' own line is a bare DROP with no re-ADD,
-- so it is NOT the shape to copy here.) No ON DELETE clause: a cited artefact version becomes
-- undeletable until its attestation is removed, which retention/GC must order accordingly.
ALTER TABLE attestations DROP CONSTRAINT IF EXISTS attestations_rerun_artefact_fk;
ALTER TABLE attestations ADD CONSTRAINT attestations_rerun_artefact_fk
    FOREIGN KEY (rerun_artefact_id, rerun_artefact_version)
    REFERENCES artefacts (artefact_id, version);

CREATE TABLE IF NOT EXISTS seat_posture (
    seat_id            text PRIMARY KEY,
    requires_claim_ref boolean NOT NULL DEFAULT true,   -- default-deny (spec §3)
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Lane membership is a STORE fact, written by the consumer at arm time (F3, spec §5.3).
CREATE TABLE IF NOT EXISTS lease_lanes (
    lease_id  text PRIMARY KEY,
    lane      text NOT NULL CHECK (lane IN ('gated','exempt')),
    armed_by  text NOT NULL,                            -- consumer identity that armed it
    armed_at  timestamptz NOT NULL DEFAULT now()
);

-- Admissibility expressed ONCE, here, so the dispatch gate and the close gate
-- cannot drift about what "confirmed" means.
CREATE OR REPLACE VIEW claim_admissibility_v AS
SELECT c.claim_id,
       ok.confirmed_now,
       ok.attested,
       ok.decorrelation_provenance,
       (ok.confirmed_now AND ok.attested) AS admissible,
       c.status, c.review_by
FROM claims c
CROSS JOIN LATERAL (
    SELECT
        (c.status = 'confirmed'
         AND (c.review_by IS NULL OR c.review_by > now()))            AS confirmed_now,
        -- Completeness AND decorrelation, in one predicate: an attestation from the
        -- author's own family does not count as an attestation at all (F1).
        EXISTS (SELECT 1 FROM attestations a
                WHERE a.claim_id = c.claim_id
                  AND a.restatement <> '' AND a.mechanism <> ''
                  AND a.falsifier <> ''
                  AND a.verifier_family <> c.author_family)           AS attested,
        -- 'wire' only when BOTH sides are machinery-attested; otherwise the
        -- decorrelation claim is degraded and the sampler weights it up (spec §7.4).
        -- count(*) = 0 FIRST: bool_and over zero rows is NULL, which would otherwise
        -- fall through to 'degraded' and report a claim with NO attestation as merely
        -- weakly-decorrelated. An aggregate subquery always returns a row, so COALESCE
        -- would never have fired.
        -- The WHERE clause MUST stay identical to `attested`'s: two subqueries measuring
        -- subtly different populations is drift waiting to be resolved in whichever
        -- direction is convenient. Without the completeness predicates an INCOMPLETE
        -- cross-family attestation yields attested=false alongside provenance='wire'.
        (SELECT CASE
                    WHEN count(*) = 0 THEN 'none'
                    WHEN bool_and(a.family_provenance = 'wire')
                         AND c.author_family_provenance = 'wire'
                    THEN 'wire' ELSE 'degraded' END
         FROM attestations a
         WHERE a.claim_id = c.claim_id
           AND a.restatement <> '' AND a.mechanism <> ''
           AND a.falsifier <> ''
           AND a.verifier_family <> c.author_family)                  AS decorrelation_provenance
) AS ok;

-- Seat posture lives behind the DSN, NOT in the seat's env file (spec §9.3).
CREATE OR REPLACE VIEW seat_posture_v AS
SELECT seat_id, requires_claim_ref FROM seat_posture;

CREATE OR REPLACE VIEW lease_lane_v AS
SELECT lease_id, lane FROM lease_lanes;

-- ---------------------------------------------------------------------------
-- Slice 1d-i: per-seat lane-writer authority (SECURITY DEFINER, session_user).
-- Bodies live ONLY here. Runtime Python binds p_lease_id as a query parameter
-- and never assembles or interpolates function-body SQL.
-- search_path is pinned, at apply time, to the schema these objects are created
-- in, so each function reaches its own tables and nothing else.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS lane_writer_bindings (
    db_role     name PRIMARY KEY,
    consumer_id text UNIQUE NOT NULL,
    lane        text NOT NULL CHECK (lane IN ('gated', 'exempt'))
);

-- Schema resolution. These functions are SECURITY DEFINER, so search_path must
-- be pinned rather than inherited from the caller — a lane-writer role controls
-- its own search_path and could otherwise redirect the lookup. Each definition
-- is therefore wrapped in a DO block that substitutes current_schema() at apply
-- time, giving `SET search_path = <own schema>, pg_catalog`. Both production and
-- each scratch test schema get functions bound to their own tables, and the
-- bodies can name relations directly instead of assembling dynamic SQL.
--
-- The previous approach resolved the schema by scanning pg_class for a
-- lease_lanes relation owned by CURRENT_USER. That assumed the owner held
-- exactly one such relation database-wide. A single leftover scratch schema — or
-- a concurrent test session, which creates one by design — made
-- SELECT ... INTO STRICT raise "query returned more than one row" BEFORE the
-- unbound-role guard was reached, so the guard's own test asserted nothing
-- (issue #8). Pinning is safe here because lane-writer roles are granted USAGE
-- on the schema and never CREATE, so they cannot shadow these relations.

DO $arm_lease_lane$
BEGIN
EXECUTE pg_catalog.replace($body$
CREATE OR REPLACE FUNCTION arm_lease_lane(p_lease_id text)
RETURNS lease_lanes
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = __LANE_SCHEMA__, pg_catalog
AS $function$
DECLARE
    result record;
    bound_consumer text;
    bound_lane text;
BEGIN
    SELECT consumer_id, lane INTO bound_consumer, bound_lane
    FROM lane_writer_bindings
    WHERE db_role = session_user;

    IF bound_consumer IS NULL THEN
        RAISE EXCEPTION 'unbound-lane-writer-role: %', session_user
            USING ERRCODE = '42501';
    END IF;

    INSERT INTO lease_lanes (lease_id, lane, armed_by)
    VALUES (p_lease_id, bound_lane, bound_consumer)
    RETURNING lease_id, lane, armed_by, armed_at INTO STRICT result;

    RETURN result;
END;
$function$;
$body$, '__LANE_SCHEMA__', pg_catalog.quote_ident(pg_catalog.current_schema()));
END
$arm_lease_lane$;

DO $retire_lease_lane$
BEGIN
EXECUTE pg_catalog.replace($body$
CREATE OR REPLACE FUNCTION retire_lease_lane(p_lease_id text)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = __LANE_SCHEMA__, pg_catalog
AS $function$
DECLARE
    bound_consumer text;
    bound_lane text;
    deleted int;
BEGIN
    SELECT consumer_id, lane INTO bound_consumer, bound_lane
    FROM lane_writer_bindings
    WHERE db_role = session_user;

    IF bound_consumer IS NULL THEN
        RAISE EXCEPTION 'unbound-lane-writer-role: %', session_user
            USING ERRCODE = '42501';
    END IF;

    -- Bound-consumer predicate: only the authenticated seat's rows.
    DELETE FROM lease_lanes
    WHERE lease_id = p_lease_id AND armed_by = bound_consumer;
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted > 0;
END;
$function$;
$body$, '__LANE_SCHEMA__', pg_catalog.quote_ident(pg_catalog.current_schema()));
END
$retire_lease_lane$;

DO $list_lease_lanes$
BEGIN
EXECUTE pg_catalog.replace($body$
CREATE OR REPLACE FUNCTION list_lease_lanes()
RETURNS SETOF lease_lanes
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = __LANE_SCHEMA__, pg_catalog
AS $function$
DECLARE
    bound_consumer text;
    bound_lane text;
BEGIN
    SELECT consumer_id, lane INTO bound_consumer, bound_lane
    FROM lane_writer_bindings
    WHERE db_role = session_user;

    IF bound_consumer IS NULL THEN
        RAISE EXCEPTION 'unbound-lane-writer-role: %', session_user
            USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT lease_id, lane, armed_by, armed_at
    FROM lease_lanes
    WHERE armed_by = bound_consumer;
END;
$function$;
$body$, '__LANE_SCHEMA__', pg_catalog.quote_ident(pg_catalog.current_schema()));
END
$list_lease_lanes$;

-- Served-hint record (S1 schema). Guide §6 + BUILD-CHARTER D-2 (query_hmac default;
-- query_text nullable opt-in). See docs/superpowers/specs/2026-07-29-served-hint-record-ERRATA.md.
CREATE TABLE IF NOT EXISTS hint_read (
    read_id         uuid PRIMARY KEY,
    door            text NOT NULL CHECK (door IN ('bus', 'local')),
    outcome         text NOT NULL CHECK (outcome IN ('ok', 'error')),
    run_id          text,
    seat_id         text,
    query_hmac      text,
    query_text      text,
    query_truncated boolean NOT NULL DEFAULT false,
    k               int  NOT NULL,
    hit_count       int  NOT NULL,
    cid             text,
    stream_entry_id text,
    served_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)
);
CREATE INDEX IF NOT EXISTS hint_read_served_idx ON hint_read (served_at DESC);
CREATE INDEX IF NOT EXISTS hint_read_run_idx  ON hint_read (run_id)  WHERE run_id  IS NOT NULL;
CREATE INDEX IF NOT EXISTS hint_read_seat_idx ON hint_read (seat_id) WHERE seat_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS hint_read_door_idx   ON hint_read (door, served_at DESC);

CREATE TABLE IF NOT EXISTS hint_read_hit (
    read_id         uuid NOT NULL REFERENCES hint_read(read_id) ON DELETE CASCADE,
    rank            int  NOT NULL,
    hint_id         bigint NOT NULL,
    withheld        boolean NOT NULL DEFAULT false,
    vector_distance double precision,
    lexical_rank    real,
    PRIMARY KEY (read_id, rank)
);
CREATE INDEX IF NOT EXISTS hint_read_hit_hint_idx ON hint_read_hit (hint_id);

CREATE TABLE IF NOT EXISTS hint_read_deadletter (
    id              bigserial PRIMARY KEY,
    stream_entry_id text,
    raw_entry       jsonb,
    error           text,
    ts              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)
);
