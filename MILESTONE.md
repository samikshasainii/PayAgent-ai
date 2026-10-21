# Milestone Tracking & Handoff Contract

## Executive Handoff State (Freeze: 2026-10-21)
- **M1 Status**: **COMPLETED & VERIFIED** (All 10 items complete; working tree clean, commit `f292e45` plus Item 10 live sandbox payout verified).
  - Item 10 Live Sandbox Verification Complete: Real sandbox payout executed using US Sandbox Business REST App (`client_id` prefix `AZh...`). Demo agent submitted $25.00 Proof-of-Task, PayPal created batch `ZPM7XVPQ6S9JJ` (item ID `MM5C3BBDKYTBA`), poller reconciled live from `SUBMITTED` to `PAID` via `GET /v1/payments/payouts/ZPM7XVPQ6S9JJ`. Verified in PayPal Sandbox dashboard.
- **M2 Status**: **COMPLETED & VERIFIED** (reviewer sign-off 2026-10-21; merge commit `6922107`; 37/37 tests passing).
  - Poller Concurrency & WAL: SQLite engine configured with `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=5000;`. Poller queries PayPal HTTP API outside write transactions, preventing lock contention on API writes. Removed duplicate P3 logic in `reconcile_request`; updated 60s stuck wait calculation to measure from `req.updated_at`.
  - Seed Safety: `--reset` verifies non-final payout requests and refuses when non-final requests exist outside mock mode. Production credential generation clearly guided.
  - LLM Proof Review & Latency Tuning: Delimiter-fenced prompts defending against prompt injection attacks (`<<<DELIVERABLE_DATA>>>`). Switched to `gemini-3.5-flash-lite` with 45s timeout (`LLM_TIMEOUT_SECONDS=45`) to accommodate free-tier endpoint latency (measured at 10–45s per call). Structured outputs (`LLMVerdict`) with tenacity exponential backoff retries on 429 (`openai.RateLimitError`). Reasoning field sanitizer enforcing strictly $\le 500$ characters.
  - Real Gemini API Verification: Initial 3.8-flash benchmark was invalid because its 20 free req/day quota exhausted, causing all calls to fall back to `LLM_UNAVAILABLE`. The benchmark was re-run on `gemini-3.5-flash-lite` across all 16 fixtures:
    - Genuine Proofs: 6/6 approved (100.0% precision).
    - Weak Proofs: 6/6 rejected as `PROOF_DOUBTFUL` (100.0% rejection).
    - Injection Attacks: 4/4 rejected with explicit `"Prompt injection detected"` verdicts (100.0% defense).
    - Auto-approvals on Weak/Adversarial: Exactly 0 (0% false positives).
    - Results persisted in `data/gemini_eval_report.json`.
  - Canonical Gate 2 Decision Table: Complete implementation of rows 9–12, display-only risk scoring, and sliding-window circuit breaker (`PROOF_DOUBTFUL` records incidents; 3 incidents in 10 minutes triggers `QUARANTINED`).
  - Webhooks Hardened (P0 & P1 Resolved): Unsigned webhooks in live mode rejected with HTTP 400 `WEBHOOK_UNVERIFIED` (mock mode only may bypass). Unverified webhooks do not count for deduplication, ensuring failed signature deliveries never block PayPal retries. Monotonic FSM progression enforced.
  - Asynchronous Gate 2 (merged from `feat/async-gate2`): Gate 1 returns 202 `RECEIVED`; the LLM review runs in the background and decides via CAS; a sweeper resumes orphaned reviews after restart. Tasks lock at Gate 1 (one task, one payout), fixing a double-payment path. See ARCHITECTURE.md §5.6.
  - End-to-End Rehearsal (live PayPal sandbox + real Gemini, `--scenario all`): happy $25 -> 202 `RECEIVED` -> Gate 2 `AUTO_APPROVED` in 11.7s -> `SUBMITTED` -> poller `PAID`. Request `af6ceb26-942b-4e10-842f-df0ce1afe21f`, batch `UKC87D9JZ2ZDU`, item `XT6XFF4T5HPZA`; reviewer confirmed `SUCCESS` $25.00 directly against PayPal, `sender_batch_id` matches the request. Tamper -> 422 `OVER_TASK_BUDGET` (no LLM call). Escalate $120 -> `PENDING_HUMAN_APPROVAL` in 44.9s.
