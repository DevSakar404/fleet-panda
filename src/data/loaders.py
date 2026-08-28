"""Raw JSON files -> typed, frozen records.

Owned by: the data layer. Called by `sources.py` (which registers each loader) and
by tests. Calls: `config` for paths, and nothing else.

Exists as a separate file because parsing is the one place that touches the shape
of the vendor's JSON. Everything downstream sees dataclasses, so a change to a
field name in `data/` is a change to this file only.

Dataclasses rather than Pydantic: these are trusted local fixtures, not request
bodies, so there is no validation to perform at this boundary -- and `frozen=True`
gives us the property that actually matters here, which is that no agent code can
mutate the loaded corpus. Pydantic is reserved for LLM output, where the input is
genuinely untrusted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from src import config


class DataFileError(RuntimeError):
    """A required data file is missing, unreadable, or not the expected shape.

    CLAUDE.md section 2 is explicit that a missing file stops the build rather
    than triggering synthetic fixtures, so this is raised, never swallowed.
    """


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    """Read a JSON file that must contain a top-level array of objects."""
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise DataFileError(f"Required data file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DataFileError(f"{path.name} is not valid JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise DataFileError(f"{path.name} must contain a JSON array, got {type(payload).__name__}")
    return payload


def _parse_date(value: str | None) -> date | None:
    """Parse an ISO date, tolerating the timestamps that some fields carry.

    Ticket timestamps look like '2026-05-12T06:00:00' while contract dates look
    like '2027-03-15'. Splitting on 'T' handles both without a format string per
    field.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value.split("T", 1)[0])
    except ValueError as exc:
        raise DataFileError(f"Unparseable date value: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Tenant:
    """A FleetPanda customer -- one of the ~12 fuel companies. From customers.json.

    Not to be confused with `dispatch.db.customers`, which holds the tenants'
    *end*-customers. The two share a name and nothing else.
    """

    tenant_id: int
    name: str
    health_score: int
    carr: int
    modules_active: frozenset[str]
    contract_end_date: date | None
    assigned_csm: str
    fleet_size: int
    onboarding_status: str
    region: str


@dataclass(frozen=True, slots=True)
class TenantAlias:
    """One row of tenant_aliases.json: an alternate spelling and its owner."""

    alias: str
    canonical_name: str
    tenant_id: int


@dataclass(frozen=True, slots=True)
class Ticket:
    """A support ticket from tickets.json.

    Carries both `tenant_id` and `tenant_name`; recon confirmed all 85 agree, but
    `tenant_id` is authoritative per CLAUDE.md and is what downstream code uses.
    """

    ticket_id: int
    tenant_id: int
    tenant_name: str
    subject: str
    description: str
    product_area: str
    status: str
    priority: str
    submitter_name: str
    submitter_email: str
    created_at: date | None
    updated_at: date | None
    resolution: str | None
    agent_name: str


@dataclass(frozen=True, slots=True)
class CallTranscript:
    """A summarised support call from call_transcripts.json.

    The only source keyed by `tenant_name` (string) with no `tenant_id`. The
    resolved id is attached at load time by `sources.py`, not here, so that this
    module stays a pure parser with no dependency on the resolver.
    """

    call_id: str
    tenant_name: str
    participants: tuple[str, ...]
    topic: str
    summary: str
    sentiment: str
    action_items: tuple[str, ...]
    call_date: date | None
    duration_minutes: int
    competitor_mentioned: bool


@dataclass(frozen=True, slots=True)
class KnowledgeArticle:
    """A known-issue article from knowledge_base.json."""

    article_id: str
    title: str
    product_area: str
    symptoms: tuple[str, ...]
    root_cause: str
    resolution: str
    created_at: date | None
    updated_at: date | None

    @property
    def searchable_text(self) -> str:
        """Title, symptoms and root cause as one string, for keyword matching."""
        return " ".join((self.title, *self.symptoms, self.root_cause))


@lru_cache(maxsize=1)
def load_tenants() -> tuple[Tenant, ...]:
    """Load customers.json. Cached: the file is read-only and read many times."""
    return tuple(
        Tenant(
            tenant_id=row["tenant_id"],
            name=row["name"],
            health_score=row["health_score"],
            carr=row["carr"],
            # NOTE: the key is `modules_active`, not `active_modules` as CLAUDE.md
            # section 7 has it. See OPEN_QUESTIONS.md Q-006.
            modules_active=frozenset(row["modules_active"]),
            contract_end_date=_parse_date(row.get("contract_end_date")),
            assigned_csm=row["assigned_csm"],
            fleet_size=row["fleet_size"],
            onboarding_status=row["onboarding_status"],
            region=row["region"],
        )
        for row in _read_json_array(config.CUSTOMERS_PATH)
    )


@lru_cache(maxsize=1)
def load_tenant_aliases() -> tuple[TenantAlias, ...]:
    """Load tenant_aliases.json."""
    return tuple(
        TenantAlias(alias=row["alias"], canonical_name=row["canonical_name"], tenant_id=row["tenant_id"])
        for row in _read_json_array(config.TENANT_ALIASES_PATH)
    )


@lru_cache(maxsize=1)
def load_tickets() -> tuple[Ticket, ...]:
    """Load tickets.json."""
    return tuple(
        Ticket(
            ticket_id=row["ticket_id"],
            tenant_id=row["tenant_id"],
            tenant_name=row["tenant_name"],
            subject=row["subject"],
            description=row["description"],
            product_area=row["product_area"],
            status=row["status"],
            priority=row["priority"],
            submitter_name=row["submitter_name"],
            submitter_email=row["submitter_email"],
            created_at=_parse_date(row.get("created_at")),
            updated_at=_parse_date(row.get("updated_at")),
            resolution=row.get("resolution"),
            agent_name=row["agent_name"],
        )
        for row in _read_json_array(config.TICKETS_PATH)
    )


@lru_cache(maxsize=1)
def load_call_transcripts() -> tuple[CallTranscript, ...]:
    """Load call_transcripts.json."""
    return tuple(
        CallTranscript(
            call_id=row["call_id"],
            tenant_name=row["tenant_name"],
            participants=tuple(row.get("participants", ())),
            topic=row["topic"],
            summary=row["summary"],
            sentiment=row["sentiment"],
            action_items=tuple(row.get("action_items", ())),
            call_date=_parse_date(row.get("date")),
            duration_minutes=row.get("duration_minutes", 0),
            competitor_mentioned=bool(row.get("competitor_mentioned", False)),
        )
        for row in _read_json_array(config.CALL_TRANSCRIPTS_PATH)
    )


@lru_cache(maxsize=1)
def load_knowledge_base() -> tuple[KnowledgeArticle, ...]:
    """Load knowledge_base.json."""
    return tuple(
        KnowledgeArticle(
            article_id=row["article_id"],
            title=row["title"],
            product_area=row["product_area"],
            symptoms=tuple(row.get("symptoms", ())),
            root_cause=row["root_cause"],
            resolution=row["resolution"],
            created_at=_parse_date(row.get("created_at")),
            updated_at=_parse_date(row.get("updated_at")),
        )
        for row in _read_json_array(config.KNOWLEDGE_BASE_PATH)
    )
