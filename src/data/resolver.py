"""Tenant name or alias -> canonical integer tenant_id, or an honest refusal.

Owned by: the data layer. Called by `repository.py` (to attach ids to call
transcripts at load time) and by the agent router (to resolve a tenant named in
free text or dictated over voice). Calls: `loaders.py` and `config.py`.

This is the first security boundary in the system. Everything downstream --
including the SQL guard -- trusts the integer this module returns, so a wrong
answer here is a cross-tenant leak that no later layer can catch. That is why the
resolver refuses rather than guesses (CLAUDE.md section 3.5).

The cascade, in order, cheapest and most certain first:

    1. exact canonical name          'Cascade Fuel Services' -> 1
    2. exact alias                   'CFS'                   -> 1
    3. normalised exact              'cascade fuel services llc' -> 1
    4. fuzzy over normalised keys    'Cascade Fuel Servces'  -> 1
    5. Unresolved + ranked candidates

Steps 1-3 are dictionary lookups. Only step 4 involves scoring, and only step 4
can be ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache

from rapidfuzz import fuzz, process

from src import config
from src.data.loaders import load_tenant_aliases, load_tenants


class MatchMethod(str, Enum):
    """How a resolution was reached. Voice mode branches on this to decide whether
    to confirm out loud: an EXACT_CANONICAL match needs no confirmation, a FUZZY
    one does."""

    EXACT_CANONICAL = "exact_canonical"
    EXACT_ALIAS = "exact_alias"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One possible tenant for an unresolved name, with the score that got it here."""

    tenant_id: int
    name: str
    score: float


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """The outcome of one resolution attempt.

    A bare `int | None` was the obvious return type and is not enough: voice mode
    needs `method` and `confidence` to decide whether to read a confirmation back
    to the caller, and the clarify path needs `candidates` to ask a useful
    question instead of "I didn't understand".
    """

    query: str
    tenant_id: int | None
    method: MatchMethod
    confidence: float
    candidates: tuple[Candidate, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.tenant_id is not None

    @property
    def needs_confirmation(self) -> bool:
        """True when the match was inexact enough that a human should confirm it.

        Exact hits against the canonical name or the curated alias table are
        trusted silently. A fuzzy hit is one transcription error away from being
        the wrong company, so it gets read back before any data is shown.
        """
        return self.method in (MatchMethod.NORMALIZED, MatchMethod.FUZZY)


# Matches a legal suffix as a whole word, with an optional trailing dot, so
# 'Summit Energy Group Inc.' and 'Great Lakes Fuel Co' lose the suffix while
# 'Cascade' keeps every character. Built once at import rather than per call.
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in config.LEGAL_SUFFIXES) + r")\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Lowercase, drop legal suffixes and punctuation, collapse whitespace.

    'Summit Energy Group, Inc.' -> 'summit energy group'
    'Cascade Fuel Svcs'         -> 'cascade fuel svcs'

    Suffix stripping happens before punctuation stripping so that 'L.L.C' is still
    recognisable as a suffix when it is matched.
    """
    lowered = _LEGAL_SUFFIX_RE.sub(" ", name.lower())
    depunctuated = _NON_ALNUM_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", depunctuated).strip()


@dataclass(frozen=True, slots=True)
class _AliasIndex:
    """Precomputed lookup tables. Built once, reused for every resolution.

    Each maps a key to the *set* of tenant ids claiming it. A set rather than a
    single id because ambiguity is the case this module exists to detect, and a
    dict of ints would silently drop the second claimant.
    """

    canonical: dict[str, int]
    by_alias: dict[str, frozenset[int]]
    by_normalized: dict[str, frozenset[int]]
    display_names: dict[int, str]

    @property
    def normalized_keys(self) -> list[str]:
        return list(self.by_normalized)


@lru_cache(maxsize=1)
def _build_index() -> _AliasIndex:
    """Fold customers.json and tenant_aliases.json into the three lookup tables."""
    tenants = load_tenants()
    canonical = {t.name: t.tenant_id for t in tenants}
    display_names = {t.tenant_id: t.name for t in tenants}

    # Canonical names are entries in the alias table too -- 'Cascade Fuel Services'
    # should resolve whether or not someone remembered to list it as its own alias.
    alias_claims: dict[str, set[int]] = {name: {tid} for name, tid in canonical.items()}
    for row in load_tenant_aliases():
        alias_claims.setdefault(row.alias, set()).add(row.tenant_id)

    normalized_claims: dict[str, set[int]] = {}
    for key, ids in alias_claims.items():
        normalized_claims.setdefault(normalize(key), set()).update(ids)

    return _AliasIndex(
        canonical=canonical,
        by_alias={k: frozenset(v) for k, v in alias_claims.items()},
        by_normalized={k: frozenset(v) for k, v in normalized_claims.items()},
        display_names=display_names,
    )


class TenantResolver:
    """Resolves a tenant name to an id, or refuses with ranked alternatives."""

    def __init__(self, threshold: float = config.FUZZY_MATCH_THRESHOLD) -> None:
        self._index = _build_index()
        self._threshold = threshold

    def resolve(self, name: str) -> ResolutionResult:
        """Run the cascade. Never raises on an unknown name -- returns Unresolved."""
        query = (name or "").strip()
        if not query:
            return ResolutionResult(query, None, MatchMethod.UNRESOLVED, 0.0)

        idx = self._index

        # 1. Exact canonical name.
        if query in idx.canonical:
            return ResolutionResult(query, idx.canonical[query], MatchMethod.EXACT_CANONICAL, 100.0)

        # 2. Exact alias from the curated table.
        exact = idx.by_alias.get(query)
        if exact is not None:
            return self._from_claims(query, exact, MatchMethod.EXACT_ALIAS, 100.0)

        # 3. Exact after normalisation ('summit energy group inc' -> canonical).
        key = normalize(query)
        normalized = idx.by_normalized.get(key)
        if normalized is not None:
            return self._from_claims(query, normalized, MatchMethod.NORMALIZED, 99.0)

        # 4. Fuzzy. See _resolve_fuzzy for why the gate is a count, not a score.
        return self._resolve_fuzzy(query, key)

    def _from_claims(
        self, query: str, claims: frozenset[int], method: MatchMethod, score: float
    ) -> ResolutionResult:
        """Accept a lookup hit only if exactly one tenant claims the key.

        The provided alias table happens to be unambiguous (recon confirmed no
        alias maps to two tenants), but that is a property of today's data file,
        not a guarantee. If a future alias row collides, this refuses rather than
        picking whichever tenant hashed first.
        """
        if len(claims) == 1:
            return ResolutionResult(query, next(iter(claims)), method, score)
        return ResolutionResult(
            query, None, MatchMethod.AMBIGUOUS, score, self._rank(claims, score)
        )

    def _resolve_fuzzy(self, query: str, key: str) -> ResolutionResult:
        """Score the normalised query against every known key, then gate on count.

        The gate is the whole point of this method, and it is not the obvious one.

        `token_set_ratio` scores a *subset* of tokens as a perfect 100 -- it asks
        "are the query's words contained in the candidate's words", which is what
        makes it good at 'Cascade Fuel Svcs' vs 'Cascade Fuel Services' and awful
        at bare words. The probe 'Fuel' scores 100 against both Cascade Fuel
        Services and Great Lakes Fuel Co; 'Energy' scores 100 against three
        tenants. A resolver that returned max(score) would answer 'Fuel' with
        tenant 1 at full confidence and leak that tenant's data.

        So the score decides *membership* of the candidate set, and the size of
        that set decides whether we answer. One tenant above the line -> resolved.
        Two or more -> refuse and list them, however high the scores are.
        See DECISIONS.md D-003 and RECON.md section 6.
        """
        idx = self._index
        matches = process.extract(
            key,
            idx.normalized_keys,
            scorer=fuzz.token_set_ratio,
            limit=len(idx.normalized_keys),
            score_cutoff=self._threshold,
        )
        if not matches:
            return ResolutionResult(query, None, MatchMethod.UNRESOLVED, 0.0, self._nearest(key))

        # Best score seen per tenant, across all of that tenant's matching keys.
        best_by_tenant: dict[int, float] = {}
        for matched_key, score, _ in matches:
            for tenant_id in idx.by_normalized[matched_key]:
                if score > best_by_tenant.get(tenant_id, 0.0):
                    best_by_tenant[tenant_id] = score

        if len(best_by_tenant) == 1:
            tenant_id, score = next(iter(best_by_tenant.items()))
            return ResolutionResult(query, tenant_id, MatchMethod.FUZZY, score)

        ranked = tuple(
            sorted(
                (Candidate(tid, idx.display_names[tid], score) for tid, score in best_by_tenant.items()),
                key=lambda c: c.score,
                reverse=True,
            )
        )[: config.MAX_RESOLUTION_CANDIDATES]
        return ResolutionResult(query, None, MatchMethod.AMBIGUOUS, ranked[0].score, ranked)

    def _rank(self, tenant_ids: frozenset[int], score: float) -> tuple[Candidate, ...]:
        """Wrap a set of tenant ids as candidates, all sharing one score."""
        idx = self._index
        return tuple(
            Candidate(tid, idx.display_names[tid], score) for tid in sorted(tenant_ids)
        )[: config.MAX_RESOLUTION_CANDIDATES]

    def _nearest(self, key: str) -> tuple[Candidate, ...]:
        """Best-effort suggestions for a name that cleared no threshold at all.

        Returned so the clarify path can say "did you mean ...?" rather than
        failing blankly. These are explicitly below the confidence bar and are
        never auto-selected.
        """
        idx = self._index
        hits = process.extract(
            key, idx.normalized_keys, scorer=fuzz.token_set_ratio,
            limit=config.MAX_RESOLUTION_CANDIDATES,
            score_cutoff=config.NEAREST_SUGGESTION_FLOOR,
        )
        seen: dict[int, float] = {}
        for matched_key, score, _ in hits:
            for tenant_id in idx.by_normalized[matched_key]:
                seen.setdefault(tenant_id, score)
        return tuple(
            Candidate(tid, idx.display_names[tid], score) for tid, score in seen.items()
        )[: config.MAX_RESOLUTION_CANDIDATES]
