# PayAgent AI — Production Deployment Guide (Render & Hosted PostgreSQL)

This document provides the deployment reference for launching PayAgent AI onto Render via `render.yaml` with an external persistent PostgreSQL database.

---

## 1. Architecture & Resource Guardrails

* **Single Worker Execution (`--workers 1`)**:
  * PayAgent AI utilizes an in-process SSE broadcaster (`asyncio.Queue`) for real-time frontend updates and an asynchronous Gate 2 background sweeper for atomic payout reviews.
  * Uvicorn must be run with `--workers 1` (configured in `render.yaml`).
* **Persistent Database (`DATABASE_URL`)**:
  * Render free-tier disks are ephemeral and reset across instances or redeploys.
  * A managed external PostgreSQL instance (e.g., Supabase, Neon, or Render Managed PostgreSQL) must be supplied via `DATABASE_URL` (`postgresql+asyncpg://...`).
* **Health Check Endpoint**:
  * `healthCheckPath: /` (returns HTTP 200 serving the operator console).
* **Live vs. Mock Posture**:
  * `APP_ENV=production`
  * `MOCK_PAYPAL=false` (real calls to PayPal sandbox)
  * `MOCK_LLM=false` (real calls to Gemini Gate 2 LLM reviewer)
  * `DEMO_MODE=false`

---

## 2. Render Environment Variables Reference

Variables marked `sync: false` in `render.yaml` must be populated directly in the Render Dashboard:

| Variable | Requirement / Description |
|---|---|
| `DATABASE_URL` | Async PostgreSQL connection string: `postgresql+asyncpg://<user>:<password>@<host>:<port>/<dbname>` |
| `PUBLIC_HOST` | Hostname of the Render deployment (e.g., `payagent-ai.onrender.com`). Automatically configures MCP transport security `allowed_hosts` and `allowed_origins`. |
| `OPERATOR_TOKEN` | Secure random string (minimum 32 characters). Production validation fails if left as default/insecure secret. |
| `PAYPAL_CLIENT_ID` | PayPal Developer sandbox REST App Client ID. |
| `PAYPAL_CLIENT_SECRET` | PayPal Developer sandbox REST App Client Secret. |
| `PAYPAL_WEBHOOK_ID` | Registered PayPal webhook ID configured for `https://<PUBLIC_HOST>/api/v1/webhooks/paypal`. |
| `SANDBOX_RECIPIENT_EMAIL` | Target contractor sandbox personal PayPal account email. |
| `GEMINI_API_KEY` | Google AI Studio Gemini API key (`gemini-3.5-flash-lite`). |

---

## 3. Database Initialization & Seeding

After provisioning the hosted PostgreSQL database and setting `DATABASE_URL`:

1. Run the database seed script to establish the frozen schema and initial demo tasks/agents:
   ```bash
   python -m src.seed
   ```
2. Verify that tables (`payout_requests`, `tasks`, `agents`, `budget_windows`, `agent_incidents`) and enum types are created cleanly without schema drift.

---

## 4. Webhook Registration (PayPal Sandbox)

1. Navigate to PayPal Developer Dashboard -> My Apps & Credentials -> Default Application -> Webhooks.
2. Add Webhook URL:
   ```
   https://<PUBLIC_HOST>/api/v1/webhooks/paypal
   ```
3. Subscribe to the following event types:
   * `PAYMENT.PAYOUTSBATCH.SUCCESS`
   * `PAYMENT.PAYOUTSBATCH.DENIED`
   * `PAYMENT.PAYOUTS-ITEM.SUCCESS`
   * `PAYMENT.PAYOUTS-ITEM.FAILED`
   * `PAYMENT.PAYOUTS-ITEM.UNCLAIMED`
   * `PAYMENT.PAYOUTS-ITEM.CANCELED`
   * `PAYMENT.PAYOUTS-ITEM.BLOCKED`
   * `PAYMENT.PAYOUTS-ITEM.RETURNED`
   * `PAYMENT.PAYOUTS-ITEM.REFUNDED`
   * `PAYMENT.PAYOUTS-ITEM.REVERSED`
4. Copy the resulting Webhook ID and configure `PAYPAL_WEBHOOK_ID` in the Render dashboard.
