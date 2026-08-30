"""Escalation scoring: pure functions, real data, no LLM and no clock.

`today` is injected everywhere so these do not start failing in September. The
fixed date below is the day the scorer was written; contract proximity is measured
against the real calendar rather than the dataset's 91-day-stale window, because
contract dates are forward-looking CRM facts.
"""

from __future__ import annotations

from datetime import date

import pytest

from src import config
from src.agent.escalation import (
    EscalationLevel,
    find_duplicates,
    level_for_score,
    score_ticket,
)

TODAY = date(2026, 8, 29)

# Real decline percentages from recon.md section 11, keyed by tenant.
DECLINE = {1: -1.5, 2: -5.8, 3: -3.7, 4: -16.3, 5: 2.1, 6: 0.7,
           7: 0.4, 8: -14.2, 9: -14.0, 10: 4.5, 11: 27.4, 12: -11.3}


def assess(repository, ticket_id: int):
    """Score one real ticket by id."""
    ticket = next(
        t for tid in range(1, 13) for t in repository.tickets_for(tid)
        if t.ticket_id == ticket_id
    )
    return score_ticket(
        ticket, repository, today=TODAY, volume_change_pct=DECLINE[ticket.tenant_id]
    )


# --- thresholds ---------------------------------------------------------------

@pytest.mark.parametrize(
    "score, expected",
    [
        (0, EscalationLevel.STANDARD),
        (config.ESCALATION_ELEVATED - 1, EscalationLevel.STANDARD),
        (config.ESCALATION_ELEVATED, EscalationLevel.ELEVATED),
        (config.ESCALATION_URGENT, EscalationLevel.URGENT),
        (config.ESCALATION_CRITICAL, EscalationLevel.CRITICAL),
        (500, EscalationLevel.CRITICAL),
    ],
)
def test_level_thresholds_are_inclusive_at_the_boundary(score, expected):
    assert level_for_score(score) is expected


def test_no_single_signal_reaches_critical():
    """CRITICAL must mean several signals agreed.

    If one weight ever grows past the threshold on its own, the level stops
    meaning 'multiple independent things are wrong' and this fails.
    """
    heaviest = max(
        config.WEIGHT_HEALTH_CRITICAL,
        config.WEIGHT_CONTRACT_EXPIRED,
        config.WEIGHT_CARR_HIGH,
        config.WEIGHT_DUPLICATE_CLUSTER,
        config.WEIGHT_MODULE_MISMATCH,
        config.WEIGHT_VOLUME_DECLINE,
        config.WEIGHT_COMPETITOR_MENTIONED,
        max(config.WEIGHT_PRIORITY.values()),
    )
    assert heaviest < config.ESCALATION_CRITICAL


# --- duplicate detection ------------------------------------------------------

def test_finds_the_tank_link_cluster(repository):
    """Ticket #1083 is the 4th filing of one subject by tenant 4 in 26 days."""
    duplicates = find_duplicates(
        next(t for t in repository.tickets_for(4) if t.ticket_id == 1083), repository
    )
    assert {t.ticket_id for t in duplicates} == {1023, 1025, 1027}


def test_a_closed_ticket_still_counts_as_a_duplicate(repository):
    """#1027 was closed on 2026-04-24 and the issue was refiled twice after.

    Treating 'closed' as terminal would report #1083 as a first occurrence (DQ-7).
    """
    duplicates = find_duplicates(
        next(t for t in repository.tickets_for(4) if t.ticket_id == 1083), repository
    )
    assert any(t.status == "closed" for t in duplicates)


def test_duplicates_never_cross_tenants(repository):
    """'Dashboard loading very slowly' is filed by several tenants independently.

    Matching across tenants would leak one tenant's ticket history into another's
    brief -- an isolation failure in the JSON half of the system.
    """
    for tenant_id in range(1, 13):
        for ticket in repository.tickets_for(tenant_id):
            for duplicate in find_duplicates(ticket, repository):
                assert duplicate.tenant_id == tenant_id


def test_a_ticket_is_not_its_own_duplicate(repository):
    for ticket in repository.tickets_for(4):
        assert ticket.ticket_id not in {d.ticket_id for d in find_duplicates(ticket, repository)}


# --- the three mandated triage cases ------------------------------------------

def test_ticket_1083_is_all_three_test_cases_at_once(repository):
    """recon.md section 9: #1083 is simultaneously the low-health/expiring-contract
    case, the duplicate case, and the module-not-active case."""
    result = assess(repository, 1083)

    assert result.level is EscalationLevel.CRITICAL
    assert result.missing_module == "tank_monitor"
    assert set(result.duplicate_ticket_ids) == {1023, 1025, 1027}

    fired = {signal.name for signal in result.signals}
    assert {"health_critical", "contract_expired", "duplicate_cluster",
            "module_mismatch", "volume_decline"} <= fired


def test_the_module_gap_names_the_module_not_just_a_flag(repository):
    """The brief has to say what is missing, not that something is."""
    result = assess(repository, 1083)
    reason = next(s.reason for s in result.signals if s.name == "module_mismatch")
    assert "tank_monitor" in reason
    assert "dispatch, pricing" in reason


# --- the case a health-only rule misses ---------------------------------------

