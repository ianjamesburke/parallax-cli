#!/usr/bin/env python3
"""
Regression tests for the manifest-first refactor.

Tests the plan confirmation gate, manifest brief block, and pipeline
completeness across five job types. All briefs are inline — no external
file dependencies.

Usage:
    TEST_MODE=true python test/test_regression.py
    TEST_MODE=true python test/test_regression.py --repeat 3
"""

import os
import sys
import argparse
import traceback
from pathlib import Path

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Force TEST_MODE
os.environ["TEST_MODE"] = "true"
os.environ.setdefault("PARALLAX_LOG_DIR", str(Path(__file__).parent.parent / ".parallax"))

# Patch input() to auto-proceed by default
import builtins
_original_input = builtins.input
_input_override = ""
builtins.input = lambda prompt="": _input_override


def set_input(value: str):
    """Set what input() returns for the next scenario."""
    global _input_override
    _input_override = value


# ── Scenario definitions ──────────────────────────────────────────────────

def scenario_script_brief():
    """Scenario 1: Basic script generation. Plan generated, evaluator runs."""
    set_input("")
    from core.head_of_production import HeadOfProduction

    job = {
        "type": "script_brief",
        "content": "Write a 30-second script for a protein bar ad targeting gym-goers.",
        "concept_id": None,
        "test_mode": True,
    }

    hop = HeadOfProduction()
    result = hop.receive_job(job)
    failures = []

    if result.get("status") != "complete":
        failures.append(f"status={result.get('status')}, expected 'complete'")
    if not result.get("concept_id"):
        failures.append("missing concept_id")
    if not result.get("run_id"):
        failures.append("missing run_id")
    # Plan should have been generated (stored on instance)
    if hop._current_plan is None:
        failures.append("_current_plan is None — plan was not generated")
    elif hop._current_plan.get("job_type") != "script_brief":
        failures.append(f"plan job_type={hop._current_plan.get('job_type')}, expected 'script_brief'")

    return result, failures


def scenario_storyboard_draft():
    """Scenario 2: Full Ken Burns pipeline. Manifest has brief block, scenes planned."""
    set_input("")
    from core.head_of_production import HeadOfProduction
    import yaml

    job = {
        "type": "storyboard",
        "content": "A whimsical animated ad for a cat toy. A fluffy orange cat discovers a new toy.",
        "deliverable": "draft",
        "concept_id": None,
        "test_mode": True,
    }

    hop = HeadOfProduction()
    result = hop.receive_job(job)
    failures = []

    if result.get("status") != "complete":
        failures.append(f"status={result.get('status')}, expected 'complete'")
    if not result.get("concept_id"):
        failures.append("missing concept_id")

    # Manifest should exist and have brief block
    from core.paths import project_dir as get_project_dir
    work_dir = get_project_dir(result["concept_id"])
    manifest_path = work_dir / "manifest.yaml"
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        if not manifest.get("brief"):
            failures.append("manifest missing 'brief' block")
        elif not manifest["brief"].get("articulated_intent"):
            failures.append("manifest brief missing 'articulated_intent'")
        if not manifest.get("scenes"):
            failures.append("manifest missing 'scenes'")
    else:
        failures.append(f"manifest not found: {manifest_path}")

    # Scenes should be planned
    if not result.get("scenes"):
        failures.append("no scenes in result")

    # Draft should have been assembled
    draft = result.get("draft", {})
    if not draft.get("success"):
        failures.append(f"draft assembly failed: {draft}")

    return result, failures


def scenario_footage_edit():
    """Scenario 3: Clips indexed, editor runs, assembly, manifest has footage block."""
    set_input("")
    from core.head_of_production import HeadOfProduction
    import yaml
    import tempfile
    import subprocess

    # Create a synthetic clip for testing (short black video)
    tmp_dir = Path(tempfile.mkdtemp(prefix="parallax_test_"))
    clip_path = str(tmp_dir / "test_clip.mp4")
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "color=black:s=320x240:d=2",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "2", "-shortest", "-y", clip_path],
        capture_output=True, timeout=30,
    )

    job = {
        "type": "footage_edit",
        "content": "Quick cut of test clips for a demo reel.",
        "clips": [clip_path],
        "concept_id": None,
        "test_mode": True,
    }

    hop = HeadOfProduction()
    result = hop.receive_job(job)
    failures = []

    if result.get("status") != "complete":
        failures.append(f"status={result.get('status')}, expected 'complete'")
    if not result.get("concept_id"):
        failures.append("missing concept_id")

    # Assembly should have run
    assembly = result.get("assembly", {})
    if not assembly.get("success"):
        failures.append(f"assembly not successful: {assembly}")

    # Manifest should exist and have both brief and footage blocks
    from core.paths import project_dir as get_project_dir
    work_dir = get_project_dir(result["concept_id"])
    manifest_path = work_dir / "manifest.yaml"
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        if not manifest.get("brief"):
            failures.append("manifest missing 'brief' block")
        if not manifest.get("footage"):
            failures.append("manifest missing 'footage' block")
    else:
        failures.append(f"manifest not found: {manifest_path}")

    return result, failures


