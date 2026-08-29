# FleetPanda AI Engineer - Take-Home Assignment

## The Objective

Build a working **voice and chat support agent** for FleetPanda, a B2B SaaS platform that provides dispatch management software to ~50 fuel delivery companies (called "tenants"). Each tenant has drivers, trucks, delivery orders, and end-customers they deliver fuel to. FleetPanda is SOC 2 Type 2 compliant and serves all tenants from shared infrastructure.

Your agent must do two things:

1. **Answer dispatch database questions** - A support rep or CSM types or says "How many deliveries did Cascade Fuel complete last week?" and gets an accurate, tenant-scoped answer powered by SQL.

2. **Triage incoming support tickets** - A new ticket comes in. The agent pulls the customer's full context (health score, contract status, delivery stats, past tickets, recent calls, relevant knowledge base articles) and produces a structured brief with an escalation recommendation.

The agent must work in **both chat mode** (text in, text out) **and voice mode** (speech in, speech out). Both modes use the same underlying intelligence.

---

## What You Receive

### Data Files

| File | Description |
|------|-------------|
| `dispatch.db` | SQLite database with 90 days of operational data: delivery orders, shifts, drivers, trucks, end-customers, tank readings. ~10K delivery orders across 12 tenants. |
| `SCHEMA.md` | Full schema documentation for dispatch.db |
| `customers.json` | 12 tenant profiles with health scores, CARR (contracted annual recurring revenue), active modules, contract dates, assigned CSM |
| `tenant_aliases.json` | Mapping of alternate company names to canonical tenant IDs. Companies are referred to by different names across different systems. |
| `tickets.json` | ~85 support tickets across 12 tenants |
| `call_transcripts.json` | ~43 call transcript summaries. These use `tenant_name` (string), NOT `tenant_id` (integer). Entity resolution required. |
| `knowledge_base.json` | 12 known-issue articles with symptoms, root causes, and resolutions |

### Important data characteristics

- The dispatch database uses `tenant_id` (integer) everywhere for multi-tenant isolation
- Call transcripts use `tenant_name` which may be a canonical name OR an alias (see `tenant_aliases.json`)
- Explore the data carefully before building. Not everything is clean.

---

## What to Build

### The Agent (this is the main deliverable)

Build a single agent application with two interfaces:

**Chat mode:**
- Text input via terminal, API, or simple web UI (your choice)
- The user types a question or pastes a ticket
- The agent responds with either a data answer (for dispatch queries) or a ticket brief (for support triage)

**Voice mode:**
- Speech input via microphone (use any STT: Whisper, Deepgram, Google, browser API, etc.)
- Speech output via any TTS (OpenAI TTS, ElevenLabs, pyttsx3, browser API, etc.)
- Same underlying agent logic as chat mode
- A basic terminal-based or browser-based implementation is fine. We are not judging UI polish. We are judging that it works end-to-end: you speak, it understands, it queries, it speaks back.

### Agent Capability 1: Dispatch Database Queries (text-to-SQL)

The agent answers natural language questions about the dispatch database by generating and executing SQL.

**Test questions your agent must handle (include these in your test suite):**

1. "How many deliveries were completed in the last 7 days across all tenants?"
2. "Which tenant delivered the most gallons of diesel last month?"
3. "Show me the top 5 drivers by total deliveries for tenant 3"
4. "What is the average gallons per delivery for propane orders?"
5. "How many emergency orders did tenant 4 have in the past 30 days?"
6. "Which trucks are currently in maintenance status?"
7. "What is the fill rate (gallons delivered / gallons ordered) for completed orders by tenant?"
8. "List tenants with declining delivery volume (compare last 30 days vs previous 30 days)"

**Requirements:**
- Generates valid SQL, executes it, returns a human-readable answer (not raw rows)
- Every query MUST be scoped to the correct tenant when a tenant is specified
- The agent must NEVER return data from tenant B when asked about tenant A
- Handles ambiguous queries gracefully (clarifies or states assumptions)
- Refuses or flags queries that would expose cross-tenant data in a tenant-scoped session

### Agent Capability 2: Support Ticket Triage

When given a support ticket (pasted in chat or described over voice), the agent produces a structured "ticket brief" by pulling context from ALL available sources:

1. **Customer profile** from `customers.json` - health score, CARR, modules active, contract end date, CSM
2. **Dispatch data** from the database - recent delivery volume, anomalies, operational stats for this tenant
3. **Past tickets** from `tickets.json` - history from this customer, similar issues, duplicates
4. **Call history** from `call_transcripts.json` - recent call sentiment, topics, action items
5. **Knowledge base** from `knowledge_base.json` - relevant articles matched by symptoms

**The ticket brief must include:**
- Customer profile summary
- Escalation recommendation with reasoning (consider health score, CARR, contract proximity - not just ticket priority)
- Relevant past tickets and duplicate detection
- Relevant KB articles ranked by relevance and recency
- Recent call context and sentiment
- Operational snapshot from the dispatch DB
- Suggested response draft

**Test with at least 3 tickets from the provided data:**
- A ticket from a low-health customer (health < 40) with an expiring contract
- A ticket that appears to be a duplicate of an earlier one
- A ticket referencing a module the customer doesn't actually have active

### Agent Foundation: Data Layer + Entity Resolution

Under the hood, your agent needs a data layer that:
- Loads all sources (SQLite + JSON files) into a unified queryable system
- Resolves entities across sources (call transcripts use tenant names/aliases, not IDs)
- Handles fuzzy or imperfect name matches
- Exposes a clean interface so the agent logic doesn't deal with source-level quirks

