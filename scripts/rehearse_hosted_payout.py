#!/usr/bin/env python3
"""
PayAgent AI — Rehearse Hosted Payout Script

Submits a genuine $25 payout request for task-pr-audit-101 against the live hosted
instance on Render with the reconciliation poller disabled, verifying that PayPal's
verified webhook alone transitions the request to PAID.
"""
import os
import sys
import time
import subprocess
from decimal import Decimal
import httpx
from dotenv import dotenv_values

HOSTED_URL = "https://payagent-ai.onrender.com"

def main():
    if not __package__:
        print("run as: python -m scripts.rehearse_hosted_payout", file=sys.stderr)
        sys.exit(2)

    key_file = ".env.hosted.agents"
    git_check = subprocess.run(["git", "check-ignore", "-q", key_file], check=False)
    if git_check.returncode != 0:
        print(f"Error: Refusing to proceed. {key_file} is not gitignored.", file=sys.stderr)
        sys.exit(2)

    if not os.path.isfile(key_file):
        print(f"Error: {key_file} not found.", file=sys.stderr)
        sys.exit(2)

    env_values = dotenv_values(key_file)
    agent_key = env_values.get("AGENT_CODE_REVIEWER_01_KEY")
    if not agent_key:
        print("Error: AGENT_CODE_REVIEWER_01_KEY not found in .env.hosted.agents", file=sys.stderr)
        sys.exit(2)

    headers = {
        "Authorization": f"Bearer {agent_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "task_id": "task-pr-audit-101",
        "amount": "25.00",
        "deliverables": {
            "review_summary": (
                "Completed OWASP Top 10 security audit on PR #42. "
                "Verified parameterized SQL queries, strict Bearer authentication headers, "
                "proper output encoding against XSS, and absence of hardcoded secret tokens."
            ),
            "security_checklist": (
                "1. SQL Injection: Verified all DB queries parameterized.\n"
                "2. XSS: Checked output escaping across templates.\n"
                "3. Broken Authentication: Bearer token auth strictly validated.\n"
                "4. Sensitive Data Exposure: Masked logging enforced.\n"
                "5. Security Misconfiguration: Default demo credentials blocked."
            ),
        },
    }

    print(f"Submitting $25 payout request for task-pr-audit-101 to {HOSTED_URL}...")
    with httpx.Client(timeout=30.0) as client:
        try:
            resp = client.post(f"{HOSTED_URL}/api/v1/payout-requests", json=payload, headers=headers)
        except Exception as exc:
            print(f"Network error submitting payout request: {type(exc).__name__}", file=sys.stderr)
            sys.exit(1)

        if resp.status_code != 202:
            print(f"Submission failed with HTTP {resp.status_code}", file=sys.stderr)
            # Never print full response body or headers to prevent credential reflection
            try:
                err_json = resp.json()
                detail = err_json.get("detail", "Unknown error")
                print(f"Error detail: {detail}", file=sys.stderr)
            except Exception:
                pass
            sys.exit(1)

        data = resp.json()
        req_id = data.get("payout_request_id")
        print(f"Payout request submitted successfully. Request ID: {req_id}")
        print("Polling GET /api/v1/payout-requests/{id} every 10s (max 30m)...")
        print("Timeline:")

        start_time = time.time()
        max_duration = 30 * 60  # 30 minutes
        poll_interval = 10.0

        approval_prompted = False
        last_status = None

        while True:
            elapsed = int(time.time() - start_time)
            if elapsed > max_duration:
                print(f"[{elapsed}s] Timeout reached (30 minutes). Polling stopped.")
                break

            try:
                get_resp = client.get(f"{HOSTED_URL}/api/v1/payout-requests/{req_id}", headers=headers)
            except Exception as exc:
                print(f"[{elapsed}s] Polling network error: {type(exc).__name__}")
                time.sleep(poll_interval)
                continue

            if get_resp.status_code != 200:
                print(f"[{elapsed}s] Polling error: HTTP {get_resp.status_code}")
                time.sleep(poll_interval)
                continue

            rdata = get_resp.json()
            status = rdata.get("status")
            batch_id = rdata.get("payout_batch_id") or "None"
            reasons = rdata.get("decision_reasons") or []

            # Print timeline entry
            print(f"  [+{elapsed:>4}s] Status: {status:<24} | Batch ID: {batch_id:<32} | Reasons: {reasons}")

            if status == "PENDING_HUMAN_APPROVAL" and not approval_prompted:
                print("\n  [!] Operator approval needed in the console! Please approve in the dashboard.\n")
                approval_prompted = True

            if status == "PAID":
                print(f"\n[+] SUCCESS: Payout request transitioned to PAID at +{elapsed}s!")
                print(f"    Payout Request ID: {req_id}")
                print(f"    Payout Batch ID:   {batch_id}")
                break

            if status in {"FAILED", "POLICY_REJECTED", "HUMAN_REJECTED", "CANCELED"}:
                print(f"\n[!] TERMINAL STATE: Payout request reached {status} at +{elapsed}s.")
                break

            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
