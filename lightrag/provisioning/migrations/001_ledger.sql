CREATE SCHEMA lightrag_storage;
REVOKE ALL ON SCHEMA lightrag_storage FROM PUBLIC;
CREATE TABLE lightrag_storage.migrations (
    version integer PRIMARY KEY,
    profile_hash text NOT NULL,
    checksum text NOT NULL,
    structure_hash text NOT NULL,
    graph_checksum text NOT NULL,
    graph_complete boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lightrag_storage.graph_steps (
    name text PRIMARY KEY,
    payload_hash text NOT NULL,
    task_id bigint,
    state text NOT NULL CHECK (state IN ('EXECUTING','CONFIRMED'))
);
REVOKE ALL ON ALL TABLES IN SCHEMA lightrag_storage FROM PUBLIC;
