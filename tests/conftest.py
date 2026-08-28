"""Shared fixtures.

Every fixture is session-scoped and read-only. The data files are fixtures in the
literal sense -- they never change during a run -- so building the repository once
and sharing it costs nothing and keeps the suite fast.
"""

from __future__ import annotations

import pytest

from src.data.repository import Repository
from src.data.resolver import TenantResolver


@pytest.fixture(scope="session")
def repository() -> Repository:
    """Unified read access to the five JSON sources."""
    return Repository()


@pytest.fixture(scope="session")
def resolver() -> TenantResolver:
    """Tenant name -> tenant_id, with the production threshold."""
    return TenantResolver()


@pytest.fixture(scope="session")
def guard():
    """The AST guard with the production allowlist."""
    from src.db.guard import SqlGuard

    return SqlGuard()


@pytest.fixture(scope="session")
def executor():
    """Query executor over the real read-only dispatch database."""
    from src.db.executor import QueryExecutor

    return QueryExecutor()


@pytest.fixture(scope="session")
def all_tenant_ids() -> tuple[int, ...]:
    """The twelve real tenant ids, from customers.json."""
    from src.data.loaders import load_tenants

    return tuple(t.tenant_id for t in load_tenants())
