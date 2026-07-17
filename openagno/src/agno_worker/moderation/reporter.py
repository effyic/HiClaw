"""命中事件上报：有界异步队列 + 批量 POST + HMAC 指纹。

- 队列有界（默认 maxsize 1000），队满丢弃并累计丢弃计数（定期告警日志）。
- 批量上报 ``POST {service}/internal/v1/hit-events:batch``，``event_id=uuid4``
  幂等；失败按指数退避重试，超过最大次数丢弃该批并计数。
- ``request_fingerprint`` / ``session_fingerprint`` 用 HMAC-SHA256（密钥
  ``SENSITIVE_CONTENT_FINGERPRINT_KEY``）生成，事件不含用户原文与规则明文。
- ``session_id`` 明文随事件上报（产品决策）：后台从命中事件跳转查看完整
  会话记录，与 ``session_fingerprint`` 并存；用户原文仍不上报。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.models import GuardrailDecision

logger = logging.getLogger(__name__)


def hmac_fingerprint(key: str, value: str) -> str:
    """HMAC-SHA256 指纹（不用裸 SHA，防止低熵标识被彩虹表还原）。"""
    if not value:
        return ""
    return hmac.new(
        key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


@dataclass
class HitEvent:
    """单条命中事件（一次请求命中多条规则时每条各一行）。"""

    event_id: str
    rule_id: int
    type_id: int
    rule_action: str
    final_action: str
    selected: bool
    final_rule_id: int
    tenant_id: str
    request_fingerprint: str
    session_fingerprint: str
    # 明文会话标识（供后台跳转查看会话记录，与指纹并存）
    session_id: str
    policy_version: str
    hit_count: int
    hit_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "rule_id": self.rule_id,
            "type_id": self.type_id,
            "rule_action": self.rule_action,
            "final_action": self.final_action,
            "selected": self.selected,
            "final_rule_id": self.final_rule_id,
            "tenant_id": self.tenant_id,
            "request_fingerprint": self.request_fingerprint,
            "session_fingerprint": self.session_fingerprint,
            "session_id": self.session_id,
            "policy_version": self.policy_version,
            "hit_count": self.hit_count,
            "hit_at": self.hit_at,
        }


def build_hit_events(
    decision: GuardrailDecision,
    *,
    tenant_id: str,
    request_id: str,
    session_id: str,
    fingerprint_key: str,
) -> list[HitEvent]:
    """由决策构建命中事件：每条命中一条事件，仅排序第一条 ``selected=TRUE``。

    ``request_fingerprint`` 从独立 ``request_id``（uuid4）派生，不得从
    ``session_id`` 派生；``session_fingerprint`` 才由 ``session_id`` 派生。
    ``session_id`` 明文同时随事件携带（后台会话跳转用）。
    """
    request_fp = hmac_fingerprint(fingerprint_key, request_id)
    session_fp = hmac_fingerprint(fingerprint_key, session_id)
    final_action = decision.action.value
    final_rule_id = decision.rule_id
    events: list[HitEvent] = []
    for index, match in enumerate(decision.matches):
        events.append(
            HitEvent(
                event_id=str(uuid.uuid4()),
                rule_id=match.rule_id,
                type_id=match.type_id,
                rule_action=match.action.value,
                final_action=final_action,
                selected=index == 0,
                final_rule_id=final_rule_id,
                tenant_id=tenant_id,
                request_fingerprint=request_fp,
                session_fingerprint=session_fp,
                session_id=session_id,
                policy_version=decision.policy_version,
                hit_count=match.hit_count,
            )
        )
    return events


class HitReporter:
    """异步批量上报器。``enqueue()`` 线程内非阻塞入队；后台任务批量发送。"""

    def __init__(
        self,
        config: ModerationConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._queue: asyncio.Queue[HitEvent] = asyncio.Queue(
            maxsize=config.reporter_queue_size
        )
        self._client = client
        self._own_client = client is None
        self._task: asyncio.Task | None = None
        self._dropped_count = 0
        self._last_drop_log = 0.0
        self._stopping = False

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def enqueue(self, events: list[HitEvent]) -> int:
        """入队命中事件，返回实际入队条数；队满丢弃并计数。"""
        accepted = 0
        for event in events:
            try:
                self._queue.put_nowait(event)
                accepted += 1
            except asyncio.QueueFull:
                self._dropped_count += 1
        dropped = len(events) - accepted
        if dropped:
            now = time.monotonic()
            # 丢弃告警限频，避免队满时日志风暴
            if now - self._last_drop_log > 10:
                self._last_drop_log = now
                logger.warning(
                    "命中事件上报队列已满，本次丢弃 %d 条（累计丢弃 %d 条）",
                    dropped,
                    self._dropped_count,
                )
        return accepted

    def ensure_started(self) -> None:
        """在事件循环内启动后台上报任务（幂等）。"""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        while not self._stopping:
            batch = await self._collect_batch()
            if batch:
                await self._send_with_retry(batch)

    async def _collect_batch(self) -> list[HitEvent]:
        """阻塞等待首条事件，然后在极短窗口内聚合成批。"""
        first = await self._queue.get()
        batch = [first]
        while len(batch) < self._config.reporter_batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _send_with_retry(self, batch: list[HitEvent]) -> None:
        delay = 0.5
        for attempt in range(1, self._config.reporter_max_retries + 1):
            try:
                await self._post(batch)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "命中事件批量上报失败（第 %d/%d 次）：%s",
                    attempt,
                    self._config.reporter_max_retries,
                    exc,
                )
                if attempt == self._config.reporter_max_retries:
                    self._dropped_count += len(batch)
                    logger.error(
                        "命中事件批量上报重试耗尽，丢弃 %d 条（累计丢弃 %d 条）",
                        len(batch),
                        self._dropped_count,
                    )
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def _post(self, batch: list[HitEvent]) -> None:
        client = self._get_client()
        url = f"{self._config.service_url}/internal/v1/hit-events:batch"
        headers = {}
        if self._config.runtime_token:
            headers["Authorization"] = f"Bearer {self._config.runtime_token}"
        response = await client.post(
            url,
            json={"events": [event.to_payload() for event in batch]},
            headers=headers,
        )
        response.raise_for_status()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client
