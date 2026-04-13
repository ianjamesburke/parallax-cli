#!/usr/bin/env python3
"""
5 realistic drill scenarios for the Parallax agent network.
Each scenario tests a different job type / edge case.

Usage:
    TEST_MODE=true python3 test/drill_scenarios.py

Runs all 5, collects results, prints a structured report.
"""

import os
import sys
import json
import builtins
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["TEST_MODE"] = "true"
os.environ["PARALLAX_LOG_DIR"] = str(Path(__file__).parent.parent / ".parallax")

from core.head_of_production import HeadOfProduction
from core.pre_watch_brief import PreWatchBrief
from core.review import ReviewSession
from core.trust import TrustScore
from core.cost_tracker import cost_report

# Patch input() globally — non-interactive drill
original_input = builtins.input
builtins.input = lambda prompt="": "skip"


SCENARIOS = [
    {
        "name": "Rugiet 15s Script Brief",
        "description": "Standard ad brief with brand file — tests ScriptWriter + Evaluator + PreWatchBrief",
        "job": {
            "type": "script_brief",
            "content": (
                "Create a 15-second ad for Rugiet Ready, a men's sublingual ED tablet. "
                "Narrator is a satisfied wife talking about her husband's results. "
                "Focus on speed of action (~15 min) as the key hook."
            ),
            "brand_file": "brands/rugiet.yaml",
            "concept_id": None,
            "test_mode": True,
        },
        "expected": {
            "has_script": True,
            "has_evaluation": True,
            "has_pre_watch_brief": True,
            "min_confidence": 0.5,
        },
    },
    {
        "name": "Still Variations — Product Shots",
        "description": "Requests image variations — tests JuniorEditor drill gate",
        "job": {
            "type": "still_variations",
            "content": (
                "Generate 3 variations of the Rugiet product shot. "
                "Dark green tablet on black surface, warm amber lighting. "
                "Each variation should have a different angle."
            ),
            "brand_file": "brands/rugiet.yaml",
            "concept_id": None,
            "test_mode": True,
        },
        "expected": {
            "has_output": True,
            "has_evaluation": True,
            "agent_used": "junior_editor",
        },
    },
    {
        "name": "Revision — Scene Regeneration",
        "description": "Feedback-driven revision — tests JuniorEditor with edit instructions",
        "job": {
            "type": "revision",
            "content": (
                "Scene 4 doesn't match the brief — it shows a water dissolve but the "
                "product dissolves sublingually. Regenerate scene 4 with the correct "
                "action: man placing tablet under tongue."
            ),
            "brand_file": "brands/rugiet.yaml",
            "concept_id": None,
            "test_mode": True,
        },
        "expected": {
            "has_output": True,
            "has_evaluation": True,
        },
    },
    {
        "name": "Script Brief — No Brand File",
        "description": "Brief without brand constraints — tests graceful handling of missing brand",
        "job": {
            "type": "script_brief",
            "content": (
                "Create a 30-second TikTok ad for a new protein powder brand. "
                "Target audience: gym bros. Tone: hype, energetic, meme-aware. "
                "Hook with a controversial claim about traditional protein."
            ),
            "brand_file": None,
            "concept_id": None,
            "test_mode": True,
        },
        "expected": {
            "has_script": True,
            "has_evaluation": True,
            "has_pre_watch_brief": True,
        },
    },
    {
        "name": "B-Roll Edit Request",
        "description": "B-roll editing task — tests JuniorEditor with specific edit instructions",
        "job": {
            "type": "broll_edit",
            "content": (
                "Cut together a 10-second B-roll sequence from the bedroom footage. "
                "Shots needed: nightstand with product, lamp turning on, sheets rustling. "
                "Warm color grade, slow motion 0.7x."
            ),
            "brand_file": "brands/rugiet.yaml",
            "concept_id": None,
            "test_mode": True,
        },
        "expected": {
            "has_output": True,
            "has_evaluation": True,
        },
    },
]


