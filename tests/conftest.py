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


class FakeLLM:
    """A scripted stand-in for `LLMClient`.

    The suite runs without an API key and without a network call, so every test
    that exercises the agent drives it with one of these. It hands back queued
    replies in order and records what it was asked, which is how the retry and
    refusal paths are asserted.

    A fake rather than a mock: it implements the real `complete()` signature and
    returns a real `LLMResponse`, so a change to that interface breaks these tests
    loudly instead of letting them keep passing against a shape that no longer
    exists.
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, str]] = []

    def complete(self, system: str, user: str, **kwargs) -> "LLMResponse":  # noqa: F821
        from src.llm.client import LLMResponse

        self.calls.append({"system": system, "user": user})
        if not self.replies:
            raise AssertionError(
                f"FakeLLM ran out of scripted replies on call {len(self.calls)}. "
                f"Last prompt: {user[:200]}"
            )
        return LLMResponse(
            text=self.replies.pop(0), input_tokens=0, output_tokens=0, model="fake"
        )


def sql_reply(sql: str, is_cross_tenant: bool = False, assumptions: str = "") -> str:
    """A well-formed generation reply, as the model is asked to produce."""
    import json

    return json.dumps(
        {"sql": sql, "is_cross_tenant": is_cross_tenant, "assumptions": assumptions}
    )


@pytest.fixture
def fake_llm():
    """Factory so a test can script its own replies."""
    return FakeLLM
