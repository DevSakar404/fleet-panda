"""Decides what an incoming request is, and refuses what this session may not ask.

STUB -- Step 4. The docstring below is the specification.

Owned by: the agent layer. Called by `interfaces/cli_chat.py` and (Step 5) the
voice interface. Calls: `TenantResolver`, `TenantContext`, `sql_agent`,
`triage_agent`, `LLMClient`.

Intended flow:

    text in
      |
      +-- 1. resolve any tenant named in the text (TenantResolver)
      |        unresolved or ambiguous -> return a clarify response with the
      |        ranked candidates; ask, never guess (DECISIONS.md D-003)
      |
      +-- 2. classify intent: dispatch_query | ticket_triage | clarify
      |        cheap heuristics first (a pasted ticket has a subject line and a
      |        product area; a question ends in '?'), LLM only when those are
      |        inconclusive -- the classifier is on the voice critical path and a
      |        whole extra round trip for a two-way branch is the wrong default
      |
      +-- 3. authority check: TenantContext.allows_question()
      |        a cross-tenant question in a tenant-scoped session is refused
      |        here, with the reason, and never silently narrowed to one tenant
      |
      +-- 4. dispatch to sql_agent or triage_agent
      |
      +-- 5. return a uniform response object so chat and voice render the same
               thing (CLAUDE.md section 2: transports over one core)

Open question for Step 4: whether step 2 can be folded into the SQL agent's own
structured output to save a round trip. See OPEN_QUESTIONS.md Q-007.
"""

from __future__ import annotations

from src.agent.session import TenantContext


def route(text: str, context: TenantContext):
    """Classify `text` and dispatch it. See module docstring for the flow."""
    raise NotImplementedError("Step 4: agent routing layer")
