"""Tests for HTTP identity resolution."""
from __future__ import annotations

from agno_worker.api.identity import resolve_role_code


def test_resolve_role_code_from_header() -> None:
    headers = {"role-code": "triage", "tenant-id": "tenant-a"}
    assert resolve_role_code(headers=headers) == "triage"


def test_resolve_role_code_prefers_header_over_query() -> None:
    headers = {"x-role-code": "expert_cardiology"}
    assert resolve_role_code(headers=headers, query_role_code="triage") == "expert_cardiology"


def test_resolve_role_code_from_query() -> None:
    assert resolve_role_code(headers={}, query_role_code="triage") == "triage"
