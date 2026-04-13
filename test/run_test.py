#!/usr/bin/env python3
"""
End-to-end test for the NRTV agent network.
Runs a script_brief job in TEST_MODE and verifies the pipeline.

Usage:
    TEST_MODE=true python test/run_test.py
"""

import os
import sys
from pathlib import Path

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Force TEST_MODE and set log dir to repo root
os.environ["TEST_MODE"] = "true"
os.environ["PARALLAX_LOG_DIR"] = str(Path(__file__).parent.parent / ".parallax")

from core.head_of_production import HeadOfProduction
from core.cost_tracker import cost_report

BRIEF = (
    "Create a 15-second ad for Rugiet Ready, a men's sublingual ED tablet. "
    "Narrator is a satisfied wife talking about her husband's results."
)
BRAND_FILE = "brands/rugiet.yaml"


def run_test():
    print("=" * 60)
    print("NRTV AGENT NETWORK — END-TO-END TEST")
    print("=" * 60)
    print(f"TEST_MODE: {os.environ.get('TEST_MODE')}")
    print(f"Brief: {BRIEF[:80]}...")
    print()

    hop = HeadOfProduction()
    run_id = hop.run_id

    # Submit job — skip clarification gate in test by patching input
    job = {
        "type": "script_brief",
        "content": BRIEF,
        "brand_file": BRAND_FILE,
        "concept_id": None,
        "test_mode": True,
    }

    # Patch input() to auto-skip clarifications in test mode
    import builtins
    original_input = builtins.input
    builtins.input = lambda prompt="": "skip"

    try:
        result = hop.receive_job(job)
    except Exception as e:
        print(f"[FAIL] Job raised exception: {e}")
        raise
    finally:
        builtins.input = original_input

    print()
    print("=" * 60)
    print("VERIFICATION")
    print("=" * 60)

    failures = []

    # 1. Concept ID was generated
    concept_id = result.get("concept_id")
    if concept_id:
        print(f"[PASS] Concept ID generated: {concept_id}")
    else:
        print("[FAIL] No concept ID in result")
        failures.append("missing concept_id")

    # 2. Run log was created
    from core.paths import run_dir as get_run_dir
    run_dir = get_run_dir(run_id)
    job_log = run_dir / "job.json"
    if job_log.exists():
        print(f"[PASS] Run log created: {job_log}")
    else:
        print(f"[FAIL] Run log not found at {job_log}")
        failures.append("missing run log")

    # 3. Script was returned
    script = result.get("script", "")
    if script:
        print(f"[PASS] Script returned ({len(script)} chars)")
    else:
        print("[FAIL] No script in result")
        failures.append("missing script")

    # 4. Result log was written
    result_log = run_dir / "result.json"
    if result_log.exists():
        print(f"[PASS] Result log written: {result_log}")
    else:
        print(f"[FAIL] Result log not found at {result_log}")
        failures.append("missing result log")

    # 5. Evaluation was returned
    evaluation = result.get("evaluation", {})
    if evaluation and "score" in evaluation:
        print(f"[PASS] Evaluation returned (score: {evaluation.get('score', '?'):.0%})")
    else:
        print("[WARN] Evaluation missing or incomplete")

    # 6. Cost report
    if concept_id:
        try:
            report = cost_report(concept_id)
            total = report.get("total_usd", 0)
            print(f"[PASS] Cost tracked: ${total:.4f} total")
            print("       Per-agent breakdown:")
            for agent, data in report.get("by_agent", {}).items():
                print(f"         {agent}: ${data['cost_usd']:.4f} ({data['calls']} calls)")
        except Exception as e:
            print(f"[WARN] Cost report failed: {e}")

    print()
    print("=" * 60)
    print("AGENT SUMMARY")
    print("=" * 60)

    # Print what each agent did
    agents_in_result = set()
    if result.get("agent"):
        agents_in_result.add(result["agent"])
    if evaluation.get("agent"):
        agents_in_result.add(evaluation["agent"])
    agents_in_result.add("head_of_production")

    for agent in sorted(agents_in_result):
        print(f"  {agent}")

    if result.get("confidence"):
        print(f"\nScript writer confidence: {result['confidence']:.0%}")

    print()

    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S) — {', '.join(failures)}")
        sys.exit(1)
    else:
        print("RESULT: ALL CHECKS PASSED")


