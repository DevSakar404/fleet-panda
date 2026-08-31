"""A thin wrapper over the OpenAI SDK.

Owned by: the llm layer. Called by `sql_agent.py` and `triage_agent.py`. Calls:
the `openai` SDK and `config`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from src import config


class LLMConfigurationError(RuntimeError):
    """OPENAI_API_KEY is not set. Raised at construction, not at first call."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One completion, plus the token counts for cost modeling."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


class LLMClient:
    """Sends prompts to OpenAI and returns text plus token usage."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise LLMConfigurationError(
                "No API key found. Set OPENAI_API_KEY -- copy "
                ".env.example to .env and add your key, or export it in the shell. The "
                "agent will not start without it."
            )
        self.provider = "openai"
        self._model = model or config.OPENAI_MODEL
        from openai import OpenAI  # imported here so the test suite needs no SDK at import

        self._client = OpenAI(api_key=key)

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = config.LLM_MAX_TOKENS,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> LLMResponse:
        """One turn, no history. Every caller here is stateless by design.

        OpenAI automatically caches prefixes >= 1024 tokens.
        """
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
