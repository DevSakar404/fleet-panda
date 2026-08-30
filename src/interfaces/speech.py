"""Microphone in, speaker out. The only file in the project that touches audio.

Owned by: the interfaces layer. Called by `voice_chat.py`. Calls: `sounddevice`
for capture and playback, and the OpenAI SDK for transcription and synthesis.

Isolated into its own file so that the voice transport above it is testable. Every
decision voice mode makes -- what to say, when to confirm, what never to read
aloud -- lives in `voice_chat.py` and can be asserted without a microphone. What
is left here is three thin SDK calls and a recording loop, which a mocked test
would only prove we can mock.

`sounddevice` and `numpy` are imported inside the functions rather than at module
scope, the same way `llm/client.py` imports its provider SDK. The test suite
imports `voice_chat`, which imports this module; a top-level import would make
PortAudio and a working audio device a prerequisite for running the tests.
"""

from __future__ import annotations

import io
import queue
import re
import shutil
import subprocess
import sys
import threading
import wave

from src import config

# Split on sentence-final punctuation followed by a space, keeping the punctuation
# on the sentence. Used to stream TTS one sentence at a time (see `_speak_streaming`).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?]) +")


class AudioUnavailableError(RuntimeError):
    """No usable microphone or speaker, or the audio backend failed to load."""


class SpeechConfigurationError(RuntimeError):
    """OPENAI_API_KEY is absent. Voice needs it for both transcription and
    synthesis, and it is raised at startup rather than mid-conversation."""


def _sounddevice():
    """Import sounddevice, turning a missing backend into our own error.

    PortAudio failures surface as OSError from the C library, which is a
    confusing thing to see at the top of a stack trace when the real answer is
    "voice mode needs an audio device".
    """
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise AudioUnavailableError(
            "Could not load the audio backend. Install voice dependencies with "
            "`pip install -r requirements.txt`, and check that this terminal has "
            f"microphone permission in System Settings > Privacy. ({exc})"
        ) from exc
    return sounddevice


def record_until_enter(prompt: str = "  recording... (press Enter to stop)") -> bytes:
    """Capture from the default microphone until the user presses Enter.

    Push-to-talk rather than silence detection, deliberately. A silence threshold
    has to be tuned to a room, and the room this gets demonstrated in is not the
    room it was tuned in: a pause mid-sentence ends the turn early, and background
    conversation never ends it at all. Enter is unambiguous in every room.

    Returns WAV bytes rather than a file path. The recording is a few seconds of
    speech on its way to an HTTP request; writing it to disk would add a temp file
    to clean up and a place for one caller's audio to be read by another.
    """
    sounddevice = _sounddevice()

    frames: list = []
    stop = threading.Event()

    def _capture(indata, _frame_count, _time_info, status) -> None:
        # Called on sounddevice's own thread. Copy because the buffer handed in is
        # reused for the next block -- appending it directly yields a list of
        # references that all end up holding the final block.
        if status:
            print(f"  (audio warning: {status})", file=sys.stderr)
        frames.append(indata.copy())

    def _wait_for_enter() -> None:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        stop.set()

    print(prompt)
    waiter = threading.Thread(target=_wait_for_enter, daemon=True)
    waiter.start()

    try:
        with sounddevice.InputStream(
            samplerate=config.AUDIO_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            callback=_capture,
        ):
            stop.wait()
    except Exception as exc:  # sounddevice raises PortAudioError, not in our namespace
        raise AudioUnavailableError(f"Microphone capture failed: {exc}") from exc

    if not frames:
        return b""
    return _to_wav(b"".join(frame.tobytes() for frame in frames))


def _to_wav(pcm: bytes) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container.

    Whisper needs a container it can identify; raw samples are rejected. The
    stdlib `wave` module writes the 44-byte header, which is the whole job -- an
    audio library for this would be a dependency to justify for a header.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)  # int16
        handle.setframerate(config.AUDIO_SAMPLE_RATE)
        handle.writeframes(pcm)
    return buffer.getvalue()


def _build_speech_prompt() -> str:
    """Prime Whisper's decoder with our vocabulary via `initial_prompt`.

    Whisper biases toward words it has just "seen": feeding it the tenant names,
    their aliases and our jargon makes it far likelier to emit "CFS", "GLFC" or
    "TankLink" as written, instead of the phonetic guesses ("see eff ess") that the
    regex repair in `voice_chat.py` then has to undo. This is the cheaper fix --
    correct at the source rather than patched after.

    Built from the data files so a new tenant or alias needs no edit here. Loaders
    are imported inside the function to keep `speech.py`'s module-load side-effect
    free, the same reason the audio backends are imported lazily.
    """
    from src.data.loaders import load_tenant_aliases, load_tenants

    names = [t.name for t in load_tenants()]
    aliases = [a.alias for a in load_tenant_aliases()]
    terms = names + aliases + list(config.SPEECH_PROMPT_JARGON)
    return "FleetPanda dispatch support. Vocabulary: " + ", ".join(terms) + "."


