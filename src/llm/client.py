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
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


class LLMClient:
    """Sends prompts to Anthropic or OpenAI and returns text plus token usage.

    One class with one branch rather than a base class and two subclasses: there
    is exactly one method, the two SDKs differ in about six lines, and an
    interface with two implementations that are each six lines is more code to
    explain than the thing it abstracts.

    The provider is whichever key is set, Anthropic first if both are. Nothing
    else in the codebase knows which one is in use.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        openai_key = os.environ.get("OPENAI_API_KEY")

        if anthropic_key or (api_key and not openai_key):
            self.provider = "anthropic"
            key = api_key or anthropic_key
            self._model = model or config.ANTHROPIC_MODEL
            from anthropic import Anthropic  # imported here so the suite needs neither SDK

            self._client = Anthropic(api_key=key)
        elif openai_key or api_key:
            self.provider = "openai"
            key = api_key or openai_key
            self._model = model or config.OPENAI_MODEL
            from openai import OpenAI

            self._client = OpenAI(api_key=key)
        else:
            raise LLMConfigurationError(
                "No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY -- copy "
                ".env.example to .env and add one, or export it in the shell. The "
                "agent will not start without it."
            )

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = config.LLM_MAX_TOKENS,
        effort: str = config.LLM_EFFORT_SQL,
        cache_system: bool = False,
    ) -> LLMResponse:
        """One turn, no history. Every caller here is stateless by design.

        `effort` rather than `temperature`: sampling parameters are removed on
        current Claude models and return a 400. `output_config.effort` is the
        replacement lever -- it controls how much thinking the model does, which
        is the knob that actually matters for both of this system's calls.

        `cache_system` enables Anthropic prompt caching (`cache_control`) on the
        system prompt block when supported (e.g. the 1,165-token schema card).
        """
        if self.provider == "anthropic":
            if cache_system:
                system_param: Any = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                system_param = system

            message = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                output_config={"effort": effort},
                system=system_param,
                messages=[{"role": "user", "content": user}],
            )
            usage = message.usage
            cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            return LLMResponse(
                text="".join(b.text for b in message.content if b.type == "text"),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                model=self._model,
                cache_creation_tokens=cache_creation,
                cache_read_tokens=cache_read,
            )

        # OpenAI: the system prompt is a message rather than a parameter, and
        # `effort` has no equivalent on chat.completions -- it is dropped rather
        # than translated, because a wrong translation is worse than none.
        # OpenAI caches automatically for prefixes >= 1024 tokens.
        completion = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        usage = completion.usage
        cached_tokens = 0
        if usage and hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
            cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0

        return LLMResponse(
            text=completion.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=self._model,
            cache_read_tokens=cached_tokens,
        )
