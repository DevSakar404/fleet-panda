"""Router: intent classification, tenant binding, and the JSON-side isolation check.

No API key needed. The router falls back to heuristics when no model is
configured, and triage is deterministic, so most paths run unmodelled.
"""

from __future__ import annotations

import pytest

from src.agent.router import Intent, ResponseKind, Router, extract_ticket_id
from src.agent.session import TenantContext
from tests.conftest import FakeLLM


@pytest.fixture(scope="module")
def router():
    return Router()


# --- ticket id extraction -----------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("triage 1083", 1083),
        ("ticket #1083", 1083),
        ("ticket 1083", 1083),
        ("#1083", 1083),
        ("1083", 1083),
        ("escalate 1083", 1083),
        ("triage ticket 1083 please", 1083),
        # A bare four-digit number that is not a ticket reference.
        ("How many gallons were delivered in 2026?", None),
        ("show me the top 5 drivers", None),
    ],
)
def test_ticket_ids_are_extracted_without_false_positives(text, expected):
    """Every year in this corpus is also four digits, so a bare number only counts
    when it is the whole input or follows a cue word."""
    assert extract_ticket_id(text) == expected


# --- intent classification ----------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("triage 1083", Intent.TICKET_TRIAGE),
        ("escalate ticket 1049", Intent.TICKET_TRIAGE),
        ("brief me on 1025", Intent.TICKET_TRIAGE),
        ("How many deliveries were completed last week?", Intent.DISPATCH_QUERY),
        ("Which tenant delivered the most diesel?", Intent.DISPATCH_QUERY),
        ("show me the top 5 drivers for tenant 3", Intent.DISPATCH_QUERY),
        ("list tenants with declining volume", Intent.DISPATCH_QUERY),
    ],
)
def test_unambiguous_input_never_costs_an_llm_call(router, text, expected):
    """Heuristics first: this sits on the voice critical path, where an extra
    round trip is an extra second of silence."""
    assert router.classify(text) is expected


def test_a_pasted_ticket_body_is_recognised_as_triage(router):
    pasted = (
        "Subject: TankLink device not sending data\n"
        "product_area: tank_monitor\n"
        "priority: high\n"
        "Our tanks have shown no readings since Tuesday."
    )
    assert router.classify(pasted) is Intent.TICKET_TRIAGE


def test_ambiguous_input_asks_the_model_when_one_is_available():
    llm = FakeLLM("ticket_triage")
    assert Router(llm=llm).classify("the thing from yesterday") is Intent.TICKET_TRIAGE
    assert len(llm.calls) == 1


def test_ambiguous_input_without_a_model_falls_through_to_the_query_path(router):
    """Better to let the SQL agent produce its own refusal than to answer
    'I don't know what you meant'."""
    assert router.classify("the thing from yesterday") is Intent.DISPATCH_QUERY


# --- tenant binding -----------------------------------------------------------

def test_an_exact_alias_binds_without_asking(router):
    response = router.resolve_tenant("CFS")
    assert response.kind is ResponseKind.ANSWER
    assert "Cascade Fuel Services" in response.text
    assert "confirm" not in response.text


def test_a_fuzzy_match_asks_for_confirmation(router):
    response = router.resolve_tenant("Cascade Fuel Servces")
    assert response.kind is ResponseKind.ANSWER
    assert "confirm" in response.text


def test_an_ambiguous_name_offers_candidates_rather_than_guessing(router):
    """The clarify path is why ResolutionResult carries candidates -- over voice,
    'did you mean X or Y?' is usable and 'unresolved' is not."""
    response = router.resolve_tenant("Fuel")
    assert response.kind is ResponseKind.CLARIFY
    assert len(response.candidates) > 1
    assert "Cascade Fuel Services" in response.text


def test_an_unknown_name_is_not_reported_as_an_ambiguous_match(router):
    """"Wobblegong Oil" scores below the threshold against everything, but the
    resolver still returns its nearest guesses so the reply can be useful.

    Those guesses must not be described as matches -- telling a caller their input
    "matches more than one customer" says it was recognised when it was not.
    """
    response = router.resolve_tenant("Wobblegong Oil")
    assert response.kind is ResponseKind.CLARIFY
    assert "don't recognise" in response.text
    assert "matches more than one" not in response.text


def test_a_name_matching_nothing_at_all_offers_no_suggestion(router):
    response = router.resolve_tenant("zzzzzzzz")
    assert response.kind is ResponseKind.CLARIFY
    assert "Did you mean" not in response.text


# --- isolation on the JSON side ----------------------------------------------

def test_a_scoped_session_cannot_triage_another_tenants_ticket(router):
    """Without this check a rep scoped to tenant 1 could pull tenant 4's full
    customer brief -- health score, CARR, contract, call history -- by guessing an
    id. The SQL guard does not cover this path; it is not a SQL query."""
    response = router.route("triage 1083", TenantContext.for_tenant(1))
    assert response.kind is ResponseKind.REFUSAL
    assert response.brief is None
    assert "another customer" in response.text


def test_a_scoped_session_can_triage_its_own_ticket(router):
    response = router.route("triage 1083", TenantContext.for_tenant(4))
    assert response.kind is ResponseKind.BRIEF
    assert response.brief.tenant_id == 4


def test_a_platform_session_can_triage_any_ticket(router):
    assert router.route("triage 1083", TenantContext.platform()).kind is ResponseKind.BRIEF


def test_an_unknown_ticket_is_reported_not_invented(router):
    response = router.route("triage 9999", TenantContext.platform())
    assert response.kind is ResponseKind.CLARIFY
    assert "can't find" in response.text


def test_triage_without_an_id_asks_for_one(router):
    response = router.route("triage that ticket", TenantContext.platform())
    assert response.kind is ResponseKind.CLARIFY
    assert "Which ticket" in response.text


# --- degraded mode ------------------------------------------------------------

def test_a_data_question_without_a_model_refuses_clearly(router):
    response = router.route("How many deliveries last week?", TenantContext.platform())
    assert response.kind is ResponseKind.REFUSAL
    assert "ANTHROPIC_API_KEY" in response.text


def test_empty_input_is_a_clarify_not_a_crash(router):
    for text in ("", "   ", "\n"):
        assert router.route(text, TenantContext.platform()).kind is ResponseKind.CLARIFY
