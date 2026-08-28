"""Builds a ticket brief from all five sources.

Owned by: the agent layer. Called by `router.py`. Calls: `Repository` (four JSON
sources), `QueryExecutor` (the dispatch snapshot), `escalation.score_ticket`, and
`LLMClient` for the narrative only.

The shape of this file is the answer to "how do five sources become one brief":

    ticket
      |
      +-- gather()   five sources -> TicketContext        deterministic, no LLM
      +-- score()    TicketContext -> EscalationAssessment  deterministic, no LLM
      +-- narrate()  both -> three prose sections           LLM, no arithmetic
      +-- TicketBrief

Everything decidable is decided before the model is called, and the model is given
the decision rather than asked for it (CLAUDE.md section 3.4). That is what makes
"why was this escalated?" answerable from `assessment.signals` without re-running
anything.

Every section degrades to empty rather than failing. Six of twelve tenants have no
`tank_readings` rows, `billing` tickets have no KB article at all, and 37 of 85
tickets have a null resolution -- a brief that needs all five sources to be
non-empty would fail on most of the corpus.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src import config
from src.agent.escalation import EscalationAssessment, score_ticket
from src.agent.session import TenantContext
from src.data.loaders import CallTranscript, KnowledgeArticle, Tenant, Ticket
from src.data.repository import Repository
from src.db.executor import QueryExecutor
from src.llm import prompts
from src.llm.client import LLMClient

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class OperationalSnapshot:
    """The dispatch-database half of the context pack.

    Computed with fixed SQL rather than through the SQL agent: these are four
    known questions, not natural-language ones, so routing them through an LLM
    would add a round trip, a failure mode and a cost for no gain. They still go
    through the guard and the executor, so the tenant predicate is injected the
    same way it would be for a typed question.
    """

    completed_last_30: int = 0
    completed_prior_30: int = 0
    volume_change_pct: float | None = None
    emergency_last_30: int = 0
    open_orders: int = 0
    anchor_date: str | None = None

    @property
    def is_declining(self) -> bool:
        return (
            self.volume_change_pct is not None
            and self.volume_change_pct < config.DECLINE_THRESHOLD_PCT
        )


@dataclass(frozen=True, slots=True)
class TicketContext:
    """Everything gathered about a ticket, before any narrative exists."""

    ticket: Ticket
    tenant: Tenant
    past_tickets: tuple[Ticket, ...]
    duplicates: tuple[Ticket, ...]
    calls: tuple[CallTranscript, ...]
    kb_articles: tuple[KnowledgeArticle, ...]
    operations: OperationalSnapshot

    @property
    def missing_sources(self) -> tuple[str, ...]:
        """Which of the five sources had nothing for this tenant.

        Surfaced rather than hidden: "no KB article matched" is information a CSM
        acts on, and it is different from "we did not look".
        """
        empty = []
        if not self.past_tickets:
            empty.append("past tickets")
        if not self.calls:
            empty.append("call history")
        if not self.kb_articles:
            empty.append("knowledge base")
        if self.operations.volume_change_pct is None:
            empty.append("dispatch snapshot")
        return tuple(empty)


@dataclass(frozen=True, slots=True)
class TicketBrief:
    """The deliverable. Deterministic parts and narrative parts, kept separable."""

    context: TicketContext
    assessment: EscalationAssessment
    summary: str = ""
    escalation_reasoning: str = ""
    suggested_response: str = ""

    @property
    def ticket_id(self) -> int:
        return self.context.ticket.ticket_id

    @property
    def tenant_id(self) -> int:
        return self.context.tenant.tenant_id


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in config.KB_STOPWORDS}


def find_kb_articles(ticket: Ticket, articles: tuple[KnowledgeArticle, ...]) -> tuple[KnowledgeArticle, ...]:
    """Rank KB articles for a ticket by product area, then symptom overlap.

    No embeddings and no vector store (D-013). The corpus is twelve articles, and
    `product_area` is a literal both tickets and articles share -- this is a join
    with a tie-break, not a semantic search problem.

    Returns empty when nothing scores. `billing` tickets have no KB article at
    all, and surfacing the least-bad match would be worse than saying so.
    """
    ticket_words = _tokens(f"{ticket.subject} {ticket.description}")
    scored: list[tuple[int, date, KnowledgeArticle]] = []

    for article in articles:
        points = 0
        if article.product_area == ticket.product_area:
            points += config.KB_AREA_MATCH_POINTS
        for symptom in article.symptoms:
            if _tokens(symptom) & ticket_words:
                points += config.KB_SYMPTOM_MATCH_POINTS
        if _tokens(article.title) & ticket_words:
            points += config.KB_TITLE_MATCH_POINTS

        if points:
            # Recency is the tie-break, so a refreshed article outranks a stale
            # one of equal relevance. `or date.min` keeps a null updated_at last
            # rather than raising.
            scored.append((points, article.updated_at or date.min, article))

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return tuple(article for _, _, article in scored[: config.KB_MAX_ARTICLES])


class TriageAgent:
    """Turns a ticket into a brief. Holds no state between calls."""

    def __init__(
        self,
        repository: Repository | None = None,
        executor: QueryExecutor | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self._repository = repository or Repository()
        self._executor = executor or QueryExecutor()
        self._llm = llm

    # --- public ---------------------------------------------------------------

    def build_brief(self, ticket: Ticket, today: date | None = None) -> TicketBrief:
        """Gather, score, narrate. Narration is skipped when no LLM is configured,
        so the deterministic half of a brief is available without an API key."""
        context = self.gather(ticket)
        assessment = score_ticket(
            ticket,
            self._repository,
            today=today,
            volume_change_pct=context.operations.volume_change_pct,
        )
        if self._llm is None:
            return TicketBrief(context=context, assessment=assessment)
        return self._narrate(context, assessment)

    def gather(self, ticket: Ticket) -> TicketContext:
        """Assemble the five-source context pack. Deterministic, no LLM."""
        from src.agent.escalation import find_duplicates

        tenant_id = ticket.tenant_id
        duplicates = find_duplicates(ticket, self._repository)
        past = tuple(
            t for t in self._repository.tickets_for(tenant_id) if t.ticket_id != ticket.ticket_id
        )[: config.BRIEF_MAX_PAST_TICKETS]

        return TicketContext(
            ticket=ticket,
            tenant=self._repository.get_tenant(tenant_id),
            past_tickets=past,
            duplicates=duplicates,
            calls=self._repository.transcripts_for(tenant_id)[: config.BRIEF_MAX_CALLS],
            kb_articles=find_kb_articles(ticket, self._repository.knowledge_base()),
            operations=self.operational_snapshot(tenant_id),
        )

    def operational_snapshot(self, tenant_id: int) -> OperationalSnapshot:
        """Four fixed queries, run tenant-scoped through the guard.

        Windows are anchored on the newest row in the data, not on `date('now')`,
        for the reason in D-001: the dataset ends 2026-05-29, so a `now()`-relative
        window reports every tenant as having stopped delivering entirely.
        """
        context = TenantContext.for_tenant(tenant_id)
        anchor = f"(SELECT MAX({config.DATE_ANCHOR_COLUMN}) FROM {config.DATE_ANCHOR_TABLE})"

        recent = self._scalar(
            f"SELECT COUNT(*) AS n FROM delivery_orders WHERE status = 'completed' "
            f"AND delivery_date > date({anchor}, '-30 day')", context)
        prior = self._scalar(
            f"SELECT COUNT(*) AS n FROM delivery_orders WHERE status = 'completed' "
            f"AND delivery_date > date({anchor}, '-60 day') "
            f"AND delivery_date <= date({anchor}, '-30 day')", context)
        # `>=` here, `>` in the paired windows above. Two conventions on purpose:
        # a single "past 30 days" window reads inclusively, while two ADJACENT
        # windows must not both claim the boundary day or the comparison
        # double-counts it. The inclusive form is also what
        # tests/test_sql_questions.py asserts for Q5 (17 emergency orders for
        # tenant 4); using `>` here quietly produced 15 and made two numbers in
        # the same system disagree.
        emergency = self._scalar(
            f"SELECT COUNT(*) AS n FROM delivery_orders WHERE priority = 'emergency' "
            f"AND order_date >= date({anchor}, '-30 day')", context)
        open_orders = self._scalar(
            "SELECT COUNT(*) AS n FROM delivery_orders "
            "WHERE status IN ('pending', 'in_progress')", context)

        # None rather than 0.0 when there is no prior window to compare against:
        # "no baseline" and "flat" are different, and only one of them should
        # produce a decline signal.
        change = 100.0 * (recent - prior) / prior if prior else None

        return OperationalSnapshot(
            completed_last_30=recent,
            completed_prior_30=prior,
            volume_change_pct=change,
            emergency_last_30=emergency,
            open_orders=open_orders,
            anchor_date=self._anchor_date(),
        )

    # --- internals ------------------------------------------------------------

    def _scalar(self, sql: str, context: TenantContext) -> int:
        verdict, result = self._executor.run(sql, context)
        if not verdict.allowed or result is None or not result.rows:
            # A snapshot query is ours, not the model's, so a rejection here is a
            # bug in this file rather than a bad generation. Degrade to zero so a
            # brief still renders, and let the guard's own tests catch the cause.
            return 0
        return int(result.rows[0][0])

    def _anchor_date(self) -> str | None:
        from src.db.schema import introspect

        return introspect().date_anchor

    def _narrate(self, context: TicketContext, assessment: EscalationAssessment) -> TicketBrief:
        """One LLM call for three prose sections. It is given the level, not asked."""
        payload = {
            "ticket": {
                "id": context.ticket.ticket_id,
                "subject": context.ticket.subject,
                "description": context.ticket.description,
                "product_area": context.ticket.product_area,
                "priority": context.ticket.priority,
                "status": context.ticket.status,
                "created_at": str(context.ticket.created_at),
            },
            "customer": {
                "name": context.tenant.name,
                "health_score": context.tenant.health_score,
                "carr": context.tenant.carr,
                "contract_end_date": str(context.tenant.contract_end_date),
                "assigned_csm": context.tenant.assigned_csm,
                "modules_active": sorted(context.tenant.modules_active),
                "onboarding_status": context.tenant.onboarding_status,
            },
            "escalation": {
                "level": assessment.level.value,
                "score": assessment.score,
                "account_risk": assessment.account_risk,
                "ticket_risk": assessment.ticket_risk,
                "reasons": list(assessment.reasons),
                "missing_module": assessment.missing_module,
            },
            "duplicates": [
                {"id": t.ticket_id, "created_at": str(t.created_at), "status": t.status}
                for t in context.duplicates
            ],
            "past_tickets": [
                {"id": t.ticket_id, "subject": t.subject, "status": t.status,
                 "resolution": t.resolution}
                for t in context.past_tickets
            ],
            "calls": [
                {"date": str(c.call_date), "topic": c.topic, "sentiment": c.sentiment,
                 "competitor_mentioned": c.competitor_mentioned,
                 "action_items": list(c.action_items)}
                for c in context.calls
            ],
            "kb_articles": [
                {"id": a.article_id, "title": a.title, "root_cause": a.root_cause,
                 "resolution": a.resolution, "updated_at": str(a.updated_at)}
                for a in context.kb_articles
            ],
            "operations": {
                "completed_last_30": context.operations.completed_last_30,
                "completed_prior_30": context.operations.completed_prior_30,
                "volume_change_pct": context.operations.volume_change_pct,
                "emergency_last_30": context.operations.emergency_last_30,
                "open_orders": context.operations.open_orders,
                "anchor_date": context.operations.anchor_date,
            },
            "sources_with_no_data": list(context.missing_sources),
        }

        response = self._llm.complete(
            system=prompts.TICKET_TRIAGE_SYSTEM_PROMPT,
            user=json.dumps(payload, default=str),
        )
        sections = self._parse_sections(response.text)
        return TicketBrief(
            context=context,
            assessment=assessment,
            summary=sections.get("summary", ""),
            escalation_reasoning=sections.get("escalation_reasoning", ""),
            suggested_response=sections.get("suggested_response", ""),
        )

    @staticmethod
    def _parse_sections(text: str) -> dict[str, str]:
        """Read the model's three prose sections.

        A parse failure degrades to putting the whole reply in `summary` rather
        than discarding it: the deterministic half of the brief is already correct
        and complete, so a malformed narrative should cost formatting, not the
        brief.
        """
        cleaned = text.strip()
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
        if fenced:
            cleaned = fenced.group(1).strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            return {"summary": cleaned}
        if not isinstance(payload, dict):
            return {"summary": cleaned}
        return {k: str(v) for k, v in payload.items() if isinstance(v, (str, int, float))}


def build_brief(ticket: Ticket, context: TenantContext, llm: LLMClient | None = None) -> TicketBrief:
    """Module-level entry point used by the router."""
    return TriageAgent(llm=llm or LLMClient()).build_brief(ticket)
