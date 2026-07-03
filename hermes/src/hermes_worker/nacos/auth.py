"""Nacos authentication: none, username/password, or STS via hiclaw-controller."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

NACOS_AUTH_NONE = "none"
NACOS_AUTH_NACOS = "nacos"
NACOS_AUTH_STS = "sts-hiclaw"

DEFAULT_STS_RESOURCES = ["agentSpec/*", "skill/*", "mcp/*", "a2a/*"]


class NacosCredential(ABC):
    @abstractmethod
    async def refresh(self, client: httpx.AsyncClient) -> None: ...

    @abstractmethod
    def apply(self, req: httpx.Request) -> None: ...


class NoneCredential(NacosCredential):
    async def refresh(self, client: httpx.AsyncClient) -> None:
        return

    def apply(self, req: httpx.Request) -> None:
        return


class UserPassCredential(NacosCredential):
    def __init__(self, server_addr: str, username: str, password: str) -> None:
        self._server_addr = server_addr
        self._username = username
        self._password = password
        self._token = ""
        self._expire_at = 0.0
        self._login_version = ""

    async def refresh(self, client: httpx.AsyncClient) -> None:
        if self._token and (self._expire_at == 0 or time.time() + 5 < self._expire_at):
            return
        await self._login(client)

    def apply(self, req: httpx.Request) -> None:
        if self._token:
            req.headers["Authorization"] = f"Bearer {self._token}"

    async def _login(self, client: httpx.AsyncClient) -> None:
        form = {"username": self._username, "password": self._password}
        endpoints = []
        if self._login_version in ("", "v3"):
            endpoints.append(
                f"http://{self._server_addr}/nacos/v3/auth/user/login"
            )
        if self._login_version in ("", "v1"):
            endpoints.append(
                f"http://{self._server_addr}/nacos/v1/auth/login"
            )
        for url in endpoints:
            resp = await client.post(
                url,
                content=urlencode(form),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                continue
            if self._apply_login_body(resp.json()):
                self._login_version = "v3" if "/v3/" in url else "v1"
                return
        raise RuntimeError("Nacos login failed with both v3 and v1 auth endpoints")

    def _apply_login_body(self, body: dict[str, Any]) -> bool:
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        token = data.get("accessToken") if isinstance(data, dict) else None
        if not token:
            return False
        self._token = str(token)
        ttl = data.get("tokenTtl", 0) if isinstance(data, dict) else 0
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            ttl = 0
        self._expire_at = time.time() + ttl if ttl > 0 else 0.0
        return True


class STSCredential(NacosCredential):
    def __init__(
        self,
        namespace: str,
        controller_url: str,
        resources: Optional[list[str]] = None,
    ) -> None:
        self._namespace = namespace
        self._controller_url = controller_url
        self._resources = resources or list(DEFAULT_STS_RESOURCES)
        self._cached: Optional[dict[str, str]] = None
        self._expire_at = 0.0

    async def refresh(self, client: httpx.AsyncClient) -> None:
        if self._cached and time.time() + 30 < self._expire_at:
            return
        token = _resolve_controller_bearer()
        if not self._controller_url or not token:
            raise RuntimeError(
                "sts-hiclaw requires HICLAW_CONTROLLER_URL and a controller bearer token"
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        cluster_id = os.environ.get("HICLAW_CLUSTER_ID", "").strip()
        if cluster_id:
            headers["X-HiClaw-Cluster-ID"] = cluster_id
        body = {
            "session_name": f"hermes-nacos-{self._namespace}",
            "entries": [
                {
                    "service": "ai-registry",
                    "permissions": ["read", "list"],
                    "scope": {
                        "namespace_id": self._namespace,
                        "resources": self._resources,
                    },
                }
            ],
        }
        resp = await client.post(
            f"{self._controller_url}/api/v1/credentials/sts",
            json=body,
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"controller STS request failed (HTTP {resp.status_code}): {resp.text}"
            )
        data = resp.json()
        self._cached = {
            "access_key_id": str(data.get("access_key_id", "")),
            "access_key_secret": str(data.get("access_key_secret", "")),
            "security_token": str(data.get("security_token", "")),
        }
        if not self._cached["access_key_id"]:
            raise RuntimeError("failed to parse STS credentials from controller")
        exp = data.get("expiration")
        if exp:
            try:
                from datetime import datetime, timezone

                dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                self._expire_at = dt.timestamp()
            except ValueError:
                self._expire_at = time.time() + 3600
        else:
            self._expire_at = time.time() + 3600

    def apply(self, req: httpx.Request) -> None:
        if not self._cached:
            return
        timestamp = str(int(time.time() * 1000))
        group = "DEFAULT_GROUP"
        sign_data = (
            f"{self._namespace}+{group}+{timestamp}"
            if self._namespace
            else timestamp
        )
        mac = hmac.new(
            self._cached["access_key_secret"].encode(),
            sign_data.encode(),
            hashlib.sha1,
        )
        signature = base64.b64encode(mac.digest()).decode()
        req.headers["Spas-AccessKey"] = self._cached["access_key_id"]
        req.headers["Spas-SecurityToken"] = self._cached["security_token"]
        req.headers["Timestamp"] = timestamp
        req.headers["Spas-Signature"] = signature


def _resolve_controller_bearer() -> str:
    if tok := os.environ.get("HICLAW_AUTH_TOKEN", "").strip():
        return tok
    path = os.environ.get("HICLAW_AUTH_TOKEN_FILE", "").strip()
    if path and os.path.isfile(path):
        return Path(path).read_text().strip()  # type: ignore[name-defined]
    return os.environ.get("HICLAW_WORKER_API_KEY", "").strip()


def build_credential(
    auth_type: str,
    server_addr: str,
    namespace: str,
    username: str,
    password: str,
    controller_url: str,
) -> NacosCredential:
    auth = auth_type or NACOS_AUTH_NONE
    if auth == NACOS_AUTH_NONE:
        if username and password:
            auth = NACOS_AUTH_NACOS
        else:
            return NoneCredential()
    if auth == NACOS_AUTH_NACOS:
        if not username or not password:
            raise RuntimeError("nacos auth requires username and password")
        return UserPassCredential(server_addr, username, password)
    if auth == NACOS_AUTH_STS:
        return STSCredential(namespace, controller_url)
    raise RuntimeError(f"unsupported NACOS_AUTH_TYPE: {auth_type!r}")
