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
* **Procedure (`scripts/seed_hosted.py`)**:
  * Execute the safe seeding wrapper script from the operator's machine:
    ```bash
    python scripts/seed_hosted.py
    ```
  * **Timing**: Run this script **ONCE** after the initial Render deployment has created the schema. Re-running is supported if tasks need resetting, but reopens demo tasks (`seed` resets task status to `OPEN`).
  * **Zero-Leak Guarantee (NEVER use shell-export)**:
    * **Prohibited**: Never run `env $(cat .env.hosted | xargs) python -m src.seed`. Shell-export patterns leak secrets into the OS process table (`ps aux`, `/proc`), shell history logs, and break on spaces or special characters.
    * **Enforced**: `scripts/seed_hosted.py` uses `dotenv_values()` to inject secrets from `.env.hosted` directly into Python's `os.environ` in-memory *before* importing any application modules.
    * **Validation Guards**:
      1. Refuses (exit 2) any command-line flags (e.g. `--reset`, `--recreate-schema`).
      2. Refuses (exit 2) unless `DATABASE_URL` starts with `postgresql+asyncpg://` and contains `.pooler.supabase.com`.
      3. Refuses (exit 2) unless `APP_ENV == "production"`.
      4. Refuses (exit 2) unless `.env.hosted` is strictly gitignored (`git check-ignore -q .env.hosted`).
    * **Host Masking**: Prints only the Supabase host name (e.g., `aws-0-us-east-1.pooler.supabase.com`) during execution—never credentials, ports, or query parameters.
  * **Safety Invariant**: Never add the hosted Supabase connection string to the default local `.env` file. Doing so would risk local automated test runs (`pytest`) or local development commands connecting to and wiping the hosted production database.

### (c) Hosted Demo Agent Keys Provisioning (Option 1: File Only)
* **Agent Key Lifecycle**:
  * Generated API keys are written strictly to `.env.hosted.agents` (`AGENT_CODE_REVIEWER_01_KEY=...`, `AGENT_DATA_LABELER_02_KEY=...`) only when called via `scripts/seed_hosted.py`.
  * **File-Only Safety**: Keys are **never** printed to stdout or logged to console in production mode.
  * **Gitignore Guard**: Refuses to write keys unless `git check-ignore -q <path>` confirms the target file is ignored.
  * **Key Rotation for Existing Agents**: On re-runs, existing demo agents have their `api_key_hash` updated in the database to match the newly generated keys (`[.] Agent key rotated: <id>`), ensuring that the credentials written to `.env.hosted.agents` are immediately functional while invalidating previously saved keys.
