"""Natural language question -> guarded SQL -> rows -> natural language answer.

Owned by: the agent layer. Called by `router.py` and, in Step 5, the voice
transport. Calls: `LLMClient`, `build_sql_prompt`, `QueryExecutor` (which owns the
guard), and `SchemaCard` for the date anchor.

Two LLM calls per question, and they do different jobs on purpose. The first turns
a question into SQL and is the only one allowed to be creative. The second turns
rows into prose and is given no ability to compute anything -- it receives the
numbers and is told not to invent others. Nothing between them trusts either.

The agent never decides tenant scoping. It hands a `TenantContext` to the executor
and the guard injects the predicate. The one authority decision made here is
whether a *cross-tenant question* may run at all in a scoped session, and that is
checked twice: once from the model's own `is_cross_tenant` flag, and once
structurally from the SQL it produced. See `_looks_cross_tenant`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from pydantic import BaseModel, Field, ValidationError
from sqlglot import exp

from src import config
from src.agent.session import TenantContext
from src.db.executor import QueryExecutor, QueryResult
from src.db.guard import GuardResult
from src.db.schema import introspect
from src.llm import prompts
from src.llm.client import LLMClient


class SqlGeneration(BaseModel):
    """The model's structured reply. Validated, never trusted.

    Pydantic here and dataclasses in the data layer is the split CLAUDE.md implies:
    this is the one place untrusted text crosses into the system, so it is the one
    place that needs validation rather than just typing.
    """

    sql: str = Field(min_length=1)
    is_cross_tenant: bool = False
    assumptions: str = ""


@dataclass(frozen=True, slots=True)
class SqlAnswer:
    """What the agent returns, whether it answered or refused.

    Refusals are first-class rather than exceptions: "I will not answer that in a
    tenant-scoped session" is a normal conversational outcome that the transport
    has to render, and it carries a reason the user can act on.

    The anchor fields exist because the dataset ends 2026-05-29 (DECISIONS.md
    D-001). Prose alone was the alternative; a machine-readable window means the
    voice transport can say the date on the first answer of a session and stay
    quiet afterwards, and a downstream consumer is not left parsing English to
    discover the numbers are 91 days old. See OPEN_QUESTIONS.md Q-007.
    """

    question: str
    answer: str
    refused: bool = False
    refusal_reasons: tuple[str, ...] = ()
    sql: str | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    row_count: int = 0
    truncated: bool = False
    assumptions: str = ""
    date_anchor: str | None = None
    anchor_mode: str = config.DATE_ANCHOR_MODE
    attempts: int = 1

    @property
    def is_empty(self) -> bool:
        return not self.refused and self.row_count == 0


class SqlAgent:
    """Answers dispatch questions. One instance per process; holds no state."""

    def __init__(self, llm: LLMClient, executor: QueryExecutor | None = None) -> None:
        self._llm = llm
        self._executor = executor or QueryExecutor()

    # --- public ---------------------------------------------------------------

    def answer(self, question: str, context: TenantContext) -> SqlAnswer:
        """Question in, answer out. Never raises for a bad question or bad SQL."""
        system = prompts.build_sql_prompt()
        user = question
        rejection: GuardResult | None = None

        # One retry, not a loop. A second rejection usually means the question is
        # unanswerable from this schema rather than that the model was careless,
        # and retrying past that burns tokens and latency to produce the same
        # refusal. The rejection reasons are fed back verbatim as the correction.
        for attempt in range(1, config.SQL_MAX_ATTEMPTS + 1):
            if rejection is not None:
                user = prompts.SQL_RETRY_TEMPLATE.format(
                    sql=rejection.rewritten_sql or "(unparseable)",
                    reasons="\n".join(f"- {r}" for r in rejection.reasons),
                )

            try:
                generation = self._generate(system, user)
            except ValueError as exc:
                return self._refusal(question, [f"Could not read the model's reply: {exc}"], attempt)

            refusal = self._authority_check(generation, context)
            if refusal:
                return self._refusal(question, refusal, attempt, generation.assumptions)

            verdict, result = self._executor.run(generation.sql, context)
            if verdict.allowed and result is not None:
                return self._synthesise(question, generation, verdict, result, attempt)

            rejection = verdict

        return self._refusal(
            question,
            ["The query could not be made safe to run.", *(rejection.reasons if rejection else ())],
            config.SQL_MAX_ATTEMPTS,
        )

    # --- steps ----------------------------------------------------------------

    def _generate(self, system: str, user: str) -> SqlGeneration:
        """Call the model and validate its JSON reply."""
        response = self._llm.complete(system=system, user=user)
        return self._parse_generation(response.text)

    @staticmethod
    def _parse_generation(text: str) -> SqlGeneration:
        """Extract and validate the JSON object from the model's reply.

        Models wrap JSON in markdown fences despite being told not to, so the
        fence is stripped before parsing rather than treated as a failure -- a
        retry would almost certainly produce the same fence. Anything else that
        does not parse raises, and the caller turns it into a refusal.
        """
        cleaned = text.strip()
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
        if fenced:
            cleaned = fenced.group(1).strip()

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"reply was not JSON ({exc.msg})") from exc

        try:
            return SqlGeneration.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"reply did not match the expected shape ({exc.error_count()} errors)") from exc

    def _authority_check(self, generation: SqlGeneration, context: TenantContext) -> list[str]:
        """Refuse a cross-tenant question inside a tenant-scoped session.

        Checked two independent ways because they fail differently. The model's
        own flag reads the *question* and catches intent the SQL does not show
        ("compare us to the others" that it then writes as a single-tenant query).
        The structural check reads the *SQL* and catches a mislabelled generation.
        Either one firing is enough.

        The refusal matters as much as the answer: narrowing "which tenant
        delivered the most gallons?" to one tenant returns that tenant's number
        presented as a platform ranking -- a wrong answer that looks right, which
        is worse than saying no.
        """
        if not context.is_bound:
            return []

        reasons: list[str] = []
        if generation.is_cross_tenant:
            reasons.append(
                "That question compares tenants, and this session is scoped to "
                f"tenant {context.tenant_id}. Ask it in an internal platform session."
            )
        elif self._looks_cross_tenant(generation.sql):
            reasons.append(
                "The query groups or ranks by tenant, which this session is not "
                f"permitted to do while scoped to tenant {context.tenant_id}."
            )
        return reasons

    @staticmethod
    def _looks_cross_tenant(sql: str) -> bool:
        """True when the SQL treats tenant_id as a dimension rather than a filter.

        Grouping by tenant_id, or ordering by it, means the query is shaped to
        return one row per tenant -- a comparison. In a bound session the guard
        would collapse that to a single group, producing a one-row "ranking" that
        looks like an answer to a question about all twelve tenants.

        Selecting tenant_id is deliberately NOT treated as cross-tenant: echoing
        the tenant back in the output is normal and harmless.
        """
        try:
            statement = sqlglot.parse_one(sql, dialect="sqlite")
        except sqlglot.errors.ParseError:
            # Unparseable SQL is the guard's problem, not this check's. Let it
            # through so the rejection carries the guard's clearer message.
            return False

        for clause_type in (exp.Group, exp.Order):
            for clause in statement.find_all(clause_type):
                for column in clause.find_all(exp.Column):
                    if column.name.lower() == config.TENANT_COLUMN:
                        return True
        return False

    def _synthesise(
        self,
        question: str,
        generation: SqlGeneration,
        verdict: GuardResult,
        result: QueryResult,
        attempt: int,
    ) -> SqlAnswer:
        """Second LLM call: rows to prose. It computes nothing."""
        anchor = introspect().date_anchor
        payload = {
            "question": question,
            "columns": list(result.columns),
            "rows": [list(row) for row in result.rows[: config.SYNTHESIS_ROW_SAMPLE]],
            "row_count": result.row_count,
            "truncated": result.truncated,
            "assumptions": generation.assumptions,
            "date_anchor": anchor,
        }
        response = self._llm.complete(
            system=prompts.SQL_SYNTHESIS_SYSTEM_PROMPT,
            user=json.dumps(payload, default=str),
            effort=config.LLM_EFFORT_SYNTHESIS,
        )
        return SqlAnswer(
            question=question,
            answer=response.text.strip(),
            sql=verdict.rewritten_sql,
            columns=result.columns,
            rows=result.rows,
            row_count=result.row_count,
            truncated=result.truncated,
            assumptions=generation.assumptions,
            date_anchor=anchor,
            attempts=attempt,
        )

    @staticmethod
    def _refusal(question: str, reasons: list[str], attempt: int, assumptions: str = "") -> SqlAnswer:
        return SqlAnswer(
            question=question,
            answer=" ".join(reasons),
            refused=True,
            refusal_reasons=tuple(reasons),
            assumptions=assumptions,
            attempts=attempt,
        )


def answer_question(question: str, context: TenantContext, llm: LLMClient | None = None) -> SqlAnswer:
    """Module-level entry point used by the router.

    Constructs an `LLMClient` lazily so that importing this module -- which the
    test suite does -- never requires an API key.
    """
    return SqlAgent(llm or LLMClient()).answer(question, context)
