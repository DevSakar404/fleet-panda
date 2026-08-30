"""Terminal voice transport. Speech in, speech out, same core as chat.

Owned by: the interfaces layer. Called by the user. Calls: `Conversation` for the
session, `speech.py` for audio.

The sibling of `cli_chat.py`. Both own exactly one thing -- rendering -- and share
everything else, which is what CLAUDE.md section 2 means by "transports over one
core". Nothing about tenant binding, the confirmation gate or cross-tenant
refusals appears in this file; those live in `Conversation` and `Router` and
behave identically whether the words arrived by keyboard or microphone.

What this file does own is the difference between reading and hearing:

  - `spoken_text()` renders a `RouterResponse` for the ear. The terminal keeps
    getting the full chat rendering, so SQL and the 25-line brief are on screen
    while the agent says the two sentences that matter.
  - `normalize_transcript()` repairs what speech-to-text does to short codes.
  - The confirmation gate matters more here than in chat, and is inherited rather
    than reimplemented -- see `Conversation`.
"""

from __future__ import annotations

import re

from src import config
from src.agent.conversation import Conversation
from src.agent.router import ResponseKind, Router, RouterResponse
from src.agent.session import TenantContext
from src.interfaces.cli_chat import _build_llm, _load_env, _prompt_label, format_response
from src.interfaces.speech import (
    AudioUnavailableError,
    SpeechClient,
    SpeechConfigurationError,
    record_until_enter,
)

BANNER = """FleetPanda support agent -- voice mode
  Press Enter to speak, Enter again to stop. Say:
    "use C F S"                scope the session to one customer
    "platform"                 switch to an internal, cross-tenant session
    "triage ticket 1083"       build a ticket brief
    "how many deliveries ..."  ask about delivery data
    "quit"                     exit
"""

# A run of single letters separated by spaces, which is how speech-to-text
# renders a spelled-out short code: "use CFS" comes back as "use C F S".
_SPELLED_OUT = re.compile(
    r"\b(?:[A-Za-z] ){%d,}[A-Za-z]\b" % (config.SPOKEN_ACRONYM_MIN_LETTERS - 1)
)

# A comma sitting between two digits, which speech-to-text inserts into any number
# four digits or longer: "ticket 1083" comes back as "ticket 1,083" and the ticket
# parser's \d+ then reads "1" and "083" as two tokens. Stripped so the id is whole.
_DIGIT_GROUPING = re.compile(r"(?<=\d),(?=\d)")

# Number words in the tenant range, spoken rather than typed: "for tenant three" is
# transcribed as words, but "tenant 3" is what the SQL prompt and the router expect.
# Only the small tenant range (1-12) is dictated this way -- ticket ids are four
# digits and arrive as digits -- so mapping stops at twelve.
_SPOKEN_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12",
}
# Only after "tenant", so an ordinary "three" in prose is left alone.
_TENANT_NUMBER_WORD = re.compile(
    r"\b(tenant)\s+(" + "|".join(_SPOKEN_NUMBERS) + r")\b", re.IGNORECASE
)

# ISO dates, which every reason string and the date anchor carry.
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")


def speakable(text: str) -> str:
    """Rewrite the parts of our own prose that only work on a page.

    Every escalation reason and the date anchor carry ISO dates, because they were
    written for a CSM reading a terminal. Spoken, "2026-07-15" comes out as a
    string of digits and dashes and the listener loses the sentence around it.

    This runs on output rather than at the source because the written form is the
    right one for the screen -- `cli_chat` should keep printing ISO. Only the ear
    needs the translation, so only the ear pays for it.
    """
    def _say_date(match: re.Match[str]) -> str:
        year, month, day = (int(part) for part in match.groups())
        if not 1 <= month <= 12:
            return match.group(0)
        return f"{day} {_MONTHS[month - 1]} {year}"

    spoken = _ISO_DATE.sub(_say_date, text)
    # A double hyphen is our house style for an aside in printed prose. Some
    # voices read it as a pause, others read the characters; a comma is
    # unambiguous in both.
    return spoken.replace(" -- ", ", ")