---

## Supporting Deliverables (alongside the build)

### SECURITY.md - Code Review Challenge

Below is a simplified text-to-SQL endpoint. It has **three security vulnerabilities** related to multi-tenant isolation and input handling. Find them, explain the attack vector for each, and write the fixed version.

```python
from fastapi import FastAPI, Request
import sqlite3
import openai

app = FastAPI()

def get_db():
    return sqlite3.connect("dispatch.db")

@app.post("/api/query")
async def query_dispatch(request: Request):
    body = await request.json()
    user_question = body["question"]
    tenant_id = body.get("tenant_id")  # optional tenant filter
    
    schema = open("SCHEMA.md").read()
    
    prompt = f"""You are a SQL assistant. Given this schema:
    {schema}
    
    Generate a SQLite query to answer: {user_question}
    {"Filter by tenant_id = " + str(tenant_id) if tenant_id else ""}
    Return ONLY the SQL query, nothing else."""
    
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    
    sql = response.choices[0].message.content.strip()
    
    db = get_db()
    results = db.execute(sql).fetchall()
    db.close()
    
    return {"sql": sql, "results": results, "count": len(results)}
```

For each vulnerability: name it, describe the specific attack scenario, and show the fix.

### DECISIONS.md - Engineering Journal

Document as you build. This is not a separate essay exercise - it is a log of decisions you actually made while building the agent.

**5 architecture decisions** with alternatives considered and why you chose what you chose. These should be SPECIFIC to your build, referencing actual code, data patterns, or trade-offs you encountered.

**2+ data quality observations** - things you found in the data that a production system would need to handle.

**Cost estimate** - what would this system cost in LLM API calls per day processing 50 tickets/day and 100 dispatch questions/day? Show token math.

**Scaling question** - if this served 150 tenants with 500K+ delivery orders each: what breaks first in your pipeline? How would you enforce tenant isolation at the database level (be specific, not just "use RLS")? How would you add a new data source without modifying existing agent code?

**End-customer agent question** - FleetPanda's tenants also have their own end-customers (homeowners, businesses that receive fuel deliveries). If THIS agent also needed to serve those end-customers (e.g., a homeowner calls Cascade Fuel and asks "when is my next delivery?" or "what's my tank level?"), how does the data scoping change? What should the end-customer agent see vs NOT see? How do you handle two layers of tenant isolation (FleetPanda -> tenant -> end-customer)?

---

## Constraints

- **Language:** Python required
- **LLM:** Any provider. Include setup instructions.
- **STT/TTS:** Any solution. Can be local (Whisper), cloud API, or browser-based. Must actually work - we will test it.
- **Time:** 72 hours from receipt. Estimated effort: 8-10 hours.
- **AI tools:** Use freely. We expect it. The decision journal, security explanations, and live session are how we evaluate your thinking.
- **Frameworks:** No agent framework required. Use one only if it genuinely helps, and explain why in DECISIONS.md.
- **Tests:** Include tests for: (a) entity resolution, (b) tenant isolation (tenant A query never returns tenant B data), (c) the 8 SQL test questions, (d) security fixes.

---

## Deliverables

```
your-submission/
    README.md            # Setup, how to run chat mode, how to run voice mode
    DECISIONS.md         # Engineering journal
    SECURITY.md          # Code review answers
    src/                 # Source code
    tests/               # Tests
    requirements.txt     # or pyproject.toml
```

Submit as a private GitHub repo (invite us) or zip file.

---

## What Happens After Submission

**Live session (60-75 min, screen share required):**

1. **Demo** (10 min) - Show us the agent working in both chat and voice mode. Ask it a dispatch question. Triage a ticket. We want to see it run.

2. **Code walkthrough** (10 min) - Walk through the ticket triage flow end to end. Show how entity resolution feeds into the SQL agent. Show tenant isolation.

3. **Live coding** (20 min) - We will give you a new requirement and ask you to implement it in your codebase while we watch. You may use any AI tools (Cursor, Copilot, Claude, etc.) during this segment. We are evaluating how you work: how you navigate your code, how you reason about data flow, how you use AI tools, and how you extend your own system.

4. **Live scenario** (10 min) - We give you a multi-signal edge case and ask you to walk through what the agent should do. Tests real-time reasoning about domain-specific problems.

5. **Architecture discussion** (10 min) - Scaling, end-customer agent, adding new data sources.

The live session carries significant weight. Come prepared to code, not just talk.

---

## Evaluation

| Area | Weight | What we're looking for |
|------|--------|----------------------|
| Working agent - chat mode | 15% | Dispatch queries return correct answers, ticket briefs are rich and useful |
| Working agent - voice mode | 10% | Speech in, speech out, same quality as chat. Handles voice-specific UX (latency, confirmation). |
| Text-to-SQL correctness + tenant isolation | 15% | All 8 questions answered correctly, tenant scoping enforced as a hard constraint |
| Ticket triage quality | 10% | All 5 sources used, smart escalation logic, duplicate/anomaly detection |
| Security challenge | 10% | All 3 vulnerabilities found with specific attack scenarios |
| DECISIONS.md | 10% | Specific trade-offs, realistic cost math, thoughtful scaling and end-customer answers |
| Code quality + tests | 10% | Clean structure, error handling, meaningful test coverage |
| Live session (demo + coding + scenario) | 20% | Agent works in demo, can extend own codebase live, handles curveball |

---

## Questions?

Email [REDACTED] with clarifying questions. Asking good questions is a positive signal.
