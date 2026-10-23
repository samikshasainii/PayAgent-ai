# PayAgent AI — Production Deployment Guide (Render & Supabase)

This document provides the deployment reference for launching PayAgent AI onto Render via `render.yaml` with a persistent Supabase PostgreSQL database.

---

## 1. Architecture & Resource Guardrails

* **Single Worker Execution (`--workers 1`)**:
  * PayAgent AI utilizes an in-process SSE broadcaster (`asyncio.Queue`) for real-time frontend updates and an asynchronous Gate 2 background sweeper for atomic payout reviews.
  * Uvicorn must be run with `--workers 1` (configured in `render.yaml`).
* **Region Pairing (Supabase us-east-1 <-> Render virginia)**:
  * Co-locate the Render web service (`region: virginia`) in the same region as the Supabase database (`us-east-1` / `aws-0-us-east-1`).
  * Cross-country latency (e.g. Oregon to Virginia) adds ~70 ms per query roundtrip. Because the reconciliation poller queries the database every 5 seconds, regional co-location ensures minimal latency and eliminates network jitter.
* **Database Provider = Supabase Only**:
  * Free-tier disks on Render are ephemeral and reset across redeploys.
  * **Why Supabase over Neon or Render Postgres?**
    * Neon provides 100 Compute Unit (CU) hours/month on its free tier. The background 5-second poller keeps Neon awake continuously, exhausting the quota halfway through the month.
    * Render's free PostgreSQL databases expire and delete all data after 30 days, which breaks evaluation during the judging window (Dec 1–15).
    * Supabase free tier does not expire as long as it receives traffic.
  * **Connection String Format (Session Pooler)**:
    * Use the **Session Pooler** (IPv4, port `5432`):
      ```
      postgresql+asyncpg://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:5432/postgres?ssl=require
      ```
    * **Crucial syntax rule**: Use `?ssl=require`. Do NOT use `sslmode=require` (the `asyncpg` driver rejects `sslmode=` with an unexpected keyword error).
    * **Never use the transaction pooler (port 6543)**: SQLAlchemy session lifecycles, statement preparation, and transaction rollback guards require session pooling.