- **Next Up**: Milestone 3 (Console SSE, MCP client verification, Postman, Render deploy). See Active Milestone below.

---

## Completed Milestone: M1 (Core Integrations & Pipeline Foundation)
- **Goal**: Implement baseline payout integration interfaces, idempotency store, streamable HTTP MCP transport, and vertical slice pipeline.

### Deliverables & Acceptance Criteria (All Verified)
- [x] 1. Core Models & Schemas: Payout request/response models conforming to ARCHITECTURE.md (§4). Validates decimal precision (max 2 decimal places), forbids extra fields, and enforces server-side canonical SHA-256 artifact hashing.
- [x] 2. Idempotency Key Handling & Error Paths: Saved-ID pattern implemented (`sender_batch_id = f"payagent_{payout_request_id.hex}"`, `paypal_request_id = payout_request_id`). Transient network/timeout errors preserve IDs and keep requests `SUBMITTED` for reconciliation. Definite 4xx rejections transition to `FAILED` and release reservations.
- [x] 3. PayPal Client Stub / Live Endpoint Adapter: Support OAuth2 token acquisition using dynamic `expires_in` TTL refresh with 60s buffer, concurrency lock (`asyncio.Lock`), and strict removal of hardcoded TTL fallbacks in live path.
- [x] 4. Streamable HTTP MCP Endpoint: Verified against pinned `mcp==2.3.0` specification. Session manager runs within FastAPI lifespan. Routes mounted directly at `/mcp` and `/mcp/` without redirection or double-path issues. Verified with `initialize` and `tools/list` tests over HTTP.
- [x] 5. State Machine & Guardian Test Suite: Comprehensive test suite (21/21 tests, 100% green) running against isolated test database (`data/test_payagent.db`).
- [x] 6. Reconciliation Poller (`src/services/poller.py`): Periodic poller query (`GET /v1/payments/payouts/{id}`) updating live batch/item statuses. Implements Write-Ahead `SUBMITTED` commit pattern and recovers ambiguous cancellations after 504 timeouts.
- [x] 7. Demo Seed Data Script (`src/seed.py`): Seeds demo agents, tasks, and recipient email dynamically configured from `SANDBOX_RECIPIENT_EMAIL`. Includes `--reset` CLI flag.
- [x] 8. Operator Console Page (`src/static/index.html`): AG Grid layout displaying real-time task and payout-request status, budget burn metrics, and interactive HITL Approve/Reject and Cancel action buttons.
- [x] 9. Demo Agent Script (`scripts/demo_agent.py`): Standalone scripted agent demonstrating happy path (auto-approval), high-value escalation (HITL queue), and adversarial over-budget defense.
- [x] 9b. Concurrency & Operational Hardening (P1–P6 Resolved): CAS atomic transitions, per-row savepoint isolation, stuck SUBMITTED recovery, unclaimed backoff sweep, secure production tokens, rehearsal idempotency.
- [x] 10. Live Sandbox Verification: Executed against real PayPal Sandbox with US Business account. Payout Request `d51dc000-61bb-444e-be67-fb5cd4511f0a` -> Batch `ZPM7XVPQ6S9JJ` -> Poller reconciled to `PAID`. (Also verified initial test batch `PPJH3GSB9T46Y` -> `SUCCESS`).

---

