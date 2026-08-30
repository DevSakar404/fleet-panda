"""Voice mode without a microphone.

Everything voice *decides* is testable; only the three SDK calls in `speech.py`
are not, and a mocked microphone would only prove the mock works. So this file
asserts the two things this transport owns -- what gets spoken, and what
speech-to-text damage gets repaired -- plus the property that matters most: voice
inherits the confirmation gate rather than reimplementing it.
"""

from __future__ import annotations

import pytest

from src import config
from src.agent.conversation import Conversation
from src.agent.router import ResponseKind, Router, RouterResponse
from src.agent.session import SessionScope, TenantContext
from src.agent.triage_agent import TriageAgent
from src.interfaces.voice_chat import normalize_transcript, speakable, spoken_text


# --- rewriting our own prose for the ear ------------------------------------


@pytest.mark.parametrize(
    "written, spoken",
    [
        ("Contract expired on 2026-07-15.", "Contract expired on 15 July 2026."),
        ("in the 7 days to 2026-05-29", "in the 7 days to 29 May 2026"),
        ("2026-01-01 and 2026-12-31", "1 January 2026 and 31 December 2026"),
    ],
)
def test_iso_dates_are_rewritten_for_the_ear(written, spoken):
    """Every escalation reason carries an ISO date, because it was written for a
    terminal. Read aloud it is a run of digits and dashes."""
    assert speakable(written) == spoken


def test_a_number_that_is_not_a_date_is_left_alone():
    assert speakable("score 90, health 28") == "score 90, health 28"


def test_an_impossible_month_is_left_alone():
    """Fail closed on anything that only looks like a date -- saying the digits is
    better than indexing off the end of the month table."""
    assert "2026-13-01" in speakable("ticket ref 2026-13-01")


def test_the_house_style_aside_becomes_a_comma():
    """'--' is read as characters by some voices and skipped by others."""
    assert speakable("a renewal conversation -- not only a ticket") == (
        "a renewal conversation, not only a ticket"
    )


# --- transcript repair ------------------------------------------------------


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("use C F S", "use CFS"),
        ("use c f s", "use cfs"),
        ("Use C F S.", "Use CFS"),
        ("use G L F C", "use GLFC"),
        # Real words are left alone -- the run has to be single letters.
        ("use Cascade Fuel Services", "use Cascade Fuel Services"),
        ("how many deliveries last week", "how many deliveries last week"),
    ],
)
def test_spelled_out_codes_are_collapsed(heard, expected):
    """`tenant_aliases.json` has 'CFS'; the microphone produces 'C F S'.

    The resolver normalises case but not spacing, so without this repair a short
    code that binds instantly in chat fails over voice.
    """
    assert normalize_transcript(heard) == expected


def test_a_two_letter_run_is_left_alone():
    """Two would fire on ordinary English. Every alias in the table is longer."""
    assert normalize_transcript("is a b tested") == "is a b tested"


@pytest.mark.parametrize(
    "heard, expected",
    [
        # Grouping commas that speech-to-text inserts into a 4-digit ticket id.
        ("triage ticket 1,083", "triage ticket 1083"),
        ("ticket 10,830", "ticket 10830"),
        # A dictated tenant number becomes a digit; ordinary prose "three" does not.
        ("top drivers for tenant three", "top drivers for tenant 3"),
        ("emergency orders tenant four", "emergency orders tenant 4"),
        ("tenant twelve", "tenant 12"),
        ("deliveries in the last three days", "deliveries in the last three days"),
    ],
)
def test_spoken_numbers_are_normalized(heard, expected):
    """Commas break the ticket parser's \\d+; spoken tenant numbers miss the id
    match. Both are repaired before the router sees the text."""
    assert normalize_transcript(heard) == expected


def test_build_speech_prompt_carries_tenant_names_aliases_and_jargon(repository):
    """The Whisper `initial_prompt` must actually contain the terms it primes."""
    from src.interfaces.speech import _build_speech_prompt

    prompt = _build_speech_prompt()
    assert "Cascade Fuel Services" in prompt  # a canonical tenant name
    assert "CFS" in prompt                     # an alias
    assert "TankLink" in prompt                # domain jargon


@pytest.mark.parametrize(
    "text, count",
    [
        ("One sentence only.", 1),
        ("First part. Second part.", 2),
        ("A question? An exclamation! A statement.", 3),
    ],
)
def test_tts_splits_answers_into_sentences(text, count):
    """Streaming TTS synthesises one sentence at a time so the first is heard
    without waiting for the whole answer."""
    from src.interfaces.speech import _SENTENCE_SPLIT

    parts = [p for p in _SENTENCE_SPLIT.split(text) if p.strip()]
    assert len(parts) == count


@pytest.mark.parametrize(
    "heard, expected",
    [("Platform.", "Platform"), ("Quit.", "Quit"), ("Scope,", "Scope")],
)
def test_trailing_punctuation_is_dropped_from_commands(heard, expected):
    """Whisper punctuates. 'Platform.' is not the `platform` command otherwise."""
    assert normalize_transcript(heard) == expected


