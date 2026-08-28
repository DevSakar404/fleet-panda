"""Every path, model name, threshold and piece of domain knowledge in one place.

Owned by: nothing -- this module imports nothing from the project and is imported
by almost everything. It is deliberately the only place a magic number is allowed
to appear (CLAUDE.md 5).

Several constants here encode findings from Step 0 recon. Each one carries the
finding that produced it, because in six weeks the number will look arbitrary and
the comment is the only thing that says why it is what it is.
"""

from pathlib import Path
from typing import Final

# --- Paths -------------------------------------------------------------------

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"

DISPATCH_DB_PATH: Final[Path] = DATA_DIR / "dispatch.db"
CUSTOMERS_PATH: Final[Path] = DATA_DIR / "customers.json"
TENANT_ALIASES_PATH: Final[Path] = DATA_DIR / "tenant_aliases.json"
TICKETS_PATH: Final[Path] = DATA_DIR / "tickets.json"
CALL_TRANSCRIPTS_PATH: Final[Path] = DATA_DIR / "call_transcripts.json"
KNOWLEDGE_BASE_PATH: Final[Path] = DATA_DIR / "knowledge_base.json"

# --- LLM ---------------------------------------------------------------------

LLM_MODEL: Final[str] = "claude-sonnet-4-5"
LLM_MAX_TOKENS: Final[int] = 2048
# Text-to-SQL is a task where we want the same question to produce the same query
# every time, so it can be cached and so a test that passes today passes tomorrow.
LLM_TEMPERATURE: Final[float] = 0.0

# --- Entity resolution -------------------------------------------------------

# rapidfuzz token_set_ratio score below which a candidate is not even considered.
# 88 sits above the highest observed *wrong* pairing in the alias index and below
# the lowest observed right one: 'cascade fuel svcs' vs 'cascade fuel services llc'
# scores 89, while the nearest unrelated pair ('gl fuel' vs that same string)
# scores 73. See RECON.md section 6.
FUZZY_MATCH_THRESHOLD: Final[float] = 88.0

# How many ranked candidates to hand back when resolution fails. Voice mode reads
# these out loud, so more than three is unusable over audio.
MAX_RESOLUTION_CANDIDATES: Final[int] = 3

# Stripped before comparison so 'Summit Energy Group Inc' matches 'Summit Energy
# Group'. Deliberately does NOT include 'services' or 'fuel' -- those are part of
# the real company names and stripping them would collapse distinct tenants.
LEGAL_SUFFIXES: Final[tuple[str, ...]] = (
    "inc", "llc", "l.l.c", "ltd", "co", "corp", "corporation", "company",
)

# --- Dispatch database -------------------------------------------------------

# Introspection allowlist. A generated query naming anything outside this set is
# rejected before execution rather than after.
TENANT_SCOPED_TABLES: Final[frozenset[str]] = frozenset({
    "customers", "drivers", "trucks", "delivery_orders", "shifts", "tank_readings",
})

# Every table in this database carries tenant_id (verified by introspection in
# Step 0), so the guard has no exempt-table case to handle. Named anyway so the
# assumption is visible and a future non-tenant table fails loudly.
TENANT_COLUMN: Final[str] = "tenant_id"

# Hard ceiling on rows returned to the LLM for synthesis. 200 rows of aggregate
# output is already more than any of the eight questions needs; the cap exists to
# stop a mis-generated cross join from filling the context window.
MAX_RESULT_ROWS: Final[int] = 200

# Wall-clock budget for a single query, enforced via sqlite3 progress handler.
QUERY_TIMEOUT_SECONDS: Final[float] = 5.0

# --- Date anchoring (DECISIONS.md D-001) -------------------------------------

# The dataset ends 2026-05-29. Anchoring relative windows on date('now') returns
# zero rows for questions 1, 2, 5 and 8. Anchoring on the newest row in the data
# keeps the answers non-empty, and the agent states the anchor in its reply so the
# reader knows the window was shifted.
DATE_ANCHOR_MODE: Final[str] = "max_data_date"
DATE_ANCHOR_COLUMN: Final[str] = "delivery_date"
DATE_ANCHOR_TABLE: Final[str] = "delivery_orders"

