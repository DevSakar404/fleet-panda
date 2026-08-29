"""Multi-turn session state: what scope we are in, and what we are waiting for.

Owned by: the agent layer. Called by `interfaces/cli_chat.py` and
`interfaces/voice_chat.py`. Calls: `Router`, `TenantContext`.

`Router` is deliberately stateless -- one text in, one typed response out, no
memory. But a *conversation* has two pieces of state that outlive a turn: which
tenant is currently bound, and whether a tenant confirmation is outstanding. That
state used to live in `cli_chat.main()`'s local variables, which meant the voice
transport could only have it by reimplementing it.

Reimplementing it was the problem. The pending-confirmation gate is a security
control -- an inexactly matched company name must not scope the session until a
human says yes -- and a control that exists in two copies is a control that will
eventually disagree with itself. It lives here once, and both transports get it by
construction rather than by remembering to.

What stays in the transports: rendering. `help` prints a banner in chat and would
read a wall of text aloud in voice, so it is presentation and it is theirs. This
file decides *what happened*; they decide how to say it.
"""

from __future__ import annotations

from src.agent.router import ResponseKind, Router, RouterResponse
from src.agent.session import TenantContext

# Accepted as consent for a pending tenant confirmation. Everything else cancels,
# including silence, a new question, and "no". Kept narrow on purpose: over voice
# this is the line between scoping to the right customer and the wrong one, and a
# speech-to-text engine that mishears a company name will also mishear a hedge.
AFFIRMATIVES: frozenset[str] = frozenset({
    "yes", "y", "yeah", "yep", "yup", "correct", "that's right", "thats right",
})

_EXIT_WORDS: frozenset[str] = frozenset({"quit", "exit", "goodbye", "bye"})


def scope_description(context: TenantContext) -> str:
    """One sentence describing what this session may and may not ask.

    Shared because a viewer needs to hear the same thing they would read: a
    refusal is about authority, not capability, and that only lands if the scope
    was stated in the same words in both modes.
    """
    if context.is_bound:
        return (f"Scoped to tenant {context.tenant_id}. Cross-tenant questions "
                f"will be refused, and every query is filtered to this tenant.")
    return "Internal platform session. Cross-tenant questions are allowed."


class Conversation:
    """One user's session. Holds scope and pending confirmation; nothing else."""

    def __init__(self, router: Router, context: TenantContext | None = None) -> None:
        self._router = router
        self.context = context or TenantContext.platform()
        # A tenant identified by an inexact match, held until a human agrees.
        # Nothing binds it; the session stays on its previous scope until then.
        self.pending_tenant: int | None = None
        self.finished = False

    def handle(self, text: str) -> RouterResponse:
        """One turn. Always returns something to render; never raises on input.

        Order matters and is not arbitrary. The pending confirmation is checked
        before every command, so that an outstanding "did you mean X?" consumes
        the next utterance whatever it is. Letting a command jump the queue would
        leave the confirmation dangling and answerable by a later, unrelated
        "yes".
        """
        stripped = (text or "").strip()
        if not stripped:
            return RouterResponse(ResponseKind.CLARIFY, "Say that again?")

        lowered = stripped.lower()

        if self.pending_tenant is not None:
            return self._resolve_confirmation(lowered)

        if lowered in _EXIT_WORDS:
            self.finished = True
            return RouterResponse(ResponseKind.ANSWER, "Goodbye.")

        if lowered == "scope":
            return RouterResponse(ResponseKind.ANSWER, scope_description(self.context))

        if lowered == "platform":
            self.context = TenantContext.platform()
            return RouterResponse(
                ResponseKind.ANSWER,
                "Switched to an internal platform session. Cross-tenant questions "
                "are allowed here.",
            )

        if lowered.startswith(("use tenant ", "use ")):
            return self._bind_tenant(stripped, lowered)

        return self._router.route(stripped, self.context)

    # --- internals -----------------------------------------------------------

    def _resolve_confirmation(self, lowered: str) -> RouterResponse:
        """Consume the reply to an outstanding "did you mean ...?".

        Cleared either way. An unrecognised reply cancels rather than re-asking,
        because a loop that keeps asking until it hears "yes" is a loop that
        eventually gets one by accident.
        """
        pending, self.pending_tenant = self.pending_tenant, None

        if lowered in AFFIRMATIVES:
            self.context = TenantContext.for_tenant(pending)
            return RouterResponse(
                ResponseKind.ANSWER, scope_description(self.context), tenant_id=pending
            )
        return RouterResponse(
            ResponseKind.CLARIFY,
            "Cancelled -- scope unchanged. Try the full company name or its short code.",
        )

    def _bind_tenant(self, stripped: str, lowered: str) -> RouterResponse:
        """Run `use <company>` through the resolver and act on how sure it was.

        Three outcomes, and the middle one is the point. An exact or curated-alias
        match binds immediately. An inexact match returns CONFIRM and binds
        NOTHING -- it only arms `pending_tenant`, so the next turn decides. An
        unresolved or ambiguous name binds nothing and asks.
        """
        name = stripped.split(" ", 2)[-1] if lowered.startswith("use tenant ") else stripped[4:]
        response = self._router.resolve_tenant(name.strip())

        # The response already carries the tenant id, so the resolver runs once
        # and this file never reaches into the router to recover an id it was
        # handed.
        if response.kind is ResponseKind.CONFIRM:
            self.pending_tenant = response.tenant_id
        elif response.kind is ResponseKind.ANSWER and response.tenant_id is not None:
            self.context = TenantContext.for_tenant(response.tenant_id)

        return response
