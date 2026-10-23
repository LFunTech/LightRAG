from types import SimpleNamespace


def test_quarantine_logs_first_cause_only_without_exception_body_or_dsn(caplog):
    from lightrag.distributed.coordinator import PostgresCoordinator

    coor = PostgresCoordinator(
        "postgresql://private:do-not-log@pg.invalid/prod", "prod", "workspace"
    )
    coor._release_context = SimpleNamespace(instance_id="pod-1")
    error = RuntimeError("private SQL body and token")
    error.sqlstate = "08006"
    from lightrag.utils import logger

    logger.addHandler(caplog.handler)
    try:
        coor._quarantine(error=error, purpose="transaction")
        coor._quarantine(error=ValueError("second secret"), purpose="witness")
    finally:
        logger.removeHandler(caplog.handler)
    logs = [r for r in caplog.records if "quarantine" in r.message.lower()]
    assert len(logs) == 1
    assert all(
        v in logs[0].message for v in ("08006", "pod-1", "workspace", "transaction")
    )
    assert "do-not-log" not in caplog.text and "private SQL" not in caplog.text
