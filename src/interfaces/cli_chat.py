"""Terminal chat transport. A thin shell over the router.

Owned by: the interfaces layer. Called by the user. Calls: `Router`.

A transport, not an implementation (CLAUDE.md section 2) -- it turns typed lines
into `route()` calls and formats what comes back. The voice interface in Step 5 is
a sibling of this file and shares everything below `route`.

It runs without an API key: tenant binding, ticket triage and every refusal path
are deterministic. Only data questions need a model, and the absence of one
produces a clear message rather than a stack trace.

The executed SQL is printed alongside every data answer on purpose. The demo turns
on being able to point at the guard's rewritten query and show the injected
predicate that was never in the model's output.
"""

from __future__ import annotations

import os
import sys

from src.agent.router import ResponseKind, Router, RouterResponse
from src.agent.session import TenantContext
from src.agent.triage_agent import TicketBrief
from src.llm.client import LLMClient, LLMConfigurationError

BANNER = """FleetPanda support agent -- chat mode
  use <company>     scope the session to one customer (try: use CFS)
  platform          switch to an internal, cross-tenant session
  scope             show the current session scope
  triage <id>       build a ticket brief (try: triage 1083)
  <question>        ask about delivery data
  quit              exit
"""


def _format_brief(brief: TicketBrief) -> str:
    """The full brief. Longer than anything voice would read aloud."""
    context, assessment = brief.context, brief.assessment
    tenant, operations = context.tenant, context.operations

    lines = [
        "",
        f"  CUSTOMER   {tenant.name} (tenant {tenant.tenant_id}) -- CSM {tenant.assigned_csm}",
        f"             health {tenant.health_score} | ${tenant.carr:,} CARR | "
        f"contract ends {tenant.contract_end_date} | {tenant.onboarding_status}",
        f"             modules: {', '.join(sorted(tenant.modules_active))}",
        "",
        f"  ESCALATION {assessment.level.value.upper()}  "
        f"(score {assessment.score} = account {assessment.account_risk}"
        f"{' capped' if assessment.account_risk_capped else ''} + ticket {assessment.ticket_risk})",
    ]
    lines += [f"             - {reason}" for reason in assessment.reasons]

    if assessment.duplicate_ticket_ids:
        ids = ", ".join(f"#{i}" for i in assessment.duplicate_ticket_ids)
        lines += ["", f"  DUPLICATES {ids}"]

    lines += [
        "",
        f"  DISPATCH   {operations.completed_last_30} completed in the last 30 days "
        f"vs {operations.completed_prior_30} in the prior 30"
        + (f" ({operations.volume_change_pct:+.1f}%)" if operations.volume_change_pct is not None else ""),
        f"             {operations.emergency_last_30} emergency orders | "
        f"{operations.open_orders} open | windows anchored on {operations.anchor_date}",
    ]

    if context.calls:
        lines += ["", "  CALLS"]
        for call in context.calls:
            flag = " [competitor mentioned]" if call.competitor_mentioned else ""
            lines.append(f"             {call.call_date}  {call.sentiment:<8} {call.topic}{flag}")

    if context.kb_articles:
        lines += ["", "  KNOWLEDGE BASE"]
        for article in context.kb_articles:
            lines.append(f"             {article.article_id}  {article.title} "
                         f"(updated {article.updated_at})")
    else:
        lines += ["", "  KNOWLEDGE BASE  no article matches this product area"]

    if context.missing_sources:
        lines += ["", f"  NO DATA FROM  {', '.join(context.missing_sources)}"]

    if brief.summary:
        lines += ["", f"  SUMMARY    {brief.summary}"]
    if brief.escalation_reasoning:
        lines += ["", f"  REASONING  {brief.escalation_reasoning}"]
    if brief.suggested_response:
        lines += ["", f"  DRAFT      {brief.suggested_response}"]

    return "\n".join(lines)


def format_response(response: RouterResponse) -> str:
    """Render any router response for a terminal."""
    if response.kind is ResponseKind.BRIEF and response.brief is not None:
        return _format_brief(response.brief)

    parts = [response.text]
    answer = response.sql_answer
    if answer is not None and answer.sql:
        parts += ["", f"  SQL   {answer.sql}",
                  f"  rows  {answer.row_count}"
                  + (" (truncated)" if answer.truncated else "")]
        if answer.assumptions:
            parts.append(f"  note  {answer.assumptions}")
    return "\n".join(parts)


def _build_llm() -> LLMClient | None:
    """An LLM if one is configured, otherwise None and a warning.

    Deliberately not fatal: triage, tenant resolution and every isolation refusal
    work without a model, and being able to demo those on a machine with no key is
    worth more than insisting on one.
    """
    try:
        return LLMClient()
    except LLMConfigurationError as exc:
        print(f"! {exc}\n! Running without a model: triage and scoping work, "
              f"data questions will not.\n")
        return None


def main() -> None:
    """Run the terminal chat loop."""
    llm = _build_llm()
    router = Router(llm=llm)
    context = TenantContext.platform()
    # A tenant identified by an inexact match, held until the user says yes.
    # Nothing binds it; the session stays on its previous scope until confirmed.
    pending_tenant: int | None = None

    print(BANNER)
    while True:
        try:
            line = input(f"[{_prompt_label(context)}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not line:
            continue
        lowered = line.lower()

        # A pending confirmation consumes the next line, whatever it is. Anything
        # other than an explicit yes cancels -- silence or an unrelated answer
        # must never be read as consent, which over voice is the difference
        # between scoping to the right customer and the wrong one.
        if pending_tenant is not None:
            if lowered in ("yes", "y", "yeah", "yep", "correct", "that's right"):
                context = TenantContext.for_tenant(pending_tenant)
                print(f"  {_scope_description(context)}\n")
            else:
                print("  Cancelled -- scope unchanged. Try the full company name "
                      "or its short code.\n")
            pending_tenant = None
            continue

        if lowered in ("quit", "exit"):
            return
        if lowered in ("help", "?"):
            print(BANNER)
            continue
        if lowered == "scope":
            print(f"  {_scope_description(context)}\n")
            continue
        if lowered == "platform":
            context = TenantContext.platform()
            print("  Switched to an internal platform session. Cross-tenant "
                  "questions are allowed here.\n")
            continue
        if lowered.startswith(("use tenant ", "use ")):
            name = line.split(" ", 2)[-1] if lowered.startswith("use tenant ") else line[4:]
            response = router.resolve_tenant(name.strip())
            print(f"  {response.text}\n")

            # The response carries the tenant id, so the resolver runs once and
            # this file never reaches into the router's internals to recover an
            # id it was already handed.
            if response.kind is ResponseKind.CONFIRM:
                pending_tenant = response.tenant_id
            elif response.kind is ResponseKind.ANSWER and response.tenant_id is not None:
                context = TenantContext.for_tenant(response.tenant_id)
            continue

        print(format_response(router.route(line, context)) + "\n")


def _prompt_label(context: TenantContext) -> str:
    return "platform" if not context.is_bound else f"tenant {context.tenant_id}"


def _scope_description(context: TenantContext) -> str:
    if context.is_bound:
        return (f"Scoped to tenant {context.tenant_id}. Cross-tenant questions "
                f"will be refused, and every query is filtered to this tenant.")
    return "Internal platform session. Cross-tenant questions are allowed."


if __name__ == "__main__":
    main()
