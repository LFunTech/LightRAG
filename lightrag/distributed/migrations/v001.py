"""Version 1: additive coordination-only schema; never modifies business tables."""

VERSION = 1
SQL = """
CREATE SCHEMA IF NOT EXISTS lightrag_coordination;
CREATE TABLE IF NOT EXISTS lightrag_coordination.schema_version (
    version integer PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS lightrag_coordination.workspaces (
    deployment_id text NOT NULL,
    workspace text NOT NULL,
    manifest_hash text NOT NULL,
    generation bigint NOT NULL DEFAULT 1,
    fenced boolean NOT NULL DEFAULT false,
    fence_reason text,
    PRIMARY KEY (deployment_id, workspace)
);
CREATE TABLE IF NOT EXISTS lightrag_coordination.operations (
    id uuid PRIMARY KEY,
    deployment_id text NOT NULL,
    workspace text NOT NULL,
    generation bigint NOT NULL,
    owner_id uuid NOT NULL,
    witness bigint NOT NULL,
    kind text NOT NULL,
    exclusive boolean NOT NULL,
    state text NOT NULL DEFAULT 'active',
    phase text NOT NULL DEFAULT 'admitted',
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS coordination_operations_scope
    ON lightrag_coordination.operations (deployment_id, workspace, state);
CREATE TABLE IF NOT EXISTS lightrag_coordination.resource_locks (
    deployment_id text NOT NULL,
    workspace text NOT NULL,
    resource_key text NOT NULL,
    operation_id uuid NOT NULL,
    task_id uuid NOT NULL,
    depth integer NOT NULL DEFAULT 1,
    PRIMARY KEY (deployment_id, workspace, resource_key)
);
CREATE TABLE IF NOT EXISTS lightrag_coordination.document_claims (
    deployment_id text NOT NULL,
    workspace text NOT NULL,
    doc_id text NOT NULL,
    operation_id uuid NOT NULL,
    phase text NOT NULL DEFAULT 'claimed',
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (deployment_id, workspace, doc_id)
);
CREATE TABLE IF NOT EXISTS lightrag_coordination.mutations (
    id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    backend text NOT NULL,
    namespace text NOT NULL,
    method text NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    acknowledged_at timestamptz,
    recovered_at timestamptz
);
CREATE INDEX IF NOT EXISTS coordination_mutations_operation
    ON lightrag_coordination.mutations (operation_id, state);
CREATE TABLE IF NOT EXISTS lightrag_coordination.recovery_audit (
    id uuid PRIMARY KEY,
    deployment_id text NOT NULL,
    workspace text NOT NULL,
    old_generation bigint NOT NULL,
    new_generation bigint NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    confirmations jsonb NOT NULL,
    snapshot jsonb NOT NULL,
    recovered_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
"""
