# EXECUTION PLAN: PayPilot AI
## Multi-Stage Implementation Roadmap, Milestones & Verification Protocol

---

## 0. Context Initialization Directive (For AI State Restoration)

```text
CRITICAL CONTEXT DIRECTIVE:
When resuming or initializing an AI session for this repository:
1. Parse ARCHITECTURE.md to ingest the system components, data schemas, security constraints, and PayPal integration specs.
2. Parse this file (EXECUTION_PLAN.md) to inspect milestone progress, pending file targets, and acceptance criteria.
3. Validate current working environment via `python --version`, `pip list`, and check git status.
4. Execute strictly within the current active milestone. Do NOT implement out-of-scope tasks ahead of schedule.
```

---

## 1. Milestone Master Schedule

```
+----------------------------------------------------------------------------------------+
|                                    MASTER ROADMAP                                      |
+----------------------------------------------------------------------------------------+
| Milestone 0: Clean Skeleton Scaffolding & Environment Setup            (Target: Step 1)|
| Milestone 1: Core PayPal Client & Deterministic Data Layer             (Target: Step 2)|
| Milestone 2: Multi-Agent Reasoning, Risk Audit & Orchestration Engine  (Target: Step 3)|
| Milestone 3: Asynchronous Webhook Receiver & SQLite Audit Store        (Target: Step 4)|
| Milestone 4: Executive Treasury Dashboard (UI) & Sponsor Integrations  (Target: Step 5)|
| Milestone 5: End-to-End Validation, Video Demo Script & Final Polish   (Target: Step 6)|
+----------------------------------------------------------------------------------------+
```

---

## 2. Granular Task Decomposition

### Milestone 0: Clean Skeleton Scaffolding & Runtime Readiness
- **Objective:** Establish modular folder architecture, sanitized configuration templates, and virtual environment setup with zero implementation code.
- **Tasks:**
  - `M0.1`: Create empty directory tree: `configs/`, `data/raw/`, `data/processed/`, `docs/postman/`, `notebooks/`, `src/components/`, `src/utils/`, `tests/` with `.gitkeep` files.
  - `M0.2`: Generate root `.gitignore` blocking `.env`, virtualenvs, cache files, and private sandbox tokens.
  - `M0.3`: Generate `.env.example` defining `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_MODE=sandbox`, `LLM_API_KEY`, `WEBHOOK_ID`, and security parameters.
  - `M0.4`: Create minimal `requirements.txt` pinning FastAPI, Uvicorn, Pydantic, HTTPX, LangChain, SQLAlchemy, and Pytest.
  - `M0.5`: Add open-source `LICENSE` (MIT) and baseline `README.md`.
- **Binary Acceptance Criteria:** Clean repo structure committed; zero python syntax errors; git detects clean status; license visible.

---

### Milestone 1: Core PayPal Client & Deterministic Data Layer
- **Objective:** Build robust, testable PayPal OAuth2 client, Payout API integration with `PayPal-Request-Id` idempotency, and strict Pydantic financial schemas.
- **File Targets:**
  - `src/utils/paypal_client.py`: Async HTTP client with OAuth token caching (9-hour TTL), automatic refresh, and retry backoff.
  - `src/components/schemas.py`: Pydantic models for `PayoutItem`, `PayoutBatchRequest`, `PayoutBatchResponse` enforcing `Decimal` precision, `recipient_type` enum (`EMAIL`, `PHONE`, `PAYPAL_ID`), and ISO 4217 zero-decimal logic.
  - `tests/test_paypal_client.py`: Unit tests validating token generation, mock payload serialization, idempotency UUIDs, and negative error simulation (`ERRPYO002`, `ERRPYO003`).
- **Binary Acceptance Criteria:** Successful generation of PayPal Sandbox OAuth token; 100% test pass on schema validation (rejecting negative values, malformed emails, and ensuring zero-decimal currencies like JPY format without decimals).

---

### Milestone 2: Multi-Agent Reasoning, Risk Audit & Orchestration Engine
- **Objective:** Build the multi-agent pipeline: Ingestion/Normalizer, Compliance & Anomaly Auditor, and HITL Gateway.
- **File Targets:**
  - `src/components/normalizer_agent.py`: Parses unstructured text/CSV into validated `PayoutItem` lists.
  - `src/components/auditor_agent.py`: Statistical anomaly detector flagging duplicate recipients, excessive variances, and assigning risk score ($0.0 - 1.0$).
  - `src/components/orchestrator.py`: LangGraph / state machine controlling pipeline flow, pausing on `requires_hitl=True`.
  - `tests/test_agents.py`: Synthetic test cases verifying duplicate detection, extraction accuracy, and threshold interrupts.
