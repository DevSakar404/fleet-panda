"""A pasted ticket body -> the `Ticket` the triage pipeline already consumes.

Owned by: the agent layer. Called by `router.py:_triage`. Calls: `config` for the
field vocabularies and `loaders.Ticket` for the shape.

Exists because chat mode accepts a pasted ticket as well as a ticket number, and
the two are not the same kind of input. An id is a lookup into a corpus we wrote
and trust. A pasted body is untrusted text that carries no id, no tenant, and no
guarantee that any particular field is present.

So this module is permissive about *shape* and strict about *vocabulary*. A rep
pastes whatever their ticket form produced, so labels are matched
case-insensitively, in any order, and anything unlabelled becomes the description.
But a `product_area` outside the seven real ones is dropped rather than carried:
an invented area would silently disable KB retrieval and the module-entitlement
check while still looking like a populated field, and a blank one at least fails
where someone can see it.

What this module deliberately does NOT do is decide the tenant. A pasted body can
claim to be from any company; honouring that would let a scoped rep build a brief
for a different customer by typing one line, which is the caller-supplied
`tenant_id` hole from security-review.md V1 wearing a different hat. The tenant is passed
in by `router.py` from the bound session, and there is no code path here that
reads one out of the text.
"""

from __future__ import annotations

import re
from datetime import date

from src import config
from src.data.loaders import Ticket

# The seven real product areas, derived from the two config maps that already
# enumerate them between them rather than restated as a third list to keep in sync.
KNOWN_PRODUCT_AREAS: frozenset[str] = (
    frozenset(config.AREA_TO_MODULE) | config.UNGATED_PRODUCT_AREAS
)

# The four real priorities, likewise taken from the scoring table.
KNOWN_PRIORITIES: frozenset[str] = frozenset(config.WEIGHT_PRIORITY)

# Unrecognised or absent priority. Scores zero points, so an unparseable field
# cannot inflate an escalation -- the safe direction for a default to fail in.
DEFAULT_PRIORITY = "medium"

# A pasted ticket has no id. 0 sits outside the real range (1000-1084), so it
# collides with nothing, and `escalation.find_duplicates` -- which excludes a
# ticket from its own duplicate set by id -- still sees every similar ticket.
PASTED_TICKET_ID = 0

# Labels a ticket form emits, as alternatives for one regex. Matched at line start
# with a required colon, so prose like "from Tuesday onwards" is not mistaken for
# a `From:` header.
_LABEL_ALTERNATIVES = (
    r"subject",
    r"product[ _]area",
    r"priority",
    r"status",
    r"submitted[ _]by",
    r"submitter(?:[ _]name)?",
    r"(?:submitter[ _])?email",
    r"from",
    r"ticket(?:[ _]id)?",
    r"tenant(?:[ _](?:id|name))?",
    r"created(?:[ _]at)?",
)
_LABEL_LINE_RE = re.compile(
    rf"^\s*(?:{'|'.join(_LABEL_ALTERNATIVES)})\s*:", re.IGNORECASE
)

# Pulled out of the submitter line when it carries an address rather than a name.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _field(label: str, text: str) -> str | None:
    """The value of a `Label: value` line, if one is present.

    Case-insensitive and anchored per line, so the labels may appear in any order
    and may be indented -- which pasted text usually is.
    """
    match = re.search(rf"^\s*{label}\s*:\s*(.+?)\s*$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _description(text: str) -> str:
    """Everything that is not a recognised label line.

    Keeping the leftovers rather than looking for a `Description:` label is what
    makes this work on real pastes: most ticket exports put the body after the
    headers with no label of its own.
    """
    body = [line for line in text.splitlines() if not _LABEL_LINE_RE.match(line)]
    return "\n".join(body).strip()


def _subject(text: str, description: str) -> str | None:
    """The `Subject:` line, or the first line of the body as a fallback.

    A ticket with neither is not a ticket we can do anything useful with -- the
    subject is what duplicate detection matches on -- so the caller is told to
    give a ticket number instead of being handed an empty brief.
    """
    labelled = _field("subject", text)
    if labelled:
        return labelled
    first_line = next((line.strip() for line in description.splitlines() if line.strip()), "")
    return first_line or None


def _vocabulary_field(label: str, text: str, allowed: frozenset[str], default: str) -> str:
    """A labelled field validated against a known vocabulary.

    Normalised to lowercase because a form may emit `Priority: High`, but not
    otherwise coerced: an unrecognised value falls back to the default rather
    than being guessed at.
    """
    raw = _field(label, text)
    if raw is None:
        return default
    value = raw.strip().lower()
    return value if value in allowed else default


def looks_like_a_ticket(text: str) -> bool:
    """Whether the text carries enough shape to be a pasted ticket at all.

    A labelled line, or at least two non-empty lines. A single unlabelled line is
    a command -- "triage that ticket" -- and treating it as a paste produced a
    brief whose subject was the word "triage", scored against a real customer.
    Asking which ticket is the honest answer to that input.

    Separate from `parse_pasted_ticket` because the router has to answer "is this
    a paste?" before it answers "whose is it?": a bare command should be told to
    name a ticket, not told to scope to a customer first.
    """
    if not text or not text.strip():
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) >= 2 or any(_LABEL_LINE_RE.match(line) for line in lines)


def parse_pasted_ticket(text: str, tenant_id: int, tenant_name: str) -> Ticket | None:
    """Build a `Ticket` from pasted text, or None if it carries no subject.

    `tenant_id` and `tenant_name` are supplied by the caller from the bound
    session. Nothing in the pasted text can influence them.
    """
    if not looks_like_a_ticket(text):
        return None

    description = _description(text)
    subject = _subject(text, description)
    if subject is None:
        return None

    submitter = _field("submitted[ _]by", text) or _field("submitter(?:[ _]name)?", text) or ""
    email = _field("(?:submitter[ _])?email", text) or _field("from", text) or ""
    # A "Submitted by: ops@cascade.com" line is an address, not a name. Move it to
    # the field that will be read as one.
    if not email and _EMAIL_RE.fullmatch(submitter):
        submitter, email = "", submitter

    return Ticket(
        ticket_id=PASTED_TICKET_ID,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        subject=subject,
        # The subject is repeated into an empty description so that KB matching and
        # duplicate detection, which read both, are not working from half the text
        # when someone pastes a bare subject line.
        description=description or subject,
        product_area=_vocabulary_field("product[ _]area", text, KNOWN_PRODUCT_AREAS, ""),
        status="open",
        priority=_vocabulary_field("priority", text, KNOWN_PRIORITIES, DEFAULT_PRIORITY),
        submitter_name=submitter,
        submitter_email=email,
        created_at=date.today(),
        updated_at=None,
        resolution=None,
        agent_name="",
    )
