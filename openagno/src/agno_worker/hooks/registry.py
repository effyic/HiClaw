"""Load hook callables from PVC-mounted directory; fail fast on any error."""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from agno_worker.hooks.errors import HookExecutionError, HookLoadError
from agno_worker.hooks.protocols import HOOK_NAMES, HookFn, HookSet

logger = logging.getLogger(__name__)

_HOOK_MODULES = ("hooks", "prompt", "mcp", "session", "data")


class HookRegistry:
    """Resolve hooks exclusively from the external hooks directory."""

    def __init__(self, hooks_dir: Path | None = None) -> None:
        self.hooks_dir = hooks_dir or Path("/etc/hiclaw/hooks")
        self._hooks: HookSet = HookSet()
        self._sources: dict[str, str] = {}
        self.reload()

    @property
    def hooks(self) -> HookSet:
        return self._hooks

    @property
    def sources(self) -> dict[str, str]:
        return dict(self._sources)

    def reload(self) -> None:
        if not self.hooks_dir.is_dir():
            raise HookLoadError(f"Hooks directory not found: {self.hooks_dir}")

        resolved: dict[str, HookFn] = {}
        sources: dict[str, str] = {}
        missing: list[str] = []

        for name in HOOK_NAMES:
            fn, source = self._resolve_hook(name)
            if fn is None:
                missing.append(name)
                continue
            resolved[name] = fn
            sources[name] = source

        if missing:
            raise HookLoadError(
                f"Missing hook implementations in {self.hooks_dir}: {', '.join(missing)}"
            )

        self._hooks = HookSet(hooks=resolved)
        self._sources = sources
        logger.info(
            "Hook registry loaded (%d hooks, dir=%s)",
            len(resolved),
            self.hooks_dir,
        )

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        fn = self._hooks.get(name)
        if fn is None:
            raise HookLoadError(f"Hook not loaded: {name}")
        try:
            return fn(*args, **kwargs)
        except HookExecutionError:
            raise
        except Exception as exc:
            raise HookExecutionError(name, str(exc)) from exc

    def _resolve_hook(self, name: str) -> tuple[HookFn | None, str]:
        for module_name in _HOOK_MODULES:
            module_path = self.hooks_dir / f"{module_name}.py"
            if not module_path.is_file():
                continue
            module = self._import_module(module_path, f"agno_ext_hooks_{module_name}")
            fn = getattr(module, name, None)
            if callable(fn):
                logger.debug("Loaded hook %s from %s", name, module_path)
                return fn, str(module_path)

        init_path = self.hooks_dir / "__init__.py"
        if init_path.is_file():
            module = self._import_module(init_path, "agno_ext_hooks_pkg")
            fn = getattr(module, name, None)
            if callable(fn):
                return fn, str(init_path)

        return None, ""

    def _import_module(self, path: Path, module_name: str) -> Any:
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise HookLoadError(f"Cannot import hook module: {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            return module
        except HookLoadError:
            raise
        except Exception as exc:
            raise HookLoadError(f"Failed to import hook module {path}: {exc}") from exc


def hooks_directory_fingerprint(hooks_dir: Path) -> str:
    """Hash hook files for hot-reload detection."""
    import hashlib

    if not hooks_dir.is_dir():
        return ""
    h = hashlib.sha256()
    for path in sorted(hooks_dir.rglob("*.py")):
        if not path.is_file():
            continue
        rel = path.relative_to(hooks_dir).as_posix()
        stat = path.stat()
        h.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}\n".encode())
    return h.hexdigest()[:16]
