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
