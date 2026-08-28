"""Entity resolution tests.

Seeded from Step 0 recon (RECON.md section 6) rather than invented: the ambiguous
probes below are the exact strings that made a score-gated resolver return the
wrong tenant at full confidence, which is what motivated DECISIONS.md D-003.

The tests are grouped by what they protect:
  - the cascade returns the right id by the cheapest method that can
  - ambiguity refuses instead of guessing        <- the security-relevant ones
  - every name in the real data resolves
"""

from __future__ import annotations

import pytest

from src.data.loaders import load_call_transcripts, load_tickets
from src.data.resolver import MatchMethod, TenantResolver, normalize


# --- the cascade -------------------------------------------------------------

@pytest.mark.parametrize(
    "name, expected_id, expected_method",
    [
        # 1. exact canonical
        ("Cascade Fuel Services", 1, MatchMethod.EXACT_CANONICAL),
        ("Desert Sun Petroleum", 4, MatchMethod.EXACT_CANONICAL),
        # 2. exact alias -- including the short forms that a length-sensitive
        #    scorer would reject, which is why token_set_ratio is kept
        ("CFS", 1, MatchMethod.EXACT_ALIAS),
        ("NSP", 11, MatchMethod.EXACT_ALIAS),
        ("PWF", 9, MatchMethod.EXACT_ALIAS),
        ("SEG", 3, MatchMethod.EXACT_ALIAS),
        ("Cascade Fuel Svcs", 1, MatchMethod.EXACT_ALIAS),
        # 3. normalised: legal suffix and punctuation stripped
        ("Summit Energy Group Inc", 3, MatchMethod.NORMALIZED),
        ("Cascade Fuel Services, LLC", 1, MatchMethod.NORMALIZED),
        ("summit energy group", 3, MatchMethod.NORMALIZED),
        # 4. fuzzy: the transcription errors voice mode will actually produce
        ("Cascade Fuel Servces", 1, MatchMethod.FUZZY),
        ("Timber Ridge Oyl", 8, MatchMethod.FUZZY),
    ],
)
def test_cascade_resolves_by_expected_method(resolver, name, expected_id, expected_method):
    result = resolver.resolve(name)
    assert result.tenant_id == expected_id
    assert result.method is expected_method


def test_exact_matches_do_not_ask_for_confirmation(resolver):
    """A curated alias is trusted silently; an inexact match is read back.

    Voice mode branches on this, so it is behaviour and not a detail.
    """
    assert resolver.resolve("CFS").needs_confirmation is False
    assert resolver.resolve("Cascade Fuel Services").needs_confirmation is False
    assert resolver.resolve("Cascade Fuel Servces").needs_confirmation is True
    assert resolver.resolve("Summit Energy Group Inc").needs_confirmation is True


# --- ambiguity must refuse (the security-relevant cases) ---------------------

@pytest.mark.parametrize(
    "probe, expected_candidates",
    [
        # token_set_ratio scores each of these 100 against several tenants,
        # because a subset of tokens is a perfect match. A score-gated resolver
        # returns the first one and leaks. See RECON.md section 6.
        ("Fuel", {1, 5, 6}),
        ("Energy", {3, 7, 12}),
        ("propane", {2, 11}),
    ],
)
def test_ambiguous_names_refuse_and_list_candidates(resolver, probe, expected_candidates):
    result = resolver.resolve(probe)
    assert result.tenant_id is None, f"{probe!r} must not resolve to a single tenant"
    assert result.method is MatchMethod.AMBIGUOUS
    returned = {c.tenant_id for c in result.candidates}
    assert returned <= expected_candidates
    assert len(returned) > 1, "an ambiguous result must offer more than one candidate"


def test_ambiguity_is_not_rescued_by_a_high_score(resolver):
    """The gate is candidate count, not confidence.

    This is the regression test for the whole design: 'Fuel' reports 100.0
    confidence *and* refuses. If someone later 'fixes' the resolver to return the
    top scorer, this fails.
    """
    result = resolver.resolve("Fuel")
    assert result.confidence == pytest.approx(100.0)
    assert result.tenant_id is None


def test_unknown_name_is_unresolved_not_guessed(resolver):
    result = resolver.resolve("Wobblegong Oil Partners")
    assert result.tenant_id is None
    assert result.method is MatchMethod.UNRESOLVED


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_input_resolves_to_nothing(resolver, empty):
    result = resolver.resolve(empty)
    assert result.tenant_id is None
    assert result.method is MatchMethod.UNRESOLVED


# --- the real data -----------------------------------------------------------

def test_every_transcript_name_resolves(resolver):
    """All 26 distinct tenant_name values in call_transcripts.json resolve.

    Recon found zero failures here. The test exists so that a future alias-table
    edit that breaks one of them fails loudly instead of silently dropping call
    history from a tenant's brief.
    """
    failures = [
        (name, resolver.resolve(name).method.value)
        for name in sorted({t.tenant_name for t in load_call_transcripts()})
        if not resolver.resolve(name).is_resolved
    ]
    assert failures == []


def test_ticket_tenant_name_agrees_with_ticket_tenant_id():
    """tickets.json carries both fields; recon found all 85 agree.

    A disagreement would mean one of the two is wrong, and we would have to decide
    which to trust. Asserting it here means we find out at test time rather than
    while assembling a brief for the wrong company.
    """
    resolver = TenantResolver()
    disagreements = [
        (t.ticket_id, t.tenant_id, t.tenant_name, resolver.resolve(t.tenant_name).tenant_id)
        for t in load_tickets()
        if resolver.resolve(t.tenant_name).tenant_id != t.tenant_id
    ]
    assert disagreements == []


# --- normalisation -----------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Summit Energy Group, Inc.", "summit energy group"),
        ("Cascade Fuel Services LLC", "cascade fuel services"),
        ("Great Lakes Fuel Co", "great lakes fuel"),
        ("  Timber   Ridge  Oil  ", "timber ridge oil"),
        ("N-Star Propane", "n star propane"),
    ],
)
def test_normalize_strips_suffixes_and_punctuation(raw, expected):
    assert normalize(raw) == expected


def test_normalize_keeps_words_that_only_look_like_suffixes():
    """'Co' is a legal suffix; 'Coast' and 'Company Fuels' are not.

    The suffix pattern uses word boundaries for exactly this reason -- a substring
    match would turn 'Atlantic Coast Energy' into 'atlantic ast energy'.
    """
    assert normalize("Atlantic Coast Energy") == "atlantic coast energy"
    assert "coast" in normalize("Atlantic Coast Energy")
