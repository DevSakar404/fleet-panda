"""Natural language question -> guarded SQL -> rows -> natural language answer.

STUB -- Step 4. The docstring below is the specification.

Owned by: the agent layer. Called by `router.py`. Calls: `LLMClient`,
`build_sql_prompt`, `SqlGuard`, `QueryExecutor`.

Intended flow:

    question + TenantContext
      |
      +-- 1. LLMClient.complete(build_sql_prompt(), question) -> candidate SQL
      |        the prompt tells the model NOT to write a tenant filter; the guard
      |        adds exactly one, from one place (src/llm/prompts.py explains why)
      |
      +-- 2. QueryExecutor.run(sql, context)
      |        guard rejects -> return the refusal with GuardResult.reasons, do
      |        not retry blindly; a rejected query is usually a wrong question,
      |        not a wrong generation
      |
      +-- 3. LLM synthesises rows into prose
      |        must state the date anchor when the question used a relative
      |        window ("in the 7 days to 2026-05-29, the most recent data"),
      |        because the data is 91 days stale (DECISIONS.md D-001)
      |
      +-- 4. return answer + executed SQL + row count, so the UI can show its work

Retry policy for Step 4: one retry on a guard rejection, feeding
`GuardResult.reasons` back to the model as the correction. Never retry a
`TenantIsolationError` -- that is a defect, not a bad generation.
"""

from __future__ import annotations

from src.agent.session import TenantContext


def answer_question(question: str, context: TenantContext):
    """Answer a dispatch question. See module docstring for the flow."""
    raise NotImplementedError("Step 4: text-to-SQL agent")
