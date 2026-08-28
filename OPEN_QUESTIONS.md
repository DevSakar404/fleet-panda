# OPEN_QUESTIONS.md

Questions raised during the build. Per CLAUDE.md §8, none of these blocked: each was resolved
by taking the more conservative option, and the alternative is recorded here for review.

---

### Q-001 · CLAUDE.md §9 undercounts the cross-tenant questions — CONFLICT WITH SOURCE OF TRUTH
**What it says:** "questions 1 and 7 are cross-tenant. In a tenant-scoped session the agent must
refuse them."
**What the data says:** four of the eight are cross-tenant by construction — Q1 ("across all
tenants"), Q2 ("which tenant delivered the most"), Q7 ("by tenant"), Q8 ("list tenants with
declining volume"). Q2 and Q8 both range over all twelve tenants and are meaningless scoped to
one.
**Taken:** implemented the correct set `{1, 2, 7, 8}` as `CROSS_TENANT_QUESTIONS` in
`src/config.py`, because building to the documented set would make the agent answer Q2 and Q8
with a single tenant's rows and present it as a platform-wide ranking — a wrong answer that
looks right. **CLAUDE.md was not edited**, per §1's instruction to write conflicts here rather
than guess.
**Needs you to:** confirm the corrected set and update CLAUDE.md §9, or tell me the documented
set was deliberate.

### Q-002 · The `product_area` → module mapping is my inference
**Context:** DECISIONS.md D-002. `billing→invoicing` and `reporting→analytics` are not
documented anywhere in the provided data; I inferred them from the vocabularies. `integration`
and `login_access` I treated as platform-wide and gated by no module.
**Taken:** the conservative direction is to under-flag rather than over-flag, so anything not in
the map is never reported as a mismatch. Lives in `src/config.py` where it is one edit to fix.
**Needs you to:** confirm with FleetPanda domain knowledge. If `login_access` is in fact gated
by an SSO module, tenant 3's #1017 becomes a real mismatch and the count moves from 26 to 27+.

### Q-003 · Runtime is Python 3.12 via `uv`, not the system 3.9.6
**Context:** CLAUDE.md §5 requires Python 3.11+. The system interpreter is 3.9.6 with no
third-party packages; `python3.12` and `uv` were available under `~/.local/bin`.
**Taken:** created `.venv` on 3.12.13 and installed dependencies there. `.venv/` is gitignored.
All commands in README assume `.venv/bin/python`.
**Needs you to:** nothing, unless you intended a different interpreter or a global install.

### Q-004 · Recon scripts are not committed
**Context:** CLAUDE.md §4 fixes the directory layout and says not to invent top-level
directories. The three recon scripts have no home in that layout.
**Taken:** inlined the queries that produce each non-obvious number into RECON.md instead, so
every claim is reproducible without the scripts. The scripts were run from a scratch directory
and discarded.
**Needs you to:** decide whether a `scripts/` directory is worth adding to §4. My view: yes for
the live session — being able to re-run recon in front of the interviewer is worth one folder.

### Q-005 · "Declining delivery volume" (Q8) has no materiality threshold
**Context:** RECON.md §11. Anchored on the data, seven of twelve tenants are technically
declining, but t1 at **-1.5%** is noise and t4 at **-16.3%** is a real signal. The question as
written admits any negative delta.
**Taken:** provisional cut at **-10%**, held in `src/config.py:DECLINE_THRESHOLD_PCT`, and the
answer states the threshold it used. Conservative in the sense that it reports fewer tenants
and says why.
**Needs you to:** confirm -10%, or say the agent should list every negative delta and let the
reader judge.

### Q-006 · Field name in CLAUDE.md §7 does not match the data
**What it says:** "tickets referencing modules not in that tenant's `active_modules`".
**What the data says:** the key in `customers.json` is **`modules_active`**.
**Taken:** used `modules_active`. Written as documented it is a `KeyError`. Flagged rather than
silently corrected in CLAUDE.md, same reasoning as Q-001.
**Needs you to:** nothing beyond a one-word edit to CLAUDE.md if you want the file consistent.

### Q-007 · The date anchor is stated in prose, not in structured output
**Context:** DECISIONS.md D-001. The agent answers "in the 7 days to 2026-05-29 (most recent
data available)". That is honest for a human reader, but a downstream consumer parsing the JSON
response has no machine-readable field telling it the window was shifted 91 days.
**Taken:** prose only for now — no response schema exists yet at Step 3.
**Needs you to:** decide whether the eventual response model carries an explicit
`window_start` / `window_end` / `anchor_mode` triple. My view: yes, and voice mode should say
the anchor out loud on the first query of a session only.

### Q-008 · LLM provider is assumed to be Anthropic
**Context:** CLAUDE.md §7 Step 3 names `ANTHROPIC_API_KEY` specifically; §3.1 says direct
provider SDK calls only.
**Taken:** `src/llm/client.py` wraps the Anthropic SDK and raises a configuration error when the
key is absent, per instruction. No live call is made this session, and no code path depends on
the key existing.
**Needs you to:** nothing, unless you want a provider-agnostic wrapper. My view: don't — one
provider, named directly, is easier to explain in the walkthrough than an abstraction with one
implementation.
