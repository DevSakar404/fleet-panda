"""Who is asking, and what they are allowed to see.

Owned by: the agent layer. Called by the router, the SQL agent, the guard and the
executor. Calls: `config` only.

Built in Step 2 rather than Step 3 because the guard cannot be written without it
-- "inject tenant_id when a TenantContext is bound" needs the thing that is bound.

The distinction this type carries is the one CLAUDE.md section 9 asks to be
explicit rather than implicit: some questions ("which tenant delivered the most
gallons") only make sense across every tenant, and some sessions are not allowed
to ask them. Modelling that as a scope on the session, rather than as a special
case inside the SQL agent, means the guard has a single unambiguous input: either
a tenant id is bound and every table gets a predicate, or none is and the query is
allowed to range.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src import config


class SessionScope(str, Enum):
    """The two kinds of session this agent serves."""

    # A support rep or CSM working one account. Every query is filtered to that
    # tenant and cross-tenant questions are refused, not silently narrowed.
    TENANT = "tenant"
    # An internal FleetPanda operator asking platform-wide questions. No predicate
    # is injected. This scope must never be reachable from an end-customer path.
    PLATFORM = "platform"


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The authority under which a query runs.

    Frozen on purpose: a session's scope must not be widened halfway through a
    request by any code that happens to hold a reference to it.
    """

    scope: SessionScope
    tenant_id: int | None = None

    def __post_init__(self) -> None:
        # A tenant-scoped session without a tenant is the dangerous shape -- it
        # would look bound to the guard while carrying nothing to filter on. Fail
        # at construction rather than let it reach the guard.
        if self.scope is SessionScope.TENANT and self.tenant_id is None:
            raise ValueError("A TENANT-scoped session requires a tenant_id")
        if self.scope is SessionScope.PLATFORM and self.tenant_id is not None:
            raise ValueError("A PLATFORM-scoped session must not carry a tenant_id")

    @classmethod
    def for_tenant(cls, tenant_id: int) -> "TenantContext":
        return cls(SessionScope.TENANT, tenant_id)

    @classmethod
    def platform(cls) -> "TenantContext":
        """An unscoped internal session. Constructed explicitly and never by
        default, so that widening authority is always a visible act in the code."""
        return cls(SessionScope.PLATFORM, None)

    @property
    def is_bound(self) -> bool:
        """True when the guard must inject a tenant predicate."""
        return self.scope is SessionScope.TENANT

    def allows_question(self, question_number: int) -> bool:
        """Whether one of the eight graded questions may run in this session.

        Questions 1, 2, 7 and 8 range over every tenant by construction. Answering
        them inside a tenant-scoped session would return one tenant's rows and
        present them as a platform-wide ranking -- a wrong answer that looks
        right, which is worse than a refusal. (CLAUDE.md section 9 lists only
        {1, 7}; see OPEN_QUESTIONS.md Q-001.)
        """
        if self.scope is SessionScope.PLATFORM:
            return True
        return question_number not in config.CROSS_TENANT_QUESTIONS
