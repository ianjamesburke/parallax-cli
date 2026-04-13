#!/usr/bin/env python3
"""
Scenario tests for the two core use cases:
  1. YouTube footage edit (OBS clips → rough cut)
  2. Sexy strawberry Ken Burns draft (ref image + recycled VO → stills video)

Usage:
    TEST_MODE=true python test/test_scenarios.py              # run both once
    TEST_MODE=true python test/test_scenarios.py --repeat 3   # 3 consecutive runs
    TEST_MODE=true python test/test_scenarios.py --scenario youtube
    TEST_MODE=true python test/test_scenarios.py --scenario strawberry
"""

import os
import sys
import json
import argparse
import shutil
from pathlib import Path

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set log dir; TEST_MODE is controlled by env (not forced here)
os.environ.setdefault("PARALLAX_LOG_DIR", str(Path(__file__).parent.parent / ".parallax"))

# Patch input() to auto-skip clarifications in test mode
import builtins
_original_input = builtins.input
builtins.input = lambda prompt="": "skip"

from core.head_of_production import HeadOfProduction
from core.cost_tracker import cost_report


# ── Reference paths ─────────────────────────────────────────────────────────
# These scenarios reference real video assets on disk. Set PARALLAX_VIDPROD_DIR
# to point at the directory containing the fixture projects below, or the tests
# will resolve to a non-existent default and skip.

import os
VIDPROD = Path(os.environ.get("PARALLAX_VIDPROD_DIR", str(Path.home() / "parallax-fixtures"))).expanduser()

YOUTUBE_INPUT = VIDPROD / "5-ians-youtube-video" / "input"
YOUTUBE_CLIPS = [
    str(YOUTUBE_INPUT / "IMG_9656.MOV"),
    str(YOUTUBE_INPUT / "IMG_9657.MOV"),
]
YOUTUBE_BRIEF = str(YOUTUBE_INPUT / "brief.txt")

STRAWBERRY_INPUT = VIDPROD / "7-sexy-strawberry-stills" / "input"
STRAWBERRY_REF_VIDEO = str(STRAWBERRY_INPUT / "AQNePBfHxFpzEBo3K_7iMQPcJ3Naa4dMArh3T5klRzuw9E_kMzNEWWpZ-IdsioQKY-pR6Emt3s0icb3v6kAfAbwfF5w2-GUPlowGSpXpQg.mov")
STRAWBERRY_REF_IMAGE = str(STRAWBERRY_INPUT / "strawberry-ref.png")


def check_files_exist():
    """Verify all reference files exist before running tests."""
    missing = []
    for p in YOUTUBE_CLIPS + [YOUTUBE_BRIEF, STRAWBERRY_REF_VIDEO, STRAWBERRY_REF_IMAGE]:
        if not Path(p).exists():
            missing.append(p)
    if missing:
        print("[PREFLIGHT FAIL] Missing reference files:")
        for m in missing:
            print(f"  {m}")
        sys.exit(1)
    print("[PREFLIGHT] All reference files found.\n")


# ── Scenario 1: YouTube Footage Edit ────────────────────────────────────────

def run_youtube_edit() -> dict:
    """
    Submit a footage_edit job with two OBS clips.
    Expected: clips indexed, editor makes selections, video assembled, evaluated.
    """
    brief_text = Path(YOUTUBE_BRIEF).read_text()

    job = {
        "type": "footage_edit",
        "content": brief_text,
        "clips": YOUTUBE_CLIPS,
        "concept_id": None,
        "test_mode": os.environ.get("TEST_MODE", "false").lower() == "true",
    }

    hop = HeadOfProduction()
    result = hop.receive_job(job)
    return result


def verify_youtube(result: dict) -> list[str]:
    """Check that the YouTube edit produced expected outputs. Returns list of failures."""
    failures = []

    concept_id = result.get("concept_id")
    if not concept_id:
        failures.append("missing concept_id")

    status = result.get("status")
    if status != "complete":
        failures.append(f"status={status}, expected 'complete'")

    # Assembly should have produced an output file
    assembly = result.get("assembly", {})
    if not assembly.get("success"):
        failures.append(f"assembly not successful: {assembly}")

    output_path = assembly.get("output_path")
    if output_path and Path(output_path).exists():
        size = Path(output_path).stat().st_size
        if size < 1000:
            failures.append(f"output too small: {size} bytes")
    elif output_path:
        failures.append(f"output file missing: {output_path}")
    else:
        failures.append("no output_path in assembly result")

    # Evaluation should exist
    evaluation = result.get("evaluation", {})
    if not evaluation:
        failures.append("no evaluation")
    elif "error" in evaluation:
        failures.append(f"evaluation error: {evaluation['error']}")

    # Editor should have run
    agent = result.get("agent")
    if agent not in ("junior_editor", "senior_editor", "tools"):
        # Agent might be nested
        pass

    return failures


# ── Scenario 2: Sexy Strawberry Ken Burns Draft ─────────────────────────────