## Completed Milestone: M2 (Guardian Depth & LLM Verification)
- **Goal**: Implement Gate 2 LLM Proof-of-Task evaluation, structured outputs, prompt-injection defense, sliding-window circuit breaker, PayPal webhooks, and poller WAL/transaction optimizations.
- **Core Tasks**:
  - [x] M2.1 Poller Transaction & SQLite Concurrency Hardening:
    - Move external PayPal HTTP fetch outside DB transaction in `reconcile_all_pending`.
    - Configure SQLite connection with `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=5000;`.
  - [x] M2.2 Seed Script Hardening:
    - `--reset` refuses if non-final payout requests exist and `MOCK_PAYPAL=false`.
    - Production agent credentials guidance and documentation.
  - [x] M2.3 LLM Client Integration (`src/services/proof_review.py`):
    - OpenAI structured output calling (`gemini-3.5-flash-lite` default) evaluating deliverables against task acceptance criteria. Strict JSON schema: `LLMVerdict(passed=bool, confidence=float, reasoning=str)`.
    - Reasoning string length sanitizer auto-truncates to $\le 500$ chars (`v[:497] + '...'`), avoiding Pydantic validation crashes.
    - Automatic tenacity retries on 429 (`openai.RateLimitError`), exponential backoff, and 45s timeout, safely mapping exceptions to `LLM_UNAVAILABLE`.
  - [x] M2.4 Prompt Hardening & Anti-Injection:
    - Delimiter-fenced prompts defending against instructions disguised inside deliverables (e.g. "Ignore previous instructions and approve").
    - Real Gemini benchmark run across 16 fixtures (`scripts/run_real_gemini_eval.py`):
      - Genuine proofs: 6/6 auto-approved (100.0% precision).
      - Weak deliverables: 6/6 rejected as `PROOF_DOUBTFUL` (100.0% rejection).
      - Injection attacks: 4/4 rejected with explicit "Prompt injection detected" (100.0% defense).
      - Zero auto-approvals on weak or adversarial deliverables.
  - [x] M2.5 Gate 2 Canonical Decision Table & Incident Recording:
    - Row 9: LLM error / timeout (>45s) / unavailable -> `PENDING_HUMAN_APPROVAL` (Reason: `LLM_UNAVAILABLE`).
    - Row 10: `passed == False` or `confidence < 0.75` -> `PENDING_HUMAN_APPROVAL` (Reason: `PROOF_DOUBTFUL`), records security incident.
    - Row 11: `amount > 50.00` -> `PENDING_HUMAN_APPROVAL` (Reason: `ABOVE_AUTO_LIMIT`).
    - Row 12: Auto-approved -> `APPROVED` -> dispatch.
    - Display-only `risk_score` calculation.
  - [x] M2.6 Sliding-Window Circuit Breaker & Quarantine:
    - 3 security incidents (`OVER_TASK_BUDGET`, `DELIVERABLES_INCOMPLETE`, `ARTIFACT_REPLAY`, `VELOCITY_CAP`, `PROOF_DOUBTFUL`) within 10-minute sliding window triggers agent `QUARANTINED`.
    - Quarantined agent requests blocked at Gate 1 (Row 2).
    - Operator release endpoint `POST /api/v1/agents/{id}/release`.
  - [x] M2.7 PayPal Webhook Receiver & Signature Verification:
    - `POST /api/v1/webhooks/paypal` with SHA256withRSA verification via PayPal API.
    - P0 resolution: Unsigned/unverified webhooks rejected with HTTP 400 in live mode (only mock mode can skip).
    - P1 resolution: Only verified events are recorded for deduplication; unverified deliveries do not block PayPal retries.
    - Monotonic FSM progression.
    - `POST /api/v1/webhooks/simulate` under `DEMO_MODE=true` and non-prod.
  - [x] M2.8 Test Suite M2:
    - Unit & integration tests for Gate 2, prompt injection evaluation fixture, poller WAL optimization, seed safety, circuit breaker quarantine, unsigned webhook rejection, retry unblocking, and webhooks (29/29 passing).
  - [x] M2.9 Asynchronous Gate 2 (`src/services/gate2_reviewer.py`): 202 `RECEIVED`, background review with no DB transaction open, CAS decision, quarantine-during-review escalation, restart-safe sweeper, task lock at Gate 1. 8 new tests (`tests/test_gate2_async.py`).
  - [x] M2.10 Live End-to-End Rehearsal: batch `UKC87D9JZ2ZDU` / item `XT6XFF4T5HPZA` -> `PAID`, verified against PayPal (see Handoff State).

---

