"""Terminal chat transport. A thin shell over the conversation.

Owned by: the interfaces layer. Called by the user. Calls: `Conversation`.

A transport, not an implementation (CLAUDE.md section 2) -- it turns typed lines
into `Conversation.handle()` calls and formats what comes back. `voice_chat.py` is
the sibling of this file: same `Conversation`, same `RouterResponse`, different
rendering. Nothing about scope, tenant binding or the confirmation gate lives
here, which is what makes "the same intelligence in both modes" structural rather
than a promise.

It runs without an API key: tenant binding, ticket triage and every refusal path
are deterministic. Only data questions need a model, and the absence of one
produces a clear message rather than a stack trace.

The executed SQL is printed alongside every data answer on purpose. The demo turns
on being able to point at the guard's rewritten query and show the injected
predicate that was never in the model's output.
"""

from __future__ import annotations

from src import config
from src.agent.conversation import Conversation
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

    # Printed as well as sent to the narrator. The history is half of what a CSM
    # reads a brief for, and without a model configured the narrative sections are
    # empty -- so a brief that only fed past tickets to the prompt showed none at
    # all on the path that runs without an API key.
    if context.past_tickets:
        lines += ["", "  PAST TICKETS"]
        for past in context.past_tickets:
            # Status only. `resolution` is deliberately not summarised beside it:
            # the two disagree in this corpus -- #1031 is status 'open' with a
            # resolution text, #1032 is 'resolved' with none -- so printing both
            # on one line reads as a rendering bug rather than as the data quirk
            # it is. The narrator still receives every resolution in full.
            lines.append(
                f"             #{past.ticket_id}  {past.created_at}  "
                f"{past.status:<8} {past.subject}"
            )

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


def _load_env() -> None:
    """Read `.env` into the environment, if one exists.

    Called from the entrypoint rather than from `LLMClient`: a library should read
    its configuration from the environment, not decide how the environment got
    populated. Each entrypoint (this CLI, and later the voice transport) loads it
    once at startup.

    `override=False` so an exported ANTHROPIC_API_KEY in the shell wins over a
    stale `.env` -- the opposite is a confusing hour of debugging.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is pinned in requirements
        return
    load_dotenv(config.PROJECT_ROOT / ".env", override=False)


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
    _load_env()
    llm = _build_llm()
    conversation = Conversation(Router(llm=llm))

    print(BANNER)
    while not conversation.finished:
        try:
            line = input(f"[{_prompt_label(conversation.context)}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not line:
            continue

        # `help` is presentation, so it stays here rather than in Conversation --
        # the banner is a terminal artefact and voice mode says something else
        # entirely. Everything that changes session state goes through handle().
        if line.lower() in ("help", "?"):
            print(BANNER)
            continue

        print(format_response(conversation.handle(line)) + "\n")


def _prompt_label(context: TenantContext) -> str:
    return "platform" if not context.is_bound else f"tenant {context.tenant_id}"


if __name__ == "__main__":
    main()
