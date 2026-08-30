"""Ticket triage: the five-source fan-in.

Runs without an API key. `TriageAgent` narrates only when an LLM is supplied, so
the deterministic half of every brief -- context pack, escalation, KB match -- is
testable on its own, which is also how it behaves in production when narration
fails.
"""

from __future__ import annotations

from datetime import date

import pytest

from src import config
from src.agent.escalation import EscalationLevel
from src.agent.triage_agent import TriageAgent, find_kb_articles
from src.data.loaders import load_knowledge_base
from tests.conftest import FakeLLM

TODAY = date(2026, 8, 29)


@pytest.fixture(scope="module")
def agent():
    return TriageAgent()


def ticket(repository, ticket_id: int):
    return next(
        t for tid in range(1, 13) for t in repository.tickets_for(tid)
        if t.ticket_id == ticket_id
    )


# --- the context pack ---------------------------------------------------------

def test_all_five_sources_are_present_for_ticket_1083(agent, repository):
    """The assignment's headline case. Every source must contribute."""
    context = agent.gather(ticket(repository, 1083))

    assert context.tenant.name == "Desert Sun Petroleum"          # 1. CRM
    assert context.operations.completed_last_30 > 0                # 2. dispatch DB
    assert context.past_tickets                                    # 3. ticket history
    assert context.calls                                           # 4. call history
    assert context.kb_articles                                     # 5. knowledge base
    assert context.missing_sources == ()


def test_the_context_pack_never_crosses_tenants(agent, repository):
    """Every record gathered must belong to the ticket's own tenant."""
    for tenant_id in (1, 4, 8, 11):
        for source_ticket in repository.tickets_for(tenant_id)[:3]:
            context = agent.gather(source_ticket)
            assert context.tenant.tenant_id == tenant_id
            assert all(t.tenant_id == tenant_id for t in context.past_tickets)
            assert all(t.tenant_id == tenant_id for t in context.duplicates)


def test_a_ticket_is_excluded_from_its_own_history(agent, repository):
    context = agent.gather(ticket(repository, 1083))
    assert 1083 not in {t.ticket_id for t in context.past_tickets}


# --- the operational snapshot -------------------------------------------------

def test_the_snapshot_is_tenant_scoped(agent):
    """Tenant 4's numbers must be tenant 4's, not the platform's."""
    snapshot = agent.operational_snapshot(4)
    assert snapshot.completed_last_30 == 87
    assert snapshot.completed_prior_30 == 104
    assert snapshot.volume_change_pct == pytest.approx(-16.3, abs=0.1)
    assert snapshot.is_declining


def test_the_snapshot_agrees_with_the_graded_question(agent):
    """`emergency_last_30` for tenant 4 must match Q5's asserted answer.

    Two windows written with different comparison operators would silently
    disagree; this pins them together.
    """
    assert agent.operational_snapshot(4).emergency_last_30 == 17


def test_the_snapshot_is_anchored_on_the_data_not_the_clock(agent):
    """Anchored on `date('now')` every tenant looks like it stopped delivering."""
    snapshot = agent.operational_snapshot(3)
    assert snapshot.anchor_date == "2026-05-29"
    assert snapshot.completed_last_30 > 0


def test_a_healthy_tenant_is_not_flagged_as_declining(agent):
    assert not agent.operational_snapshot(10).is_declining


# --- knowledge base retrieval -------------------------------------------------

def test_the_tank_link_ticket_finds_the_tank_link_article(repository):
    articles = find_kb_articles(ticket(repository, 1083), load_knowledge_base())
    assert articles[0].article_id == "KB-003"
    assert "TankLink" in articles[0].title


def test_no_more_than_the_configured_number_of_articles(repository):
    for tenant_id in range(1, 13):
        for source_ticket in repository.tickets_for(tenant_id):
            assert len(find_kb_articles(source_ticket, load_knowledge_base())) <= config.KB_MAX_ARTICLES


def test_billing_tickets_return_no_article_rather_than_a_bad_one(repository):
    """recon.md section 10: `billing` is the one ticket area with no KB coverage.

    Surfacing the least-bad match would be worse than saying nothing -- a CSM
    would follow an irrelevant runbook.

    The previous version of this test was vacuous: it asserted no returned article
    had `product_area == "billing"`, which is trivially true because no such
    article exists. It passed while ticket #1048 ("Invoice shows wrong gallon
    count") was being served KB-011 "Tank monitor alert threshold configuration",
    matched on the word "gallon". This version asserts the behaviour the name
    claims.
    """
    billing = [
        t for tid in range(1, 13) for t in repository.tickets_for(tid)
        if t.product_area == "billing"
    ]
    assert billing, "expected billing tickets in the corpus"
    for source_ticket in billing:
        assert find_kb_articles(source_ticket, load_knowledge_base()) == ()