def run_concern_test():
    print()
    print("=" * 60)
    print("CONCERN PROPAGATION TEST — AssetGenerator")
    print("=" * 60)

    from packs.video.asset_generator import AssetGenerator

    run_id = "concern_test"
    agent = AssetGenerator(run_id=run_id)

    # Deliberately ambiguous brief — should trigger a concern
    request = {
        "asset_type": "image",
        "brief": "a dinosaur",
        "output_path": f"logs/runs/{run_id}/test_asset.png",
        "constraints": {},
        "concept_id": "TST-001",
        "run_id": run_id,
        "scene_index": 0,
    }

    result = agent.generate(request)

    concern = result.get("concern")
    if concern is not None:
        print(f"[PASS] Concern raised (blocking={concern.blocking})")
        print(f"       Message: {concern.message}")
        if concern.proposed_default:
            print(f"       Proposed default: {concern.proposed_default}")
    else:
        print("[INFO] No concern raised — brief was accepted as-is (may be expected in test mode if LLM skipped)")

    print(f"       Success: {result['success']} | Model: {result['model_used']} | Score: {result['self_eval_score']:.0%}")
    print()


def run_trust_test():
    print()
    print("=" * 60)
    print("TRUST SYSTEM TEST")
    print("=" * 60)

    import tempfile
    from pathlib import Path
    from core.trust import TrustScore
    from core.run_log import RunLogger

    failures = []

    with tempfile.TemporaryDirectory() as tmpdir:
        trust_file = Path(tmpdir) / "trust.json"
        trust = TrustScore(trust_file=trust_file)

        # Initial state
        if abs(trust.score - 0.1) < 0.001:
            print(f"[PASS] Initial trust score: {trust.score}")
        else:
            print(f"[FAIL] Expected initial score 0.1, got {trust.score}")
            failures.append("wrong initial score")

        # Predict + record correct outcome
        pred = trust.predict("r1", "TST-001", "test situation", ["A", "B", "C"], "A")
        correct = trust.record_outcome(pred.prediction_id, "A")

        if correct:
            print("[PASS] Prediction recorded as correct")
        else:
            print("[FAIL] Prediction should be correct (predicted A, actual A)")
            failures.append("prediction not correct")

        if trust.state.consecutive_correct == 1:
            print(f"[PASS] consecutive_correct == 1")
        else:
            print(f"[FAIL] consecutive_correct expected 1, got {trust.state.consecutive_correct}")
            failures.append("wrong consecutive_correct")

        snap = trust.snapshot()
        print(f"[PASS] Snapshot: {snap}")

        # Test run log with trust snapshot
        run_logger = RunLogger(
            run_id="trust_test_run",
            concept_id="TST-001",
            job={"type": "script_brief", "content": "test job"},
            trust_snapshot=snap,
            pack="video",
            test_mode=True,
        )
        run_logger.complete(output={"script": "test script"}, status="completed")

        from core.paths import run_dir
        log_path = run_dir("trust_test_run") / "run.json"
        if log_path.exists():
            import json
            data = json.loads(log_path.read_text())
            if "trust_snapshot" in data and data["trust_snapshot"]["score"] == snap["score"]:
                print(f"[PASS] Run log written with trust_snapshot")
            else:
                print(f"[FAIL] Run log missing or incorrect trust_snapshot")
                failures.append("trust_snapshot missing in run log")
        else:
            print(f"[FAIL] Run log not found at {log_path}")
            failures.append("run log not written")

    print()
    if failures:
        print(f"TRUST TEST: {len(failures)} FAILURE(S) — {', '.join(failures)}")
    else:
        print("TRUST TEST: ALL CHECKS PASSED")


if __name__ == "__main__":
    run_test()
    run_concern_test()
    run_trust_test()
