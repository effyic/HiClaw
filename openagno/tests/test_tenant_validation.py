"""Tests for tenant_id validation in request filters."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agno_worker.hooks.filters import RequestFilterPipeline, RequestRejectedError
from agno_worker.hooks.protocols import UserContext
from agno_worker.hooks.registry import HookRegistry


@pytest.fixture(autouse=True)
def _reset_require_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGNO_REQUIRE_TENANT_ID", raising=False)


def test_require_tenant_id_rejects_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGNO_REQUIRE_TENANT_ID", "true")
    registry = HookRegistry(tmp_path / "missing")
    pipeline = RequestFilterPipeline(registry)

    with pytest.raises(RequestRejectedError, match="tenant_id is required"):
        pipeline.apply_pre_filter(UserContext(user_id="u1"), {})


def test_require_tenant_id_allows_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGNO_REQUIRE_TENANT_ID", "true")
    registry = HookRegistry(tmp_path / "missing")
    pipeline = RequestFilterPipeline(registry)

    meta = pipeline.apply_pre_filter(
        UserContext(user_id="u1", tenant_id="tenant-a"),
        {"extra": 1},
    )
    assert meta["extra"] == 1


def test_require_tenant_id_reads_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGNO_REQUIRE_TENANT_ID", "true")
    registry = HookRegistry(tmp_path / "missing")
    pipeline = RequestFilterPipeline(registry)

    meta = pipeline.apply_pre_filter(UserContext(user_id="u1"), {"tenant_id": "tenant-b"})
    assert meta["tenant_id"] == "tenant-b"
