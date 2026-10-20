# Milestone Tracking & Acceptance Contract

## Current Active Milestone: M1 (Core Integrations & Pipeline Foundation)
- **Window**: Fri Oct 23 – Sun Oct 25
- **Goal**: Implement baseline payout integration interfaces, idempotency store, and streamable HTTP MCP transport.

### Deliverables & Acceptance Criteria
- [x] 1. Core Models & Schemas: Payout request/response models conforming to ARCHITECTURE.md.
- [x] 2. Idempotency Key Handling: Saved-ID pattern implemented (stored ID approach to prevent duplicate triggers).
- [x] 3. PayPal Client Stub / Live Endpoint Adapter: Support OAuth2 token acquisition using dynamic `expires_in` TTL refresh.
- [x] 4. Streamable HTTP MCP Endpoint: Verified against minimum required `mcp` version specification.
- [x] 5. Test Suite: Unit tests verifying state machine transitions (including CANCELED and RETURNED budget releases) pass with 100% green status.

### Review Gate Instructions (for Auditor)
- Scope check: Diff against `main` / previous commit only.
- Strict rejection on: logic flaws, unhandled HTTP 409 conflict states on cancellation, hardcoded secrets/token TTLs.
- Non-blocking: code style/formatting preference.

---
## Completed Milestones
- **M0 (Planning & Setup)**: Completed Wed Oct 21 – Docs synchronized, dates aligned, dependencies pinned.
