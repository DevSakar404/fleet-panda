"""SqlAgent behaviour: parsing, authority, retry, refusal.

Driven by `FakeLLM`, so nothing here needs an API key or a network call. What
these tests do NOT cover is whether a real model writes good SQL -- that is
unverifiable without a key and is called out in open-questions.md Q-012.
"""

from __future__ import annotations

import pytest

from src.agent.session import TenantContext
from src.agent.sql_agent import SqlAgent, SqlAnswer
from tests.conftest import FakeLLM, sql_reply

SCOPED = TenantContext.for_tenant(4)
PLATFORM = TenantContext.platform()

COUNT_SQL = "SELECT COUNT(*) AS n FROM delivery_orders"


# --- the happy path -----------------------------------------------------------

def test_answers_a_scoped_question_end_to_end():
    llm = FakeLLM(sql_reply(COUNT_SQL), "Tenant 4 completed 415 orders.")
    answer = SqlAgent(llm).answer("How many orders do we have?", SCOPED)

    assert not answer.refused
    assert answer.row_count == 1
    assert answer.rows[0][0] == 415          # tenant 4's real count, not 9769
    assert "tenant_id = 4" in answer.sql     # the guard's predicate, not the model's
    assert answer.answer == "Tenant 4 completed 415 orders."
    assert len(llm.calls) == 2               # generation, then synthesis
    assert llm.calls[0].get("cache_system") is True, "Call 1 (SQL generation) must enable prompt caching"
    assert llm.calls[1].get("cache_system") is not True, "Call 2 (Synthesis) does not cache prompt"


def test_the_synthesis_call_receives_rows_and_the_anchor_not_the_question_alone():
    """The second call computes nothing -- it is handed the numbers.

    Asserted because the whole reason for splitting the two calls is that the
    model that writes prose must not be in a position to invent a figure.
    """
    llm = FakeLLM(sql_reply(COUNT_SQL), "415 orders.")
    SqlAgent(llm).answer("how many?", SCOPED)

    synthesis_prompt = llm.calls[1]["user"]
    assert "415" in synthesis_prompt
    assert "2026-05-29" in synthesis_prompt, "the date anchor must reach the synthesiser"


def test_the_answer_carries_a_machine_readable_anchor():
    """Prose alone would leave a consumer parsing English to find out the data is
    91 days stale. See open-questions.md Q-007."""
    llm = FakeLLM(sql_reply(COUNT_SQL), "415 orders.")
    answer = SqlAgent(llm).answer("how many?", SCOPED)

    assert answer.date_anchor == "2026-05-29"
    assert answer.anchor_mode == "max_data_date"


def test_generation_is_told_not_to_write_a_tenant_filter():
    """The prompt's instruction and the guard's injection are one design, not two.

    If this instruction were dropped, the model's own filter would be redundant
    when right and invisible when wrong.
    """
    llm = FakeLLM(sql_reply(COUNT_SQL), "ok")
    SqlAgent(llm).answer("how many?", SCOPED)
    assert "Do NOT add any tenant_id filter" in llm.calls[0]["system"]


# --- parsing the model's reply ------------------------------------------------

def test_markdown_fenced_json_is_accepted():
    """Models fence JSON despite being told not to. Retrying would produce the
    same fence, so it is stripped rather than rejected."""
    llm = FakeLLM(f"```json\n{sql_reply(COUNT_SQL)}\n```", "415 orders.")
    assert not SqlAgent(llm).answer("how many?", SCOPED).refused


@pytest.mark.parametrize(
    "reply, why",
    [
        ("SELECT COUNT(*) FROM delivery_orders", "bare SQL instead of JSON"),
        ("{'sql': 'SELECT 1'}", "single quotes are not JSON"),
        ('{"is_cross_tenant": false}', "no sql field"),
        ('{"sql": ""}', "empty sql"),
        ("", "empty reply"),
    ],
)
def test_an_unreadable_reply_becomes_a_refusal_not_a_crash(reply, why):
    answer = SqlAgent(FakeLLM(reply)).answer("how many?", SCOPED)
    assert answer.refused, why
    assert "Could not read the model's reply" in answer.answer


# --- authority ----------------------------------------------------------------

def test_a_cross_tenant_question_is_refused_when_scoped():
    """The model's own flag is enough on its own."""
    llm = FakeLLM(sql_reply("SELECT tenant_id, COUNT(*) AS n FROM delivery_orders",
                            is_cross_tenant=True))
    answer = SqlAgent(llm).answer("which tenant delivered the most?", SCOPED)

    assert answer.refused
    assert "compares tenants" in answer.answer
    assert len(llm.calls) == 1, "must refuse before spending a synthesis call"


def test_a_mislabelled_cross_tenant_query_is_caught_structurally():
    """The model says false; the SQL groups by tenant_id. The structural check is
    the one that fires, which is why there are two."""
    llm = FakeLLM(sql_reply(
        "SELECT tenant_id, COUNT(*) AS n FROM delivery_orders GROUP BY tenant_id",
        is_cross_tenant=False,
    ))
    answer = SqlAgent(llm).answer("deliveries by tenant", SCOPED)

    assert answer.refused
    assert "groups or ranks by tenant" in answer.answer


