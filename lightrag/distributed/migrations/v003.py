"""Retry target cutoff uses the application clock domain."""

VERSION = 3
SQL = """
ALTER TABLE lightrag_coordination.pipeline_requests
 ADD COLUMN IF NOT EXISTS target_cutoff_at timestamptz NOT NULL DEFAULT clock_timestamp();
"""
