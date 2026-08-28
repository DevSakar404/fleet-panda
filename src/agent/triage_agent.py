"""Builds a ticket brief from all five sources.

STUB -- Step 4. The docstring below is the specification.

Owned by: the agent layer. Called by `router.py`. Calls: `Repository`,
`escalation.score_ticket`, `sql_agent` (for the operational snapshot),
`LLMClient`.

Intended flow:

    ticket + TenantContext
      |
      +-- 1. assemble the context pack, five sources, all tenant-scoped:
      |        customer profile        Repository.get_tenant
      |        operational snapshot    sql_agent, TenantContext.for_tenant(...)
      |        past tickets            Repository.tickets_for
      |        call history            Repository.transcripts_for
      |        KB articles             matched on product_area + symptom overlap
      |
      +-- 2. deterministic signals: escalation.score_ticket
      |
      +-- 3. LLM writes the narrative sections only, from the context pack
      |
      +-- 4. assemble the TicketBrief

KB retrieval note (Step 4 decision, pre-argued here): the corpus is 12 articles.
A vector database for 12 documents is a dependency, a model download and an index
to explain, for a ranking problem that `product_area` equality plus symptom token
overlap solves exactly. Start there; reach for embeddings only if it measurably
misses. Note that `billing` tickets have NO KB article at all (RECON.md section
10), so the retrieval step must be able to return nothing rather than surfacing
the least-bad match.

The brief must degrade gracefully: a tenant with no calls, no past tickets or no
matching article still produces a brief, with those sections marked empty. Six of
twelve tenants have no `tank_readings` rows at all (DQ-6).
"""

from __future__ import annotations

from src.agent.session import TenantContext
from src.data.loaders import Ticket


def build_brief(ticket: Ticket, context: TenantContext):
    """Produce a TicketBrief. See module docstring for the flow."""
    raise NotImplementedError("Step 4: ticket triage pipeline")
