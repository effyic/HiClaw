"""策略快照客户端：内存缓存 + JSON 落盘 + 后台组合版本刷新。

- 启动时确保缓存目录存在（``makedirs(mode=0o700)``），不可写时降级为纯内存
  缓存并告警；先加载落盘缓存（损坏 JSON 视为无缓存并删除自愈）。
- 原子写入：临时文件 + fsync + ``os.replace``，避免刷新中途崩溃留下半截 JSON。
- 后台任务按 ``SENSITIVE_CONTENT_REFRESH_INTERVAL``（默认 30s）带
  ``If-None-Match`` 刷新；304 表示内容未变，仅续期 ``fetched_at``。
- 快照超过 ``SENSITIVE_CONTENT_MAX_STALE``（默认 10 分钟）视为无有效快照，
  与"完全无快照"一样按 ``SENSITIVE_CONTENT_FAIL_MODE`` 处理（默认 open）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any

import httpx

from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.detector import CompiledPolicy, compile_policy
from agno_worker.moderation.models import PolicySnapshot

logger = logging.getLogger(__name__)


class SnapshotClient:
    """按租户维护策略快照（内存 + 落盘），并提供编译后的可执行策略。"""

    def __init__(self, config: ModerationConfig) -> None:
        self._config = config
        self._snapshots: dict[str, PolicySnapshot] = {}
        self._policies: dict[str, CompiledPolicy] = {}
        self._known_tenants: set[str] = set()
        self._disk_enabled = True
        self._refresh_task: asyncio.Task | None = None
        self._stopping = False
        self._stale_warned: set[str] = set()
        self._ensure_cache_dir()
        self._load_from_disk()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def get_policy(self, tenant_id: str) -> CompiledPolicy | None:
        """获取租户的有效编译策略；无有效快照（含过期超限）返回 None。

        纯内存读取，不发起网络请求；刷新由后台任务负责。
        """
        tenant_id = tenant_id or ""
        self._known_tenants.add(tenant_id)
        snapshot = self._snapshots.get(tenant_id)
        if snapshot is None:
            return None
        if snapshot.is_stale(self._config.max_stale):
            if tenant_id not in self._stale_warned:
                self._stale_warned.add(tenant_id)
                logger.warning(
                    "租户 %s 的策略快照已超过过期上限 %.0fs（版本 %s），视为无有效快照",
                    tenant_id,
                    self._config.max_stale,
                    snapshot.version,
                )
            return None
        return self._policies.get(tenant_id)

    async def fetch(self, tenant_id: str, *, client: httpx.AsyncClient | None = None) -> bool:
        """立即刷新一个租户的快照；返回是否仍持有有效快照。"""
        tenant_id = tenant_id or ""
        self._known_tenants.add(tenant_id)
        own_client = client is None
        http = client or httpx.AsyncClient(timeout=10.0)
        try:
            await self._refresh_tenant(http, tenant_id)
        finally:
            if own_client:
                await http.aclose()
        snapshot = self._snapshots.get(tenant_id)
        return snapshot is not None and not snapshot.is_stale(self._config.max_stale)

    def ensure_background_refresh(self) -> None:
        """在事件循环内启动后台刷新任务（幂等）。"""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.get_running_loop().create_task(
                self._refresh_loop()
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
            self._refresh_task = None

    # ------------------------------------------------------------------
    # 落盘缓存
    # ------------------------------------------------------------------

    def _ensure_cache_dir(self) -> None:
        cache_dir = os.path.dirname(self._config.cache_path) or "."
        try:
            os.makedirs(cache_dir, mode=0o700, exist_ok=True)
            # 验证目录确实可写（目录已存在但只读的场景）
            probe = os.path.join(cache_dir, ".write-probe")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("")
            os.remove(probe)
        except OSError as exc:
            self._disk_enabled = False
            logger.warning(
                "敏感内容快照缓存目录 %s 不可写（%s），降级为纯内存缓存",
                cache_dir,
                exc,
            )

    def _load_from_disk(self) -> None:
        if not self._disk_enabled or not os.path.exists(self._config.cache_path):
            return
        try:
            with open(self._config.cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            tenants = data.get("tenants")
            if not isinstance(tenants, dict):
                raise ValueError("invalid cache structure")
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            # 损坏的缓存文件自愈：删除并视为无缓存
            logger.warning(
                "敏感内容快照缓存文件损坏（%s），已删除并重新拉取", exc
            )
            try:
                os.remove(self._config.cache_path)
            except OSError:
                pass
            return
        for tenant_id, payload in tenants.items():
            try:
                snapshot = PolicySnapshot.from_payload(str(tenant_id), payload)
                self._install_snapshot(snapshot, persist=False)
            except Exception as exc:
                logger.warning("租户 %s 的落盘快照解析失败：%s", tenant_id, exc)

    def _persist_to_disk(self) -> None:
        if not self._disk_enabled:
            return
        data = {
            "tenants": {
                tenant_id: snapshot.to_payload()
                for tenant_id, snapshot in self._snapshots.items()
            }
        }
        cache_dir = os.path.dirname(self._config.cache_path) or "."
        try:
            # 原子写入：同目录临时文件 + fsync + os.replace
            fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self._config.cache_path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        except OSError as exc:
            logger.warning("敏感内容快照落盘失败：%s", exc)

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------

    async def _refresh_loop(self) -> None:
        while not self._stopping:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    for tenant_id in sorted(self._known_tenants):
                        try:
                            await self._refresh_tenant(client, tenant_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            # 管理服务不可用：沿用最后有效快照
                            logger.warning(
                                "刷新租户 %s 策略快照失败（沿用缓存）：%s",
                                tenant_id,
                                exc,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("敏感内容快照刷新循环异常：%s", exc)
            await asyncio.sleep(self._config.refresh_interval)

    async def _refresh_tenant(self, client: httpx.AsyncClient, tenant_id: str) -> None:
        url = (
            f"{self._config.service_url}/internal/v1/tenants/"
            f"{tenant_id or 'global'}/policy-snapshot"
        )
        headers: dict[str, str] = {}
        if self._config.runtime_token:
            headers["Authorization"] = f"Bearer {self._config.runtime_token}"
        cached = self._snapshots.get(tenant_id)
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag
        response = await client.get(url, headers=headers)
        if response.status_code == 304:
            # 内容未变：仅续期，保持有效
            if cached is not None:
                cached.fetched_at = time.time()
                self._stale_warned.discard(tenant_id)
                self._persist_to_disk()
            return
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        snapshot = PolicySnapshot.from_payload(tenant_id, payload)
        snapshot.fetched_at = time.time()
        etag = response.headers.get("etag", "")
        if etag and not snapshot.etag:
            snapshot.etag = etag
        old_version = cached.version if cached else "<none>"
        self._install_snapshot(snapshot, persist=True)
        if old_version != snapshot.version:
            logger.info(
                "租户 %s 策略快照更新：%s -> %s（规则 %d 条）",
                tenant_id,
                old_version,
                snapshot.version,
                len(snapshot.rules),
            )

    def _install_snapshot(self, snapshot: PolicySnapshot, *, persist: bool) -> None:
        self._snapshots[snapshot.tenant_id] = snapshot
        self._policies[snapshot.tenant_id] = compile_policy(snapshot)
        self._known_tenants.add(snapshot.tenant_id)
        self._stale_warned.discard(snapshot.tenant_id)
        if persist:
            self._persist_to_disk()
