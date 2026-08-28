"""Deterministic escalation scoring. No LLM anywhere in this file.

Owned by: the agent layer. Called by `triage_agent.py`. Calls: `Repository` and
`config` thresholds only. Pure with respect to the outside world -- no network, no
database, no clock unless one is passed in.

Why this is not a prompt (CLAUDE.md section 3.4): a model asked to weigh health
score against CARR against contract proximity gives a different answer on Tuesday,
and "why was this escalated?" has to be answerable from code in a live session.
The LLM is handed the level this file computes and asked to explain it in prose.
It does not get a vote.

Every signal returns points AND a sentence. The sentences are what the brief
prints, so they are written for a CSM to read rather than for a log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from rapidfuzz import fuzz

from src import config
from src.data.loaders import CallTranscript, Ticket
from src.data.repository import Repository


class EscalationLevel(str, Enum):
    """Ordered. Comparison is by score, not by member order, but reading order
    matters for the brief."""

    STANDARD = "standard"
    ELEVATED = "elevated"
    URGENT = "urgent"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Signal:
    """One scored observation, with the sentence the brief will print."""

    name: str
    points: int
    reason: str


@dataclass(frozen=True, slots=True)
class EscalationAssessment:
    """The verdict and everything that produced it.

    `signals` is the audit trail: the level is exactly `sum(s.points)` bucketed,
    so a disputed escalation can be traced to the observations behind it without
    re-running anything.
    """

    ticket_id: int
    tenant_id: int
    level: EscalationLevel
    score: int
    signals: tuple[Signal, ...]
    account_risk: int = 0
    ticket_risk: int = 0
    account_risk_capped: bool = False
    duplicate_ticket_ids: tuple[int, ...] = ()
    missing_module: str | None = None

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(signal.reason for signal in self.signals)

    @property
    def is_duplicate(self) -> bool:
        return bool(self.duplicate_ticket_ids)

    @property
    def account_signals(self) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.name in config.ACCOUNT_LEVEL_SIGNALS)

    @property
    def ticket_signals(self) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.name not in config.ACCOUNT_LEVEL_SIGNALS)


def _plural(count: int, noun: str) -> str:
    """'1 day' / '2 days'. Small, but these strings are read aloud in voice mode."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def level_for_score(score: int) -> EscalationLevel:
    """Bucket a score. Separate from scoring so thresholds can be tested alone."""
    if score >= config.ESCALATION_CRITICAL:
        return EscalationLevel.CRITICAL
    if score >= config.ESCALATION_URGENT:
        return EscalationLevel.URGENT
    if score >= config.ESCALATION_ELEVATED:
        return EscalationLevel.ELEVATED
    return EscalationLevel.STANDARD


def find_duplicates(ticket: Ticket, repository: Repository) -> tuple[Ticket, ...]:
    """Other tickets from the same tenant with a near-identical subject, oldest first.

    Same tenant only -- two tenants reporting "Dashboard loading very slowly" are
    two unrelated reports, and matching across tenants would leak one tenant's
    ticket history into another's brief.

    `status` is deliberately ignored. Tenant 4's TankLink cluster contains a
    ticket closed on 2026-04-24 and refiled twice afterwards, so treating 'closed'
    as terminal would report the newest filing as a first occurrence (DQ-7).
    """
    matches = [
        other
        for other in repository.tickets_for(ticket.tenant_id)
        if other.ticket_id != ticket.ticket_id
        and fuzz.token_set_ratio(other.subject, ticket.subject)
        >= config.DUPLICATE_SUBJECT_THRESHOLD
    ]
    return tuple(sorted(matches, key=lambda t: (t.created_at is None, t.created_at)))


def _health_signal(health_score: int) -> Signal | None:
    if health_score < config.HEALTH_SCORE_CRITICAL:
        return Signal("health_critical", config.WEIGHT_HEALTH_CRITICAL,
                      f"Account health is {health_score}, below the critical threshold of "
                      f"{config.HEALTH_SCORE_CRITICAL}.")
    if health_score < config.HEALTH_SCORE_AT_RISK:
        return Signal("health_at_risk", config.WEIGHT_HEALTH_AT_RISK,
                      f"Account health is {health_score}, in the at-risk band.")
    return None


def _contract_signal(contract_end: date | None, today: date) -> Signal | None:
    """Expired outranks expiring, and both are scored near health on purpose."""
    if contract_end is None:
        return None
    days = (contract_end - today).days
    if days < 0:
        ago = _plural(abs(days), "day")
        return Signal("contract_expired", config.WEIGHT_CONTRACT_EXPIRED,
                      f"Contract expired {ago} ago on {contract_end}.")
    if days <= config.CONTRACT_RENEWAL_WINDOW_DAYS:
        left = "today" if days == 0 else f"in {_plural(days, 'day')}"
        return Signal("contract_renewal", config.WEIGHT_CONTRACT_RENEWAL_WINDOW,
                      f"Contract expires {left} on {contract_end} -- this is a "
                      "renewal conversation, not only a support ticket.")
    return None


def _carr_signal(carr: int) -> Signal | None:
    if carr >= config.CARR_HIGH:
        return Signal("carr_high", config.WEIGHT_CARR_HIGH,
                      f"${carr:,} CARR puts this in the top tier of accounts.")
    if carr >= config.CARR_MEDIUM:
        return Signal("carr_medium", config.WEIGHT_CARR_MEDIUM,
                      f"${carr:,} CARR is a mid-tier account.")
    return None


