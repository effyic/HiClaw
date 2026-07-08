"""Hook loading and execution errors."""
from __future__ import annotations


class HookLoadError(RuntimeError):
    """Raised when hook modules cannot be loaded from the hooks directory."""


class HookExecutionError(RuntimeError):
    """Raised when a loaded hook fails during execution."""

    def __init__(self, hook_name: str, message: str) -> None:
        self.hook_name = hook_name
        super().__init__(f"Hook '{hook_name}' failed: {message}")
