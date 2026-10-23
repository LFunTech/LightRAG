import os
import subprocess
import sys


def test_migration_cli_requires_explicit_operator_dsn_and_confirmation():
    env = os.environ.copy()
    env.pop("LIGHTRAG_RELEASE_OPERATOR_DSN", None)
    env["LIGHTRAG_COORDINATION_DSN"] = "postgresql://hidden:must-not-use@invalid/db"
    result = subprocess.run(
        [sys.executable, "-m", "lightrag.distributed.release", "migrate"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "must-not-use" not in result.stdout + result.stderr
    assert "OPERATOR_DSN" in result.stderr or "confirmation" in result.stderr


def test_status_cli_rejects_missing_scope_without_reading_database():
    env = os.environ.copy()
    env["LIGHTRAG_RELEASE_OPERATOR_DSN"] = "postgresql://hidden:must-not-use@invalid/db"
    result = subprocess.run(
        [sys.executable, "-m", "lightrag.distributed.release", "status"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "scope" in result.stderr
    assert "must-not-use" not in result.stdout + result.stderr


def test_enrollment_requires_explicit_maintenance_confirmation(monkeypatch, capsys):
    from lightrag.distributed.release.__main__ import main

    monkeypatch.setenv(
        "LIGHTRAG_RELEASE_OPERATOR_DSN", "postgresql://synthetic-never-connect"
    )
    assert (
        main(
            [
                "enroll",
                "--config",
                "missing.json",
                "--environment",
                "missing.json",
                "--active-slot",
                "blue",
            ]
        )
        == 2
    )
    assert "confirmation" in capsys.readouterr().err.lower()
