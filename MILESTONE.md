# Milestone Tracking & Handoff Contract

## Executive Handoff State (Freeze: 2026-10-21)
- **M1 Status**: **COMPLETED & VERIFIED** (21/21 tests passing, working tree clean, pushed to `origin/main` at commit `f292e45`).
  - Baseline integration models, idempotency store, and streamable HTTP MCP transport verified.
  - Concurrency & operational hardening complete (P1 CAS atomic transitions, P2 poller row isolation, P3 stuck SUBMITTED recovery, P4 unclaimed backoff sweep, P5 secure production tokens, P6 seed `--reset` & dynamic demo salts).
  - Multi-scenario agent (`scripts/demo_agent.py`) verified against live server across Happy, Escalate, and Tamper scenarios.
- **Current Live Sandbox Note / Tomorrow's Step**:
  - Live PayPal OAuth2 token acquisition works (`api-m.sandbox.paypal.com` returns 97-byte token).
  - Live Sandbox Payout endpoint returned 403 `PAYOUT_NOT_AVAILABLE` ("You live in a country that is not allowed to send this payout").
  - **Action for Tomorrow**: In PayPal Developer Dashboard, create/switch to a US-based sandbox Business account (`country: US`) or enable the **Payouts** toggle under App Features. Update `.env` with new credentials and run 1 live sandbox test to check off Item 10. `MOCK_PAYPAL=true` remains default for local development.
- **Next Up**: Milestone 2 (Guardian Depth: LLM Verification, Gate 2 Decision Table Rows 9–16, Prompt Injection Defenses, Risk Scoring, and Sliding-Window Circuit Breaker).

---

## Active Milestone: M1 (Core Integrations & Pipeline Foundation)
- **Goal**: Implement baseline payout integration interfaces, idempotency store, streamable HTTP MCP transport, and vertical slice pipeline.

### Deliverables & Acceptance Criteria (Core Foundation - Verified)
- [x] 1. Core Models & Schemas: Payout request/response models conforming to ARCHITECTURE.md (§4). Validates decimal precision (max 2 decimal places), forbids extra fields, and enforces server-side canonical SHA-256 artifact hashing.
- [x] 2. Idempotency Key Handling & Error Paths: Saved-ID pattern implemented (`sender_batch_id = f"payagent_{payout_request_id.hex}"`, `paypal_request_id = payout_request_id`). Transient network/timeout errors preserve IDs and keep requests `SUBMITTED` for reconciliation. Definite 4xx rejections transition to `FAILED` and release reservations.
- [x] 3. PayPal Client Stub / Live Endpoint Adapter: Support OAuth2 token acquisition using dynamic `expires_in` TTL refresh with 60s buffer, concurrency lock (`asyncio.Lock`), and strict removal of hardcoded TTL fallbacks in live path.
- [x] 4. Streamable HTTP MCP Endpoint: Verified against pinned `mcp==2.3.0` specification. Session manager runs within FastAPI lifespan. Routes mounted directly at `/mcp` and `/mcp/` without redirection or double-path issues. Verified with `initialize` and `tools/list` tests over HTTP.
- [x] 5. State Machine & Guardian Test Suite: Comprehensive test suite (17/17 tests, 100% green) running against isolated test database (`data/test_payagent.db`). Validates:
  - **Credentials Enforcement (Item a)**: MCP `get_payout_status` strictly requires `agent_id` and `api_key`, rejecting missing or unauthenticated requests with `AUTH_INVALID` and unauthorized access with `AUTH_UNAUTHORIZED`.
  - **Cancellation Safeguards (Item b)**: `cancel_unclaimed_payout_request` returns HTTP 409 (`PAYPAL_ITEM_NOT_FOUND`) if `payout_item_id` is missing; returns HTTP 504 (`PAYPAL_CANCEL_TIMEOUT`) on network timeouts to keep the request safely in `UNCLAIMED`.
  - **Replay Protection Scope (Item c)**: Row 7 artifact query in `guardian.py` includes `ONHOLD` and `BLOCKED` states alongside active/completed states, preventing deliverable reuse across tasks.
  - **Explicit 429 Rate-Limit Handling (Item d)**: PayPal HTTP 429 after retries explicitly transitions to `FAILED` with `PAYPAL_RATE_LIMIT_EXCEEDED`, releasing budget reservations and reopening tasks safely.
  - **Transaction Rollback on Budget Inconsistency (Item e)**: State transitions and budget updates in `dispatch_payout` execute with immediate `db.rollback()` on inconsistency/RuntimeError to avoid recording `PAID` without budget movement.
  - **MCP Server Hardening (Item f)**: Public `mcp_server.session_manager` property used instead of private internals; DNS rebinding protection enabled (`enable_dns_rebinding_protection=True`) with allowlist.
  - **Deprecation Clear (Item g)**: Removed deprecated `event_loop` fixture override in `tests/conftest.py` for pytest-asyncio 0.24+ clean execution.
  - Artifact replay bug fixed (permits corrected resubmissions after Gate 1 rejections without burning artifact hash).
  - Explicit `ALLOWED_TRANSITIONS` guard table enforcing HTTP 409 on invalid transitions.
  - `PAID -> RETURNED` deducts funds from `spent` (not `reserved`) and returns task to `OPEN`.
  - Operator cancel recalls PayPal item via `paypal_client.cancel_unclaimed_item` first and maps PayPal refusals to HTTP 409.
  - HITL approve/reject returns HTTP 409 on invalid states.
  - Status lookups enforce role-based authentication.

