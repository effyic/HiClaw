"""Nacos connection settings from environment."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse


def parse_duration(value: str, default_seconds: int) -> int:
    """Parse ``30s``, ``5m``, or plain integer seconds."""
    raw = (value or "").strip()
    if not raw:
        return default_seconds
    if raw.isdigit():
        return int(raw)
    m = re.fullmatch(r"(\d+)([smh])", raw)
    if not m:
        return default_seconds
    n, unit = int(m.group(1)), m.group(2)
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    return n


def _skills_api_url() -> str:
    return (
        os.environ.get("SKILLS_API_URL", "").strip()
        or os.environ.get("HICLAW_SKILLS_API_URL", "").strip()
    )


@dataclass(frozen=True)
class NacosConfig:
    enabled: bool
    server_addr: str  # host:port
    namespace: str
    auth_type: str  # none | nacos | sts-hiclaw
    username: str
    password: str
    capability_mode: str  # off | platform | self | hybrid
    bootstrap_skills: list[str]
    mcp_names: list[str]
    agent_register: bool
    agent_card_name: str
    heartbeat_interval: int
    watch_enabled: bool
    watch_interval: int
    control_port: int
    control_bind: str
    control_token: str
    ai_gateway_url: str
    worker_gateway_key: str
    controller_url: str
    worker_name: str

    @property
    def base_url(self) -> str:
        return f"http://{self.server_addr}"


def parse_nacos_uri(raw: str) -> tuple[str, str, str, str]:
    """Return host, port, namespace, username, password from nacos:// URI."""
    url = raw.strip()
    if url.startswith("nacos://"):
        url = url[len("nacos://") :]

    username = os.environ.get("HICLAW_NACOS_USERNAME", "")
    password = os.environ.get("HICLAW_NACOS_PASSWORD", "")
    namespace = ""

    auth_part, _, rest = url.partition("@")
    if rest:
        if ":" in auth_part:
            username, password = auth_part.split(":", 1)
        else:
            username = auth_part
        url = rest

    if "?" in url:
        url, _query = url.split("?", 1)

    if "/" in url:
        host_port, path = url.split("/", 1)
        namespace = path.split("/")[0]
    else:
        host_port = url

    host = host_port
    port = "8848"
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)

    if not namespace:
        namespace = "public"
    return host, port, namespace, username, password


def load_nacos_config(worker_name: str) -> NacosConfig:
    mode = os.environ.get("NACOS_CAPABILITY_MODE", "hybrid").strip().lower()
    skills_url = _skills_api_url()
    nacos_enabled = skills_url.startswith("nacos://") and mode != "off"

    host, port, namespace, username, password = "", "8848", "public", "", ""
    if nacos_enabled:
        host, port, namespace, username, password = parse_nacos_uri(skills_url)

    agent_name = os.environ.get("NACOS_AGENT_CARD_NAME", f"hermes-{worker_name}").strip()
    bootstrap = [
        s.strip()
        for s in os.environ.get("NACOS_BOOTSTRAP_SKILLS", "").split(",")
        if s.strip()
    ]
    mcp_names = [
        s.strip()
        for s in os.environ.get("NACOS_MCP_NAMES", "").split(",")
        if s.strip()
    ]

    return NacosConfig(
        enabled=nacos_enabled and mode in ("self", "hybrid"),
        server_addr=f"{host}:{port}" if host else "",
        namespace=namespace,
        auth_type=os.environ.get("NACOS_AUTH_TYPE", "").strip().lower() or "none",
        username=username,
        password=password,
        capability_mode=mode,
        bootstrap_skills=bootstrap,
        mcp_names=mcp_names,
        agent_register=os.environ.get("NACOS_AGENT_REGISTER", "0").strip() in ("1", "true", "yes"),
        agent_card_name=agent_name,
        heartbeat_interval=parse_duration(
            os.environ.get("NACOS_HEARTBEAT_INTERVAL", "30s"), 30
        ),
        watch_enabled=os.environ.get("NACOS_WATCH_ENABLED", "1").strip()
        in ("1", "true", "yes"),
        watch_interval=parse_duration(
            os.environ.get("NACOS_WATCH_INTERVAL", "60s"), 60
        ),
        control_port=int(os.environ.get("HERMES_CONTROL_PORT", "8088")),
        control_bind=os.environ.get("HERMES_CONTROL_BIND", "0.0.0.0").strip(),
        control_token=os.environ.get("HERMES_CONTROL_TOKEN", "").strip(),
        ai_gateway_url=os.environ.get("HICLAW_AI_GATEWAY_URL", "").rstrip("/"),
        worker_gateway_key=os.environ.get("HICLAW_WORKER_GATEWAY_KEY", ""),
        controller_url=os.environ.get("HICLAW_CONTROLLER_URL", "").rstrip("/"),
        worker_name=worker_name,
    )


def parse_skill_spec(spec: str) -> tuple[str, Optional[str], Optional[str]]:
    """Parse ``name``, ``name@1.0.0``, or ``name@label:stable``."""
    spec = spec.strip()
    if "@" not in spec:
        return spec, None, None
    name, rest = spec.split("@", 1)
    if rest.startswith("label:"):
        return name, None, rest[len("label:") :]
    if rest.startswith("version:"):
        return name, rest[len("version:") :], None
    return name, rest, None
