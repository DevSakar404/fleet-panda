# Run locally — chat, voice, offline, tests

← [README](../../README.md) · Reference: [design](../reference/design.md) · [voice interface](../reference/voice-interface.md)

Task-oriented runbook. The [README](../../README.md) has the quickstart; this is the
complete version, including offline voice mode and the evaluation harness.

## Install

With [`uv`](https://github.com/astral-sh/uv):

```bash
uv venv --python python3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Without `uv`:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

Then add a key (optional for chat, required for voice):

```bash
cp .env.example .env
```

## Chat mode

```bash
.venv/bin/python -m src.interfaces.cli_chat
```

Runs **without** an API key: tenant binding, ticket triage, and every isolation
refusal path are deterministic. Only dispatch-data questions call a model
(`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`; Anthropic wins if both are set).

## Voice mode

```bash
OPENAI_API_KEY=sk-... .venv/bin/python -m src.interfaces.voice_chat
```

Push to talk: Enter to start recording, Enter again to stop. Uses `whisper-1` for
speech-to-text and `tts-1` for synthesis. On macOS, grant microphone permission to
the terminal application (not to Python) on first run.

## Voice mode — offline (no API key)

```bash
env -u OPENAI_API_KEY .venv/bin/python -m src.interfaces.voice_chat
```

With no key, voice drops to an offline path: you **type** each turn and the agent
replies aloud through the macOS `say` command. There is no offline speech-to-text,
so this is a demo/dev path — the full loop, scoping, triage, and every refusal
still run. See [voice-interface.md](../reference/voice-interface.md) §1 and §4.

## Run the tests

```bash
.venv/bin/python -m pytest tests/ -q
```

No test needs an API key, a microphone, or a network connection. The isolation
tests are the ones to read first:

```bash
.venv/bin/python -m pytest tests/test_tenant_isolation.py tests/test_security.py -v
```

## Evaluate against a real model

The eight graded questions double as an evaluation harness. With a key set, this
runs them against the live model — nothing is primed, so the model writes the SQL
itself and every assertion stays unchanged:

```bash
env $(cat .env | xargs) FLEETPANDA_EVAL_LLM=1 .venv/bin/python -m pytest tests/test_sql_questions.py -v
```