# --- The eight graded questions ----------------------------------------------

# Questions that range over every tenant by construction. A tenant-scoped session
# must refuse these rather than answer them with one tenant's rows and present the
# result as a platform-wide ranking.
#
# NOTE: CLAUDE.md section 9 lists only {1, 7}. Q2 ('which tenant delivered the
# most') and Q8 ('list tenants with declining volume') are equally cross-tenant.
# See OPEN_QUESTIONS.md Q-001 -- the charter was left unedited, the correct set is
# implemented here.
CROSS_TENANT_QUESTIONS: Final[frozenset[int]] = frozenset({1, 2, 7, 8})

# Below this decline a tenant is noise, not a trend. Anchored on the data, seven
# of twelve tenants are technically negative but t1 is at -1.5%. See
# OPEN_QUESTIONS.md Q-005.
DECLINE_THRESHOLD_PCT: Final[float] = -10.0

# --- Ticket triage domain knowledge (DECISIONS.md D-002) ---------------------

# Tickets describe themselves with `product_area`; customers are entitled to
# `modules_active`. The two vocabularies share only dispatch, pricing and
# tank_monitor. Mapping them is what turns a 58/85 false-positive rate into 26
# genuine entitlement gaps. billing->invoicing and reporting->analytics are
# inferred, not documented -- see OPEN_QUESTIONS.md Q-002.
AREA_TO_MODULE: Final[dict[str, str]] = {
    "dispatch": "dispatch",
    "pricing": "pricing",
    "tank_monitor": "tank_monitor",
    "billing": "invoicing",
    "reporting": "analytics",
}

# Product areas that no module gates -- every tenant can file these regardless of
# entitlement, so they can never be a module mismatch.
UNGATED_PRODUCT_AREAS: Final[frozenset[str]] = frozenset({"integration", "login_access"})

# --- Escalation signal thresholds --------------------------------------------

# Health scores in customers.json run 28-91. 40 is the assignment's own cut for
# "low health"; 60 is where the roster's middle tier starts.
HEALTH_SCORE_CRITICAL: Final[int] = 40
HEALTH_SCORE_AT_RISK: Final[int] = 60

# A contract inside this window is a renewal conversation, which changes how a
# support ticket should be handled regardless of its stated priority.
CONTRACT_RENEWAL_WINDOW_DAYS: Final[int] = 90

# Two tickets with subjects this similar, from the same tenant, are treated as
# duplicate candidates. 85 comes from RECON.md section 9: at that cut the 14 pairs
# found are all genuine refilings, and nothing unrelated is caught.
DUPLICATE_SUBJECT_THRESHOLD: Final[float] = 85.0


# --- SQL agent ---------------------------------------------------------------

# Generation attempts before giving up. One retry, not a loop: a second guard
# rejection usually means the question cannot be answered from this schema rather
# than that the model was careless, and retrying past that spends tokens and
# latency to arrive at the same refusal.
SQL_MAX_ATTEMPTS: Final[int] = 2

# Rows sent to the synthesis call. The eight graded questions all return far fewer
# than this; the cap stops a 200-row result from dominating the prompt when the
# answer only needs the shape of the top of it.
SYNTHESIS_ROW_SAMPLE: Final[int] = 25


# --- Escalation scoring weights (DECISIONS.md D-010) -------------------------
#
# Points, not probabilities. The scale is arbitrary but the ORDER is not, and the
# order is the argument: an expired contract on a healthy account outranks a
# routine ticket from a struggling one, and no single signal reaches CRITICAL
# alone. Calibrated against the real roster -- health runs 28-91 and CARR runs
# 30k-96k in even 6k steps, so the tiers below split a roster that has no natural
# clusters in it.

# Account health. The assignment's own cut for "low health" is 40.
WEIGHT_HEALTH_CRITICAL: Final[int] = 30      # health < 40  (t4, t8)
WEIGHT_HEALTH_AT_RISK: Final[int] = 15       # health < 60  (t2, t7, t11)

