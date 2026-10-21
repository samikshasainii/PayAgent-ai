import asyncio
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
settings.LLM_TIMEOUT_SECONDS = 30

from src.services.proof_review import review_proof_of_task
from src.services.guardian import evaluate_gate2_and_decision
from src.schemas.enums import PayoutRequestStatus
from tests.fixtures.generate_proofs import FIXTURES

async def run_evaluation():
    print("=" * 80)
    print(f"RUNNING 16-FIXTURE BENCHMARK AGAINST REAL GEMINI ({settings.LLM_MODEL})")
    print(f"Base URL: {settings.LLM_BASE_URL}")
    print("=" * 80)

    results = []
    auto_approvals_weak_or_injection = 0

    for i, fixture in enumerate(FIXTURES):
        fid = fixture["id"]
        cat = fixture["category"]
        title = fixture["task_title"]
        criteria = fixture["acceptance_criteria"]
        req_deliv = fixture["required_deliverables"]
        deliv = fixture["deliverables"]

        print(f"\n[{i+1}/{len(FIXTURES)}] Evaluating '{fid}' ({cat})...")

        t0 = time.time()
        verdict = await review_proof_of_task(
            task_title=title,
            acceptance_criteria=criteria,
            required_deliverables=req_deliv,
            deliverables=deliv
        )
        elapsed = time.time() - t0

        if verdict is None:
            # Gate 2 Row 9 fail-closed fallback
            status, reasons, risk = evaluate_gate2_and_decision(Decimal("25.00"), None)
            v_passed = False
            v_conf = 0.0
            v_reason = "LLM unavailable (Rate limit 429 / Quota / Timeout). Fail-closed escalation."
        else:
            status, reasons, risk = evaluate_gate2_and_decision(Decimal("25.00"), verdict)
            v_passed = verdict.passed
            v_conf = verdict.confidence
            v_reason = verdict.reasoning

        is_auto_approved = (status == PayoutRequestStatus.APPROVED)

        if cat in ("weak", "injection") and is_auto_approved:
            auto_approvals_weak_or_injection += 1

        res_item = {
            "id": fid,
            "category": cat,
            "passed": v_passed,
            "confidence": v_conf,
            "reasoning": str(v_reason),
            "status": status.value,
            "reasons": reasons,
            "auto_approved": is_auto_approved,
            "elapsed_s": round(elapsed, 2)
        }
        results.append(res_item)
        print(f"    Verdict: passed={v_passed}, conf={v_conf} (took {elapsed:.2f}s)")
        print(f"    Gate 2 Status: {status.value} (Reasons: {reasons})")
        print(f"    Reasoning: {str(v_reason)[:120]}")

        # Short delay/sleep between successive calls (2–4 seconds) as advised
        if i < len(FIXTURES) - 1:
            print("    [Pacing 3.0s delay between calls]...")
            await asyncio.sleep(3.0)

    print("\n" + "=" * 80)
    print("EVALUATION SUMMARY:")
    print(f"Total Fixtures Evaluated: {len(results)}")
    print(f"Auto-approvals on Weak/Injection proofs: {auto_approvals_weak_or_injection} (Target: 0)")
    print("=" * 80)

    # Save summary report to JSON
    os.makedirs("data", exist_ok=True)
    with open("data/gemini_eval_report.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("Saved report to data/gemini_eval_report.json")

    return results, auto_approvals_weak_or_injection

if __name__ == "__main__":
    results, auto_approvals = asyncio.run(run_evaluation())
    if auto_approvals > 0:
        sys.exit(1)
    sys.exit(0)
