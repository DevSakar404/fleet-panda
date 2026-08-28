"""The data-source registry: the seam that lets a sixth source be added without
touching agent code.

Owned by: the data layer. Called by `repository.py`, which iterates the registry
rather than naming sources individually. Calls: `loaders.py` and `resolver.py`.

This is one of the two load-bearing registries in CLAUDE.md section 3.7. The
contract it enforces is deliberately tiny -- a source knows its own name, how to
load itself, and how to find the tenant a record belongs to. Everything else
(indexing, filtering, isolation) is done generically by the repository, so adding
a source is one new `DataSource` instance and one line in `REGISTRY`.

The interesting member is `call_transcripts`, which is the reason `tenant_id_of`
is a function on the source rather than an attribute on the record: transcripts
carry a tenant *name*, so their tenant id has to be resolved rather than read.
That difference is contained here and is invisible to every caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final, Protocol, Sequence

from src.data import loaders
from src.data.resolver import ResolutionResult, TenantResolver


class DataSource(Protocol):
    """What the repository needs from any source of tenant-scoped records."""

    name: str

    def load(self) -> Sequence[Any]:
        """Return every record in this source."""
        ...

    def tenant_id_of(self, record: Any) -> int | None:
        """The tenant owning `record`, or None when it cannot be established."""
        ...


@dataclass(frozen=True, slots=True)
class AttributeSource:
    """A source whose records already carry an integer `tenant_id`.

    Covers four of the five sources. The attribute name is a parameter only
    because `Tenant` uses `tenant_id` as its own primary key, and being explicit
    costs nothing.
    """

    name: str
    loader: Callable[[], Sequence[Any]]
    attribute: str = "tenant_id"

    def load(self) -> Sequence[Any]:
        return self.loader()

    def tenant_id_of(self, record: Any) -> int | None:
        return getattr(record, self.attribute, None)


@dataclass(frozen=True, slots=True)
class GlobalSource:
    """A source with no tenant dimension at all -- the knowledge base.

    KB articles are FleetPanda's, not any tenant's. Modelling that as
    `tenant_id_of() -> None` rather than omitting the source keeps the registry
    uniform: the repository asks every source the same question, and this one
    honestly answers "nobody's".
    """

    name: str
    loader: Callable[[], Sequence[Any]]

    def load(self) -> Sequence[Any]:
        return self.loader()

    def tenant_id_of(self, record: Any) -> None:
        return None


class ResolvedNameSource:
    """A source keyed by tenant *name*, resolved to an id on first use.

    call_transcripts.json is the only source in this shape. Resolution runs once
    per distinct name and is memoised, because 43 transcripts share 26 names and
    the resolver is the most expensive lookup in the data layer.

    Unresolvable names return None rather than raising. A transcript we cannot
    attribute is dropped from every tenant's view by the repository, which is the
    fail-closed behaviour: better to lose one call summary from a brief than to
    attach it to the wrong company.
    """

    def __init__(self, name: str, loader: Callable[[], Sequence[Any]], attribute: str = "tenant_name") -> None:
        self.name = name
        self._loader = loader
        self._attribute = attribute
        self._resolver = TenantResolver()
        self._cache: dict[str, ResolutionResult] = {}

    def load(self) -> Sequence[Any]:
        return self._loader()

    def resolve_name(self, raw_name: str) -> ResolutionResult:
        """Resolve and memoise. Exposed so recon and tests can inspect the result."""
        if raw_name not in self._cache:
            self._cache[raw_name] = self._resolver.resolve(raw_name)
        return self._cache[raw_name]

    def tenant_id_of(self, record: Any) -> int | None:
        raw_name = getattr(record, self._attribute, None)
        if not raw_name:
            return None
        return self.resolve_name(raw_name).tenant_id


# --- The registry ------------------------------------------------------------
#
# Adding a sixth source is: write its loader in loaders.py, then add one line
# here. No agent code changes. This is the property CLAUDE.md section 3.7 asks for
# and the one the architecture question in DECISIONS.md will point at.

REGISTRY: Final[dict[str, DataSource]] = {
    "tenants": AttributeSource("tenants", loaders.load_tenants),
    "tickets": AttributeSource("tickets", loaders.load_tickets),
    "call_transcripts": ResolvedNameSource("call_transcripts", loaders.load_call_transcripts),
    "knowledge_base": GlobalSource("knowledge_base", loaders.load_knowledge_base),
}


def get_source(name: str) -> DataSource:
    """Look up a registered source, with a message that lists the real options."""
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"Unknown data source {name!r}. Registered: {sorted(REGISTRY)}") from None