def test_a_cross_area_article_needs_real_symptom_overlap_to_survive(repository):
    """The floor must not throw away genuinely useful cross-area matches.

    "Data not flowing to customer portal" is filed under `integration`; the useful
    article is KB-008 "Customer portal access setup" under `login_access`. It has
    no area match, so it survives only on symptom overlap -- which is exactly the
    case the floor is tuned to keep.
    """
    ticket = next(
        t for tid in range(1, 13) for t in repository.tickets_for(tid)
        if t.subject == "Data not flowing to customer portal"
    )
    matched = find_kb_articles(ticket, load_knowledge_base())
    assert matched and matched[0].article_id == "KB-008"


def test_kb_retrieval_quality_across_the_whole_corpus(repository):
    """A measured floor on retrieval quality, so a scoring change cannot quietly
    degrade it.

    Measured 2026-08-29: 73 of 85 tickets get a top article from their own product
    area, 9 correctly get nothing (8 `billing` plus one with no overlap), and 3 are
    the cross-area customer-portal case above.
    """
    kb = load_knowledge_base()
    same_area = empty = 0
    for tenant_id in range(1, 13):
        for ticket in repository.tickets_for(tenant_id):
            matched = find_kb_articles(ticket, kb)
            if not matched:
                empty += 1
            elif matched[0].product_area == ticket.product_area:
                same_area += 1

    assert same_area >= 73, f"retrieval degraded: {same_area} same-area top hits"
    assert empty <= 10, f"too many tickets get no article at all: {empty}"


def test_articles_are_ranked_by_area_then_recency(repository):
    """A shared product_area must outrank incidental word overlap."""
    matched = find_kb_articles(ticket(repository, 1083), load_knowledge_base())
    assert matched[0].product_area == "tank_monitor"


# --- the brief ----------------------------------------------------------------

def test_a_brief_is_produced_without_an_llm(agent, repository):
    """The deterministic half stands alone -- which is also the degraded mode when
    narration fails."""
    brief = agent.build_brief(ticket(repository, 1083), today=TODAY)

    assert brief.assessment.level is EscalationLevel.CRITICAL
    assert brief.assessment.missing_module == "tank_monitor"
    assert set(brief.assessment.duplicate_ticket_ids) == {1023, 1025, 1027}
    assert brief.summary == ""


def test_the_brief_feeds_the_snapshot_into_escalation(agent, repository):
    """The volume_decline signal can only fire if the dispatch numbers actually
    reached the scorer -- this is the seam between the two halves."""
    brief = agent.build_brief(ticket(repository, 1083), today=TODAY)
    assert "volume_decline" in {s.name for s in brief.assessment.signals}


def test_narration_fills_the_three_prose_sections(repository):
    llm = FakeLLM(
        '{"summary": "Fourth report of a TankLink outage.",'
        ' "escalation_reasoning": "Expired contract and health 28.",'
        ' "suggested_response": "We are investigating."}'
    )
    brief = TriageAgent(llm=llm).build_brief(ticket(repository, 1083), today=TODAY)

    assert brief.summary.startswith("Fourth report")
    assert brief.escalation_reasoning
    assert brief.suggested_response
    assert len(llm.calls) == 1, "one narration call, not one per section"


def test_the_narrator_is_given_the_level_rather_than_asked_for_it(repository):
    """CLAUDE.md 3.4: the model explains the decision, it does not make it."""
    llm = FakeLLM('{"summary": "x"}')
    TriageAgent(llm=llm).build_brief(ticket(repository, 1083), today=TODAY)

    payload = llm.calls[0]["user"]
    assert '"level": "critical"' in payload
    assert "Do not decide the escalation level" in llm.calls[0]["system"]


def test_a_malformed_narrative_costs_formatting_not_the_brief(repository):
    """The deterministic half is already correct; a bad narrative must not discard
    it, and must not discard the model's text either."""
    brief = TriageAgent(llm=FakeLLM("Not JSON at all.")).build_brief(
        ticket(repository, 1083), today=TODAY
    )

    assert brief.summary == "Not JSON at all."
    assert brief.assessment.level is EscalationLevel.CRITICAL


def test_every_ticket_in_the_corpus_produces_a_brief(agent, repository):
    """Degradation check across all 85 -- six tenants have no tank_readings, one
    product area has no KB article, and 37 tickets have no resolution."""
    for tenant_id in range(1, 13):
        for source_ticket in repository.tickets_for(tenant_id):
            brief = agent.build_brief(source_ticket, today=TODAY)
            assert brief.ticket_id == source_ticket.ticket_id
            assert brief.assessment.score >= 0