def test_the_same_question_is_answered_in_a_platform_session():
    """Refusal is about authority, not capability."""
    llm = FakeLLM(
        sql_reply("SELECT tenant_id, COUNT(*) AS n FROM delivery_orders GROUP BY tenant_id",
                  is_cross_tenant=True),
        "Tenant 3 leads with 1413 orders.",
    )
    answer = SqlAgent(llm).answer("deliveries by tenant", PLATFORM)

    assert not answer.refused
    assert answer.row_count == 12


def test_selecting_tenant_id_is_not_treated_as_cross_tenant():
    """Echoing the tenant back in the output is normal. Only grouping or ordering
    by it means the query is shaped as a comparison."""
    llm = FakeLLM(sql_reply("SELECT tenant_id, order_id FROM delivery_orders"), "ok")
    assert not SqlAgent(llm).answer("show me our orders", SCOPED).refused


# --- retry --------------------------------------------------------------------

def test_a_guard_rejection_is_retried_once_with_the_reasons():
    llm = FakeLLM(
        sql_reply("SELECT * FROM api_keys"),   # off-allowlist, rejected
        sql_reply(COUNT_SQL),                  # corrected
        "415 orders.",
    )
    answer = SqlAgent(llm).answer("how many?", SCOPED)

    assert not answer.refused
    assert answer.attempts == 2
    assert "allowlist" in llm.calls[1]["user"], "the rejection reason must be fed back"
    assert "api_keys" in llm.calls[1]["user"], "and so must the rejected query"


def test_two_rejections_stop_rather_than_looping():
    llm = FakeLLM(sql_reply("SELECT * FROM api_keys"), sql_reply("DROP TABLE trucks"))
    answer = SqlAgent(llm).answer("how many?", SCOPED)

    assert answer.refused
    assert answer.attempts == 2
    assert llm.replies == [], "exactly two generation calls, no third"


def test_a_refusal_still_reports_which_question_it_refused():
    answer = SqlAgent(FakeLLM(sql_reply("DELETE FROM trucks"), sql_reply("DROP TABLE trucks"))).answer(
        "delete everything", SCOPED
    )
    assert answer.question == "delete everything"
    assert answer.refusal_reasons


# --- empty results ------------------------------------------------------------

def test_an_empty_result_is_answered_not_refused():
    """Zero rows is a valid answer. Conflating it with a refusal would hide the
    91-day date gap behind an error message."""
    llm = FakeLLM(
        sql_reply("SELECT order_id FROM delivery_orders WHERE order_date >= date('now')"),
        "No deliveries in that window.",
    )
    answer = SqlAgent(llm).answer("anything today?", SCOPED)

    assert not answer.refused
    assert answer.is_empty


# --- provider selection -------------------------------------------------------

def test_provider_initializes_with_openai_key(monkeypatch):
    """Empty-string keys must not count -- a blank line in .env is not a key."""
    from src.llm.client import LLMClient

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert LLMClient().provider == "openai"


def test_no_key_at_all_raises_naming_openai_key(monkeypatch):
    from src.llm.client import LLMClient, LLMConfigurationError

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(LLMConfigurationError, match="OPENAI_API_KEY"):
        LLMClient()


# --- SQL that the guard allows and SQLite still rejects ------------------------
#
# Found by the first live run, not by this suite: the model wrote `d.tenant_id`
# against an alias with no such column. The guard approved it -- it validates
# statement shape and table access, not column names -- and the resulting
# sqlite3.OperationalError propagated straight out of `answer()`, which documents
# itself as never raising for bad SQL. A stack trace reached the caller.

# Reproduces the shape the live model produced: an alias `d` that is never bound,
# so `d.tenant_id` resolves to nothing. The guard allows it -- `delivery_orders`
# and `drivers` are both allowlisted and it is a single SELECT.
BAD_COLUMN_SQL = "SELECT d.tenant_id FROM delivery_orders o JOIN drivers d2 ON 1=1"


def test_an_unrunnable_query_is_retried_rather_than_raised():
    """The DB's own error is the correction handed back to the model."""
    llm = FakeLLM(
        sql_reply(BAD_COLUMN_SQL),
        sql_reply("SELECT COUNT(*) AS n FROM delivery_orders"),
        "There are 9769 orders.",
    )
    answer = SqlAgent(llm).answer("how many orders?", TenantContext.platform())

    assert not answer.refused, answer.refusal_reasons
    assert answer.attempts == 2
    # The retry prompt carried SQLite's message, not a generic failure.
    retry_prompt = llm.calls[1]["user"]
    assert "no such column" in retry_prompt
    assert "d.tenant_id" in retry_prompt


def test_a_query_that_never_runs_becomes_a_refusal_not_a_crash():
    """Both attempts unrunnable: the caller gets a refusal, never an exception."""
    llm = FakeLLM(sql_reply(BAD_COLUMN_SQL), sql_reply(BAD_COLUMN_SQL))
    answer = SqlAgent(llm).answer("how many orders?", TenantContext.platform())

    assert answer.refused
    assert answer.rows == ()
    assert any("no such column" in reason for reason in answer.refusal_reasons)