# Contract. Deliberately weighted near health: t2 Heartland has health 45 -- above
# the "low health" cut -- and a contract expiring 2026-08-30. A health-only rule
# never surfaces the most time-critical account on the roster.
WEIGHT_CONTRACT_EXPIRED: Final[int] = 25
WEIGHT_CONTRACT_RENEWAL_WINDOW: Final[int] = 18

# Revenue at risk. The same ticket text from t3 (96k) and t11 (30k) is not the
# same ticket.
CARR_HIGH: Final[int] = 72_000               # top third of the roster
CARR_MEDIUM: Final[int] = 54_000
WEIGHT_CARR_HIGH: Final[int] = 15
WEIGHT_CARR_MEDIUM: Final[int] = 8

# Repeat filings. A 4th report of the same subject in 26 days (t4's TankLink
# cluster) is a different problem from a 1st, and one of those four was CLOSED and
# refiled twice after -- so 'closed' is not treated as terminal (DQ-7).
DUPLICATE_CLUSTER_SIZE: Final[int] = 3
WEIGHT_DUPLICATE_CLUSTER: Final[int] = 20
WEIGHT_DUPLICATE: Final[int] = 10

# Entitlement gap: a sales/enablement signal rather than a bug. Scored low on
# purpose -- it changes who should handle the ticket more than how urgent it is.
WEIGHT_MODULE_MISMATCH: Final[int] = 10

# Operational decline over DECLINE_THRESHOLD_PCT. The two steepest decliners (t4,
# t8) are also the two lowest health scores; when the operational and CRM signals
# agree the account is genuinely moving, not just unhappy.
WEIGHT_VOLUME_DECLINE: Final[int] = 15

# Call signals. competitor_mentioned outweighs sentiment: 7 of 43 transcripts
# carry it, it is unambiguous, and it is the cheapest churn signal in the corpus.
WEIGHT_NEGATIVE_SENTIMENT: Final[int] = 10
WEIGHT_COMPETITOR_MENTIONED: Final[int] = 15
RECENT_CALL_COUNT: Final[int] = 3            # how many recent calls count as "recent"

# The ticket's own stated priority contributes but never dominates -- the
# assignment asks for escalation that considers health, CARR and contract
# proximity "not just ticket priority".
WEIGHT_PRIORITY: Final[dict[str, int]] = {"urgent": 10, "high": 5, "medium": 0, "low": 0}

# Level thresholds. CRITICAL needs several signals agreeing; nothing single
# reaches it.
ESCALATION_CRITICAL: Final[int] = 70
ESCALATION_URGENT: Final[int] = 45
ESCALATION_ELEVATED: Final[int] = 25


# Ceiling on the account-state portion of an escalation score (D-012).
#
# Without this, account signals (up to 95 points: health, contract, CARR, volume,
# sentiment, competitor) swamp ticket signals (up to 35: duplicates, module gap,
# priority), so every one of tenant 4's twelve tickets scored CRITICAL and the
# level could not rank them against each other.
#
# The cap lands mid-URGENT rather than just under CRITICAL. Just under (69) was
# tried first and was no better than no cap at all: it left one point of headroom,
# so a single "filed as high" (5 points) tipped every ticket to CRITICAL and 11 of
# tenant 4's 12 stayed identical.
#
# At URGENT + 10, a bad account alone is solidly URGENT, and CRITICAL needs 15+
# points about THIS ticket -- a repeat filing (20), or an entitlement gap plus a
# stated urgency (10 + 5). A lone "filed as high" no longer promotes anything.
MAX_ACCOUNT_RISK_POINTS: Final[int] = ESCALATION_URGENT + 10

# Which signals describe the account rather than the ticket. Named here rather
# than inferred so the split is a stated policy, not an accident of naming.
ACCOUNT_LEVEL_SIGNALS: Final[frozenset[str]] = frozenset({
    "health_critical", "health_at_risk", "contract_expired", "contract_renewal",
    "carr_high", "carr_medium", "volume_decline", "negative_sentiment",
    "competitor_mentioned",
})
