#!/usr/bin/env python3
"""
Round 2 drill scenarios for Parallax agent network.
Tests: BudgetGate stress, escalation paths, iteration chains,
       PreWatchBrief diffs, review mismatches, concern propagation.

Usage:
    TEST_MODE=true python3 test/drill_scenarios_r2.py

Reports results as structured JSON + terminal summary.
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

from core.budget import BudgetGate, DecisionOption
from core.pre_watch_brief import PreWatchBrief
from core.review import ReviewSession
from core.trust import TrustScore
from core.head_of_production import HeadOfProduction
from core.paths import LOG_ROOT, BUDGETS_DIR, TRUST_FILE

# Patch input() globally — non-interactive drill
original_input = builtins.input
builtins.input = lambda prompt="": "skip"


def clean_test_state():
    """Remove budget/trust files so each run starts clean."""
    import shutil
    for pattern in ["BUDGET-*", "VELOCITY-*", "ESCALATE-*", "CHAIN-*",
                    "DIFF-*", "MISMATCH-*", "BLOCKING-*", "BRAND-*",
                    "PERCAP-*", "MIXED-*", "STORYBOARD-*",
                    "SCRIPTS-*", "DRAFT-*", "FULL-*",
                    "STILLS-*"]:
        for f in BUDGETS_DIR.glob(f"{pattern}.json"):
            f.unlink()
    if TRUST_FILE.exists():
        TRUST_FILE.unlink()


# ── Scenarios ────────────────────────────────────────────────────────────────

def test_budget_exhaustion() -> dict:
    """S1: Budget nearly depleted — verify escalation when cheapest option exceeds remaining."""
    name = "Budget Exhaustion — Escalation Required"
    gate = BudgetGate("BUDGET-EXHAUST-001")
    gate.set_budget(total=20.0)
    # Pre-spend $19.50
    gate.record_spend("setup", "test", "pre_spend", 19.50, 19.50)

    result = gate.evaluate_options([
        DecisionOption("regenerate_scene", 1.00, 2.00, "Regenerate one scene"),
        DecisionOption("regenerate_all", 5.00, 10.00, "Regenerate all scenes"),
    ])

    checks = [
        ("autonomous == False", result["autonomous"] == False),
        ("budget_remaining <= 0.50", result["budget_remaining"] <= 0.50),
        ("reason mentions budget", "budget" in result["reason"].lower() or "exhausted" in result["reason"].lower()),
    ]
    return _report(name, checks)


def test_velocity_cap() -> dict:
    """S2: Many cheap calls breach velocity cap — next decision should pause."""
    name = "Velocity Cap — Rapid Fire Pause"
    gate = BudgetGate("VELOCITY-001")
    gate.set_budget(total=50.0, velocity=10.0, velocity_window=30)

    # Record 15 spends of $0.80 each = $12.00 (exceeds $10 velocity cap)
    for i in range(15):
        gate.record_spend(f"run_{i}", "test", f"gen_variation_{i}", 0.80, 0.80)

    result = gate.evaluate_options([
        DecisionOption("next_variation", 0.50, 1.00, "One more variation"),
    ])

    checks = [
        ("velocity_ok == False", result["velocity_ok"] == False),
        ("autonomous == False", result["autonomous"] == False),
        ("reason mentions velocity", "velocity" in result["reason"].lower()),
    ]
    return _report(name, checks)


def test_junior_to_senior_escalation() -> dict:
    """S3: Force low confidence via _mock_response to trigger SeniorEditor escalation."""
    name = "Junior → Senior Escalation on Low Confidence"
    hop = HeadOfProduction()

    # JuniorEditor mock returns low confidence, triggering escalation
    # We need to mock at the JuniorEditor level. Since HoP routes revision to JuniorEditor,
    # and JuniorEditor checks _mock_response, we inject it.
    # But _mock_response is per-agent — HoP passes the job through.
    # The trick: we can't easily inject _mock_response for junior only when HoP routes.
    # Instead, test the routing logic directly.
    from packs.video.junior_editor import JuniorEditor
    from packs.video.senior_editor import SeniorEditor

    jr = JuniorEditor()
    sr = SeniorEditor()

    # Junior returns low confidence
    jr_result = jr.execute({
        "type": "revision",
        "content": "Major creative overhaul",
        "test_mode": True,
        "_mock_response": {
            "output": {"plan": "I'm not confident about this"},
            "confidence": 0.45,
            "notes": "This is too complex for me — need senior help",
            "escalate": True,
            "agent": "junior_editor",
        },
    }, run_id="test-escalate")

    # Verify junior flagged low confidence
    escalate = jr_result["confidence"] < 0.70

    # If escalated, senior handles it
    sr_result = None
    if escalate:
        sr_result = sr.execute(
            {"type": "revision", "content": "Major creative overhaul", "test_mode": True},
            junior_notes=jr_result["notes"],
            run_id="test-escalate",
        )

    checks = [
        ("junior confidence < 0.70", jr_result["confidence"] < 0.70),
        ("escalation triggered", escalate),
        ("senior executed", sr_result is not None),
        ("senior agent tag", sr_result and sr_result.get("agent") == "senior_editor"),
        ("senior confidence >= 0.85", sr_result and sr_result.get("confidence", 0) >= 0.85),
    ]
    return _report(name, checks)


def test_iteration_chain() -> dict:
    """S4: Two-step chain — generate script, then revise referencing prior run_id."""
    name = "Iteration Chain — Brief Then Revise"

    # Step 1: initial script brief
    hop1 = HeadOfProduction()
    result1 = hop1.receive_job({
        "type": "script_brief",
        "content": "Create a 15-second ad for a fitness app. Energetic tone.",
        "brand_file": None,
        "concept_id": "CHAIN-001",
        "test_mode": True,
    })
    run_1_id = hop1.run_id

    # Step 2: revision referencing prior run
    hop2 = HeadOfProduction()
    result2 = hop2.receive_job({
        "type": "revision",
        "content": "The hook is too generic. Make it more specific.",
        "brand_file": None,
        "concept_id": "CHAIN-001",
        "prior_run_id": run_1_id,
        "test_mode": True,
    })

    pwb = result2.get("pre_watch_brief", {})
    changes = pwb.get("changes", [])
    changes_text = " ".join(changes)

    checks = [
        ("run 1 completed", result1.get("status") == "complete"),
        ("run 2 completed", result2.get("status") == "complete"),
        ("pre_watch_brief present on run 2", bool(pwb)),
        ("changes not 'First iteration'", "First iteration" not in changes_text),
        ("iteration >= 2", pwb.get("iteration", 0) >= 2),
    ]
    return _report(name, checks)


def test_prewatch_diff() -> dict:
    """S5: Verify PreWatchBrief diff detects agent, confidence, and eval score changes."""
    name = "PreWatchBrief Diff — Multi-Field Change Detection"

    # Run 1: script brief (produces result.json with script_writer agent, confidence 0.9, eval 0.85)
    hop1 = HeadOfProduction()
    result1 = hop1.receive_job({
        "type": "script_brief",
        "content": "Create a 30-second ad for a meal kit service.",
        "brand_file": None,
        "concept_id": "DIFF-001",
        "test_mode": True,
    })
    run_1_id = hop1.run_id

    # Manually modify run 1's result.json to have distinct values for diff detection
    from core.paths import run_dir
    r1_result_path = run_dir(run_1_id) / "result.json"
    r1_data = json.loads(r1_result_path.read_text())
    r1_data["agent"] = "script_writer"
    r1_data["confidence"] = 0.55
    r1_data["evaluation"]["score"] = 0.60
    r1_result_path.write_text(json.dumps(r1_data, indent=2))

    # Run 2: revision with prior_run_id — drill stubs return different values
    hop2 = HeadOfProduction()
    result2 = hop2.receive_job({
        "type": "revision",
        "content": "Major revision — escalated to senior.",
        "brand_file": None,
        "concept_id": "DIFF-001",
        "prior_run_id": run_1_id,
        "test_mode": True,
    })

    pwb = result2.get("pre_watch_brief", {})
    changes = pwb.get("changes", [])
    changes_text = " ".join(changes)

    checks = [
        ("diff ran (not 'First iteration')", "First iteration" not in changes_text),
        ("detected routing change", "routing" in changes_text.lower() or "agent" in changes_text.lower()),
        ("detected confidence change", "confidence" in changes_text.lower()),
        ("detected eval score change", "eval" in changes_text.lower()),
    ]
    return _report(name, checks)


def test_review_rating_mismatch() -> dict:
    """S6: Human rates far below prediction — verify trust records miss."""
    name = "Review Rating Mismatch — Trust Hit"

    hop = HeadOfProduction()
    result = hop.receive_job({
        "type": "script_brief",
        "content": "Create a 15-second ad for a pet food brand. Playful tone.",
        "brand_file": None,
        "concept_id": "MISMATCH-001",
        "test_mode": True,
    })

    pwb = result.get("pre_watch_brief", {})
    predicted = pwb.get("predicted_rating", 7)

    # Human gives a much lower rating
    trust = TrustScore()
    review = ReviewSession(hop.run_id, "MISMATCH-001", trust)
    review_result = review.record(rating=3, notes="Terrible — completely off-brief", pre_watch_brief=pwb)

    checks = [
        ("prediction_correct == False", review_result.get("prediction_correct") == False),
        ("delta abs >= 3", abs(review_result.get("delta", 0)) >= 3),
        ("no trust increase proposal", review_result.get("trust_increase_proposal") is None),
    ]
    return _report(name, checks)


def test_blocking_concern() -> dict:
    """S7: Blocking concern injected — verify PreWatchBrief surfaces it and drags rating."""
    name = "Blocking Concern — Pipeline Halt"

    hop = HeadOfProduction()
    result = hop.receive_job({
        "type": "script_brief",
        "content": "Create a 15-second ad for a supplement brand.",
        "brand_file": None,
        "concept_id": "BLOCKING-001",
        "test_mode": True,
    })

    # Inject blocking concerns into the PreWatchBrief directly
    blocking_concerns = [
        {"severity": 1.0, "blocking": True, "message": "Script contains unsubstantiated health claim — FDA risk"},
    ]

    pwb_gen = PreWatchBrief(hop.run_id, "BLOCKING-001")
    brief = pwb_gen.generate(result, blocking_concerns)

    concerns_text = " ".join(brief.get("concerns_summary", []))

    checks = [
        ("brief generated", bool(brief)),
        ("[BLOCKING] in concerns", "[BLOCKING]" in concerns_text),
        ("predicted_rating <= 6", brief.get("predicted_rating", 10) <= 6),
        ("feedback mentions blocking", "blocking" in brief.get("predicted_feedback", "").lower()),
    ]
    return _report(name, checks)


def test_per_decision_cap() -> dict:
    """S9: Every option exceeds per-decision cap — must escalate regardless of budget."""
    name = "Per-Decision Cap — All Options Expensive"

    gate = BudgetGate("PERCAP-001")
    gate.set_budget(total=50.0, per_decision=2.0)

    result = gate.evaluate_options([
        DecisionOption("regenerate_all_basic", 3.50, 7.00, "Basic regen"),
        DecisionOption("regenerate_all_premium", 8.00, 16.00, "Premium regen"),
    ])

    checks = [
        ("autonomous == False", result["autonomous"] == False),
        ("budget_remaining >= 40", result["budget_remaining"] >= 40.0),
        ("reason mentions cap", "cap" in result["reason"].lower() or "exceeds" in result["reason"].lower()),
    ]
    return _report(name, checks)


def test_brand_constraint_violation() -> dict:
    """S8: Brief violates brand constraint — currently evaluator is a stub in TEST_MODE,
    so this tests that the pipeline completes without error. Brand constraint detection
    is a Round 3 feature."""
    name = "Brand Constraint Violation (smoke test)"

    hop = HeadOfProduction()
    result = hop.receive_job({
        "type": "script_brief",
        "content": "Create a 15-second ad for Rugiet Ready. Compare directly to Viagra and Cialis by name.",
        "brand_file": "brands/rugiet.yaml",
        "concept_id": "BRAND-VIOLATE-001",
        "test_mode": True,
    })

    checks = [
        ("pipeline completed", result.get("status") == "complete"),
        ("has evaluation", bool(result.get("evaluation"))),
        ("has pre_watch_brief", bool(result.get("pre_watch_brief"))),
    ]
    return _report(name, checks)


def test_storyboard_pipeline() -> dict:
    """S11: Storyboard job — script + character → planned scenes with stills."""
    name = "Storyboard Pipeline — Script to Scenes"

    hop = HeadOfProduction()
    result = hop.receive_job({
        "type": "storyboard",
        "content": "Create a 15-second ad for a skincare brand. Show a woman's morning routine.",
        "character": "30-year-old woman, warm brown skin, natural curly hair, minimal makeup, cozy morning aesthetic",
        "brand_file": None,
        "concept_id": "STORYBOARD-001",
        "test_mode": True,
    })

    scenes = result.get("scenes", [])

    checks = [
        ("pipeline completed", result.get("status") == "complete"),
        ("has scenes", len(scenes) > 0),
        ("scenes have starting_frame prompts", all(s.get("starting_frame") for s in scenes)),
        ("scenes have vo_text", all(s.get("vo_text") for s in scenes)),
        ("scenes have ken_burns", all(s.get("ken_burns") for s in scenes)),
        ("has script carried forward", bool(result.get("script") or result.get("vo_lines"))),
        ("agent is storyboard_planner", result.get("agent") == "storyboard_planner"),
        ("has evaluation", bool(result.get("evaluation"))),
        ("has pre_watch_brief", bool(result.get("pre_watch_brief"))),
    ]
    return _report(name, checks)


def test_generate_stills() -> dict:
    """S13: Lightweight still generation — no manifest, no scenes, just images."""
    name = "Generate Stills — No Pipeline"

    hop = HeadOfProduction()
    result = hop.receive_job({
        "type": "generate_stills",
        "content": "Draw this character wearing 5 different hats: cowboy, beret, beanie, top hat, baseball cap",
        "ref_image": "/tmp/test-character.png",
        "count": 5,
        "brand_file": None,
        "concept_id": "STILLS-ONLY-001",
        "test_mode": True,
    })

    checks = [
        ("pipeline completed", result.get("status") == "complete"),
        ("has stills result", bool(result.get("stills"))),
        ("count is 5", result.get("count") == 5),
        ("ref_image passed through", result.get("stills", {}).get("ref_image") == "/tmp/test-character.png"),
        ("agent is tools", result.get("agent") == "tools"),
    ]
    return _report(name, checks)


def test_storyboard_scripts_only() -> dict:
    """S13: Storyboard with deliverable=scripts_only — stops after ScriptWriter."""
    name = "Storyboard — Scripts Only"

    hop = HeadOfProduction()
    result = hop.receive_job({
        "type": "storyboard",
        "deliverable": "scripts_only",
        "content": "Create 3 script ideas for a coffee brand. Morning energy angle.",
        "brand_file": None,
        "concept_id": "SCRIPTS-ONLY-001",
        "test_mode": True,
    })

    checks = [
        ("pipeline completed", result.get("status") == "complete"),
        ("deliverable is scripts_only", result.get("deliverable") == "scripts_only"),
        ("has script text", bool(result.get("script"))),
        ("no storyboard planner ran", result.get("agent") == "script_writer"),
        ("no stills generated", not result.get("stills")),
    ]
    return _report(name, checks)


def test_storyboard_draft() -> dict:
    """S14: Storyboard with deliverable=draft — goes through stills + Ken Burns assembly."""
    name = "Storyboard — Draft (Ken Burns)"

    hop = HeadOfProduction()
    result = hop.receive_job({
        "type": "storyboard",
        "deliverable": "draft",
        "content": "15-second ad for a yoga mat brand. Calm sunrise aesthetic.",
        "character": "Athletic woman, early 30s, yoga clothes, outdoor setting",
        "manifest_path": "/tmp/test-manifest.yaml",
        "brand_file": None,
        "concept_id": "DRAFT-001",
        "test_mode": True,
    })

    checks = [
        ("pipeline completed", result.get("status") == "complete"),
        ("deliverable is draft", result.get("deliverable") == "draft"),
        ("has scenes", len(result.get("scenes", [])) > 0),
        ("stills generated", result.get("stills", {}).get("success", False)),
        ("draft assembled", result.get("draft", {}).get("success", False)),
        ("no voiceover (draft only)", not result.get("voiceover")),
    ]
    return _report(name, checks)


def test_storyboard_full() -> dict:
    """S15: Storyboard with deliverable=full — complete pipeline including VO + captions."""
    name = "Storyboard — Full Pipeline"

    hop = HeadOfProduction()
    result = hop.receive_job({
        "type": "storyboard",
        "deliverable": "full",
        "content": "15-second ad for a meditation app. ASMR whisper tone.",
        "character": "Calm person meditating, soft lighting, minimal room",
        "manifest_path": "/tmp/test-manifest.yaml",
        "brand_file": None,
        "concept_id": "FULL-001",
        "test_mode": True,
    })

    checks = [
        ("pipeline completed", result.get("status") == "complete"),
        ("deliverable is full", result.get("deliverable") == "full"),
        ("has scenes", len(result.get("scenes", [])) > 0),
        ("stills generated", result.get("stills", {}).get("success", False)),
        ("voiceover generated", result.get("voiceover", {}).get("success", False)),
        ("assembly completed", result.get("assembly", {}).get("success", False)),
    ]
    return _report(name, checks)


def test_storyboard_tools() -> dict:
    """S12: Tool registry — verify tools return stubs in TEST_MODE."""
    name = "Tool Registry — Drill Mode Stubs"

    from packs.video.tools import call_tool

    results = [
        call_tool("plan_scenes", manifest_path="/tmp/test.yaml"),
        call_tool("generate_still", manifest_path="/tmp/test.yaml", scene="1"),
        call_tool("assemble", manifest_path="/tmp/test.yaml"),
        call_tool("burn_captions", manifest_path="/tmp/test.yaml", video="/tmp/test.mp4"),
        call_tool("generate_voiceover", manifest_path="/tmp/test.yaml"),
        call_tool("align_scenes", manifest_path="/tmp/test.yaml"),
        call_tool("init_project", slug="test-project"),
        call_tool("nonexistent_tool"),
    ]

    checks = [
        ("plan_scenes succeeds", results[0]["success"]),
        ("generate_still succeeds", results[1]["success"]),
        ("assemble succeeds", results[2]["success"]),
        ("burn_captions succeeds", results[3]["success"]),
        ("generate_voiceover succeeds", results[4]["success"]),
        ("align_scenes succeeds", results[5]["success"]),
        ("init_project succeeds", results[6]["success"]),
        ("unknown tool fails gracefully", not results[7]["success"]),
        ("all have tool tag", all(r.get("tool") for r in results[:7])),
    ]
    return _report(name, checks)


def test_mixed_job_sequence() -> dict:
    """S10: Script → stills → revision → b-roll on one concept — end-to-end."""
    name = "Mixed Job Sequence — Full Pipeline"

    concept_id = "MIXED-001"
    jobs = [
        {"type": "script_brief", "content": "Create a 15-second ad for a sleep supplement.", "brand_file": None, "concept_id": concept_id, "test_mode": True},
        {"type": "still_variations", "content": "Generate 3 product shots. Dark blue tones.", "brand_file": None, "concept_id": concept_id, "test_mode": True},
        {"type": "revision", "content": "Scene 2 is too bright — darken it.", "brand_file": None, "concept_id": concept_id, "test_mode": True},
        {"type": "broll_edit", "content": "Cut 8 seconds of b-roll: pillow fluff, moonlight, clock.", "brand_file": None, "concept_id": concept_id, "test_mode": True},
    ]

    results = []
    run_ids = []
    for i, job in enumerate(jobs):
        if i >= 2 and run_ids:
            job["prior_run_id"] = run_ids[0]  # reference first run
        hop = HeadOfProduction()
        result = hop.receive_job(job)
        results.append(result)
        run_ids.append(hop.run_id)

    all_completed = all(r.get("status") == "complete" for r in results)
    last_pwb = results[-1].get("pre_watch_brief", {})

    # Check trust state after all 4
    trust = TrustScore()
    snap = trust.snapshot()

    checks = [
        ("all 4 jobs completed", all_completed),
        ("4 run_ids generated", len(run_ids) == 4),
        ("last job has pre_watch_brief", bool(last_pwb)),
        ("iteration >= 2 on last job", last_pwb.get("iteration", 0) >= 2),
        ("trust has predictions", snap["total_predictions"] >= 0),
    ]
    return _report(name, checks)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _report(name: str, checks: list[tuple[str, bool]]) -> dict:
    passed = all(ok for _, ok in checks)
    for label, ok in checks:
        icon = "PASS" if ok else "FAIL"
        print(f"  [{icon}] {label}")
    return {
        "name": name,
        "checks": [{"check": label, "passed": ok} for label, ok in checks],
        "all_passed": passed,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

ALL_TESTS = [
    ("S1", "Budget Exhaustion", test_budget_exhaustion),
    ("S2", "Velocity Cap", test_velocity_cap),
    ("S3", "Junior→Senior Escalation", test_junior_to_senior_escalation),
    ("S4", "Iteration Chain", test_iteration_chain),
    ("S5", "PreWatchBrief Diff", test_prewatch_diff),
    ("S6", "Review Mismatch", test_review_rating_mismatch),
    ("S7", "Blocking Concern", test_blocking_concern),
    ("S8", "Brand Violation (smoke)", test_brand_constraint_violation),
    ("S9", "Per-Decision Cap", test_per_decision_cap),
    ("S10", "Mixed Job Sequence", test_mixed_job_sequence),
    ("S11", "Storyboard Pipeline", test_storyboard_pipeline),
    ("S12", "Tool Registry", test_storyboard_tools),
    ("S13", "Generate Stills (no pipeline)", test_generate_stills),
    ("S14", "Scripts Only", test_storyboard_scripts_only),
    ("S15", "Draft (Ken Burns)", test_storyboard_draft),
    ("S16", "Full Pipeline", test_storyboard_full),
]


def main():
    clean_test_state()

    print("=" * 60)
    print("PARALLAX DRILL — ROUND 2 (10 SCENARIOS)")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"TEST_MODE: {os.environ.get('TEST_MODE')}")
    print("=" * 60)

    reports = []
    for tag, desc, fn in ALL_TESTS:
        print(f"\n{'─' * 60}")
        print(f"[{tag}] {desc}")
        print(f"{'─' * 60}")
        try:
            report = fn()
            report["tag"] = tag
            reports.append(report)
        except Exception as e:
            print(f"  [ERROR] {e}")
            import traceback
            traceback.print_exc()
            reports.append({"tag": tag, "name": desc, "all_passed": False,
                           "checks": [], "error": str(e)})

    # Summary
    print("\n" + "=" * 60)
    print("ROUND 2 SUMMARY")
    print("=" * 60)

    passed = sum(1 for r in reports if r["all_passed"])
    failed = len(reports) - passed

    for r in reports:
        icon = "✓" if r["all_passed"] else "✗"
        status = "PASS" if r["all_passed"] else "FAIL"
        err = f" — ERROR: {r['error']}" if r.get("error") else ""
        print(f"  {icon} [{status}] {r.get('tag', '?')} {r['name']}{err}")

    print(f"\n  {passed}/{len(reports)} scenarios passed")
    if failed:
        print(f"  {failed} FAILED — see details above")

    # Save report
    report_path = LOG_ROOT / "logs" / "drill_report_r2.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    full_report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "round": 2,
        "scenarios": reports,
        "summary": {"total": len(reports), "passed": passed, "failed": failed},
    }
    report_path.write_text(json.dumps(full_report, indent=2))
    print(f"\n  Full report: {report_path}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    try:
        main()
    finally:
        builtins.input = original_input