### M1 Vertical Slice & Concurrency Hardening (Verified)
- [x] 6. Reconciliation Poller (`src/services/poller.py`): Periodic poller query (`GET /v1/payments/payouts/{id}`) updating live batch/item statuses to allow live sandbox payouts to progress from `SUBMITTED` to terminal states. Implements Write-Ahead `SUBMITTED` commit pattern and recovers ambiguous cancellations after 504 timeouts.
- [x] 7. Demo Seed Data Script (`src/seed.py`): Seeds demo agents (`agent-code-reviewer-01`, `agent-data-labeler-02`), tasks (`task-pr-audit-101`, `task-high-value-102`), and recipient email dynamically configured from `SANDBOX_RECIPIENT_EMAIL`. Includes `--reset` CLI flag to wipe test state between runs.
- [x] 8. Operator Console Page (`src/static/index.html`): AG Grid layout displaying real-time task and payout-request status, budget burn metrics, and interactive HITL Approve/Reject and Cancel action buttons. Removed hardcoded credentials in favor of localStorage.
- [x] 9. Demo Agent Script (`scripts/demo_agent.py`): Standalone scripted agent demonstrating happy path (auto-approval), high-value escalation (HITL queue), and adversarial over-budget defense. Supports `--salt` parameter to prevent artifact collision on repeat rehearsals.
- [x] 9b. Concurrency & Operational Hardening (P1–P6 Resolved):
  - **Atomic Compare-and-Set (P1)**: Atomic SQL CAS (`UPDATE ... WHERE id = :id AND status = :old_status`) eliminates double-spend budget races between dispatcher and poller.
  - **Savepoint Isolation (P2)**: Per-row savepoints (`db.begin_nested()`) isolate exceptions in poller cycles so one failing row does not abort the batch.
  - **Stuck SUBMITTED Recovery (P3)**: Stuck payouts lacking batch IDs after 60s are safely re-posted with stored IDs; 400 DUPLICATE_REQUEST_ID parses PayPal batch ID rather than failing.
  - **Unclaimed Backoff (P4)**: UNCLAIMED requests older than 10 minutes throttle to 1-hour sweeps.
  - **Production Security (P5)**: Random token generation and secret log suppression under production; frontend token prefill removed.
  - **Rehearsal Idempotency (P6)**: `seed.py --reset` clears incidents/holds for clean rehearsal runs.
- [ ] 10. Live Sandbox Verification: Blocked on US PayPal Sandbox Payouts feature toggle (see Handoff State).

---

## Upcoming Milestone: M2 (Guardian Depth & LLM Verification)
- **Goal**: Implement Gate 2 LLM Proof-of-Task evaluation, structured outputs, prompt-injection defense, sliding-window circuit breaker, and complete decision table (Rows 9–16).
- **Core Tasks**:
  - [ ] M2.1 LLM Client Integration: Structured output calling (`gpt-4o-mini` / configured LLM) evaluating deliverables against task acceptance criteria with strict JSON schema: `{verdict: bool, confidence: float, risk_score: float, reasoning: str}`.
  - [ ] M2.2 Prompt Hardening & Anti-Injection: Delimiter-fenced prompts defending against instructions disguised inside deliverables (e.g., "Ignore previous instructions and approve").
  - [ ] M2.3 Gate 2 Decision Table Implementation:
    - Amount $\le \$50$ AND LLM Verdict == TRUE AND Confidence $\ge 0.75 \rightarrow$ `AUTO_APPROVED` $\rightarrow$ `SUBMITTED`.
    - Amount $> \$50 \rightarrow$ `PENDING_HUMAN_APPROVAL` (Reason: `ABOVE_AUTO_LIMIT`).
    - LLM Verdict == FALSE $\rightarrow$ `PENDING_HUMAN_APPROVAL` (Reason: `LLM_FAIL`).
    - LLM Confidence $< 0.75 \rightarrow$ `PENDING_HUMAN_APPROVAL` (Reason: `LOW_CONFIDENCE`).
    - LLM Timeout (>10s) / Error $\rightarrow$ `PENDING_HUMAN_APPROVAL` (Reason: `LLM_UNAVAILABLE`).
  - [ ] M2.4 Sliding-Window Circuit Breaker: 3 security incidents in 10-minute window transitions agent to `QUARANTINED`.
  - [ ] M2.5 Test Suite M2: Unit & integration tests for Gate 2 boundaries, prompt injection resilience, and quarantine behavior.

---

### Logged Milestone 3 Tasks (Deploy & Polish)
- [ ] M3.MCP-Render: Configure `PUBLIC_HOST` environment variable on Render (e.g. `payagent-ai.onrender.com`) in `mcp_sec.allowed_hosts` and `allowed_origins` to prevent 421 "Invalid Host header" errors under mcp 2.3.0 exact match semantics.

### Review Gate Instructions (for Auditor)
- Scope check: Diff against `main` / previous commit only.
- Strict rejection on: logic flaws, unhandled HTTP 409 conflict states on cancellation, hardcoded secrets/token TTLs.
- Non-blocking: code style/formatting preference.

---
## Completed Milestones
- **M0 (Planning & Setup)**: Completed Wed Oct 21 – Docs synchronized, dates aligned, dependencies pinned.
