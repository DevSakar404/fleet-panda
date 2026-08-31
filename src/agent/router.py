"""Decides what an incoming request is, and refuses what this session may not ask.

Owned by: the agent layer. Called by `interfaces/cli_chat.py` and, in Step 5, the
voice transport. Calls: `TenantResolver`, `SqlAgent`, `TriageAgent`, `Repository`.

Everything below this file is shared by chat and voice (CLAUDE.md section 2), so
this is where a request stops being text and becomes a typed decision.

Intent classification is heuristic-first and LLM-last. An explicit "triage ticket
1083", a bare ticket id, or a pasted ticket body are all unambiguous, and so is a
question ending in a question mark. Only genuinely ambiguous input costs a round
trip -- which matters because this sits on the voice critical path, where an extra
call is an extra second of silence before the caller hears anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.agent.session import TenantContext
from src.agent.sql_agent import SqlAgent, SqlAnswer
from src.agent.ticket_parser import looks_like_a_ticket, parse_pasted_ticket
from src.agent.triage_agent import TicketBrief, TriageAgent
from src.data.repository import Repository
from src.data.resolver import Candidate, MatchMethod, TenantResolver
from src.llm import prompts
from src.llm.client import LLMClient


class Intent(str, Enum):
    DISPATCH_QUERY = "dispatch_query"
    TICKET_TRIAGE = "ticket_triage"
    CLARIFY = "clarify"


class ResponseKind(str, Enum):
    ANSWER = "answer"
    BRIEF = "brief"
    CLARIFY = "clarify"
    REFUSAL = "refusal"
    # A tenant was identified, but not exactly enough to bind without a human
    # saying yes. Distinct from CLARIFY (we do not know who) and from ANSWER (we
    # are sure). See `resolve_tenant`.
    CONFIRM = "confirm"


@dataclass(frozen=True, slots=True)
class RouterResponse:
    """One uniform shape, so chat and voice render the same decisions.

    `spoken` is what a voice transport reads aloud; `answer` may carry detail
    (a SQL string, a full brief) that would be unbearable over audio.
    """

    kind: ResponseKind
    text: str
    intent: Intent | None = None
    sql_answer: SqlAnswer | None = None
    brief: TicketBrief | None = None
    candidates: tuple[Candidate, ...] = ()
    # The tenant this response identified. On CONFIRM it is *pending* -- the
    # caller must not bind it until a human agrees. Carried here so transports
    # never re-run the resolver to recover an id they were already told.
    tenant_id: int | None = None

    @property
    def spoken(self) -> str:
        return self.text


# Three patterns, tried in descending confidence. A bare four-digit number is only
# read as a ticket id when it is the entire input or follows a cue word -- "how
# many gallons in 2026?" must not become a triage request, and every year in this
# corpus is also four digits.
_TICKET_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"#\s*(\d{4})\b"),                                    # "#1083"
    re.compile(r"\b(?:ticket|triage|escalate)\s*#?\s*(\d{4})\b", re.IGNORECASE),
    re.compile(r"^\s*#?(\d{4})\s*$"),                                # the whole input
)
_TRIAGE_WORDS = ("triage", "escalate", "brief me", "what should i do with")
_QUESTION_WORDS = ("how many", "how much", "which", "what", "who", "when", "list", "show", "top")


def extract_ticket_id(text: str) -> int | None:
    """Pull a ticket id out of free text, if one is unambiguously present."""
    for pattern in _TICKET_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


class Router:
    """Turns text plus a session into a typed response."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        repository: Repository | None = None,
        sql_agent: SqlAgent | None = None,
        triage_agent: TriageAgent | None = None,
    ) -> None:
        self._llm = llm
        self._repository = repository or Repository()
        self._resolver = TenantResolver()
        self._sql_agent = sql_agent or (SqlAgent(llm) if llm else None)
        self._triage_agent = triage_agent or TriageAgent(repository=self._repository, llm=llm)

    def route(self, text: str, context: TenantContext) -> RouterResponse:
        """Classify and dispatch. Never raises for bad input."""
        if not text or not text.strip():
            return RouterResponse(ResponseKind.CLARIFY, "Say that again?", Intent.CLARIFY)

        intent = self.classify(text)
        if intent is Intent.TICKET_TRIAGE:
            return self._triage(text, context)
        if intent is Intent.CLARIFY:
            return RouterResponse(
                ResponseKind.CLARIFY,
                "I can answer questions about delivery data, or triage a support "
                "ticket. Which did you mean?",
                Intent.CLARIFY,
            )
        return self._dispatch_query(text, context)

    # --- classification -------------------------------------------------------

    def classify(self, text: str) -> Intent:
        """Heuristics first; the model only for genuinely ambiguous input."""
        lowered = text.lower().strip()

        if any(word in lowered for word in _TRIAGE_WORDS) or extract_ticket_id(text) is not None:
            return Intent.TICKET_TRIAGE

        # A pasted ticket body: multi-line, and carrying the field labels a ticket
        # form produces. Cheaper and more reliable than asking.
        if "\n" in text.strip() and any(
            label in lowered for label in ("subject:", "product_area", "priority:", "submitted by")
        ):
            return Intent.TICKET_TRIAGE

        if lowered.endswith("?") or lowered.startswith(_QUESTION_WORDS):
            return Intent.DISPATCH_QUERY

        if self._llm is None:
            # Without a model, guess the commoner case rather than refusing --
            # the SQL agent's own refusal path is a better error than "I don't
            # know what you meant".
            return Intent.DISPATCH_QUERY

        reply = self._llm.complete(
            system=prompts.INTENT_ROUTER_SYSTEM_PROMPT, user=text
        ).text.strip().lower()
        for intent in Intent:
            if intent.value in reply:
                return intent
        return Intent.CLARIFY

    # --- tenant binding -------------------------------------------------------

    def resolve_tenant(self, name: str) -> RouterResponse:
        """Turn a spoken or typed company name into a bound session, or ask.

        The clarify path is the reason `ResolutionResult` carries candidates: over
        voice, "did you mean Cascade Fuel Services or Great Lakes Fuel Co?" is a
        usable question and "unresolved" is not.
        """
        result = self._resolver.resolve(name)
        if result.is_resolved:
            tenant = self._repository.get_tenant(result.tenant_id)

            # An inexact match returns CONFIRM and does NOT bind. Previously this
            # printed "(say yes to confirm)" and bound the session on the same
            # line -- the sentence described a control that did not exist. Over
            # voice that is the whole risk: speech-to-text produces exactly these
            # near-misses, and a mangled company name would silently scope the
            # session to the wrong customer while claiming to have asked.
            if result.needs_confirmation:
                return RouterResponse(
                    ResponseKind.CONFIRM,
                    f"Did you mean {tenant.name} (tenant {tenant.tenant_id})? "
                    f"Say yes to continue.",
                    Intent.CLARIFY,
                    tenant_id=tenant.tenant_id,
                )

            return RouterResponse(
                ResponseKind.ANSWER,
                f"Scoped to {tenant.name} (tenant {tenant.tenant_id}).",
                tenant_id=tenant.tenant_id,
            )

        # Two different failures, and they need different sentences. AMBIGUOUS
        # means several customers genuinely matched and the caller must pick.
        # UNRESOLVED means none did -- the resolver still offers its nearest
        # guesses so the reply can be useful, but calling those "matches" would
        # tell the caller their input was recognised when it was not.
        options = " or ".join(c.name for c in result.candidates)
        if result.method is MatchMethod.AMBIGUOUS:
            return RouterResponse(
                ResponseKind.CLARIFY,
                f"{name!r} matches more than one customer. Did you mean {options}?",
                Intent.CLARIFY,
                candidates=result.candidates,
            )

        suggestion = f" Did you mean {options}?" if result.candidates else ""
        return RouterResponse(
            ResponseKind.CLARIFY,
            f"I don't recognise {name!r} as a customer.{suggestion}",
            Intent.CLARIFY,
            candidates=result.candidates,
        )

    # --- dispatch -------------------------------------------------------------

    def _dispatch_query(self, text: str, context: TenantContext) -> RouterResponse:
        if self._sql_agent is None:
            return RouterResponse(
                ResponseKind.REFUSAL,
                "No language model is configured, so I cannot answer data questions. "
                "Set OPENAI_API_KEY.",
                Intent.DISPATCH_QUERY,
            )
        answer = self._sql_agent.answer(text, context)
        kind = ResponseKind.REFUSAL if answer.refused else ResponseKind.ANSWER
        return RouterResponse(kind, answer.answer, Intent.DISPATCH_QUERY, sql_answer=answer)

    def _triage(self, text: str, context: TenantContext) -> RouterResponse:
        """Triage a ticket named by id, or one pasted into the chat.

        The two arrive as different problems. An id is a lookup into a corpus we
        trust, and the work is deciding whether this session may see it. A pasted
        body is untrusted text, and the work is deciding whose ticket it is --
        see `_triage_pasted`.
        """
        ticket_id = extract_ticket_id(text)
        if ticket_id is None:
            return self._triage_pasted(text, context)

        # Tenant isolation for the JSON half: a scoped session may only triage its
        # own tenant's tickets. Without this check a rep scoped to tenant 4 could
        # pull tenant 7's full customer brief by guessing an id.
        #
        # "Belongs to someone else" and "does not exist" deliberately return the
        # SAME message. Distinguishing them turned the endpoint into an
        # enumeration oracle: ticket ids are sequential four-digit integers, so a
        # scoped user could map every id in use across the platform by sorting
        # responses into "refused" and "not found". Ticket volume per id range is
        # competitive intelligence, and it is the usual precursor to a targeted
        # IDOR. Answering identically costs nothing -- neither reply was
        # actionable to a legitimate user anyway.
        ticket = self._find_ticket(ticket_id)
        invisible = ticket is None or (
            context.is_bound and ticket.tenant_id != context.tenant_id
        )
        if invisible:
            return RouterResponse(
                ResponseKind.CLARIFY, f"I can't find ticket #{ticket_id}.", Intent.TICKET_TRIAGE
            )

        brief = self._triage_agent.build_brief(ticket)
        headline = (
            f"Ticket #{brief.ticket_id} for {brief.context.tenant.name}: "
            f"{brief.assessment.level.value.upper()} "
            f"(score {brief.assessment.score})."
        )
        return RouterResponse(
            ResponseKind.BRIEF,
            f"{headline} {brief.summary}".strip(),
            Intent.TICKET_TRIAGE,
            brief=brief,
        )

    def _triage_pasted(self, text: str, context: TenantContext) -> RouterResponse:
        """Triage a ticket body pasted into the chat rather than named by id.

        The tenant comes from the bound session and never from the pasted text.
        A body can claim to be from any company, and honouring that claim would
        let a scoped rep assemble a brief about a different customer by typing one
        line -- the caller-supplied `tenant_id` hole from security-review.md V1 arriving
        through a different door. So an unscoped session is asked to scope first
        rather than guessing, which is the same answer the resolver gives to an
        ambiguous name: say who you mean.
        """
        # Shape before ownership. "triage that ticket" carries no ticket at all,
        # and answering it with "scope to a customer first" would send the reader
        # to fix the wrong thing.
        if not looks_like_a_ticket(text):
            return RouterResponse(
                ResponseKind.CLARIFY,
                "Which ticket? Give me a ticket number, for example 'triage 1083', "
                "or paste the ticket body.",
                Intent.TICKET_TRIAGE,
            )

        if not context.is_bound:
            return RouterResponse(
                ResponseKind.CLARIFY,
                "Scope to a customer before pasting a ticket -- a brief is built "
                "from one customer's history, contract and call record, so I need "
                "to know whose. Try 'use CFS', or give me a ticket number.",
                Intent.TICKET_TRIAGE,
            )

        tenant = self._repository.get_tenant(context.tenant_id)
        ticket = parse_pasted_ticket(text, tenant.tenant_id, tenant.name)
        if ticket is None:
            return RouterResponse(
                ResponseKind.CLARIFY,
                "Which ticket? Give me a ticket number, for example 'triage 1083', "
                "or paste the ticket with a subject line.",
                Intent.TICKET_TRIAGE,
            )

        brief = self._triage_agent.build_brief(ticket)
        # Named rather than numbered, because a pasted ticket has no id -- and the
        # tenant is stated so the reader can see which customer it was scoped to,
        # which is the one thing the paste did not get to decide.
        headline = (
            f"Pasted ticket for {tenant.name} (tenant {tenant.tenant_id}): "
            f"{brief.assessment.level.value.upper()} "
            f"(score {brief.assessment.score})."
        )
        return RouterResponse(
            ResponseKind.BRIEF,
            f"{headline} {brief.summary}".strip(),
            Intent.TICKET_TRIAGE,
            brief=brief,
        )

    def _find_ticket(self, ticket_id: int):
        for tenant in self._repository.all_tenants():
            for ticket in self._repository.tickets_for(tenant.tenant_id):
                if ticket.ticket_id == ticket_id:
                    return ticket
        return None


def route(text: str, context: TenantContext, llm: LLMClient | None = None) -> RouterResponse:
    """Module-level entry point."""
    return Router(llm=llm).route(text, context)
