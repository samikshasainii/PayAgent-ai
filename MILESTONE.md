# Milestone Tracking & Acceptance Contract

## Current Active Milestone: M1 (Core Integrations & Pipeline Foundation)
- **Window**: Fri Oct 23 – Sun Oct 25
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

### Remaining M1 Vertical Slice Items (Tracked Before M2 Guardian Depth)
- [ ] 6. Reconciliation Poller (`src/services/poller.py`): Periodic poller query (`GET /v1/payments/payouts/{id}`) updating live batch/item statuses to allow live sandbox payouts to progress from `SUBMITTED` to terminal states.
- [ ] 7. Demo Seed Data Script (`src/seed.py`): Seed demo agents, tasks, and sandbox recipient email from `SANDBOX_RECIPIENT_EMAIL`.
- [ ] 8. Operator Console Page (`src/static/index.html`): AG Grid layout displaying real-time task and payout-request status.
- [ ] 9. Demo Agent Script (`scripts/demo_agent.py`): Standalone scripted agent for end-to-end task execution and payout submission.
- [ ] 10. Live Sandbox Verification: End-to-end sandbox verification reaching `PAID` status against `https://api-m.sandbox.paypal.com`.

### Review Gate Instructions (for Auditor)
- Scope check: Diff against `main` / previous commit only.
- Strict rejection on: logic flaws, unhandled HTTP 409 conflict states on cancellation, hardcoded secrets/token TTLs.
- Non-blocking: code style/formatting preference.

---
## Completed Milestones
- **M0 (Planning & Setup)**: Completed Wed Oct 21 – Docs synchronized, dates aligned, dependencies pinned.
