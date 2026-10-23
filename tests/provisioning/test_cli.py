import importlib.util
import json

from .test_plan import environment


def invoke(args):
    assert importlib.util.find_spec("lightrag.provisioning.__main__"), (
        "Protected migration CLI is missing"
    )
    from lightrag.provisioning.__main__ import main

    return main(args)


def inputs(tmp_path):
    values = dict(
        environment(),
        POSTGRES_USER="migration_test_app",
        POSTGRES_PASSWORD="never-emit-fixture",
        HUGEGRAPH_USERNAME="graph_app",
        HUGEGRAPH_PASSWORD="never-emit-graph",
        LIGHTRAG_OBJECT_STORAGE="s3",
    )
    p = tmp_path / "environment.json"
    p.write_text(json.dumps(values))
    return p


def test_offline_plan_never_needs_credentials_or_connections(
    tmp_path, monkeypatch, capsys
):
    import socket

    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *a: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )
    monkeypatch.delenv("LIGHTRAG_STORAGE_MIGRATION_DSN", raising=False)
    p = inputs(tmp_path)
    assert invoke(["plan", "--environment", str(p)]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert len(report["plan_hash"]) == len(report["baseline_manifest_hash"]) == 64
    assert "never-emit" not in output


def test_mutating_cli_requires_confirmation_before_any_connection(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(
        "LIGHTRAG_STORAGE_MIGRATION_DSN",
        "postgresql://x:must-not-emit@invalid/db?sslmode=disable",
    )
    assert invoke(["apply", "--environment", str(inputs(tmp_path))]) == 2
    output = capsys.readouterr()
    assert "must-not-emit" not in output.out + output.err


def test_wrong_approved_plan_or_missing_transport_policy_refuses_before_connect(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(
        "LIGHTRAG_STORAGE_MIGRATION_DSN", "postgresql://x:must-not-emit@invalid/db"
    )
    assert (
        invoke(
            [
                "apply",
                "--environment",
                str(inputs(tmp_path)),
                "--confirm-maintenance",
                "--confirm-empty-target",
                "--expected-profile-hash",
                "a" * 64,
                "--expected-plan-hash",
                "b" * 64,
                "--graph-wait-seconds",
                "1",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert "must-not-emit" not in output.out + output.err


def test_operator_dsn_identity_and_explicit_transport_are_bound_without_network():
    import pytest
    from lightrag.provisioning.__main__ import check_dsn

    values = dict(environment(), POSTGRES_USER="runtime")
    valid = "postgresql://operator:fixture@pg.invalid:5432/prod?sslmode=disable"
    check_dsn(valid, values)
    for dsn in (
        valid.replace("operator", "runtime"),
        valid.replace("pg.invalid", "other.invalid"),
        valid.replace("/prod?", "/other?"),
        valid.split("?")[0],
        valid + "&sslmode=require",
    ):
        with pytest.raises(ValueError):
            check_dsn(dsn, values)


def test_encoded_runtime_dsn_user_is_not_a_separate_operator():
    import pytest
    from lightrag.provisioning.__main__ import check_dsn

    with pytest.raises(ValueError):
        check_dsn(
            "postgresql://%72untime:fixture@pg.invalid:5432/prod?sslmode=disable",
            dict(environment(), POSTGRES_USER="runtime"),
        )


def test_dsn_query_cannot_override_the_approved_target_or_user():
    import pytest
    from lightrag.provisioning.__main__ import check_dsn

    base = "postgresql://operator:fixture@pg.invalid:5432/prod?sslmode=disable"
    for suffix in (
        "&host=other.invalid",
        "&user=runtime",
        "&dbname=other",
        "&port=1234",
        "#unapproved",
    ):
        with pytest.raises(ValueError):
            check_dsn(base + suffix, dict(environment(), POSTGRES_USER="runtime"))


async def test_graph_migration_cannot_borrow_the_runtime_identity(monkeypatch):
    import asyncpg
    import pytest
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from .test_plan import build
    from lightrag.provisioning.__main__ import run

    monkeypatch.setenv("LIGHTRAG_GRAPH_MIGRATION_USERNAME", "same_runtime")
    monkeypatch.setenv("LIGHTRAG_GRAPH_MIGRATION_PASSWORD", "synthetic")
    connect = AsyncMock(side_effect=AssertionError("no network"))
    monkeypatch.setattr(asyncpg, "connect", connect)
    with pytest.raises(ValueError):
        await run(
            SimpleNamespace(command="apply", graph_wait_seconds=1),
            dict(environment(), HUGEGRAPH_USERNAME="same_runtime"),
            build(),
            "not-used",
        )
    assert connect.await_count == 0


def test_repeated_slash_database_path_does_not_match_approved_database():
    import pytest
    from lightrag.provisioning.__main__ import check_dsn

    with pytest.raises(ValueError):
        check_dsn(
            "postgresql://operator:fixture@pg.invalid:5432//prod?sslmode=disable",
            dict(environment(), POSTGRES_USER="runtime"),
        )


def test_operator_dsn_requires_explicit_port_not_ambient_pgport(monkeypatch):
    import pytest
    from lightrag.provisioning.__main__ import check_dsn

    monkeypatch.setenv("PGPORT", "6543")
    for port in ("", ":0"):
        with pytest.raises(ValueError):
            check_dsn(
                f"postgresql://operator:fixture@pg.invalid{port}/prod?sslmode=disable",
                dict(environment(), POSTGRES_USER="runtime"),
            )