- **Binary Acceptance Criteria:** Agent correctly detects duplicated recipient in test prompt; successfully halts batch disbursal when aggregate amount exceeds safety threshold.

---

### Milestone 3: Asynchronous Webhook Receiver & Persistence Layer
- **Objective:** Establish SQLite transaction audit store and real-time PayPal webhook ingestion.
- **File Targets:**
  - `src/utils/db.py`: Async SQLAlchemy setup with models: `Batch`, `PayoutItemModel`, `WebhookEvent`.
  - `src/components/webhook_handler.py`: Webhook verification, event parsing (`PAYMENT.PAYOUTSBATCH.SUCCESS`), and state transitions.
  - `src/main.py`: FastAPI server exposing `/api/v1/payouts/prepare`, `/api/v1/payouts/execute`, and `/api/v1/webhooks`.
  - `tests/test_webhooks.py`: Ingestion test dispatching synthetic PayPal webhook events and validating database state transition.
- **Binary Acceptance Criteria:** Webhook endpoint returns 200 OK to PayPal; database records state shift from `SUBMITTED` $\to$ `SUCCESS` upon simulated event.

---

### Milestone 4: Executive Treasury Dashboard & Sponsor Tool Artifacts
- **Objective:** Modern, interactive financial UI and official sponsor tool assets (Postman & Render).
- **File Targets:**
  - `src/static/` or dashboard UI: Executive interface displaying active batches, risk scoring badges, and one-click "Approve & Disburse" authorization gate.
  - `docs/postman/PayPilot_PayPal_Sandbox.postman_collection.json`: Importable Postman Collection with pre-configured requests for PayPal auth, payout creation, and webhook simulation.
  - `docs/postman/PayPilot_Environment.postman_environment.json`: Postman environment template.
  - `render.yaml`: Render deployment blueprint for FastAPI web service.
- **Binary Acceptance Criteria:** Web dashboard displays interactive batch table with live status; Postman collection successfully runs in Postman CLI or app without error.

---

### Milestone 5: End-to-End Validation, Demo Script & Final Polish
- **Objective:** Complete full-system sandbox verification, produce demo video recording guidelines, and finalize submission documentation.
- **Tasks:**
  - Execute end-to-end sandbox payout with live PayPal credentials.
  - Verify recipient account in PayPal Sandbox Dashboard (`sandbox.paypal.com`).
  - Draft precise, timestamped 2:30 video demo script.
  - Complete `README.md` with reproduction instructions and Devpost submission summary.
- **Binary Acceptance Criteria:** Live payout batch recorded in PayPal Sandbox; all integration tests pass; README passes judging reproducibility audit.

---

## 3. Validation & Verification Protocol

| Stage | Command / Test | Verification Target |
| :--- | :--- | :--- |
| **Lint & Syntax** | `python -m py_compile src/main.py` | Zero syntax or import errors. |
| **Unit Testing** | `pytest tests/test_paypal_client.py tests/test_agents.py -v` | 100% schema and logic assertion pass. |
| **OAuth Sanity** | `pytest tests/test_e2e_sandbox.py -k test_auth` | Valid 200 OK and bearer token return from PayPal Sandbox. |
| **HITL Interrupt** | `pytest tests/test_agents.py -k test_hitl_trigger` | Pipeline state halts with `WAITING_FOR_APPROVAL`. |
| **Webhook Delivery**| `curl -X POST http://localhost:8000/api/v1/webhooks -d @tests/fixtures/sample_webhook.json` | Webhook recorded and status updated in SQLite DB. |

---

## 4. Risk Matrix & Fallback Strategies

| Identified Risk | Severity | Mitigation & Pivot Fallback |
| :--- | :--- | :--- |
| **PayPal Sandbox Latency / Downtime** | High | Built-in offline Mock Engine toggle (`MOCK_PAYPAL=true`) so the full agent and UI pipeline runs flawlessly during local judging or video demo recording. |
| **LLM Entity Extraction Variance** | Medium | Few-shot prompting + Pydantic validation retry loop (self-correcting parser on JSON syntax error). |
| **Webhook Inaccessibility Locally** | Low | Dual-mode webhook receiver: supports public tunnel (ngrok) as well as an internal simulation endpoint (`/api/v1/webhooks/simulate`) for judges running locally. |
| **Sandbox Email Quarantine** | Low | Sandbox does not send external emails; all mock receipts route to Developer Dashboard > Notifications console. Tested via API response status and webhook events. |
| **Video Length Overrun** | High | Strict 150-second timer cap in the script to ensure zero risk of exceeding the 3-minute hard ceiling. |