def test_a_trailing_question_mark_survives():
    """The router reads a trailing '?' as evidence of a question. Stripping it
    would push borderline input onto the slower LLM classification path."""
    assert normalize_transcript("how many trucks are in maintenance?").endswith("?")


def test_normalization_never_returns_empty_for_real_speech():
    """A transcript of only punctuation must not become "", which the loop reads
    as "nothing heard"."""
    assert normalize_transcript("...") != ""


# --- what gets spoken -------------------------------------------------------


@pytest.fixture(scope="module")
def brief_response(repository):
    """A real brief for ticket 1083 -- tenant 4's TankLink duplicate cluster,
    which carries a module mismatch and several account signals."""
    agent = TriageAgent(repository=repository)
    ticket = next(
        t for t in repository.tickets_for(4) if t.ticket_id == 1083
    )
    brief = agent.build_brief(ticket)
    return RouterResponse(ResponseKind.BRIEF, "unused", brief=brief)


def test_a_spoken_brief_states_the_level_and_score(brief_response):
    spoken = spoken_text(brief_response)
    assessment = brief_response.brief.assessment

    assert assessment.level.value in spoken
    assert str(assessment.score) in spoken


def test_a_spoken_brief_is_short_enough_to_hear(brief_response):
    """The printed brief is ~25 lines. Spoken, that is ninety seconds of
    monologue. The ceiling here is what a listener can hold."""
    spoken = spoken_text(brief_response)

    assert len(spoken.split(". ")) <= 6
    assert len(spoken) < 600


def test_a_spoken_brief_carries_at_most_the_configured_reasons(brief_response):
    """All reasons still print; only the strongest few are read aloud."""
    assessment = brief_response.brief.assessment
    assert len(assessment.reasons) > config.SPOKEN_BRIEF_MAX_REASONS

    spoken = spoken_text(brief_response)
    dropped = assessment.reasons[config.SPOKEN_BRIEF_MAX_REASONS:]
    assert any(reason not in spoken for reason in dropped)


def test_a_spoken_brief_points_at_the_screen_rather_than_offering_more(brief_response):
    """It must not offer a follow-up this transport cannot service. Describing a
    control that does not exist is the bug already fixed in `resolve_tenant`."""
    spoken = spoken_text(brief_response).lower()

    assert "on screen" in spoken
    assert "?" not in spoken


def test_sql_is_never_spoken(repository):
    """The screen carries the evidence; the voice carries the answer. A SELECT
    read aloud is unusable, and it is the thing most likely to be in `sql_answer`.
    """
    from src.agent.sql_agent import SqlAnswer

    answer = SqlAnswer(
        question="how many trucks are in maintenance?",
        answer="Four trucks are currently in maintenance.",
        sql="SELECT COUNT(*) FROM trucks WHERE status = 'maintenance' AND trucks.tenant_id = 4",
    )
    response = RouterResponse(ResponseKind.ANSWER, answer.answer, sql_answer=answer)

    spoken = spoken_text(response)

    assert spoken == "Four trucks are currently in maintenance."
    assert "SELECT" not in spoken


@pytest.mark.parametrize("kind", [ResponseKind.CLARIFY, ResponseKind.REFUSAL, ResponseKind.CONFIRM])
def test_short_responses_are_spoken_verbatim(kind):
    """Refusals, clarifications and confirmations are already written for a human
    and are already short. Rewriting them for voice would be a second place for
    the wording of a refusal to drift."""
    response = RouterResponse(kind, "Did you mean Cascade Fuel Services?")
    assert spoken_text(response) == "Did you mean Cascade Fuel Services?"


# --- inherited behaviour ----------------------------------------------------


def test_voice_inherits_the_confirmation_gate(repository):
    """The property the whole transport split exists for.

    This is not testing voice code -- it is asserting that voice has no way to
    diverge, because the gate is in `Conversation` and both transports call it.
    Over voice this matters more than in chat: speech-to-text produces exactly the
    near-miss company names that CONFIRM exists to catch.
    """
    conversation = Conversation(Router(repository=repository))

    heard = normalize_transcript("use Cascade Fuel Servces")
    response = conversation.handle(heard)

    assert response.kind is ResponseKind.CONFIRM
    assert conversation.context.scope is SessionScope.PLATFORM
    assert spoken_text(response).endswith("Say yes to continue.")

    conversation.handle(normalize_transcript("Yes."))
    assert conversation.context.tenant_id == 1


def test_a_mangled_company_name_over_voice_binds_nothing(repository):
    """Speech-to-text mangling must fail closed. 'Cascade' alone is ambiguous
    against the roster, so nothing is bound and nothing is even armed."""
    conversation = Conversation(Router(repository=repository))

    response = conversation.handle(normalize_transcript("use Fuel"))

    assert response.kind is ResponseKind.CLARIFY
    assert conversation.pending_tenant is None
    assert conversation.context.scope is SessionScope.PLATFORM
