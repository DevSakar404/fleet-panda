"""A thin wrapper over the Anthropic SDK. Nothing clever, nothing cached.

Owned by: the llm layer. Called by `sql_agent.py` and `triage_agent.py`. Calls:
the `anthropic` SDK and `config`.

Deliberately thin (CLAUDE.md section 3.1). There is one provider, named directly.
An abstract `BaseLLMClient` with one implementation would be an abstraction with
nothing to abstract over, and the live walkthrough is easier with a file that
says what it does.

No call is made during the foundation session. The constructor raises if the key
is absent rather than substituting a stub, because a stub that quietly returns
canned text is how a broken deployment passes its own health check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src import config


class LLMConfigurationError(RuntimeError):
    """ANTHROPIC_API_KEY is not set. Raised at construction, not at first call."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One completion, plus the token counts the cost model in DECISIONS.md needs."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str


class LLMClient:
    """Sends prompts to Anthropic and returns text plus token usage."""

    def __init__(self, api_key: str | None = None, model: str = config.LLM_MODEL) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMConfigurationError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key, "
                "or export it in the shell. The agent will not start without it."
            )
        # Imported here rather than at module scope so that importing this module
        # (which the test suite does, transitively) does not require the SDK to be
        # installed or the key to exist.
        from anthropic import Anthropic

        self._client = Anthropic(api_key=key)
        self._model = model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = config.LLM_MAX_TOKENS,
        effort: str = config.LLM_EFFORT_SQL,
    ) -> LLMResponse:
        """One turn, no history. Every caller here is stateless by design.

        `effort` rather than `temperature`: sampling parameters are removed on
        current Claude models and return a 400. `output_config.effort` is the
        replacement lever -- it controls how much thinking the model does, which
        is the knob that actually matters for both of this system's calls.
        """
        message = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            output_config={"effort": effort},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResponse(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            model=self._model,
        )
