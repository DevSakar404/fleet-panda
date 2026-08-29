"""Session state: scope switching, and the tenant confirmation gate.

This logic lived in `cli_chat.main()` until voice mode needed it too, where it was
untestable -- it was interleaved with `input()` and `print()`. Extracting it to
`Conversation` is what makes the gate below assertable, and the gate is the reason
the extraction was worth doing: it is a security control, and a control that
exists in two transports is a control that will eventually differ between them.
"""

from __future__ import annotations

import pytest

from src.agent.conversation import AFFIRMATIVES, Conversation
from src.agent.router import ResponseKind, Router
from src.agent.session import SessionScope, TenantContext


@pytest.fixture()
def conversation(repository) -> Conversation:
    """A session with no LLM. Every path exercised here is deterministic --
    binding, refusing and confirming never reach a model."""
    return Conversation(Router(repository=repository))


# --- scope ------------------------------------------------------------------


def test_a_session_starts_unscoped(conversation):
    """Platform scope is the default because binding is an explicit act."""
    assert conversation.context.scope is SessionScope.PLATFORM


def test_an_exact_alias_binds_immediately(conversation):
    """'CFS' is in the curated alias table, so there is nothing to confirm."""
    response = conversation.handle("use CFS")

    assert response.kind is ResponseKind.ANSWER
    assert conversation.context.tenant_id == 1
    assert conversation.pending_tenant is None


def test_platform_returns_to_an_unscoped_session(conversation):
    conversation.handle("use CFS")
    conversation.handle("platform")

    assert conversation.context.scope is SessionScope.PLATFORM
    assert conversation.context.tenant_id is None


def test_an_unknown_name_binds_nothing(conversation):
    response = conversation.handle("use Wobblegong Oil")

    assert response.kind is ResponseKind.CLARIFY
    assert conversation.context.scope is SessionScope.PLATFORM
    assert conversation.pending_tenant is None


def test_an_ambiguous_name_binds_nothing(conversation):
    """'Fuel' scores 100 against several tenants. It must arm nothing at all --
    not even a pending confirmation, because we do not know which one to offer."""
    response = conversation.handle("use Fuel")

    assert response.kind is ResponseKind.CLARIFY
    assert conversation.pending_tenant is None
    assert conversation.context.scope is SessionScope.PLATFORM


# --- the confirmation gate --------------------------------------------------


def test_an_inexact_match_asks_and_does_not_bind(conversation):
    """The whole point. A near-miss arms a question; it does not scope anything.

    Speech-to-text produces exactly these near-misses, so if CONFIRM bound the
    session the caller would be looking at the wrong customer's data while the
    agent claimed to have asked.
    """
    response = conversation.handle("use Cascade Fuel Servces")

    assert response.kind is ResponseKind.CONFIRM
    assert conversation.pending_tenant == 1
    assert conversation.context.scope is SessionScope.PLATFORM


@pytest.mark.parametrize("reply", sorted(AFFIRMATIVES))
def test_an_explicit_yes_binds(conversation, reply):
    conversation.handle("use Cascade Fuel Servces")
    response = conversation.handle(reply)

    assert response.kind is ResponseKind.ANSWER
    assert conversation.context.tenant_id == 1
    assert conversation.pending_tenant is None


@pytest.mark.parametrize(
    "reply",
    ["no", "nope", "wait", "how many deliveries last week?", "use something else",
     "yes please but actually", "maybe", "platform", "quit"],
)
def test_anything_other_than_yes_cancels(conversation, reply):
    """Cancels, and clears. An unrelated reply is not consent, and a question
    asked while a confirmation is outstanding must not answer it either."""
    conversation.handle("use Cascade Fuel Servces")
    conversation.handle(reply)

    assert conversation.context.scope is SessionScope.PLATFORM
    assert conversation.pending_tenant is None


def test_a_pending_confirmation_outranks_every_command(conversation):
    """'platform' is a command, but while a confirmation is outstanding it is
    read as "not yes" and cancels instead.

    Letting commands jump the queue would leave the confirmation armed and
    answerable by a later, unrelated 'yes' -- which is how a session ends up
    scoped to a customer nobody named in that turn.
    """
    conversation.handle("use CFS")
    conversation.handle("use Summit Enrgy Group")
    assert conversation.pending_tenant is not None

    conversation.handle("platform")

    assert conversation.pending_tenant is None
    assert conversation.context.tenant_id == 1  # unchanged by the cancelled confirm


def test_a_cancelled_confirmation_leaves_a_previous_scope_intact(conversation):
    conversation.handle("use CFS")
    conversation.handle("use Summit Enrgy Group")
    conversation.handle("no")

    assert conversation.context.tenant_id == 1


def test_confirmation_does_not_survive_into_the_next_turn(conversation):
    """One outstanding question, answered once. A 'yes' arriving two turns later
    must not retroactively bind the tenant that was offered."""
    conversation.handle("use Cascade Fuel Servces")
    conversation.handle("no")
    conversation.handle("yes")

    assert conversation.context.scope is SessionScope.PLATFORM


# --- lifecycle --------------------------------------------------------------


@pytest.mark.parametrize("word", ["quit", "exit", "bye", "QUIT"])
def test_exit_words_end_the_session(conversation, word):
    conversation.handle(word)
    assert conversation.finished is True


def test_empty_input_is_a_clarify_not_a_crash(conversation):
    assert conversation.handle("   ").kind is ResponseKind.CLARIFY
    assert conversation.finished is False


def test_scope_reports_without_changing_anything(conversation):
    conversation.handle("use CFS")
    before = conversation.context

    response = conversation.handle("scope")

    assert "tenant 1" in response.text
    assert conversation.context is before
