"""Tenant data access — standard logic lives in agno_worker.tenant."""

from agno_worker.tenant.data import TenantDataProvider

__all__ = ["TenantDataProvider"]

# Backward compatibility alias
MySQLDataContextProvider = TenantDataProvider