def test_heartland_escalates_despite_passing_the_health_cut(repository):
    """t2 has health 45 -- above the assignment's 'health < 40' phrasing -- and a
    contract expiring 2026-08-30, the day after TODAY.

    This is the case that justifies scoring composed signals rather than matching
    one rule: no health threshold surfaces the most time-critical account on the
    roster.
    """
    result = assess(repository, repository.tickets_for(2)[0].ticket_id)

    assert result.tenant_id == 2
    fired = {s.name for s in result.signals}
    assert "health_critical" not in fired, "t2 is deliberately above the critical cut"
    assert {"health_at_risk", "contract_renewal", "carr_high"} <= fired

    # The claim is about composition, not about one level: five moderate signals,
    # none of which fires CRITICAL alone, lift a routine ticket well clear of
    # STANDARD. Account state alone caps at URGENT (D-012), so a t2 ticket reaches
    # CRITICAL only when the ticket itself adds something -- which is the intended
    # behaviour, not a weaker result.
    assert result.level in (EscalationLevel.URGENT, EscalationLevel.CRITICAL)
    assert result.account_risk == config.MAX_ACCOUNT_RISK_POINTS
    assert result.account_risk_capped


def test_a_healthy_account_does_not_escalate(repository):
    """t6: health 73, contract 2027-09-01, volume flat. Nothing should fire."""
    results = [assess(repository, t.ticket_id) for t in repository.tickets_for(6)]
    assert all(r.level is EscalationLevel.STANDARD for r in results)


def test_levels_span_the_whole_range_across_the_roster(repository):
    """A scorer that returns one level for everything is not triaging."""
    levels = {
        assess(repository, t.ticket_id).level
        for tenant_id in range(1, 13)
        for t in repository.tickets_for(tenant_id)
    }
    assert levels == set(EscalationLevel)


# --- audit trail --------------------------------------------------------------

def test_the_score_is_the_capped_account_risk_plus_the_ticket_risk(repository):
    """The signals are the audit trail. If the score can drift from them, a
    disputed escalation cannot be explained from the record.

    Since D-012 the relationship is `min(account, cap) + ticket` rather than a
    plain sum, and `account_risk_capped` says which of the two applied.
    """
    for tenant_id in range(1, 13):
        for ticket in repository.tickets_for(tenant_id):
            result = score_ticket(ticket, repository, today=TODAY,
                                  volume_change_pct=DECLINE[tenant_id])

            raw_account = sum(s.points for s in result.account_signals)
            assert result.ticket_risk == sum(s.points for s in result.ticket_signals)
            assert result.account_risk == min(raw_account, config.MAX_ACCOUNT_RISK_POINTS)
            assert result.score == result.account_risk + result.ticket_risk
            assert result.account_risk_capped == (raw_account > config.MAX_ACCOUNT_RISK_POINTS)

            if not result.account_risk_capped:
                assert result.score == sum(s.points for s in result.signals)


def test_account_state_alone_never_reaches_critical(repository):
    """The property D-012 exists to create.

    Every ticket carrying no ticket-level signal at all must sit below CRITICAL,
    however bad its account is. Before the cap, all twelve of tenant 4's tickets
    scored CRITICAL and the level could not rank them.
    """
    bare = [
        r for tenant_id in range(1, 13)
        for r in [score_ticket(t, repository, today=TODAY, volume_change_pct=DECLINE[tenant_id])
                  for t in repository.tickets_for(tenant_id)]
        if r.ticket_risk == 0
    ]
    assert bare, "expected at least one ticket with no ticket-level signal"
    assert all(r.level is not EscalationLevel.CRITICAL for r in bare)


def test_the_worst_account_still_spreads_across_levels(repository):
    """Tenant 4 is the most distressed account and its twelve tickets must still
    be rankable against each other."""
    results = [
        score_ticket(t, repository, today=TODAY, volume_change_pct=DECLINE[4])
        for t in repository.tickets_for(4)
    ]
    assert len({r.level for r in results}) > 1
    assert len({r.score for r in results}) > 3


def test_every_signal_carries_a_sentence_a_human_can_read(repository):
    for tenant_id in range(1, 13):
        for ticket in repository.tickets_for(tenant_id):
            for signal in assess(repository, ticket.ticket_id).signals:
                assert signal.reason.endswith((".", "!"))
                assert len(signal.reason) > 20


# --- inputs -------------------------------------------------------------------

def test_omitting_volume_skips_the_signal_rather_than_scoring_zero(repository):
    """'Not measured' and 'measured, no decline' are different, and only one of
    them should be absent from the brief."""
    # Tenant 12: account risk sits below the cap, so the extra points actually
    # move the total. Tenant 4 is capped either way, which would make the score
    # comparison vacuous -- the reason this test names a different tenant.
    ticket = repository.tickets_for(12)[0]

    without = score_ticket(ticket, repository, today=TODAY)
    with_decline = score_ticket(ticket, repository, today=TODAY, volume_change_pct=-16.3)

    assert not with_decline.account_risk_capped
    assert "volume_decline" not in {s.name for s in without.signals}
    assert "volume_decline" in {s.name for s in with_decline.signals}
    assert with_decline.score > without.score


def test_a_decline_above_the_threshold_does_not_fire(repository):
    ticket = next(t for t in repository.tickets_for(4) if t.ticket_id == 1083)
    result = score_ticket(ticket, repository, today=TODAY, volume_change_pct=-1.5)
    assert "volume_decline" not in {s.name for s in result.signals}


def test_contract_signal_moves_with_the_injected_date(repository):
    """Same ticket, three different 'today's: expired, expiring, comfortable."""
    ticket = next(t for t in repository.tickets_for(8) if t.ticket_id == 1049)  # ends 2026-09-10

    def signal_names(today):
        return {s.name for s in score_ticket(ticket, repository, today=today).signals}

    assert "contract_renewal" in signal_names(date(2026, 8, 29))
    assert "contract_expired" in signal_names(date(2026, 9, 20))
    assert "contract_renewal" not in signal_names(date(2026, 1, 1))
    assert "contract_expired" not in signal_names(date(2026, 1, 1))
