"""Deterministic escalation scoring. No LLM anywhere in this file.

STUB -- Step 4. The docstring below is the specification.

Owned by: the agent layer. Called by `triage_agent.py`. Calls: `Repository` and
`config` thresholds only.

Pure functions with unit tests, per CLAUDE.md section 3.4: the LLM writes the
narrative, it does not make the call. A model asked to weigh health score against
CARR against contract proximity will produce a different answer on Tuesday, and
"why was this escalated" has to be answerable from code.

Signals to score, all sourced from recon (RECON.md sections 8, 9, 11):

  health_score      < 40 critical, < 60 at risk        (config.HEALTH_SCORE_*)
  contract_proximity within 90 days is a renewal conversation regardless of
                    ticket priority -- t2 Heartland expires 2026-08-30 with
                    health 45, which no health-only rule would surface
  carr              revenue at risk; t3 at 96k and t11 at 30k are not the same
                    ticket even when the text is identical
  duplicate_count   a 4th filing of the same subject in 26 days is a different
                    problem from a 1st (t4's TankLink cluster). 'closed' does not
                    mean resolved -- one of those four was closed and refiled
                    twice after (DQ-7)
  module_mismatch   asking about a module the tenant is not entitled to; a
                    sales/enablement signal, not a bug (D-002)
  volume_decline    last 30d vs prior 30d below config.DECLINE_THRESHOLD_PCT.
                    t4 and t8 are the steepest decliners AND the two lowest
                    health scores -- the operational and CRM signals agree
  call_sentiment    recent negative calls; competitor_mentioned is the strongest
                    cheap churn signal in the corpus (7 transcripts carry it)

Each returns a score and a human-readable reason string. The reasons are what the
brief prints, so they are written for a CSM to read, not for a log.
"""

from __future__ import annotations

from src.data.loaders import Ticket
from src.data.repository import Repository


def score_ticket(ticket: Ticket, repository: Repository):
    """Compute escalation level and the reasons behind it. Pure, no LLM, no I/O
    beyond the repository."""
    raise NotImplementedError("Step 4: escalation scoring")