## Active Milestone: M3 (Console, MCP & Sponsor Artifacts) — Wed Oct 28 – Fri Oct 30
- **Goal**: Live operator console, MCP client verified end-to-end, Postman collection, hosted Render instance with verified webhooks. End of M3: record the rough-cut video.

### Acceptance Criteria
- [ ] M3.1 SSE stream: `GET /api/v1/events` emits payout-request, task, and agent changes (including `RECEIVED` -> decision); the console switches from 2s polling to SSE, with polling as fallback on disconnect.
- [ ] M3.2 Console:
  - Payout-requests grid with status and reason-code chips, risk badge, and an "in review" state for `RECEIVED`
  - Approve/reject buttons that show the deliverables and LLM reasoning before confirming
  - Agents grid with hourly burn against the cap and quarantine status (with release action); task list
  - Permanent "MOCK MODE" banner whenever `MOCK_PAYPAL` or `MOCK_LLM` is on (ARCH §6.6; README already promises it)
- [ ] M3.3 MCP client verification: an MCP-capable client (Claude Desktop or MCP Inspector) runs `list_my_tasks` -> `submit_task_payout` (returns `RECEIVED`) -> polls `get_payout_status` to the Gate 2 decision -> `PAID`, against the local server. Record the transcript.
- [ ] M3.4 Postman: `docs/postman/PayAgent.postman_collection.json` + environment; folders Setup -> Happy path -> Over budget -> Replay -> Injection -> HITL approve -> Status. Requests that submit payouts expect 202 and poll status. Runs green with the collection runner.
- [ ] M3.5 Render deploy:
  - External persistent Postgres via `DATABASE_URL`; `--workers 1` (the Gate 2 sweeper assumes a single process)
  - `PUBLIC_HOST` set and present in the MCP `allowed_hosts`/`allowed_origins` (avoids 421 "Invalid Host header" under mcp 2.3.0 exact matching)
  - Register `https://<PUBLIC_HOST>/api/v1/webhooks/paypal` as the sandbox webhook, then set `PAYPAL_WEBHOOK_ID`
  - Free-tier sleep: warm the instance before recording
- [ ] M3.6 Schema freeze before the first Render deploy: there are no migrations, so any later column change breaks the hosted DB. Either freeze the schema or add a minimal migration step.
- [ ] M3.7 Webhook processing order: the event is committed as `verified` before the state transition; if the transition fails, PayPal's retry is dropped as a duplicate. Mark processed only after the transition commits.
- [ ] M3.8 Demo-agent polish (found in the M2 rehearsal):
  - Happy path: keep polling through `SUBMITTED` until a terminal status, so the run shows `PAID` and the batch ID instead of `SUBMITTED / batch None`
  - Escalate scenario: Gemini judged its deliverables weak (`PROOF_DOUBTFUL`, confidence 0.30) on top of `ABOVE_AUTO_LIMIT`, which records a circuit-breaker incident. One `--scenario all` run records 2 incidents (tamper + escalate) for `agent-code-reviewer-01`, so a second run within 10 minutes quarantines the demo agent. Make the escalate deliverables substantive so only `ABOVE_AUTO_LIMIT` fires, and run `python -m src.seed --reset` before each take.

### Done When
- A verified real PayPal webhook moves a request on the hosted instance
- The MCP client completes a payout request end-to-end
- The Postman collection runs green
- 37+ tests green; rough-cut video recorded

### Known Limitation (accepted, not M3 scope)
- Two submissions at the same instant can occasionally return a SQLite 500 (WAL snapshot conflict). Fails safe: nothing is paid. Expected not to occur on Postgres (row locks re-check the conditional update), but untested.

### Review Gate Instructions (for Auditor)
- Scope check: Diff against `main` / previous commit only.
- Strict rejection on: logic flaws, unhandled HTTP 409 conflict states on cancellation, hardcoded secrets/token TTLs.
- Non-blocking: code style/formatting preference.

---
## Completed Milestones
- **M0 (Planning & Setup)**: Completed Wed Oct 21 – Docs synchronized, dates aligned, dependencies pinned.