* **Health Check & Keep-Warm Endpoint**:
  * `healthCheckPath: /healthz` (runs `SELECT 1`, returning `{"status": "ok"}` or HTTP 503 `{"status": "db_unavailable"}`).
  * Free instances on Render sleep after 15 minutes of inactivity, and waking causes a cold start of ~1 minute.
  * Set up an external ping monitor (e.g., [UptimeRobot](https://uptimerobot.com) or [cron-job.org](https://cron-job.org)) hitting `https://<PUBLIC_HOST>/healthz` every **5 minutes** to keep the service warm and responsive for judges.
  * Render provides 750 free instance hours/month per workspace. Run only this single free web service to stay comfortably within the free allocation.
* **Live vs. Mock Posture**:
  * `APP_ENV=production`
  * `MOCK_PAYPAL=false` (live calls to PayPal sandbox)
  * `MOCK_LLM=false` (live calls to Gemini Gate 2 LLM reviewer)
  * `DEMO_MODE=false`

---

## 2. Render Environment Variables Reference

Variables marked `sync: false` in `render.yaml` must be populated directly in the Render Dashboard:

| Variable | Requirement / Description |
|---|---|
| `DATABASE_URL` | Supabase Session Pooler connection string: `postgresql+asyncpg://postgres.[ref]:[pw]@aws-0-[region].pooler.supabase.com:5432/postgres?ssl=require` |
| `PUBLIC_HOST` | Hostname of the Render deployment (e.g., `payagent-ai.onrender.com`). Automatically configures MCP transport security `allowed_hosts` and `allowed_origins`. |
| `OPERATOR_TOKEN` | Unique, secure random token (minimum 32 characters, never reused). Production startup validation strictly rejects default or weak tokens. Rotate after judging (Dec 15). |
| `PAYPAL_CLIENT_ID` | PayPal Developer sandbox REST App Client ID. |
| `PAYPAL_CLIENT_SECRET` | PayPal Developer sandbox REST App Client Secret. |
| `PAYPAL_WEBHOOK_ID` | Registered PayPal webhook ID configured for `https://<PUBLIC_HOST>/api/v1/webhooks/paypal`. |
| `SANDBOX_RECIPIENT_EMAIL` | Dedicated Sandbox Personal account email (e.g. `sb-contractor-prod@personal.example.com`). Note: sharing the operator token with judges allows inspecting task recipient details, so always use a dedicated sandbox personal account rather than personal email. |
| `GEMINI_API_KEY` | Google AI Studio Gemini API key (`gemini-3.5-flash-lite`). |

---

## 3. Database Initialization & Seeding

After provisioning the Supabase database and setting `DATABASE_URL`:

1. Run the database seed script to establish the frozen schema and initial demo tasks/agents:
   ```bash
   python -m src.seed
   ```
2. Verify that tables (`payout_requests`, `tasks`, `agents`, `budget_windows`, `agent_incidents`) and enum types are created cleanly without schema drift.
3. In production mode (`APP_ENV=production`), `src/seed.py` outputs a masked recipient (`sb***@personal.example.com`) and suppresses credentials.

---

## 4. Webhook Registration (PayPal Sandbox)

1. Navigate to PayPal Developer Dashboard -> My Apps & Credentials -> Default Application -> Webhooks.
2. Add Webhook URL:
   ```
   https://<PUBLIC_HOST>/api/v1/webhooks/paypal
   ```
3. Subscribe to the following event types (defined canonically in `src/services/status_mapping.py`):
   <!-- Canonical mapping reference: src/services/status_mapping.py:PAYPAL_EVENT_TO_REQUEST_STATUS -->
   * `PAYMENT.PAYOUTS-ITEM.SUCCEEDED`
   * `PAYMENT.PAYOUTS-ITEM.FAILED`
   * `PAYMENT.PAYOUTS-ITEM.DENIED`
   * `PAYMENT.PAYOUTS-ITEM.BLOCKED`
   * `PAYMENT.PAYOUTS-ITEM.HELD`
   * `PAYMENT.PAYOUTS-ITEM.UNCLAIMED`
   * `PAYMENT.PAYOUTS-ITEM.CANCELED`
   * `PAYMENT.PAYOUTS-ITEM.RETURNED`
   * `PAYMENT.PAYOUTS-ITEM.REFUNDED`
   * `PAYMENT.PAYOUTS-ITEM.REVERSED`
   * `PAYMENT.PAYOUTSBATCH.DENIED`
   * `PAYMENT.PAYOUTSBATCH.PROCESSING` *(optional)*
   * `PAYMENT.PAYOUTSBATCH.SUCCESS` *(optional)*

   *(Note: PayPal webhook event for item success is `PAYMENT.PAYOUTS-ITEM.SUCCEEDED`, not `.SUCCESS`.)*
4. Copy the resulting Webhook ID and configure `PAYPAL_WEBHOOK_ID` in the Render dashboard.

---

## 5. Hosted Seeding & Architecture Design Notes

### (a) Database Initialization on Empty Supabase DB
* **Does `init_db` create tables on an empty DB, or must `seed` run first?**
  * App startup (`src/main.py:81` lifespan -> `await init_db()`) executes `conn.run_sync(Base.metadata.create_all)` in `src/database.py:75`.
  * `Base.metadata.create_all` automatically creates all missing tables (`payout_requests`, `tasks`, `agents`, `budget_windows`, `agent_incidents`), foreign keys, and PostgreSQL native enum types (`payoutrequeststatus`, `taskstatus`, `agentstatus`) from scratch on a blank database.
  * The column verification guard `_verify_columns` (`src/database.py:77-105`) runs immediately after `create_all`; on a freshly created schema, `missing` is empty (`[]`).
  * **Conclusion**: The app starts cleanly on an empty Supabase database and initializes its entire schema automatically. `seed` does **not** need to run before app startup.

### (b) Seeding the Hosted DB Without a Render Shell
* **Procedure**:
  * Run `python -m src.seed` (strictly **without** `--reset` and **never** `--recreate-schema`) from the local operator machine.
  * Inject `DATABASE_URL` (Supabase Session Pooler with `?ssl=require`), `OPERATOR_TOKEN`, and `APP_ENV=production` strictly for that one local process invocation using a gitignored `.env.hosted` file:
    ```bash
    # Run locally with .env.hosted variables injected only into this process:
    env $(cat .env.hosted | xargs) python -m src.seed
    ```
  * **Safety Invariant**: Never add the hosted Supabase connection string to the default local `.env` file. Doing so would risk local automated test runs (`pytest`) or local development commands (`python -m src.seed --reset`) connecting to and wiping the hosted production database.

### (c) Hosted Demo Agent Keys Provisioning Proposal
* **Problem**:
  * In production (`APP_ENV=production`), `src/seed.py:96-106` generates random 32-byte tokens and line 240 intentionally suppresses console output of keys.
  * Demo tasks (`task-pr-audit-101`, `task-high-value-102`, `task-dataset-curation-103`) are assigned to `agent-code-reviewer-01` and `agent-data-labeler-02`.
  * `POST /api/v1/agents` returns HTTP 409 Conflict for existing agent IDs, preventing demo runs with these pre-assigned tasks if the seeded keys are unknown.
* **Proposals for Reviewer Alignment**:
  * **Option 1 (Local operator file output - Recommended)**: When `python -m src.seed` is executed directly via CLI from the operator's machine (not in cloud logs), output the generated agent keys to the local terminal once and/or write them to a gitignored local file `.env.hosted.agents`. Cloud logs on Render remain completely sterile because `seed.py` is not executed in the cloud service.
  * **Option 2 (Deterministic env-var keys via `.env.hosted`)**: Update `src/seed.py` so that if `APP_ENV=production` and `SEED_AGENT_KEYS_JSON` (or `AGENT_CODE_REVIEWER_KEY` / `AGENT_DATA_LABELER_KEY`) is present in the local execution environment, seed uses those operator-supplied keys rather than random unprinted tokens.
  * **Option 3 (Operator key rotation endpoint)**: Implement `POST /api/v1/agents/{agent_id}/rotate-key` protected by `OPERATOR_TOKEN` which rotates an existing agent's hashed key and returns the single-use plaintext key to the operator.