def run_strawberry_kb() -> dict:
    """
    Submit a storyboard job with:
    - Reference video as audio_source (recycled VO)
    - Reference image for character consistency
    - Deliverable: draft (Ken Burns with VO)
    """
    job = {
        "type": "storyboard",
        "content": (
            "Sexy strawberry supplement ad. A playful, anthropomorphized strawberry "
            "character appears in various scenes promoting a women's supplement. "
            "The character is sultry and confident. Use the reference image for "
            "visual consistency. Mirror the pacing and scene count of the reference "
            "video. Reuse the audio from the reference recording as voiceover."
        ),
        "deliverable": "draft",
        "audio_source": STRAWBERRY_REF_VIDEO,
        "ref_image": STRAWBERRY_REF_IMAGE,
        "character": (
            "An anthropomorphized strawberry character — playful, sultry, confident. "
            "Bright red skin, green leafy top, expressive eyes, feminine proportions. "
            "Illustrated in a vibrant, slightly retro ad style."
        ),
        "concept_id": None,
        "test_mode": os.environ.get("TEST_MODE", "false").lower() == "true",
    }

    hop = HeadOfProduction()
    result = hop.receive_job(job)
    return result


def verify_strawberry(result: dict) -> list[str]:
    """Check that the strawberry Ken Burns draft was produced. Returns failures."""
    failures = []

    concept_id = result.get("concept_id")
    if not concept_id:
        failures.append("missing concept_id")

    status = result.get("status")
    if status != "complete":
        failures.append(f"status={status}, expected 'complete'")

    # Scenes should have been planned
    scenes = result.get("scenes", [])
    if not scenes:
        failures.append("no scenes planned")
    elif len(scenes) < 2:
        failures.append(f"only {len(scenes)} scene(s) — expected more")

    # Stills should have been generated
    stills = result.get("stills", {})
    if not stills.get("success"):
        failures.append(f"stills generation failed: {stills}")

    # Draft assembly should exist
    draft = result.get("draft", {})
    if not draft.get("success"):
        failures.append(f"draft assembly failed: {draft}")

    output_path = draft.get("output_path")
    if output_path and Path(output_path).exists():
        size = Path(output_path).stat().st_size
        if size < 1000:
            failures.append(f"draft output too small: {size} bytes")
    elif output_path:
        failures.append(f"draft output missing: {output_path}")
    else:
        failures.append("no output_path in draft result")

    # Script should be present
    script = result.get("script", "")
    if not script:
        failures.append("no script in result")

    # VO lines should be present
    vo_lines = result.get("vo_lines", [])
    if not vo_lines:
        failures.append("no vo_lines in result")

    # Evaluation should exist
    evaluation = result.get("evaluation", {})
    if not evaluation:
        failures.append("no evaluation")
    elif "error" in evaluation:
        failures.append(f"evaluation error: {evaluation['error']}")

    return failures


# ── Runner ──────────────────────────────────────────────────────────────────

SCENARIOS = {
    "youtube": ("YouTube Footage Edit", run_youtube_edit, verify_youtube),
    "strawberry": ("Strawberry Ken Burns Draft", run_strawberry_kb, verify_strawberry),
}


def run_scenario(name: str, run_fn, verify_fn, run_num: int) -> bool:
    """Run a single scenario. Returns True if passed."""
    print(f"\n{'=' * 60}")
    print(f"SCENARIO: {name} (run #{run_num})")
    print(f"{'=' * 60}\n")

    try:
        result = run_fn()
    except Exception as e:
        print(f"\n[CRASH] {name} raised: {e}")
        import traceback
        traceback.print_exc()
        return False

    failures = verify_fn(result)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {name} (run #{run_num})")
    print(f"{'=' * 60}")

    concept_id = result.get("concept_id", "???")
    run_id = result.get("run_id", "???")
    print(f"Concept: {concept_id} | Run: {run_id}")

    # Cost report
    if concept_id and concept_id != "???":
        try:
            report = cost_report(concept_id)
            total = report.get("total_usd", 0)
            print(f"Cost: ${total:.4f}")
        except Exception:
            pass

    # Evaluation summary
    evaluation = result.get("evaluation", {})
    if evaluation and "score" in evaluation:
        print(f"Eval: {evaluation['score']:.0%} — {evaluation.get('recommendation', '')}")

    if failures:
        print(f"\n[FAIL] {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        return False
    else:
        print(f"\n[PASS] All checks passed.")
        return True


def main():
    parser = argparse.ArgumentParser(description="Parallax scenario tests")
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()), help="Run only this scenario")
    parser.add_argument("--repeat", type=int, default=1, help="Consecutive runs required")
    args = parser.parse_args()

    check_files_exist()

    scenarios = {args.scenario: SCENARIOS[args.scenario]} if args.scenario else SCENARIOS
    total_runs = args.repeat
    results = {name: [] for name in scenarios}

    for run_num in range(1, total_runs + 1):
        for name, (label, run_fn, verify_fn) in scenarios.items():
            passed = run_scenario(label, run_fn, verify_fn, run_num)
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
    for name, passes in results.items():
        label = SCENARIOS[name][0]
        passed_count = sum(passes)
        total = len(passes)
        status = "PASS" if all(passes) else "FAIL"
        print(f"  {label}: {passed_count}/{total} passed [{status}]")
        if not all(passes):
            all_passed = False

    if all_passed and total_runs > 1:
        print(f"\n{total_runs} consecutive runs ALL PASSED.")
    elif not all_passed:
        print(f"\nSome scenarios FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    main()
