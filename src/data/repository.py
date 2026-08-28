"""One read API over every source, with tenant filtering applied centrally.

Owned by: the data layer. Called by the triage agent, the router, and tests.
Calls: `sources.py` (the registry) and `config.py`.

Exists so that agent code never opens a JSON file, never knows that transcripts
are keyed by name, and never writes its own tenant filter. There is exactly one
place in this module where a record is matched to a tenant -- `_index_by_tenant`
-- which means there is exactly one place to audit for the JSON half of tenant
isolation. (The SQL half lives in `src/db/guard.py`.)
"""

from __future__ import annotations

from collections import defaultdict
from functools import cached_property
from typing import Any, Sequence

from src import config
from src.data.loaders import CallTranscript, KnowledgeArticle, Tenant, Ticket
from src.data.sources import REGISTRY, DataSource


class UnknownTenantError(LookupError):
    """Asked for a tenant id that does not exist in customers.json."""


class Repository:
    """Read-only access to the five JSON sources, indexed by tenant.

    Indexes are built lazily on first access and then held, because the corpus is
    ~95KB of read-only fixture data. At 150 tenants this becomes a database query
    instead; the interface does not change, which is the point of having one.
    """

    def __init__(self, registry: dict[str, DataSource] | None = None) -> None:
        self._registry = registry if registry is not None else REGISTRY
        self._indexes: dict[str, dict[int, list[Any]]] = {}

    # --- generic access ------------------------------------------------------

    def _index_by_tenant(self, source_name: str) -> dict[int, list[Any]]:
        """Group one source's records by owning tenant.

        Records whose tenant cannot be established (an unresolvable transcript
        name, or a global KB article) are excluded entirely rather than filed
        under a placeholder. Nothing downstream can then accidentally serve them
        to a tenant that does not own them.
        """
        if source_name not in self._indexes:
            source = self._registry[source_name]
            grouped: dict[int, list[Any]] = defaultdict(list)
            for record in source.load():
                tenant_id = source.tenant_id_of(record)
                if tenant_id is not None:
                    grouped[tenant_id].append(record)
            self._indexes[source_name] = dict(grouped)
        return self._indexes[source_name]

    def records_for(self, source_name: str, tenant_id: int) -> tuple[Any, ...]:
        """Every record in `source_name` belonging to `tenant_id`.

        The generic path. A new data source is queryable through this the moment
        it is registered, with no change here.
        """
        return tuple(self._index_by_tenant(source_name).get(tenant_id, ()))

    def unattributed(self, source_name: str) -> tuple[Any, ...]:
        """Records the registry could not attribute to any tenant.

        Not used to serve answers -- it exists so that dropped records are
        observable rather than silent. A rising count here is a data quality
        alarm (an alias table falling behind the transcripts, say).
        """
        source = self._registry[source_name]
        return tuple(r for r in source.load() if source.tenant_id_of(r) is None)

    # --- typed accessors -----------------------------------------------------

    @cached_property
    def _tenants_by_id(self) -> dict[int, Tenant]:
        return {t.tenant_id: t for t in self._registry["tenants"].load()}

    def all_tenants(self) -> tuple[Tenant, ...]:
        return tuple(sorted(self._tenants_by_id.values(), key=lambda t: t.tenant_id))

    def get_tenant(self, tenant_id: int) -> Tenant:
        """The tenant profile, or raise. Callers have already resolved an id, so a
        miss here is a bug rather than user error."""
        try:
            return self._tenants_by_id[tenant_id]
        except KeyError:
            raise UnknownTenantError(
                f"tenant_id {tenant_id} is not in customers.json "
                f"(known: {sorted(self._tenants_by_id)})"
            ) from None

    def tickets_for(self, tenant_id: int) -> tuple[Ticket, ...]:
        """This tenant's tickets, newest first."""
        tickets: tuple[Ticket, ...] = self.records_for("tickets", tenant_id)
        return tuple(sorted(tickets, key=lambda t: (t.created_at is None, t.created_at), reverse=True))

    def transcripts_for(self, tenant_id: int) -> tuple[CallTranscript, ...]:
        """This tenant's call summaries, newest first."""
        calls: tuple[CallTranscript, ...] = self.records_for("call_transcripts", tenant_id)
        return tuple(sorted(calls, key=lambda c: (c.call_date is None, c.call_date), reverse=True))

    def knowledge_base(self) -> tuple[KnowledgeArticle, ...]:
        """Every KB article. Global to FleetPanda -- not tenant-scoped."""
        return tuple(self._registry["knowledge_base"].load())

    # --- derived signals -----------------------------------------------------

    def module_mismatch(self, ticket: Ticket) -> str | None:
        """The module this ticket needs and the tenant is not entitled to, if any.

        Returns the *module* name, not a boolean, so the brief can say "asks about
        tank_monitor, which this tenant does not have" rather than just "anomaly".

        A bare `product_area not in modules_active` check flags 58 of 85 tickets
        because the two fields are different vocabularies -- `integration` and
        `login_access` are gated by no module at all. Mapping first, and treating
        unmapped areas as ungated, brings that to 26 genuine gaps.
        See DECISIONS.md D-002.
        """
        if ticket.product_area in config.UNGATED_PRODUCT_AREAS:
            return None
        required = config.AREA_TO_MODULE.get(ticket.product_area)
        if required is None:
            # An area we have no mapping for. Under-flag rather than over-flag:
            # an unknown area is not evidence of an entitlement gap.
            return None
        tenant = self.get_tenant(ticket.tenant_id)
        return None if required in tenant.modules_active else required
