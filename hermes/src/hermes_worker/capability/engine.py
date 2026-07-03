"""Orchestrate F1–F4 capability subsystems."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from hermes_worker.capability.control import ControlServer
from hermes_worker.capability.reloader import ReloadScope, RuntimeReloader
from hermes_worker.capability.store import CapabilityStore
from hermes_worker.capability.watcher import CapabilityWatcher
from hermes_worker.nacos.agent_registry import AgentRegistry
from hermes_worker.nacos.client import NacosClient
from hermes_worker.nacos.config import NacosConfig, load_nacos_config
from hermes_worker.nacos import mcp as mcp_mod
from hermes_worker.nacos import skill as skill_mod
from hermes_worker.sync import FileSync, push_local

logger = logging.getLogger(__name__)


class CapabilityEngine:
    """Unified engine: Nacos client, store, control port, watcher, registry."""

    def __init__(
        self,
        worker_name: str,
        sync: FileSync,
        hermes_home: Path,
        *,
        sync_skills: Callable[[], None],
        copy_mcporter: Callable[[], None],
        rebridge_prompts: Callable[[], Awaitable[None]],
        gateway_running: Callable[[], bool],
    ) -> None:
        self._worker_name = worker_name
        self._sync = sync
        self._hermes_home = hermes_home
        self._sync_skills = sync_skills
        self._copy_mcporter = copy_mcporter
        self._rebridge_prompts = rebridge_prompts
        self._gateway_running = gateway_running
        self._config = load_nacos_config(worker_name)
        self._client: Optional[NacosClient] = None
        self._store = CapabilityStore(
            sync.local_dir,
            hermes_home,
            push_callback=self._push_state,
        )
        self._reloader = RuntimeReloader(
            sync_skills=sync_skills,
            copy_mcporter=copy_mcporter,
            rebridge_prompts=rebridge_prompts,
            on_applied=self._after_reload,
        )
        self._control: Optional[ControlServer] = None
        self._registry: Optional[AgentRegistry] = None
        self._watcher: Optional[CapabilityWatcher] = None
        self._mcporter_cache: dict[str, dict[str, Any]] = {}
        self._prompt_hashes: dict[str, str] = {}

    @property
    def store(self) -> CapabilityStore:
        return self._store

    @property
    def config(self) -> NacosConfig:
        return self._config

    def protected_skills(self) -> set[str]:
        return self._store.protected_skill_names()

    async def start(self) -> None:
        self._store.load()
        self._store.update_prompt_hashes()
        self._snapshot_prompt_hashes()

        if self._config.enabled and self._config.server_addr:
            self._client = NacosClient(self._config)
            self._start_control()
            await self.start_control_server()
            await self._bootstrap()
            if self._config.agent_register:
                self._registry = AgentRegistry(
                    self._client,
                    self._config,
                    self._control_url(),
                    self._capabilities_snapshot,
                )
                await self._registry.register()
                self._registry.start_heartbeat()
            if self._config.watch_enabled:
                self._watcher = CapabilityWatcher(
                    self._client,
                    self._config,
                    get_state=self._store.snapshot,
                    on_skill_update=self._watch_skill_update,
                    on_mcp_update=self._watch_mcp_update,
                    on_prompt_change=self._watch_prompt_change,
                    prompt_hash_check=self._prompts_changed,
                )
                self._watcher.start()
        else:
            self._start_control()
            logger.info(
                "Nacos capability engine: control port only (mode=%s)",
                self._config.capability_mode,
            )
            await self.start_control_server()

    async def stop(self) -> None:
        if self._watcher:
            await self._watcher.stop()
        if self._registry:
            await self._registry.stop()
        if self._control:
            await self._control.stop()
        if self._client:
            await self._client.close()

    async def _bootstrap(self) -> None:
        assert self._client is not None
        for name, version, label in self._store.bootstrap_specs(
            self._config.bootstrap_skills
        ):
            await self.install_skill(
                {"name": name, "version": version, "label": label}
            )
        for mcp_name in self._config.mcp_names:
            await self.mount_mcp({"name": mcp_name})
        await self._reloader.reload(ReloadScope.ALL)

    def _start_control(self) -> None:
        self._control = ControlServer(
            self._config.control_bind,
            self._config.control_port,
            self._config.control_token,
            {
                "status": self._api_status,
                "capabilities": self._api_capabilities,
                "install_skill": self._api_install_skill,
                "uninstall_skill": self._api_uninstall_skill,
                "mount_mcp": self._api_mount_mcp,
                "unmount_mcp": self._api_unmount_mcp,
                "reload_prompts": self._api_reload_prompts,
                "reregister_agent": self._api_reregister,
            },
        )

    def _control_url(self) -> str:
        if self._control:
            return self._control.base_url
        host = self._config.control_bind
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return f"http://{host}:{self._config.control_port}"

    async def start_control_server(self) -> None:
        if self._control:
            await self._control.start()

    def _push_state(self) -> None:
        try:
            push_local(self._sync)
        except Exception as exc:
            logger.debug("capability-state push: %s", exc)

    def _capabilities_snapshot(self) -> dict[str, Any]:
        snap = self._store.snapshot()
        return {
            "skills": snap.get("skills", {}),
            "mcp": snap.get("mcp", {}),
            "team": "",
        }

    async def _after_reload(self, scope: ReloadScope) -> None:
        if self._registry:
            await self._registry.refresh_capabilities()

    async def install_skill(
        self, body: dict[str, Any], _params: Optional[dict] = None
    ) -> dict[str, Any]:
        if not self._client:
            raise ValueError("Nacos client not configured")
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        version = body.get("version")
        label = body.get("label")
        ver = await skill_mod.download_skill(
            self._client,
            name,
            self._hermes_home / "skills",
            version=str(version) if version else None,
            label=str(label) if label else None,
        )
        self._store.record_skill(name, version=ver, label=label)
        self._store.sync_skill_to_workspace(name)
        await self._reloader.reload(ReloadScope.SKILLS)
        return {"name": name, "version": ver}

    async def uninstall_skill(
        self, _body: dict[str, Any], params: dict[str, str]
    ) -> dict[str, Any]:
        name = params.get("name", "").strip()
        if not name:
            raise ValueError("name is required")
        self._store.remove_skill(name)
        await self._reloader.reload(ReloadScope.SKILLS)
        return {"removed": name}

    async def mount_mcp(
        self, body: dict[str, Any], _params: Optional[dict] = None
    ) -> dict[str, Any]:
        if not self._client:
            raise ValueError("Nacos client not configured")
        discover = str(body.get("discover", "")).strip()
        if discover:
            items = await mcp_mod.list_mcp_servers(self._client, name=discover)
            mounted = []
            for item in items:
                mcp_name = item.get("mcpName") or item.get("name")
                if mcp_name:
                    await self.mount_mcp({"name": str(mcp_name)})
                    mounted.append(mcp_name)
            return {"mounted": mounted}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValueError("name or discover is required")
        detail = await mcp_mod.get_mcp_server(self._client, name)
        mapped = mcp_mod.mcp_to_mcporter_entry(detail, self._config)
        if not mapped:
            raise ValueError(f"cannot map MCP {name} to mcporter entry")
        mcp_name, entry = mapped
        self._mcporter_cache[mcp_name] = entry
        meta = detail.get("metadata") or {}
        route = meta.get("higressRoute") or meta.get("higress_route") or mcp_name
        version = str(detail.get("version") or "")
        self._store.record_mcp(
            mcp_name, higress_route=str(route), version=version
        )
        self._store.write_mcporter(
            {"mcpServers": self._mcporter_cache},
            self._sync.local_dir / "mcporter-servers.json",
            hybrid=self._config.capability_mode == "hybrid",
        )
        await self._reloader.reload(ReloadScope.MCP)
        return {"name": mcp_name, "higressRoute": route}

    async def unmount_mcp(
        self, _body: dict[str, Any], params: dict[str, str]
    ) -> dict[str, Any]:
        name = params.get("name", "").strip()
        self._mcporter_cache.pop(name, None)
        self._store.remove_mcp(name)
        self._store.write_mcporter(
            {"mcpServers": self._mcporter_cache},
            self._sync.local_dir / "mcporter-servers.json",
            hybrid=self._config.capability_mode == "hybrid",
        )
        await self._reloader.reload(ReloadScope.MCP)
        return {"removed": name}

    async def _watch_skill_update(
        self, name: str, version: str, label: Optional[str]
    ) -> None:
        await self.install_skill(
            {"name": name, "version": version, "label": label}
        )

    async def _watch_mcp_update(self, name: str) -> None:
        await self.mount_mcp({"name": name})

    async def _watch_prompt_change(self) -> None:
        self._store.update_prompt_hashes()
        self._snapshot_prompt_hashes()
        await self._reloader.reload(ReloadScope.PROMPTS)

    def _snapshot_prompt_hashes(self) -> None:
        for fname in ("SOUL.md", "AGENTS.md"):
            for base in (self._sync.local_dir, self._hermes_home):
                p = base / fname
                if p.exists():
                    self._prompt_hashes[fname] = p.read_text(
                        errors="replace"
                    )
                    break

    def _prompts_changed(self) -> bool:
        for fname in ("SOUL.md", "AGENTS.md"):
            for base in (self._sync.local_dir, self._hermes_home):
                p = base / fname
                if not p.exists():
                    continue
                content = p.read_text(errors="replace")
                if self._prompt_hashes.get(fname) != content:
                    return True
        return False

    async def on_files_pulled(self, pulled: list[str]) -> None:
        if any(f in pulled for f in ("SOUL.md", "AGENTS.md")):
            await self._watch_prompt_change()
        if any(f.startswith("skills/") for f in pulled):
            await self._reloader.reload(ReloadScope.SKILLS)
        if "config/mcporter.json" in pulled:
            await self._reloader.reload(ReloadScope.MCP)

    async def _api_status(
        self, _body: dict, _params: dict
    ) -> dict[str, Any]:
        return {
            "worker": self._worker_name,
            "gateway": "running" if self._gateway_running() else "starting",
            "agentCardOnline": bool(self._registry and self._registry.online),
            "controlUrl": self._control_url(),
            "capabilities": self._store.snapshot(),
            **self._reloader.status_extra(),
        }

    async def _api_capabilities(
        self, _body: dict, _params: dict
    ) -> dict[str, Any]:
        return self._store.snapshot()

    async def _api_install_skill(self, body: dict, _params: dict) -> dict:
        return await self.install_skill(body)

    async def _api_uninstall_skill(self, _body: dict, params: dict) -> dict:
        return await self.uninstall_skill(_body, params)

    async def _api_mount_mcp(self, body: dict, _params: dict) -> dict:
        return await self.mount_mcp(body)

    async def _api_unmount_mcp(self, _body: dict, params: dict) -> dict:
        return await self.unmount_mcp(_body, params)

    async def _api_reload_prompts(self, _body: dict, _params: dict) -> dict:
        await self._watch_prompt_change()
        return {"ok": True}

    async def _api_reregister(self, _body: dict, _params: dict) -> dict:
        if self._registry:
            await self._registry.register()
        return {"ok": True}