def scenario_plan_abort():
    """Scenario 4: User types 'abort' at plan confirmation. No agents run."""
    # In TEST_MODE, _confirm_plan returns True immediately, so we need to
    # temporarily unset TEST_MODE and patch input to return "abort"
    old_test_mode = os.environ.get("TEST_MODE")
    os.environ.pop("TEST_MODE", None)
    set_input("abort")

    from core.head_of_production import HeadOfProduction

    job = {
        "type": "script_brief",
        "content": "This job should be aborted before any agent runs.",
        "concept_id": None,
        "test_mode": False,
    }

    try:
        hop = HeadOfProduction(skip_health_check=True)
        result = hop.receive_job(job)
    finally:
        # Restore TEST_MODE
        if old_test_mode is not None:
            os.environ["TEST_MODE"] = old_test_mode
        else:
            os.environ["TEST_MODE"] = "true"
        set_input("")

    failures = []

    if result.get("status") != "aborted":
        failures.append(f"status={result.get('status')}, expected 'aborted'")
    if not result.get("plan"):
        failures.append("missing plan in abort result")
    if not result.get("concept_id"):
        failures.append("missing concept_id in abort result")

    return result, failures


def scenario_storyboard_stills_only():
    """Scenario 5: Stills generated but no video assembly."""
    set_input("")
    from core.head_of_production import HeadOfProduction
    import yaml

    job = {
        "type": "storyboard",
        "content": "A minimalist ad for a candle brand. Three scenes: lit candle, hands cupping candle, dark room with glow.",
        "deliverable": "stills_only",
        "concept_id": None,
        "test_mode": True,
    }

    hop = HeadOfProduction()
    result = hop.receive_job(job)
    failures = []

    if result.get("status") != "complete":
        failures.append(f"status={result.get('status')}, expected 'complete'")
    if not result.get("concept_id"):
        failures.append("missing concept_id")

    # Plan should have been generated
    if hop._current_plan is None:
        failures.append("_current_plan is None")

    # Scenes should be planned
    if not result.get("scenes"):
        failures.append("no scenes in result")

    # Stills should have been generated
    stills = result.get("stills", {})
    if not stills.get("success"):
        failures.append(f"stills generation failed: {stills}")

    # Draft should NOT exist (stills_only deliverable)
    if result.get("draft"):
        failures.append("draft exists but should not for stills_only")

    # Manifest should have brief block
    from core.paths import project_dir as get_project_dir
    work_dir = get_project_dir(result["concept_id"])
    manifest_path = work_dir / "manifest.yaml"
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        if not manifest.get("brief"):
            failures.append("manifest missing 'brief' block")
    else:
        failures.append(f"manifest not found: {manifest_path}")

    return result, failures


# ── Runner ────────────────────────────────────────────────────────────────

SCENARIOS = [
    ("script_brief", "Script Brief", scenario_script_brief),
    ("storyboard_draft", "Storyboard Draft (Ken Burns)", scenario_storyboard_draft),
    ("footage_edit", "Footage Edit", scenario_footage_edit),
    ("plan_abort", "Plan Abort", scenario_plan_abort),
    ("stills_only", "Storyboard Stills Only", scenario_storyboard_stills_only),
]


def run_scenario(name: str, label: str, fn, run_num: int) -> bool:
    """Run a single scenario. Returns True if passed."""
    print(f"\n{'=' * 60}")
    print(f"SCENARIO {name}: {label} (run #{run_num})")
    print(f"{'=' * 60}\n")

    try:
        result, failures = fn()
    except Exception as e:
        print(f"\n[CRASH] {label} raised: {e}")
        traceback.print_exc()
        return False

    concept_id = result.get("concept_id", "???")
    run_id = result.get("run_id", "???")
    status = result.get("status", "???")

    print(f"\n{'=' * 60}")
    print(f"RESULT: {label} (run #{run_num})")
    print(f"{'=' * 60}")
    print(f"Concept: {concept_id} | Run: {run_id} | Status: {status}")

    if failures:
        print(f"\n[FAIL] {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        return False
    else:
        print(f"\n[PASS] All checks passed.")
        return True


def main():
    parser = argparse.ArgumentParser(description="Parallax regression tests")
    parser.add_argument("--repeat", type=int, default=1, help="Consecutive runs")
    args = parser.parse_args()

    total_runs = args.repeat
    results = {name: [] for name, _, _ in SCENARIOS}

    for run_num in range(1, total_runs + 1):
        for name, label, fn in SCENARIOS:
            passed = run_scenario(name, label, fn, run_num)
            results[name].append(passed)
            if not passed and total_runs > 1:
                print(f"\n[ABORT] {label} failed on run #{run_num} — stopping.")
                break
        else:
            continue
        break

    # Final summary
    print(f"\n\n{'=' * 60}")
    print("FINAL SUMMARY")
    print(f"{'=' * 60}")
    all_passed = True
    for name, label, _ in SCENARIOS:
        passes = results[name]
        passed_count = sum(passes)
        total = len(passes)
        status = "PASS" if all(passes) else "FAIL"
        print(f"  {label}: {passed_count}/{total} passed [{status}]")
        if not all(passes):
            all_passed = False

    if all_passed:
        if total_runs > 1:
            print(f"\n{total_runs} consecutive runs ALL PASSED.")
        else:
            print(f"\nAll scenarios PASSED.")
    else:
        print(f"\nSome scenarios FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    main()
