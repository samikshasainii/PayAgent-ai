# PayAgent AI: Spend-Control Guardian for AI Agents, Settled on PayPal Payouts

> Built for the **PayPal AI Hackathon 2026**.  
> Lets AI agents get paid for completed tasks through **PayPal Payouts** without ever holding payment credentials. Every payout is bounded by an operator-defined task budget and recipient, and is checked by deterministic rules plus an LLM Proof-of-Task review, with Human-in-the-Loop approval for anything risky.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![PayPal SDK](https://img.shields.io/badge/PayPal-Sandbox%20Payouts%20API-003087?logo=paypal)](https://developer.paypal.com)
[![FastAPI](https://img.shields.io/badge/FastAPI-v0.115-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python)](https://python.org)

---

## 1. Executive Summary

As autonomous AI agents (code reviewers, research assistants, data miners, and bounty solvers) proliferate, they increasingly need to transact: paying human-in-the-loop reviewers, purchasing compute/data, and settling completed milestones.

Currently, financial autonomy for AI agents faces two fatal extremes:
1. **The Runaway Card Risk:** Giving agents unrestricted payment access causes disastrous runaway spend from infinite loops, hallucinations, or prompt injection.
2. **The Micro-Approval Bottleneck:** Requiring human managers to sign off on every \$0.50 micro-payout destroys agent autonomy.

**PayAgent AI** is the only component that holds PayPal credentials. Agents never see them.
1. **Operators register tasks** with a budget, a fixed PayPal recipient, and required deliverables.
2. **Agents request payment** against an assigned task (REST or PayAgent's own **MCP server**). They cannot choose the amount above budget or the recipient.
3. **Gate 1: deterministic rules** (assignment, budget, deliverables, replay, hourly velocity) reject bad requests instantly, with no LLM call.
4. **Gate 2: LLM Proof-of-Task review** can only *escalate* to a human, never approve beyond Gate 1, so prompt injection stays bounded.
5. **Human-in-the-Loop approval** for payouts above $50 or with doubtful proof.
6. **PayPal Payouts v1** settlement with stored idempotency keys, RSA-verified webhooks, and a reconciliation poller.
7. **AG Grid operator console** with live (SSE) status, reason codes, and one-click approve/reject.

Decision rules, state machines and threat model: [ARCHITECTURE.md](./ARCHITECTURE.md). Future work (e.g. a PYUSD rail) is listed in its §9.

---

## 2. System Architecture

For the full design, see [ARCHITECTURE.md](./ARCHITECTURE.md).

```
 Operator ──► Task Registry (budget, recipient, required deliverables)
                     │
 AI Agent ──REST/MCP──► PayAgent Guardian
                     │   Gate 1: deterministic rules ──fail──► POLICY_REJECTED
                     │   Gate 2: LLM Proof-of-Task review (escalate-only)
                     │
          ┌──────────┴───────────┐
     ≤ $50 & proof OK     > $50 or doubtful proof
          │                      │
          │              Human approves in console
          ▼                      ▼
       PayPal Payouts v1 (sandbox) ──webhooks / poller──► Audit DB ──SSE──► AG Grid console
```

---

## 3. Sponsor Tool Integrations

* **Postman:** Collection in `docs/postman/PayAgent.postman_collection.json` covering setup, the happy path, over-budget, replay and injection requests, HITL approval, and status checks.
* **Render:** `render.yaml` blueprint for the hosted demo (single worker, external persistent DB).
* **AG Grid:** Operator console with live payout status, reason codes, agent budget burn, and one-click approve/reject.

---

## 4. Repository Structure

```text
├── configs/               # Environment & experiment configs (.yaml, .json)
├── data/
│   ├── raw/               # Immutable raw records (git-ignored)
│   └── processed/         # Artifacts & intermediate caches (git-ignored)
├── docs/
│   ├── postman/           # Official Postman collection & environment
│   └── reference/         # Copies of PayPal AI developer docs (third-party, not MIT)
├── notebooks/             # Exploratory prototyping & agent simulation
├── src/                   # Production application & pipeline code
│   ├── guardian.py        # Gate 1 rules, decision table, budget reservation
│   ├── proof_review.py    # Gate 2 LLM Proof-of-Task review
│   ├── gate2_reviewer.py  # Async Gate 2: background review, restart-safe sweeper
│   ├── paypal_client.py   # OAuth cache, Payouts v1, mock transport
│   ├── poller.py / webhooks.py  # Payout status reconciliation
│   ├── mcp_server.py      # PayAgent MCP server for agents
│   ├── static/            # AG Grid operator console
│   └── main.py            # FastAPI service entry point
├── tests/                 # Unit, integration, and sandbox evaluation tests
├── .env.example           # Sanitized environment variable template
├── .gitignore             # Strict ignore rules for secrets, DBs, caches
├── ARCHITECTURE.md        # Complete system blueprint & data contracts
├── EXECUTION_PLAN.md      # Milestone implementation schedule
├── HACKATHON_RESEARCH.md  # Competitive intelligence & PayPal API specs
├── LICENSE                # MIT License
├── render.yaml            # Render cloud deployment blueprint
└── requirements.txt       # Pinned project dependencies
```

---

## 5. Quickstart & Installation

### Prerequisites
- Python 3.11 or higher
- A free [PayPal Developer Sandbox Account](https://developer.paypal.com)
- A Gemini API key (free tier, for the Proof-of-Task review)

### Step 1: Clone and Set Up Virtual Environment
```bash
git clone https://github.com/samikshasainii/PayPal_Hackathon.git
cd PayPal_Hackathon

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows (PowerShell):
venv\Scripts\Activate.ps1
# macOS / Linux:
# source venv/bin/activate
```

### Step 2: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 3: Configure Environment Variables
```bash
cp .env.example .env
```
Fill in `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `SANDBOX_RECIPIENT_EMAIL`, and `GEMINI_API_KEY`. To try it without keys, set `MOCK_PAYPAL=true` (the console shows a MOCK MODE banner).

### Step 4: Run the Application
```bash
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```
Open [http://localhost:8000/docs](http://localhost:8000/docs) for the interactive OpenAPI documentation or [http://localhost:8000](http://localhost:8000) for the Swarm Treasury Terminal.

### Step 5: Run the Demo Agent (Live Demonstration)
Before every demo run or video take, reset the demo database to a clean baseline:
```bash
python -m src.seed --reset
```

*(Optional / Recommended)* Send a warm-up call to prime the LLM endpoint connection and cache before recording:
```bash
python -m scripts.demo_agent --scenario happy
python -m src.seed --reset
```

Then run the automated multi-scenario demo agent:
```bash
python -m scripts.demo_agent --scenario all
```
This executes:
1. **Happy Path (`--scenario happy`)**: Agent submits $25.00 Proof-of-Task -> Gate 1 returns 202 `RECEIVED` -> Gate 2 LLM reviews asynchronously and auto-approves (`AUTO_APPROVED`) -> Dispatches to PayPal Sandbox (`SUBMITTED`) -> Poller reconciles to `PAID`.
2. **Tamper Detection (`--scenario tamper`)**: Submits an over-budget / tampered request ($150 vs $120 cap) -> Gate 1 hard rejection 422 `OVER_TASK_BUDGET` without invoking the LLM.
3. **High-Value Escalation (`--scenario escalate`)**: Submits a high-value $120.00 proof -> Gate 1 returns 202 `RECEIVED` -> Gate 2 LLM verifies proof (`passed=True`), flags `ABOVE_AUTO_LIMIT` (>$50), and routes to `PENDING_HUMAN_APPROVAL` for operator review.

---

## 6. Testing & Verification

Run the test suite:
```bash
pytest tests/ -v
```

---

## 7. License
Distributed under the **MIT License**. See [LICENSE](./LICENSE) for details.
