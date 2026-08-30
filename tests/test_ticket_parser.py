"""Parsing a pasted ticket body into a Ticket.

The security-relevant assertion in this file is
`test_the_pasted_text_cannot_choose_its_own_tenant`: the parser takes the tenant
as an argument and there is no path by which the text can influence it. The
session-level half of that rule is asserted in test_router.py.
"""

from __future__ import annotations

from datetime import date

import pytest

from src import config
from src.agent.ticket_parser import (
    DEFAULT_PRIORITY,
    PASTED_TICKET_ID,
    parse_pasted_ticket,
)
from src.data.loaders import load_tickets

WELL_FORMED = """Subject: TankLink readings frozen since Tuesday
product_area: tank_monitor
Priority: High
Submitted by: ops@midwestpetro.com

Our tank monitors stopped reporting on Tuesday morning.
Dashboard shows stale readings and we are dispatching blind."""


def parse(text: str, tenant_id: int = 4, tenant_name: str = "Desert Sun Petroleum"):
    return parse_pasted_ticket(text, tenant_id, tenant_name)


# --- the happy path -----------------------------------------------------------

def test_a_well_formed_paste_yields_every_field():
    ticket = parse(WELL_FORMED)

    assert ticket is not None
    assert ticket.subject == "TankLink readings frozen since Tuesday"
    assert ticket.product_area == "tank_monitor"
    assert ticket.priority == "high"          # normalised from "High"
    assert ticket.submitter_email == "ops@midwestpetro.com"
    assert ticket.status == "open"
    assert ticket.created_at == date.today()


def test_the_description_is_the_text_that_carried_no_label():
    ticket = parse(WELL_FORMED)

    assert "stopped reporting on Tuesday" in ticket.description
    # The header lines were consumed, not left in the body.
    assert "Subject:" not in ticket.description
    assert "product_area:" not in ticket.description
    assert "Submitted by:" not in ticket.description


def test_labels_may_appear_in_any_order_and_any_case():
    ticket = parse(
        "PRIORITY: urgent\n"
        "SUBJECT: Invoice shows wrong gallon count\n"
        "  Product Area:  billing\n"
        "\nThe April invoice double counts one delivery."
    )
    assert ticket.subject == "Invoice shows wrong gallon count"
    assert ticket.product_area == "billing"
    assert ticket.priority == "urgent"


def test_an_unlabelled_paste_uses_its_first_line_as_the_subject():
    ticket = parse("Tank readings showing 0% for all tanks\nSince the firmware update.")
    assert ticket.subject == "Tank readings showing 0% for all tanks"


# --- fail-safe defaults -------------------------------------------------------

def test_text_with_no_subject_is_refused_rather_than_guessed_at():
    """Duplicate detection matches on the subject, so a ticket without one would
    produce a brief built on nothing. The caller falls back to asking for an id."""
    assert parse("") is None
    assert parse("   \n  \n") is None


def test_an_invented_product_area_is_dropped_not_carried():
    """A made-up area would look like a populated field while silently disabling
    KB retrieval and the entitlement check. Blank at least fails visibly."""
    ticket = parse("Subject: Something broke\nproduct_area: quantum_flux\n")
    assert ticket.product_area == ""
    assert ticket.product_area not in config.AREA_TO_MODULE


def test_an_unrecognised_priority_falls_back_to_a_zero_scoring_default():
    """The default must not be able to inflate an escalation."""
    ticket = parse("Subject: Something broke\nPriority: EXTREMELY URGENT!!!\n")
    assert ticket.priority == DEFAULT_PRIORITY
    assert config.WEIGHT_PRIORITY[ticket.priority] == 0


def test_a_missing_priority_gets_the_same_zero_scoring_default():
    assert parse("Subject: Something broke").priority == DEFAULT_PRIORITY


def test_a_bare_subject_line_still_gets_a_searchable_description():
    """KB matching and duplicate detection both read subject AND description."""
    ticket = parse("Subject: Tank readings showing 0% for all tanks")
    assert ticket.description == ticket.subject


# --- the properties the rest of the pipeline relies on ------------------------

def test_the_pasted_id_collides_with_no_real_ticket():
    """`find_duplicates` excludes a ticket from its own duplicate set by id. A
    colliding id would silently hide one real prior filing."""
    real_ids = {t.ticket_id for t in load_tickets()}
    assert PASTED_TICKET_ID not in real_ids


@pytest.mark.parametrize(
    "claim",
    [
        "tenant_id: 7",
        "tenant: Great Lakes Fuel Co",
        "tenant_name: Cascade Fuel Services",
    ],
)
def test_the_pasted_text_cannot_choose_its_own_tenant(claim):
    """The body may claim any company it likes; the caller's tenant is what binds.

    This is security-review.md V1 in a different costume -- a tenant taken from the
    payload rather than from the session.
    """
    ticket = parse(f"Subject: Something broke\n{claim}\n", tenant_id=4)

    assert ticket.tenant_id == 4
    assert ticket.tenant_name == "Desert Sun Petroleum"
    # The claim was consumed as a header rather than left to pollute the body.
    assert claim not in ticket.description