def normalize_transcript(text: str) -> str:
    """Repair the two things speech-to-text reliably does to our inputs.

    Short codes come back spelled out. `tenant_aliases.json` contains "CFS", the
    microphone produces "C F S", and the resolver normalises case but not spacing
    -- so "c f s" and "cfs" are different keys and a name that binds instantly in
    chat fails over voice. Collapsing the run fixes it before resolution rather
    than teaching the resolver about audio, which is not its problem.

    Trailing sentence punctuation is dropped because Whisper punctuates commands.
    "Platform." is not the `platform` command, and "quit." does not exit.

    Two number repairs run first: grouping commas are stripped from ids
    ("1,083" -> "1083") so the ticket parser sees a whole number, and a spoken
    tenant number is turned into a digit ("tenant three" -> "tenant 3") so it
    matches what the SQL prompt and router expect. Both are narrow on purpose --
    only digit-adjacent commas, only number words right after "tenant".

    Deliberately conservative on letters too: it collapses runs of three or more.
    Two would fire on ordinary speech, and every alias in the table is three or more.
    """
    cleaned = _DIGIT_GROUPING.sub("", text.strip())
    cleaned = _TENANT_NUMBER_WORD.sub(
        lambda m: f"{m.group(1)} {_SPOKEN_NUMBERS[m.group(2).lower()]}", cleaned
    )
    collapsed = _SPELLED_OUT.sub(lambda m: m.group(0).replace(" ", ""), cleaned)
    # Only strip trailing punctuation. A question mark mid-string is the model's
    # business, and the router's own heuristics read a trailing '?' as a question
    # -- so it is kept and only '.'/',' are removed.
    return collapsed.rstrip(" .,").strip() or text.strip()


def spoken_text(response: RouterResponse) -> str:
    """Render a response for the ear.

    The rule this enforces is that the spoken channel carries the answer and the
    screen carries the evidence. Reading a SQL statement aloud is unusable, and
    reading a full ticket brief aloud is ninety seconds of monologue -- but both
    belong on screen, where the demo points at them.
    """
    if response.kind is ResponseKind.BRIEF and response.brief is not None:
        return speakable(_spoken_brief(response))

    # A data answer speaks the synthesised prose only. `response.text` is already
    # that prose; the SQL lives on `sql_answer` and is printed, never spoken.
    return speakable(response.text)


def _spoken_brief(response: RouterResponse) -> str:
    """Headline, level, and the strongest couple of reasons.

    Says the level and the score because those are the decision, then the top
    reasons because "why" is the next question a human asks. It stops there and
    points at the screen instead of offering to read more: an offer implies a
    follow-up turn that this transport does not implement, and a sentence that
    describes a control which does not exist is the exact bug this codebase has
    already fixed once (see `Router.resolve_tenant`).

    `assessment.reasons` is already ordered by the signals that produced it, so
    taking the first few is taking the strongest few.
    """
    brief = response.brief
    assessment = brief.assessment
    tenant = brief.context.tenant

    parts = [
        f"Ticket {brief.ticket_id} for {tenant.name} is "
        f"{assessment.level.value}, score {assessment.score}."
    ]

    reasons = assessment.reasons[: config.SPOKEN_BRIEF_MAX_REASONS]
    if reasons:
        lead = "Two things drive that:" if len(reasons) > 1 else "The reason:"
        parts.append(f"{lead} {' '.join(reasons)}")

    if assessment.duplicate_ticket_ids:
        parts.append(f"It has been reported {len(assessment.duplicate_ticket_ids) + 1} times.")

    parts.append("The full brief is on screen.")
    return " ".join(parts)


def _listen(speech: SpeechClient, conversation: Conversation) -> str:
    """Get the user's next turn -- spoken online, typed offline -- and repair it.

    Offline mode has no speech-to-text, so the turn is typed. Online it is the
    push-to-talk recording sent through Whisper. Both paths end in
    `normalize_transcript`, so nothing downstream can tell how the words arrived.
    """
    label = _prompt_label(conversation.context)
    if speech.offline:
        return normalize_transcript(input(f"[{label}] type your turn > "))
    input(f"[{label}] press Enter to speak > ")
    wav = record_until_enter()
    return normalize_transcript(speech.transcribe(wav))


def main() -> None:
    """Run the voice loop. Push to talk, listen, speak, repeat."""
    _load_env()

    try:
        speech = SpeechClient()
    except SpeechConfigurationError as exc:
        print(f"! {exc}\n! Voice mode cannot start. Chat mode still works: "
              f"python -m src.interfaces.cli_chat\n")
        return

    llm = _build_llm()
    conversation = Conversation(Router(llm=llm))

    print(BANNER)
    if speech.offline:
        print("! No OPENAI_API_KEY -- offline mode: type each turn, the agent "
              "replies aloud via macOS `say`. (No offline speech-to-text.)\n")
    while not conversation.finished:
        try:
            heard = _listen(speech, conversation)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        except AudioUnavailableError as exc:
            print(f"! {exc}\n")
            return

        if not heard:
            # Silence, or nothing intelligible. Say so out loud rather than only
            # printing it -- the user is looking at the microphone, not the
            # screen, and silent failure reads as a hung agent.
            print("  (nothing heard)\n")
            speech.speak("I didn't catch that.")
            continue

        print(f'  you said: "{heard}"')
        print("  thinking...")

        response = conversation.handle(heard)

        # Screen gets the full chat rendering -- SQL, the whole brief, everything.
        print(format_response(response) + "\n")
        speech.speak(spoken_text(response))


if __name__ == "__main__":
    main()
