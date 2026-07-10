"""Request filter helpers using optional extension hooks."""
from __future__ import annotations

import os
from typing import Any

from agno_worker.hooks.errors import HookExecutionError
from agno_worker.hooks.protocols import UserContext
from agno_worker.hooks.registry import HookRegistry


def require_tenant_id_enabled() -> bool:
    return os.environ.get("AGNO_REQUIRE_TENANT_ID", "false").lower() in (
        "1",
        "true",
        "yes",
    )


def resolve_effective_tenant_id(
    user_context: UserContext,
    metadata: dict[str, Any] | None = None,
) -> str:
    meta = metadata or {}
    return str(user_context.tenant_id or meta.get("tenant_id") or "").strip()


class RequestRejectedError(HookExecutionError):
    """Raised when request_pre_filter_hook blocks a request."""

    def __init__(self, reason: str) -> None:
        super().__init__("request_pre_filter_hook", reason)
        self.reason = reason


class RequestFilterPipeline:
    """Apply optional pre/post request filters from extension hooks."""

    def __init__(self, registry: HookRegistry) -> None:
        self.registry = registry

    def apply_pre_filter(
        self,
        user_context: UserContext,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta = dict(metadata or {})
        if require_tenant_id_enabled() and not resolve_effective_tenant_id(
            user_context, meta
        ):
            raise RequestRejectedError("tenant_id is required")

        if not self.registry.has("request_pre_filter_hook"):
            return meta

        result = self.registry.call(
            "request_pre_filter_hook",
            user_context,
            meta,
        )
        if not isinstance(result, dict):
            return meta

        if result.get("allowed") is False:
            reason = str(result.get("reason") or "request rejected by pre-filter hook")
            raise RequestRejectedError(reason)

        filtered_meta = result.get("metadata")
        if isinstance(filtered_meta, dict):
            meta.update(filtered_meta)
        elif "metadata" not in result:
            meta.update({k: v for k, v in result.items() if k not in ("allowed", "reason")})
        return meta

    def apply_post_filter(
        self,
        user_context: UserContext,
        run_output: dict[str, Any],
        run_context: Any = None,
    ) -> dict[str, Any]:
        output = dict(run_output)
        if not self.registry.has("request_post_filter_hook"):
            return output

        result = self.registry.call(
            "request_post_filter_hook",
            user_context,
            output,
            run_context,
        )
        if isinstance(result, dict):
            output.update(result)
        return output
