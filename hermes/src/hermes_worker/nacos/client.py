"""HTTP client for Nacos AI Registry v3 APIs."""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from hermes_worker.nacos.auth import NacosCredential, build_credential
from hermes_worker.nacos.config import NacosConfig

logger = logging.getLogger(__name__)


class NacosAPIError(RuntimeError):
    def __init__(self, operation: str, status: int, message: str) -> None:
        super().__init__(f"{operation} failed (HTTP {status}): {message}")
        self.status = status
        self.message = message


class NacosClient:
    def __init__(self, config: NacosConfig) -> None:
        if not config.server_addr:
            raise ValueError("Nacos server address is not configured")
        self.config = config
        self._http = httpx.AsyncClient(timeout=60.0)
        self._cred: NacosCredential = build_credential(
            config.auth_type,
            config.server_addr,
            config.namespace,
            config.username,
            config.password,
            config.controller_url,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _prepare(self, req: httpx.Request) -> None:
        await self._cred.refresh(self._http)
        self._cred.apply(req)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, str]] = None,
        data: Optional[dict[str, str]] = None,
        json_body: Optional[Any] = None,
        raw: bool = False,
        operation: str = "nacos request",
    ) -> Any:
        url = f"{self.config.base_url}{path}"
        req = self._http.build_request(
            method, url, params=params, data=data, json=json_body
        )
        await self._prepare(req)
        resp = await self._http.send(req)
        if raw:
            if resp.status_code != 200:
                raise NacosAPIError(operation, resp.status_code, resp.text[:500])
            return resp.content
        if resp.status_code != 200:
            raise NacosAPIError(operation, resp.status_code, resp.text[:500])
        if not resp.content:
            return None
        body = resp.json()
        if isinstance(body, dict) and "code" in body:
            code = body.get("code")
            if code not in (0, None, "0"):
                msg = body.get("message", str(body))
                raise NacosAPIError(operation, resp.status_code, str(msg))
            return body.get("data")
        return body

    async def get_json(
        self,
        path: str,
        params: dict[str, str],
        *,
        operation: str,
    ) -> Any:
        p = dict(params)
        p.setdefault("namespaceId", self.config.namespace)
        return await self.request("GET", path, params=p, operation=operation)

    async def post_form(
        self,
        path: str,
        data: dict[str, str],
        *,
        operation: str,
    ) -> Any:
        d = dict(data)
        d.setdefault("namespaceId", self.config.namespace)
        return await self.request("POST", path, data=d, operation=operation)

    async def download(
        self,
        path: str,
        params: dict[str, str],
        *,
        operation: str,
    ) -> bytes:
        p = dict(params)
        p.setdefault("namespaceId", self.config.namespace)
        content = await self.request(
            "GET", path, params=p, raw=True, operation=operation
        )
        return bytes(content)
