# PayAgent: Spend-Control & Payout Gateway for Autonomous Agents

> A secure, policy-governed payout orchestration layer connecting autonomous LLM agents to PayPal's REST Payouts API.
> PayAgent allows autonomous agents to receive payments for verified work without ever exposing API credentials or private keys to the agents themselves. Every payout is constrained by operator-defined budgets and recipients, protected by deterministic validation rules and LLM proof-of-work review, with automated escalation to human operators for high-risk transactions.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![PayPal SDK](https://img.shields.io/badge/PayPal-Sandbox%20Payouts%20API-003087?logo=paypal)](https://developer.paypal.com)
[![FastAPI](https://img.shields.io/badge/FastAPI-v0.115-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python)](https://python.org)
[![CI](https://github.com/samikshasainii/PayPal_Hackathon/actions/workflows/ci.yml/badge.svg)](https://github.com/samikshasainii/PayPal_Hackathon/actions/workflows/ci.yml)

---

## 1. Overview & Architecture

Autonomous AI agents (such as code reviewers, data labelers, and security bounty solvers) increasingly need to transact programmatically. However, granting agents direct access to payment cards or financial credentials creates significant security and financial risks, while requiring human approval for every micro-transaction eliminates autonomy.

**PayAgent** solves this by serving as an isolated, non-custodial financial gateway:
1. **Operators Register Tasks**: Tasks are initialized with immutable budgets, assigned agent IDs, fixed PayPal recipient emails, and required deliverable criteria.
2. **Agents Request Payouts**: Agents authenticate via REST or PayAgent's Model Context Protocol (MCP) server. Payout amounts are capped by the task budget, and recipients cannot be overridden by the agent.
3. **Gate 1 (Deterministic Policy Evaluation)**: Submissions are checked against strict rules (budget caps, hourly agent velocity limits, task assignment validation, and replay protection) before invoking any external models.
4. **Gate 2 (LLM Proof-of-Work Verification)**: Deliverables are verified asynchronously by an evaluation model (e.g. Gemini). The verification engine can only approve within predefined policy limits or escalate to human review; prompt injection or model hallucination cannot bypass budget ceilings.
5. **Human-in-the-Loop (HITL) Governance**: Payouts exceeding auto-approval thresholds (e.g., >$50) or containing ambiguous deliverables route to an operator queue.
6. **PayPal Settlement & Reconciliation**: Approved payouts settle via the PayPal Payouts v1 REST API with stored idempotency keys, cryptographically verified webhooks, and automated polling reconciliation.
7. **Real-Time Operator Console**: Web dashboard featuring live Server-Sent Events (SSE), audit logs, and one-click manual approval/rejection.

For full architecture details, state machines, and threat models, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
 Operator ──► Task Registry (budget, recipient, required deliverables)
                     │
 AI Agent ──REST/MCP─► PayAgent Gateway
                     │   Gate 1: Deterministic Rules ──fail──► POLICY_REJECTED
                     │   Gate 2: LLM Proof Review (Escalate-only)
                     │
          ┌──────────┴───────────┐
     ≤ $50 & proof OK     > $50 or flagged proof
          │                      │
          │              Human Review in Console
          ▼                      ▼
       PayPal Payouts v1 ──webhooks / poller──► Audit DB ──SSE──► Operator Console
```

---

## 2. Tools & Sponsor Integrations

* **PayPal Developer Platform**: Uses the sandbox REST Payouts API v1 for programmatic batch payouts, OAuth 2.0 with in-memory token caching, and real-time webhook listeners with cryptographic RSA signature verification.
* **Google Gemini AI**: Utilizes `gemini-3.5-flash-lite` for async Gate 2 semantic Proof-of-Work reviews, prompt injection defense, and automated Human-in-the-Loop escalation.
* **FastMCP (Model Context Protocol)**: Exposes native tool-calling endpoints (`list_my_tasks`, `request_payout`, `get_payout_status`) for seamless autonomous agent integration.
* **AG Grid (Sponsor)**: Powers the live multi-grid Operator Console (`src/static/index.html`) with reactive sorting, filtering, and Server-Sent Events (SSE) live updates.
* **Postman (Sponsor)**: Complete API test suites, preflight scenarios, and environment configs in `docs/postman/`.
* **Render (Sponsor)**: Cloud infrastructure-as-code automated blueprint in `render.yaml`.

---

## 3. Repository Structure

```text
├── .github/
│   └── workflows/
│       └── ci.yml             # GitHub Actions CI pipeline (Pytest suite)
├── docs/                      # Documentation & API Specifications
│   ├── ARCHITECTURE.md        # System architecture, state machine & threat model
│   ├── DEPLOYMENT.md          # Hosting & deployment runbook (Render, Docker)
│   └── postman/               # Postman collection & environment specifications
├── src/                       # Application Source Code
│   ├── main.py                # FastAPI entry point, routers & middleware
│   ├── config.py              # Application settings & environment parsing
│   ├── database.py            # Async SQLAlchemy connection & session management
│   ├── seed.py                # Database seeding utility for local development
│   ├── mcp_server/            # Model Context Protocol (FastMCP) server
│   │   └── server.py
│   ├── models/                # SQLAlchemy ORM models
│   │   ├── agent.py, budget.py, incident.py, payout.py, task.py, webhook.py
│   ├── schemas/               # Pydantic schemas & data transfer contracts
│   │   ├── agent.py, enums.py, llm.py, payout.py, task.py
│   ├── services/              # Domain logic & integration services
│   │   ├── auth.py            # API key hashing (SHA-256) & authentication
│   │   ├── demo_scenarios.py  # Automated test scenario runner
│   │   ├── events.py          # Server-Sent Events (SSE) broadcaster
│   │   ├── gate2_reviewer.py  # Asynchronous Gate 2 review orchestrator
│   │   ├── guardian.py        # Gate 1 deterministic policy evaluator
│   │   ├── payout_service.py  # Payout lifecycle manager & transaction handler
│   │   ├── paypal_client.py   # Async PayPal REST Payouts API client
│   │   ├── poller.py          # Payout batch status reconciliation poller
│   │   ├── proof_review.py    # LLM verification engine
│   │   ├── state_machine.py   # State transitions & status mapping
│   │   └── webhooks.py        # Webhook signature verification & handler
│   └── static/
│       └── index.html         # Real-time web operator dashboard
├── scripts/                   # Tooling & Simulation Scripts
│   ├── demo_agent.py          # Multi-scenario agent simulator for E2E testing
│   ├── seed_hosted.py         # Remote database initializer
│   └── snapshot_schema.py     # Database schema snapshot generator
├── tests/                     # Automated Test Suite
│   ├── conftest.py            # Pytest async database and client fixtures
│   ├── schema_snapshot.json   # Schema migration detection snapshot
│   ├── fixtures/              # Test payloads (genuine, injection, weak work)
│   └── test_*.py              # Comprehensive test modules
├── .env.example               # Sanitized environment variable template
├── .gitignore                 # Git ignore rules
├── LICENSE                    # MIT License
├── pytest.ini                 # Pytest runner configuration
├── render.yaml                # Infrastructure-as-code deployment blueprint
└── requirements.txt           # Pinned project dependencies
```

---

## 3. Quickstart

### Prerequisites
- Python 3.11+
- [PayPal Developer Sandbox Account](https://developer.paypal.com) (or run in Mock Mode)
- Google Gemini API key (optional for Gate 2 LLM reviews)

### 1. Clone & Set Up Environment
```bash
git clone https://github.com/samikshasainii/PayPal_Hackathon.git
cd PayPal_Hackathon

python -m venv .venv
# Windows:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
```
Populate `.env` with your credentials:
```env
PAYPAL_CLIENT_ID=your_paypal_client_id
PAYPAL_CLIENT_SECRET=your_paypal_client_secret
SANDBOX_RECIPIENT_EMAIL=your_sandbox_account@example.com
GEMINI_API_KEY=your_gemini_api_key

# Set to true to test locally without live credentials:
MOCK_PAYPAL=true
```

### 3. Run the Service
```bash
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```
- **Operator Dashboard**: [http://localhost:8000](http://localhost:8000)
- **Interactive OpenAPI Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## 4. Testing & Simulation

Initialize test data and execute the automated demo agent:
```bash
# Seed development database
python -m src.seed --reset

# Run end-to-end agent simulation scenarios
python -m scripts.demo_agent --scenario all
```

Run the automated test suite:
```bash
pytest
```

---

## 5. License
Distributed under the **MIT License**. See [LICENSE](./LICENSE) for details.
