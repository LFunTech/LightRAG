"""Persistent pause and exactly-once manual retry selection/progress."""

VERSION = 2
SQL = """
CREATE TABLE IF NOT EXISTS lightrag_coordination.pipeline_control (
 deployment_id text NOT NULL,
 workspace text NOT NULL,
 paused boolean NOT NULL DEFAULT false,
 cancel_epoch bigint NOT NULL DEFAULT 0,
 PRIMARY KEY (deployment_id, workspace)
);
CREATE TABLE IF NOT EXISTS lightrag_coordination.pipeline_requests (
 deployment_id text NOT NULL,
 workspace text NOT NULL,
 request_id text NOT NULL,
 state text NOT NULL DEFAULT 'selecting',
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (deployment_id, workspace, request_id)
);
CREATE TABLE IF NOT EXISTS lightrag_coordination.pipeline_retry_targets (
 deployment_id text NOT NULL,
 workspace text NOT NULL,
 request_id text NOT NULL,
 doc_id text NOT NULL,
 version text NOT NULL,
 done boolean NOT NULL DEFAULT false,
 PRIMARY KEY (deployment_id, workspace, request_id, doc_id)
);
CREATE INDEX IF NOT EXISTS pipeline_requests_pending ON lightrag_coordination.pipeline_requests
 (deployment_id, workspace, created_at) WHERE state <> 'completed';
"""
