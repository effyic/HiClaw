"""Tests for Nacos capability modules."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from hermes_worker.capability.store import CapabilityStore
from hermes_worker.nacos.config import (
    load_nacos_config,
    parse_duration,
    parse_nacos_uri,
    parse_skill_spec,
)
from hermes_worker.nacos.mcp import build_mcporter_config, mcp_to_mcporter_entry
from hermes_worker.nacos.config import NacosConfig
from hermes_worker.nacos.skill import extract_skill_zip
from hermes_worker.nacos.cli import _build_patterns, _is_valid_skill_name_pattern, _score
from hermes_worker.nacos.agent_registry import AgentRegistry, _parse_control_endpoint
from hermes_worker.nacos.client import NacosAPIError


def test_parse_duration():
    assert parse_duration("30s", 10) == 30
    assert parse_duration("5m", 10) == 300
    assert parse_duration("", 42) == 42
    assert parse_duration("120", 10) == 120


def test_parse_nacos_uri():
    host, port, ns, user, pw = parse_nacos_uri(
        "nacos://admin:secret@registry.example.com:8848/my-ns"
    )
    assert host == "registry.example.com"
    assert port == "8848"
    assert ns == "my-ns"
    assert user == "admin"
    assert pw == "secret"


def test_parse_skill_spec():
    assert parse_skill_spec("foo") == ("foo", None, None)
    assert parse_skill_spec("foo@1.2.3") == ("foo", "1.2.3", None)
    assert parse_skill_spec("foo@label:stable") == ("foo", None, "stable")


def test_capability_store_roundtrip(tmp_path: Path):
    ws = tmp_path / "ws"
    home = ws / ".hermes"
    ws.mkdir()
    home.mkdir()
    store = CapabilityStore(ws, home)
    store.record_skill("demo", version="1.0.0")
    assert "demo" in store.protected_skill_names()
    store.save()
    store2 = CapabilityStore(ws, home)
    store2.load()
    assert store2.snapshot()["skills"]["demo"]["version"] == "1.0.0"


def test_extract_skill_zip_safe(tmp_path: Path):
    zpath = tmp_path / "skill.zip"
    out = tmp_path / "out"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("demo/SKILL.md", "# Demo")
        zf.writestr("demo/run.sh", "#!/bin/sh\necho hi")
    extract_skill_zip(zpath, out)
    assert (out / "demo" / "SKILL.md").exists()
    assert (out / "demo" / "run.sh").exists()


def test_mcp_to_mcporter_entry():
    cfg = NacosConfig(
        enabled=True,
        server_addr="h:8848",
        namespace="public",
        auth_type="none",
        username="",
        password="",
        capability_mode="hybrid",
        bootstrap_skills=[],
        mcp_names=[],
        agent_register=False,
        agent_card_name="x",
        heartbeat_interval=30,
        watch_enabled=False,
        watch_interval=60,
        control_port=8088,
        control_bind="127.0.0.1",
        control_token="",
        ai_gateway_url="https://gw.example.com",
        worker_gateway_key="KEY",
        controller_url="",
        worker_name="alice",
    )
    mapped = mcp_to_mcporter_entry(
        {
            "mcpName": "github",
            "metadata": {"higressRoute": "github", "transport": "http"},
        },
        cfg,
    )
    assert mapped is not None
    name, entry = mapped
    assert name == "github"
    assert entry["url"] == "https://gw.example.com/mcp-servers/github/mcp"
    assert entry["headers"]["Authorization"] == "Bearer KEY"


def test_mcp_to_mcporter_entry_admin_api_shape():
    cfg = NacosConfig(
        enabled=True,
        server_addr="h:8848",
        namespace="public",
        auth_type="none",
        username="",
        password="",
        capability_mode="hybrid",
        bootstrap_skills=[],
        mcp_names=[],
        agent_register=False,
        agent_card_name="x",
        heartbeat_interval=30,
        watch_enabled=False,
        watch_interval=60,
        control_port=8088,
        control_bind="127.0.0.1",
        control_token="",
        ai_gateway_url="https://gw.example.com",
        worker_gateway_key="KEY",
        controller_url="",
        worker_name="alice",
    )
    mapped = mcp_to_mcporter_entry(
        {
            "name": "deepwiki",
            "protocol": "mcp-streamable",
            "frontProtocol": "mcp-streamable",
            "version": "1.0.0",
        },
        cfg,
    )
    assert mapped is not None
    name, entry = mapped
    assert name == "deepwiki"
    assert entry["url"] == "https://gw.example.com/mcp-servers/deepwiki/mcp"
    assert entry["transport"] == "streamable-http"


def test_build_mcporter_hybrid_overrides_platform():
    platform = {"github": {"url": "https://old/mcp", "transport": "http"}}
    nacos = {"github": {"url": "https://new/mcp", "transport": "http"}}
    merged = build_mcporter_config(nacos, platform, hybrid=True)
    assert merged["mcpServers"]["github"]["url"] == "https://new/mcp"


def test_load_nacos_config_off(monkeypatch):
    monkeypatch.delenv("SKILLS_API_URL", raising=False)
    monkeypatch.setenv("NACOS_CAPABILITY_MODE", "off")
    cfg = load_nacos_config("w1")
    assert cfg.enabled is False


def test_is_valid_skill_name_pattern():
    assert _is_valid_skill_name_pattern("find-skills")
    assert _is_valid_skill_name_pattern("polish")
    assert not _is_valid_skill_name_pattern("find skill")
    assert not _is_valid_skill_name_pattern("-bad")
    assert not _is_valid_skill_name_pattern("bad-")


def test_build_patterns_hyphenates_multi_word():
    assert _build_patterns("find skill") == ["find-skill", "find", "skill"]
    assert _build_patterns("find-skills") == ["find-skills", "find", "skills"]


def test_build_patterns_falls_back_to_unfiltered():
    assert _build_patterns("!!") == [""]


def test_score_ranks_exact_name_higher():
    assert _score("polish", "polish", "") > _score("polish", "optimize", "")


def _nacos_config(agent_name: str = "hermes-test") -> NacosConfig:
    return NacosConfig(
        enabled=True,
        server_addr="nacos:8848",
        namespace="public",
        auth_type="nacos",
        username="nacos",
        password="nacos",
        capability_mode="hybrid",
        bootstrap_skills=[],
        mcp_names=[],
        agent_register=True,
        agent_card_name=agent_name,
        heartbeat_interval=30,
        watch_enabled=False,
        watch_interval=60,
        control_port=8088,
        control_bind="0.0.0.0",
        control_token="",
        ai_gateway_url="",
        worker_gateway_key="",
        controller_url="",
        worker_name="test",
    )


class _FakeNacosClient:
    def __init__(self, *, endpoint_status: int = 404, card_status: int = 200) -> None:
        self.endpoint_status = endpoint_status
        self.card_status = card_status
        self.post_paths: list[str] = []
        self.delete_paths: list[str] = []
        self.delete_params: dict[str, str] = {}

    async def post_form(self, path: str, data: dict[str, str], *, operation: str) -> None:
        self.post_paths.append(path)
        if path.endswith("/a2a/endpoint"):
            if self.endpoint_status != 200:
                raise NacosAPIError(operation, self.endpoint_status, "not found")
            return None
        if self.card_status != 200:
            raise NacosAPIError(operation, self.card_status, "conflict")
        return None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: object = None,
        raw: bool = False,
        operation: str = "nacos request",
    ) -> None:
        self.delete_paths.append(f"{method} {path}")
        if path.endswith("/a2a/endpoint") and self.endpoint_status == 404:
            raise NacosAPIError(operation, 404, "not found")
        if method == "DELETE" and path.endswith("/a2a"):
            self.delete_params = params or {}
        return None


def test_parse_control_endpoint_rewrites_bind_all():
    host, port = _parse_control_endpoint("http://0.0.0.0:9090")
    assert port == 9090
    assert host not in ("0.0.0.0", "")


def test_agent_register_url_mode_skips_endpoint_404():
    import asyncio

    client = _FakeNacosClient(endpoint_status=404)
    reg = AgentRegistry(
        client,
        _nacos_config(),
        "http://127.0.0.1:8088",
        lambda: {"skills": {}, "mcp": {}, "team": ""},
    )
    asyncio.run(reg.register())
    assert reg.online
    assert client.post_paths == [
        "/nacos/v3/admin/ai/a2a",
        "/nacos/v3/admin/ai/a2a/endpoint",
    ]


def test_agent_register_treats_card_conflict_as_success():
    import asyncio

    client = _FakeNacosClient(endpoint_status=404, card_status=409)
    reg = AgentRegistry(
        client,
        _nacos_config(),
        "http://127.0.0.1:8088",
        lambda: {"skills": {}, "mcp": {}, "team": ""},
    )
    asyncio.run(reg.register())
    assert reg.online


def test_agent_deregister_uses_card_delete():
    import asyncio

    client = _FakeNacosClient(endpoint_status=404)
    reg = AgentRegistry(
        client,
        _nacos_config(),
        "http://127.0.0.1:8088",
        lambda: {"skills": {}, "mcp": {}, "team": ""},
    )
    asyncio.run(reg.deregister())
    assert not reg.online
    assert client.delete_paths[0] == "DELETE /nacos/v3/admin/ai/a2a"
    assert client.delete_params.get("version") == "1.0.0"
    assert client.delete_params.get("agentName") == "hermes-test"
