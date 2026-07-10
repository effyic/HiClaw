"""Backward-compatible re-export — use agno_worker.tenant.data.TenantDataProvider."""
from agno_worker.tenant.data import TenantDataProvider

MySQLDataContextProvider = TenantDataProvider

__all__ = ["MySQLDataContextProvider", "TenantDataProvider"]