def _duplicate_signal(duplicates: tuple[Ticket, ...]) -> Signal | None:
    if not duplicates:
        return None
    ids = ", ".join(f"#{t.ticket_id}" for t in duplicates)
    reopened = any(t.status in ("closed", "resolved") for t in duplicates)
    note = " One of those was already closed and the issue came back." if reopened else ""
    if len(duplicates) >= config.DUPLICATE_CLUSTER_SIZE:
        return Signal("duplicate_cluster", config.WEIGHT_DUPLICATE_CLUSTER,
                      f"Reported {len(duplicates) + 1} times by this tenant; "
                      f"earlier filings {ids}.{note}")
    return Signal("duplicate", config.WEIGHT_DUPLICATE,
                  f"Looks like a repeat of {ids}.{note}")


def _call_signals(calls: tuple[CallTranscript, ...]) -> list[Signal]:
    """Signals from the most recent calls. Competitor mention outweighs sentiment."""
    recent = calls[: config.RECENT_CALL_COUNT]
    signals: list[Signal] = []

    negative = [c for c in recent if c.sentiment == "negative"]
    if negative:
        verb = "was" if len(negative) == 1 else "were"
        signals.append(Signal(
            "negative_sentiment", config.WEIGHT_NEGATIVE_SENTIMENT,
            f"{len(negative)} of the last {len(recent)} calls {verb} negative "
            f"(most recent {negative[0].call_date}: {negative[0].topic}).",
        ))

    competitor = next((c for c in recent if c.competitor_mentioned), None)
    if competitor is not None:
        signals.append(Signal(
            "competitor_mentioned", config.WEIGHT_COMPETITOR_MENTIONED,
            f"A competitor was mentioned on the {competitor.call_date} call "
            f"({competitor.topic}).",
        ))
    return signals


def score_ticket(
    ticket: Ticket,
    repository: Repository,
    today: date | None = None,
    volume_change_pct: float | None = None,
) -> EscalationAssessment:
    """Compute escalation level and the reasons behind it.

    `today` is injectable because contract proximity is the one signal measured
    against the real calendar rather than against the dataset -- contract dates are
    forward-looking CRM facts, not operational history, so they do not move with
    the data's 91-day staleness (D-001). Injecting it also makes the tests
    deterministic.

    `volume_change_pct` is passed in rather than queried because this module does
    no I/O beyond the in-memory repository; the caller computes it through the SQL
    agent. Omitted means the signal is skipped, not that it scored zero.
    """
    today = today or date.today()
    tenant = repository.get_tenant(ticket.tenant_id)
    duplicates = find_duplicates(ticket, repository)
    missing_module = repository.module_mismatch(ticket)

    signals: list[Signal] = []
    for signal in (
        _health_signal(tenant.health_score),
        _contract_signal(tenant.contract_end_date, today),
        _carr_signal(tenant.carr),
        _duplicate_signal(duplicates),
    ):
        if signal is not None:
            signals.append(signal)

    if missing_module is not None:
        signals.append(Signal(
            "module_mismatch", config.WEIGHT_MODULE_MISMATCH,
            f"Ticket is about {ticket.product_area}, which needs the "
            f"'{missing_module}' module. This tenant is not entitled to it "
            f"(active: {', '.join(sorted(tenant.modules_active))}).",
        ))

    if volume_change_pct is not None and volume_change_pct < config.DECLINE_THRESHOLD_PCT:
        signals.append(Signal(
            "volume_decline", config.WEIGHT_VOLUME_DECLINE,
            f"Delivery volume is down {abs(volume_change_pct):.1f}% versus the "
            "previous 30 days.",
        ))

    signals.extend(_call_signals(repository.transcripts_for(ticket.tenant_id)))

    priority_points = config.WEIGHT_PRIORITY.get(ticket.priority, 0)
    if priority_points:
        signals.append(Signal("ticket_priority", priority_points,
                              f"Ticket was filed as {ticket.priority}."))

    # Account state raises the floor; it does not decide the ceiling.
    #
    # Summed unchecked, the account signals reach 95 and the ticket signals only
    # 35, so every ticket from a struggling tenant scored CRITICAL and the level
    # stopped ranking them -- all twelve of tenant 4's were identical. Capping the
    # account portion just below CRITICAL means a bad account alone is URGENT, and
    # CRITICAL additionally requires something about this particular ticket: a
    # repeat filing, an entitlement gap, or a stated urgency. See D-012.
    raw_account = sum(s.points for s in signals if s.name in config.ACCOUNT_LEVEL_SIGNALS)
    ticket_risk = sum(s.points for s in signals if s.name not in config.ACCOUNT_LEVEL_SIGNALS)
    account_risk = min(raw_account, config.MAX_ACCOUNT_RISK_POINTS)
    score = account_risk + ticket_risk

    return EscalationAssessment(
        ticket_id=ticket.ticket_id,
        tenant_id=ticket.tenant_id,
        level=level_for_score(score),
        score=score,
        signals=tuple(signals),
        account_risk=account_risk,
        ticket_risk=ticket_risk,
        account_risk_capped=raw_account > config.MAX_ACCOUNT_RISK_POINTS,
        duplicate_ticket_ids=tuple(t.ticket_id for t in duplicates),
        missing_module=missing_module,
    )