def run_scenario(scenario: dict) -> dict:
    """Run a single scenario and return a structured report."""
    name = scenario["name"]
    job = scenario["job"]
    expected = scenario["expected"]

    print(f"\n{'─' * 60}")
    print(f"DRILL: {name}")
    print(f"  {scenario['description']}")
    print(f"{'─' * 60}")

    hop = HeadOfProduction()
    run_id = hop.run_id

    try:
        result = hop.receive_job(job)
        status = "completed"
        error = None
    except Exception as e:
        result = {"error": str(e)}
        status = "failed"
        error = str(e)

    # Validate expectations
    checks = []

    if expected.get("has_script"):
        has_it = bool(result.get("script"))
        checks.append(("Script present", has_it))

    if expected.get("has_evaluation"):
        eval_data = result.get("evaluation", {})
        has_it = bool(eval_data and "score" in eval_data)
        checks.append(("Evaluation present", has_it))

    if expected.get("has_pre_watch_brief"):
        has_it = bool(result.get("pre_watch_brief"))
        checks.append(("PreWatchBrief present", has_it))

    if expected.get("has_output"):
        has_it = bool(result.get("output") or result.get("script"))
        checks.append(("Output present", has_it))

    if expected.get("agent_used"):
        agent = result.get("agent", "")
        matches = agent == expected["agent_used"]
        checks.append((f"Agent is {expected['agent_used']}", matches))

    if expected.get("min_confidence"):
        conf = result.get("confidence", 0)
        meets = conf >= expected["min_confidence"]
        checks.append((f"Confidence >= {expected['min_confidence']}", meets))

    # Simulate a review (non-interactive)
    trust = TrustScore()
    pwb_data = result.get("pre_watch_brief")
    if pwb_data:
        review = ReviewSession(run_id, result.get("concept_id", ""), trust)
        # Simulate: human gives the predicted rating (optimistic test)
        review_result = review.record(
            rating=pwb_data.get("predicted_rating", 7),
            notes="[DRILL] Auto-review — rating matches prediction",
            pre_watch_brief=pwb_data,
        )
    else:
        review_result = None

    passed = all(ok for _, ok in checks)

    report = {
        "name": name,
        "description": scenario["description"],
        "status": status,
        "error": error,
        "run_id": run_id,
        "concept_id": result.get("concept_id"),
        "checks": [{"check": label, "passed": ok} for label, ok in checks],
        "all_passed": passed,
        "pre_watch_brief": pwb_data,
        "review": review_result,
        "eval_score": result.get("evaluation", {}).get("score"),
        "agent": result.get("agent"),
        "confidence": result.get("confidence"),
    }

    # Print check results
    for label, ok in checks:
        icon = "PASS" if ok else "FAIL"
        print(f"  [{icon}] {label}")

    if error:
        print(f"  [ERROR] {error}")

    return report


def main():
    print("=" * 60)
    print("PARALLAX DRILL — 5 REALISTIC SCENARIOS")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"TEST_MODE: {os.environ.get('TEST_MODE')}")
    print("=" * 60)

    reports = []
    for scenario in SCENARIOS:
        report = run_scenario(scenario)
        reports.append(report)

    # Summary
    print("\n" + "=" * 60)
    print("DRILL SUMMARY")
    print("=" * 60)

    passed = sum(1 for r in reports if r["all_passed"])
    failed = len(reports) - passed

    for r in reports:
        icon = "✓" if r["all_passed"] else "✗"
        status = "PASS" if r["all_passed"] else "FAIL"
        brief_info = ""
        if r.get("pre_watch_brief"):
            pwb = r["pre_watch_brief"]
            brief_info = f" | Predicted: {pwb.get('predicted_rating', '?')}/10"
        review_info = ""
        if r.get("review"):
            rv = r["review"]
            match = "correct" if rv.get("prediction_correct") else f"off by {abs(rv.get('delta', 0))}"
            review_info = f" | Review: {rv.get('rating', '?')}/10 ({match})"
        print(f"  {icon} [{status}] {r['name']} — {r['concept_id']}{brief_info}{review_info}")

    print(f"\n  {passed}/{len(reports)} scenarios passed")
    if failed:
        print(f"  {failed} FAILED — see details above")

    # Trust state after all scenarios
    trust = TrustScore()
    snap = trust.snapshot()
    print(f"\n  Trust after drills: {snap['score']:.2f} ({snap['autonomy_level']})")
    print(f"  Predictions: {snap['total_predictions']} total, {snap['correct_predictions']} correct")
    print(f"  Consecutive correct: {snap['consecutive_correct']}")

    # Save full report
    from core.paths import LOG_ROOT
    report_path = LOG_ROOT / "logs" / "drill_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    full_report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scenarios": reports,
        "summary": {
            "total": len(reports),
            "passed": passed,
            "failed": failed,
            "trust_after": snap,
        },
    }
    report_path.write_text(json.dumps(full_report, indent=2))
    print(f"\n  Full report: {report_path}")

    # Exit code
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    try:
        main()
    finally:
        builtins.input = original_input