class SpeechClient:
    """Transcription and synthesis over the OpenAI SDK, with an offline fallback.

    A class rather than two functions so the SDK client is constructed once and
    the key is validated at startup. Discovering a missing key at the moment
    someone speaks is a bad first impression of a voice agent.

    When no key is present it drops to `offline` mode: synthesis goes through the
    macOS `say` command so the loop can be demoed without an API key. There is no
    offline speech-to-text, so `voice_chat.py` reads typed input in that mode.
    """

    def __init__(self, api_key: str | None = None) -> None:
        import os

        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            if not shutil.which("say"):
                raise SpeechConfigurationError(
                    "OPENAI_API_KEY is not set and macOS `say` is unavailable, so "
                    "there is no way to synthesise speech. Copy .env.example to .env "
                    "and add your key, or export it in the shell."
                )
            self.offline = True
            self._client = None
            self._initial_prompt = ""
            self._sdk_error: type[Exception] | tuple = ()  # nothing to catch offline
            return

        from openai import OpenAI, OpenAIError

        self.offline = False
        self._client = OpenAI(api_key=key)
        self._initial_prompt = _build_speech_prompt()
        # Held rather than imported at module scope: `openai` is a lazy dependency
        # (the test suite runs without it installed), so its exception type can only
        # be named once we know a key is present and the import has succeeded.
        self._sdk_error = OpenAIError

    def transcribe(self, wav_bytes: bytes) -> str:
        """Speech to text. Returns "" for an empty or unintelligible recording.

        `language` is pinned rather than auto-detected: a two-second clip of a
        company name gives the detector very little to work with, and it
        occasionally decides a short English utterance is Welsh. Pinning it also
        shaves a little latency. `prompt` biases the decoder toward our vocabulary
        (see `_build_speech_prompt`).
        """
        if self.offline:
            raise SpeechConfigurationError(
                "Offline mode has no speech-to-text. Type your turn instead."
            )
        if not wav_bytes:
            return ""

        # The SDK infers the format from the filename, so the tuple's first
        # element matters even though nothing is written to disk.
        transcript = self._client.audio.transcriptions.create(
            model=config.STT_MODEL,
            file=("speech.wav", wav_bytes, "audio/wav"),
            language=config.SPEECH_LANGUAGE,
            prompt=self._initial_prompt,
        )
        return (transcript.text or "").strip()

    def speak(self, text: str) -> None:
        """Text to speech, played through the default output device.

        Online, sentences are synthesised and played one at a time so the first
        words are heard while the rest are still synthesising. If OpenAI is
        unreachable mid-answer (rate limit, quota, network) it degrades to `say`
        rather than dying in the middle of a demo.
        """
        text = text.strip()
        if not text:
            return
        if self.offline:
            _say(text)
            return

        try:
            self._speak_streaming(text)
        except self._sdk_error as exc:
            print(f"  (speech synthesis unavailable: {exc}; using system voice)",
                  file=sys.stderr)
            _say(text)

    def _speak_streaming(self, text: str) -> None:
        """Synthesise sentence by sentence, playing each as the next is prepared.

        A background thread synthesises ahead into a small bounded queue while this
        thread plays what is ready. Time-to-first-audio becomes the cost of the
        first sentence, not the whole answer -- on a multi-sentence brief that is
        the difference between speaking at once and 1.5-2.5s of dead air.

        The queue is bounded so the producer cannot run far ahead and synthesise an
        answer the user has already stopped listening to. A synthesis failure is
        carried back on `error` and re-raised here, so `speak`'s fallback can catch
        it; anything already played is not replayed by that fallback in the common
        case, because these calls usually fail on the first sentence, not midway.
        """
        sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
        if len(sentences) <= 1:
            _play_wav(self._synthesize(text))
            return

        audio: queue.Queue = queue.Queue(maxsize=2)
        error: list[Exception] = []

        def _produce() -> None:
            try:
                for sentence in sentences:
                    audio.put(self._synthesize(sentence))
            except self._sdk_error as exc:
                error.append(exc)
            finally:
                audio.put(None)  # end sentinel, always sent

        threading.Thread(target=_produce, daemon=True).start()
        while (wav := audio.get()) is not None:
            _play_wav(wav)
        if error:
            raise error[0]

    def _synthesize(self, text: str) -> bytes:
        """One TTS call. Returns WAV bytes. Raises OpenAIError on failure."""
        response = self._client.audio.speech.create(
            model=config.TTS_MODEL,
            voice=config.TTS_VOICE,
            input=text,
            response_format="wav",
            speed=config.TTS_SPEED,
        )
        return response.read()


def _say(text: str) -> None:
    """Speak via the macOS `say` command -- the offline / degraded path.

    `say` takes a words-per-minute rate, so TTS_SPEED (a multiplier) is applied to
    the default WPM to keep the offline voice roughly as brisk as the online one.
    """
    rate = int(config.SAY_BASE_WPM * config.TTS_SPEED)
    try:
        subprocess.run(["say", "-r", str(rate), text], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AudioUnavailableError(f"macOS `say` failed: {exc}") from exc


def _play_wav(wav_bytes: bytes) -> None:
    """Play WAV bytes and block until the audio finishes.

    Blocking is correct here: the next thing the loop does is offer the
    microphone again, and doing that while the agent is still talking means the
    agent records itself.

    Falls back to macOS `afplay` if the audio backend is unavailable, so a
    machine that can synthesise but not open an output stream still speaks.
    """
    try:
        sounddevice = _sounddevice()
        import numpy
    except AudioUnavailableError:
        _play_with_afplay(wav_bytes)
        return

    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        samples = numpy.frombuffer(handle.readframes(handle.getnframes()), dtype=numpy.int16)
        channels = handle.getnchannels()
        rate = handle.getframerate()

    if channels > 1:
        samples = samples.reshape(-1, channels)

    try:
        sounddevice.play(samples, rate)
        sounddevice.wait()
    except Exception:  # PortAudioError and friends; the fallback is the point
        _play_with_afplay(wav_bytes)


def _play_with_afplay(wav_bytes: bytes) -> None:
    """Last resort on macOS: hand the bytes to the system player on stdin."""
    try:
        subprocess.run(
            ["afplay", "-"], input=wav_bytes, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AudioUnavailableError(
            f"Could not play audio through any available output. ({exc})"
        ) from exc
