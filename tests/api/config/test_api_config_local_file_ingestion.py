"""Local file ingestion can be disabled without disabling object uploads."""

from __future__ import annotations

import sys

import pytest

from lightrag.api.config import parse_args

pytestmark = pytest.mark.offline


def _parse(monkeypatch, value: str | None = None):
    monkeypatch.setattr(sys, "argv", ["lightrag-server"])
    monkeypatch.delenv("ENABLE_LOCAL_FILE_INGESTION", raising=False)
    if value is not None:
        monkeypatch.setenv("ENABLE_LOCAL_FILE_INGESTION", value)
    return parse_args()


def test_local_file_ingestion_stays_enabled_by_default(monkeypatch):
    args = _parse(monkeypatch)

    assert args.enable_local_file_ingestion is True


@pytest.mark.parametrize("value", ["false", "0", "off", "no"])
def test_local_file_ingestion_can_be_disabled(monkeypatch, value):
    args = _parse(monkeypatch, value)

    assert args.enable_local_file_ingestion is False
