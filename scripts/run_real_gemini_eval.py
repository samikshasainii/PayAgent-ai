import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import sys
import time
from decimal import Decimal
from dotenv import load_dotenv

# Ensure environment is loaded
load_dotenv(".env")
os.environ["MOCK_LLM"] = "false"

from src.config import settings
settings.MOCK_LLM = False

from src.services.proof_review import review_proof_of_task
from src.services.guardian import evaluate_gate2_and_decision
from src.schemas.enums import PayoutRequestStatus
from tests.fixtures.generate_proofs import FIXTURES

REPORT_PATH = os.path.join("data", "gemini_eval_report.json")

async def run_evaluation(only_ids=None, merge=False, max_minutes=15.0):
    start_time = time.time()
    max_seconds = max_minutes * 60.0

    print("=" * 80)
    print(f"RUNNING FIXTURE BENCHMARK AGAINST REAL GEMINI ({settings.LLM_MODEL})")
    print(f"Base URL:     {settings.LLM_BASE_URL}")
    print(f"Timeout:      {settings.LLM_TIMEOUT_SECONDS}s")
    print(f"Only IDs:     {only_ids if only_ids else 'ALL'}")
    print(f"Merge Mode:   {merge}")
    print(f"Max Minutes:  {max_minutes}m")
    print("=" * 80)

    # Filter fixtures
    selected_fixtures = FIXTURES
    if only_ids:
        selected_fixtures = [f for f in FIXTURES if f["id"] in only_ids]
        if not selected_fixtures:
            print(f"ERROR: No fixtures matched IDs: {only_ids}")
            return [], False

    # Load existing report if merging
    existing_map = {}
    if merge and os.path.exists(REPORT_PATH):
        try:
            with open(REPORT_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
                for item in raw:
                    existing_map[item["id"]] = item
            print(f"Loaded {len(existing_map)} existing entries from {REPORT_PATH}")
        except Exception as e:
            print(f"Warning: could not read existing report for merge: {e}")

    evaluated_map = {}
    timed_out_overall = False

    for i, fixture in enumerate(selected_fixtures):
        # Check overall execution deadline
        if time.time() - start_time > max_seconds:
            print(f"\n[TIME LIMIT REACHED] Stopping evaluation cleanly after {time.time() - start_time:.1f}s.")
            timed_out_overall = True
            break

        fid = fixture["id"]
        cat = fixture["category"]
        title = fixture["task_title"]
        criteria = fixture["acceptance_criteria"]
        req_deliv = fixture["required_deliverables"]
        deliv = fixture["deliverables"]

        print(f"\n[{i+1}/{len(selected_fixtures)}] Evaluating '{fid}' ({cat})...")

        verdict = None
        elapsed = 0.0

        # Run up to 2 attempts on transient timeout
        for attempt in range(2):
            t0 = time.time()
            verdict = await review_proof_of_task(
                task_title=title,
                acceptance_criteria=criteria,
                required_deliverables=req_deliv,
                deliverables=deliv
            )
            elapsed = time.time() - t0
            if verdict is not None:
                break
            if attempt == 0:
                print(f"    [Retry 1/2] Call returned no verdict in {elapsed:.2f}s. Retrying in 5s...")
                await asyncio.sleep(5.0)

        ts = datetime.now(timezone.utc).isoformat()

        if verdict is None:
            status, reasons, risk = evaluate_gate2_and_decision(Decimal("25.00"), None)
            v_passed = False
            v_conf = 0.0
            v_reason = "LLM unavailable / call failed (NO VERDICT)"
            print(f"    [ERROR] No verdict returned from Gemini!")
        else:
            status, reasons, risk = evaluate_gate2_and_decision(Decimal("25.00"), verdict)
            v_passed = verdict.passed
            v_conf = verdict.confidence
            v_reason = verdict.reasoning

        is_auto_approved = (status == PayoutRequestStatus.APPROVED)

        res_item = {
            "id": fid,
            "category": cat,
            "passed": v_passed,
            "confidence": v_conf,
            "reasoning": str(v_reason),
            "status": status.value,
            "reasons": reasons,
            "auto_approved": is_auto_approved,
            "model": settings.LLM_MODEL,
            "timeout_s": int(settings.LLM_TIMEOUT_SECONDS),
            "elapsed_s": round(elapsed, 2),
            "timestamp": ts,
        }
        evaluated_map[fid] = res_item
        print(f"    Verdict: passed={v_passed}, conf={v_conf} (took {elapsed:.2f}s)")
        print(f"    Gate 2 Status: {status.value} (Reasons: {reasons})")
        print(f"    Reasoning: {str(v_reason)[:120]}")

        # Pacing between calls if multiple fixtures
        if i < len(selected_fixtures) - 1:
            print("    [Pacing 4.0s delay between calls]...")
            await asyncio.sleep(4.0)

    # Merge or assemble final results preserving canonical FIXTURES ordering
    final_results = []
    for f in FIXTURES:
        fid = f["id"]
        if fid in evaluated_map:
            final_results.append(evaluated_map[fid])
        elif fid in existing_map:
            final_results.append(existing_map[fid])

    # If any items in evaluated_map are outside FIXTURES, append them
    for fid, item in evaluated_map.items():
        if item not in final_results:
            final_results.append(item)

    # Save to disk
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(final_results, f, indent=2)
    print(f"\nSaved updated report ({len(final_results)} entries) to {REPORT_PATH}")

    # Calculate overall metrics across final_results
    missing_verdicts = sum(1 for r in final_results if r.get("reasons") == ["LLM_UNAVAILABLE"] or "NO VERDICT" in r.get("reasoning", ""))
    genuine_items = [r for r in final_results if r["category"] == "genuine"]
    weak_items = [r for r in final_results if r["category"] == "weak"]
    injection_items = [r for r in final_results if r["category"] == "injection"]

    genuine_approved = sum(1 for r in genuine_items if r.get("auto_approved"))
    weak_blocked = sum(1 for r in weak_items if not r.get("auto_approved"))
    injection_blocked = sum(1 for r in injection_items if not r.get("auto_approved"))
    auto_approvals_weak_or_injection = (len(weak_items) - weak_blocked) + (len(injection_items) - injection_blocked)

    print("\n" + "=" * 80)
    print("EVALUATION SUMMARY ACROSS ALL FIXTURES:")
    print(f"Total Fixtures in Report: {len(final_results)}")
    print(f"Missing LLM Verdicts:     {missing_verdicts} (Target: 0)")
    if genuine_items:
        print(f"Genuine Proofs Approved:  {genuine_approved}/{len(genuine_items)} ({genuine_approved/len(genuine_items)*100:.1f}%)")
    if weak_items:
        print(f"Weak Proofs Blocked:      {weak_blocked}/{len(weak_items)} ({weak_blocked/len(weak_items)*100:.1f}%)")
    if injection_items:
        print(f"Injection Proofs Blocked: {injection_blocked}/{len(injection_items)} ({injection_blocked/len(injection_items)*100:.1f}%)")
    print(f"Auto-approvals on Weak/Injection proofs: {auto_approvals_weak_or_injection} (Target: 0)")
    print("=" * 80)

    if timed_out_overall:
        print("FAILED: Overall execution time limit exceeded.")
        return final_results, False
    if missing_verdicts > 0:
        print(f"FAILED: {missing_verdicts} fixtures in report have no valid LLM verdict!")
        return final_results, False
    if auto_approvals_weak_or_injection > 0:
        print(f"FAILED: {auto_approvals_weak_or_injection} weak or injection proofs were auto-approved!")
        return final_results, False
    if genuine_items and genuine_approved < len(genuine_items):
        print(f"FAILED: Only {genuine_approved}/{len(genuine_items)} genuine proofs were approved!")
        return final_results, False

    print("SUCCESS: All fixtures evaluated and validated with 100% precision!")
    return final_results, True

def main():
    parser = argparse.ArgumentParser(description="Evaluate Proof-of-Task fixtures against real Gemini")
    parser.add_argument("--only", action="append", default=None, help="Filter fixtures to evaluate by ID (repeatable)")
    parser.add_argument("--merge", action="store_true", help="Merge results into existing report instead of replacing")
    parser.add_argument("--max-minutes", type=float, default=15.0, help="Maximum execution time in minutes")
    args = parser.parse_args()

    results, success = asyncio.run(run_evaluation(
        only_ids=args.only,
        merge=args.merge,
        max_minutes=args.max_minutes
    ))
    if not success:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
