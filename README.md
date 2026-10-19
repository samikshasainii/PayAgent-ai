# PayPilot AI: Autonomous Agentic Treasury & Payout Engine

> Built for the **PayPal AI Hackathon 2026**.  
> Transforming messy, unstructured commercial performance data into audited, multi-party PayPal disbursements with Human-in-the-Loop safeguards and real-time webhook intelligence.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![PayPal SDK](https://img.shields.io/badge/PayPal-Sandbox%20Payouts%20API-003087?logo=paypal)](https://developer.paypal.com)
[![FastAPI](https://img.shields.io/badge/FastAPI-v0.115-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python)](https://python.org)

---

## 1. Executive Summary

Disbursing funds to gig workers, affiliates, prize winners, and vendors is a multi-billion dollar friction point. Companies waste dozens of hours manually formatting spreadsheets, calculating deductions, and double-checking emails, constantly risking fat-finger duplicate payments.

**PayPilot AI** is an autonomous financial operations co-pilot that:
1. **Ingests** natural language instructions or raw tabular timesheets.
2. **Normalizes & Audits** recipients, currencies, and amounts via a multi-agent reasoning graph.
3. **Screens for Fraud & Anomalies**, flagging duplicate emails and statistical spikes.
4. **Applies a Human-in-the-Loop (HITL) Safety Gate**, requiring one-click executive authorization before any funds move.
5. **Executes Disbursals** via the **PayPal Payouts REST API** (`POST /v1/payments/payouts`) with idempotent batch tracking.
6. **Captures Asynchronous Lifecycle Events** through real-time PayPal Sandbox Webhook listeners.

---

## 2. System Architecture

For complete system design, data contracts, and threat models, see [ARCHITECTURE.md](./ARCHITECTURE.md).

```
[ Natural Language / CSV Input ]
              │
              ▼
[ Ingestion & Normalizer Agent ] ──> Pydantic Schema Validation
              │
              ▼
[ Compliance & Fraud Auditor ]   ──> Duplicate / Variance Scoring
              │
              ▼
[ Human-in-the-Loop Gateway ]    ──> Executive 1-Click Authorization
              │
              ▼
[ PayPal Payouts REST API ]      ──> api-m.sandbox.paypal.com
              │
              ▼
[ PayPal Webhook Listener ]      ──> Real-Time SQLite Audit Trail
```

---

## 3. Sponsor Tool Integrations

* **Postman:** Complete, importable collection in `docs/postman/PayPilot_PayPal_Sandbox.postman_collection.json` with pre-configured sandbox variables for direct API inspection.
* **Render:** Infrastructure-as-code deployment specification via `render.yaml` for 1-click cloud hosting.
* **AG Grid:** Interactive executive table with live sorting, risk badges, and batch filtering in the management dashboard.

---

## 4. Repository Structure

```text
├── configs/               # Environment & experiment configs (.yaml, .json)
├── data/
│   ├── raw/               # Immutable raw data (git-ignored)
│   └── processed/         # Artifacts & intermediate caches (git-ignored)
├── docs/
│   └── postman/           # Official Postman collection & environment
├── notebooks/             # Exploratory work & rapid prototyping
├── src/                   # Production application & pipeline code
│   ├── components/        # Multi-agent stages, schemas & webhook handlers
│   ├── utils/             # PayPal OAuth client, DB models & loggers
│   └── main.py            # FastAPI service entry point
├── tests/                 # Unit, integration, and sandbox evaluation tests
├── .env.example           # Sanitized environment variable template
├── .gitignore             # Strict ignore rules for data, secrets, caches
├── ARCHITECTURE.md        # Complete system blueprint & data contracts
├── EXECUTION_PLAN.md      # Milestone implementation schedule
├── LICENSE                # MIT License
├── render.yaml            # Render cloud deployment blueprint
└── requirements.txt       # Pinned project dependencies
```

---

## 5. Quickstart & Installation

### Prerequisites
- Python 3.11 or higher
- A free [PayPal Developer Sandbox Account](https://developer.paypal.com)
- An OpenAI or compatible LLM API Key

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
Copy the template and fill in your PayPal Sandbox credentials:
```bash
cp .env.example .env
```
Update `.env` with your `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, and `OPENAI_API_KEY`.

### Step 4: Run the Application
```bash
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```
Open [http://localhost:8000/docs](http://localhost:8000/docs) to explore the interactive OpenAPI/Swagger documentation.

---

## 6. Testing & Verification

Run the test harness to verify schema validation and PayPal API client readiness:
```bash
pytest tests/ -v
```

---

## 7. License
Distributed under the **MIT License**. See [LICENSE](./LICENSE) for full details.