# --- the three scenarios the assignment asks for ------------------------------
#
# "Test with at least 3 tickets from the provided data": a low-health customer
# with an expiring contract, an apparent duplicate, and a ticket about a module
# the customer does not have.
#
# test_escalation.py asserts that #1083 is all three at once, which is the
# interesting case. These are the opposite: three DIFFERENT tickets from three
# DIFFERENT tenants, each chosen so that exactly one of the three signals fires.
# A case that isolates one signal is what shows the signal works; a case where
# all three fire cannot tell you which one produced the level.

SCENARIO_LOW_HEALTH = 1050      # Timber Ridge Oil (t8) -- health 39, contract in days
SCENARIO_DUPLICATE = 1058       # Prairie Wind Fuels (t9) -- refiling of #1057
SCENARIO_MODULE_GAP = 1005      # Cascade Fuel Services (t1) -- billing, no invoicing


def brief_for(agent, repository, ticket_id: int):
    return agent.build_brief(ticket(repository, ticket_id), today=TODAY)


def test_scenario_low_health_customer_with_an_expiring_contract(agent, repository):
    """Health below the assignment's 40 cut, and a contract inside the renewal
    window -- with no duplicate and no entitlement gap to share the credit."""
    brief = brief_for(agent, repository, SCENARIO_LOW_HEALTH)
    tenant, assessment = brief.context.tenant, brief.assessment

    assert tenant.health_score < config.HEALTH_SCORE_CRITICAL
    days_left = (tenant.contract_end_date - TODAY).days
    assert 0 <= days_left <= config.CONTRACT_RENEWAL_WINDOW_DAYS

    fired = {s.name for s in assessment.signals}
    assert "health_critical" in fired
    assert "contract_renewal" in fired
    # Isolated: neither of the other two scenarios is contributing here.
    assert assessment.duplicate_ticket_ids == ()
    assert assessment.missing_module is None

    # Account state alone reaches URGENT and no further -- the D-012 cap.
    assert assessment.level is EscalationLevel.URGENT
    assert assessment.ticket_risk == 0
    assert assessment.account_risk_capped


def test_scenario_a_ticket_that_duplicates_an_earlier_one(agent, repository):
    """A healthy, well-funded account, so the duplicate is the whole signal."""
    brief = brief_for(agent, repository, SCENARIO_DUPLICATE)
    assessment = brief.assessment

    assert 1057 in assessment.duplicate_ticket_ids
    assert assessment.is_duplicate

    fired = {s.name for s in assessment.signals}
    assert "duplicate" in fired
    # Isolated: the account is healthy and entitled.
    assert "health_critical" not in fired and "health_at_risk" not in fired
    assert assessment.missing_module is None

    # The brief has to say which earlier ticket, not just that there was one.
    reason = next(s.reason for s in assessment.signals if s.name == "duplicate")
    assert "#1057" in reason


def test_scenario_a_ticket_about_a_module_the_customer_lacks(agent, repository):
    """A billing ticket from a tenant with no invoicing module."""
    brief = brief_for(agent, repository, SCENARIO_MODULE_GAP)
    context, assessment = brief.context, brief.assessment

    assert assessment.missing_module == "invoicing"
    assert "invoicing" not in context.tenant.modules_active
    assert context.ticket.product_area == "billing"

    fired = {s.name for s in assessment.signals}
    assert "module_mismatch" in fired
    # Isolated: healthy account, no repeat filing.
    assert assessment.duplicate_ticket_ids == ()
    assert "health_critical" not in fired and "health_at_risk" not in fired

    # billing is the one product area with no KB coverage, so this scenario also
    # pins the honest-empty path: no article rather than the least-bad one.
    assert context.kb_articles == ()
    assert "knowledge base" in context.missing_sources


@pytest.mark.parametrize(
    "ticket_id",
    [SCENARIO_LOW_HEALTH, SCENARIO_DUPLICATE, SCENARIO_MODULE_GAP],
    ids=["low_health", "duplicate", "module_gap"],
)
def test_every_scenario_brief_carries_what_the_brief_must_include(agent, repository, ticket_id):
    """The assignment's list of required brief contents, asserted per scenario.

    The suggested-response draft is not here: it is the one narrative section, so
    it needs an LLM. `test_narration_fills_the_three_prose_sections` covers it.
    """
    brief = brief_for(agent, repository, ticket_id)
    context, assessment = brief.context, brief.assessment

    assert context.tenant.name and context.tenant.assigned_csm      # customer profile
    assert context.tenant.health_score and context.tenant.carr
    assert assessment.level in EscalationLevel                      # escalation + reasoning
    assert assessment.reasons
    assert all(reason.strip() for reason in assessment.reasons)
    assert context.past_tickets                                     # history + duplicates
    assert context.operations.anchor_date                           # operational snapshot
    assert isinstance(context.kb_articles, tuple)                   # KB, possibly empty
    assert isinstance(context.calls, tuple)                         # call context
