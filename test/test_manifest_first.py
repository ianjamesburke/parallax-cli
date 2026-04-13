#!/usr/bin/env python3
"""
Manifest-first refactor tests.

Verifies that footage_edit jobs produce manifest YAML instead of ffmpeg commands,
and that the pipeline assembles from the manifest.

Usage:
    TEST_MODE=true python3.11 test/test_manifest_first.py
    TEST_MODE=true python3.11 test/test_manifest_first.py --repeat 3
"""

import os
import sys
import yaml
import tempfile
import subprocess
import argparse
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["TEST_MODE"] = "true"
os.environ.setdefault("PARALLAX_LOG_DIR", str(Path(__file__).parent.parent / ".parallax"))

import builtins
builtins.input = lambda prompt="": ""


# ── Test cases ────────────────────────────────────────────────────────────────

def test_footage_edit_produces_manifest():
    """
    Test 1: footage_edit pipeline runs end-to-end and writes manifest.yaml with
    both brief and footage blocks; output MP4 exists with non-zero size.
    """
    from core.head_of_production import HeadOfProduction

    # Create a synthetic 2-second black clip for testing
    tmp_dir = Path(tempfile.mkdtemp(prefix="parallax_test_manifest_"))
    clip_path = str(tmp_dir / "clip_01.mp4")
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "color=black:s=320x240:d=2",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "2", "-shortest", "-y", clip_path],
        capture_output=True, timeout=30,
    )

    job = {
        "type": "footage_edit",
        "content": "Assemble a quick cut for a manifest-first smoke test.",
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

    # Assembly should have succeeded
    assembly = result.get("assembly", {})
    if not assembly.get("success"):
        failures.append(f"assembly not successful: {assembly}")

    # Output MP4 should exist with non-zero size
    output_path = assembly.get("output_path")
    if output_path:
        p = Path(output_path)
        if not p.exists():
            failures.append(f"output MP4 does not exist: {output_path}")
        elif p.stat().st_size == 0:
            failures.append(f"output MP4 is zero bytes: {output_path}")
    else:
        failures.append("assembly result missing output_path")

    # Manifest should exist with brief and footage blocks
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


def test_editor_no_ffmpeg_tool():
    """
    Test 2: JuniorEditor and SeniorEditor do NOT include 'ffmpeg' in tool list
    for footage_edit jobs (with or without clip_index_data).
    """
    from packs.video.junior_editor import JuniorEditor
    from packs.video.senior_editor import SeniorEditor

    failures = []

    # Test with clip_index_data (the indexed path)
    job_with_index = {
        "type": "footage_edit",
        "content": "test",
        "clip_index_data": [{"name": "clip.mp4", "duration_s": 10.0, "manifest": "/tmp/test.yaml",
                              "transcript": "hello", "clips": []}],
    }
    jr_tools = JuniorEditor()._get_tools(job_with_index)
    if "ffmpeg" in jr_tools:
        failures.append(f"JuniorEditor._get_tools(clip_index_data) still contains 'ffmpeg': {jr_tools}")

    sr_tools = SeniorEditor()._get_tools(job_with_index)
    if "ffmpeg" in sr_tools:
        failures.append(f"SeniorEditor._get_tools(clip_index_data) still contains 'ffmpeg': {sr_tools}")

    # Test without clip_index_data (the non-indexed path)
    job_no_index = {"type": "footage_edit", "content": "test"}
    jr_tools_ni = JuniorEditor()._get_tools(job_no_index)
    if "ffmpeg" in jr_tools_ni:
        failures.append(f"JuniorEditor._get_tools(no index) contains 'ffmpeg': {jr_tools_ni}")

    sr_tools_ni = SeniorEditor()._get_tools(job_no_index)
    if "ffmpeg" in sr_tools_ni:
        failures.append(f"SeniorEditor._get_tools(no index) contains 'ffmpeg': {sr_tools_ni}")

    return {}, failures


def test_editor_manifest_output_mode():
    """
    Test 3: JuniorEditor._build_prompt() includes manifest prompt (not tool_calls prompt)
    when job type is footage_edit, regardless of output_mode flag.
    """
    from packs.video.junior_editor import JuniorEditor
    from packs.video.senior_editor import SeniorEditor

    failures = []

    # Case A: footage_edit without output_mode flag — should still get manifest prompt
    job_a = {
        "type": "footage_edit",
        "content": "Cut a demo reel",
    }
    jr_prompt_a = JuniorEditor()._build_prompt(job_a)
    if "MANIFEST" not in jr_prompt_a:
        failures.append("JuniorEditor prompt missing 'MANIFEST' for footage_edit (no output_mode flag)")
    if "tool_calls" in jr_prompt_a and "Plan tool calls" in jr_prompt_a:
        failures.append("JuniorEditor prompt still contains tool_calls prompt for footage_edit")

    sr_prompt_a = SeniorEditor()._build_prompt(job_a, junior_notes=None)
    if "MANIFEST" not in sr_prompt_a:
        failures.append("SeniorEditor prompt missing 'MANIFEST' for footage_edit (no output_mode flag)")

    # Case B: footage_edit with output_mode=manifest — should also get manifest prompt
    job_b = {
        "type": "footage_edit",
        "content": "Cut a demo reel",
        "output_mode": "manifest",
    }
    jr_prompt_b = JuniorEditor()._build_prompt(job_b)
    if "MANIFEST" not in jr_prompt_b:
        failures.append("JuniorEditor prompt missing 'MANIFEST' for footage_edit with output_mode=manifest")

    # Case C: non-footage job without output_mode — should NOT get manifest prompt
    job_c = {
        "type": "broll_edit",
        "content": "Edit broll",
    }
    jr_prompt_c = JuniorEditor()._build_prompt(job_c)
    # For non-footage jobs without output_mode, the manifest prompt should NOT be injected
    # (the else branch should fire, giving the tool_calls / describe prompt)
    # We just verify there's no crash and the prompt is non-empty
    if not jr_prompt_c:
        failures.append("JuniorEditor._build_prompt() returned empty string for broll_edit")

    return {}, failures


def test_write_manifest_scenes_tool():
    """
    Test 4: write_manifest_scenes tool correctly writes scenes into manifest YAML
    and preserves existing blocks (brief).
    """
    from packs.video.tools import write_manifest_scenes

    failures = []

    tmp_dir = Path(tempfile.mkdtemp(prefix="parallax_test_wms_"))
    manifest_path = str(tmp_dir / "manifest.yaml")

    # Write a manifest with just a brief block first
    initial = {
        "brief": {
            "articulated_intent": "Test brief for manifest scenes",
            "job_type": "footage_edit",
        }
    }
    with open(manifest_path, "w") as f:
        yaml.dump(initial, f)

    # Write scenes via the tool
    scenes = [
        {"index": 0, "type": "video", "source": "/tmp/clip_01.mp4", "start_s": 0.0, "end_s": 5.0,
         "description": "Opening shot"},
        {"index": 1, "type": "video", "source": "/tmp/clip_02.mp4", "start_s": 2.0, "end_s": 8.0},
        {"index": 2, "type": "text_overlay", "overlay_text": "Title Card", "estimated_duration_s": 3.0},
    ]

    result = write_manifest_scenes(manifest_path, scenes)

    if not result.get("success"):
        failures.append(f"write_manifest_scenes returned failure: {result.get('stderr')}")
        return {}, failures

    # Read manifest back and verify
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    # brief should be preserved
    if not manifest.get("brief"):
        failures.append("brief block was clobbered by write_manifest_scenes")
    elif manifest["brief"].get("articulated_intent") != "Test brief for manifest scenes":
        failures.append(f"brief.articulated_intent changed unexpectedly: {manifest['brief']}")

    # footage.source_clips should exist and have 3 entries
    footage = manifest.get("footage", {})
    if not footage:
        failures.append("footage block missing from manifest after write_manifest_scenes")
    else:
        source_clips = footage.get("source_clips", [])
        if len(source_clips) != 3:
            failures.append(f"expected 3 source_clips, got {len(source_clips)}: {source_clips}")
        else:
            # Verify first clip
            if source_clips[0].get("path") != "/tmp/clip_01.mp4":
                failures.append(f"first clip path wrong: {source_clips[0]}")
            if source_clips[0].get("start_s") != 0.0:
                failures.append(f"first clip start_s wrong: {source_clips[0]}")
            if source_clips[0].get("label") != "Opening shot":
                failures.append(f"first clip label (description) wrong: {source_clips[0]}")
            # Verify text overlay
            if source_clips[2].get("overlay_text") != "Title Card":
                failures.append(f"text overlay clip missing overlay_text: {source_clips[2]}")

        assembly_order = footage.get("assembly_order", [])
        if assembly_order != [0, 1, 2]:
            failures.append(f"assembly_order wrong: {assembly_order}")

    return {}, failures


def test_storyboard_pipeline_unchanged():
    """
    Test 5: Storyboard pipeline (Ken Burns) still works after refactor.
    Result has 'scenes', manifest has 'scenes' block (not 'footage'), draft assembled.
    """
    from core.head_of_production import HeadOfProduction

    job = {
        "type": "storyboard",
        "content": "A minimal ad for a coffee brand. Three scenes: beans, pour, cup.",
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
    if not result.get("scenes"):
        failures.append("no scenes in result")

    # Draft should have been assembled
    draft = result.get("draft", {})
    if not draft.get("success"):
        failures.append(f"draft assembly failed: {draft}")

    # Manifest should have scenes block (NOT footage block)
    from core.paths import project_dir as get_project_dir
    work_dir = get_project_dir(result["concept_id"])
    manifest_path = work_dir / "manifest.yaml"
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        if not manifest.get("brief"):
            failures.append("manifest missing 'brief' block")
        if not manifest.get("scenes"):
            failures.append("manifest missing 'scenes' block (storyboard regression)")
        if manifest.get("footage"):
            failures.append("manifest has 'footage' block — storyboard pipeline should not write footage")
    else:
        failures.append(f"manifest not found: {manifest_path}")

    return result, failures


# ── Runner ────────────────────────────────────────────────────────────────────

TESTS = [
    ("footage_edit_produces_manifest", "Footage Edit Produces Manifest", test_footage_edit_produces_manifest),
    ("editor_no_ffmpeg_tool", "Editor Tool Set — No ffmpeg", test_editor_no_ffmpeg_tool),
    ("editor_manifest_output_mode", "Editor Prompt — Manifest Mode for footage_edit", test_editor_manifest_output_mode),
    ("write_manifest_scenes_tool", "write_manifest_scenes Tool", test_write_manifest_scenes_tool),
    ("storyboard_pipeline_unchanged", "Storyboard Pipeline Unchanged (Regression Guard)", test_storyboard_pipeline_unchanged),
]


def run_test(name: str, label: str, fn, run_num: int) -> bool:
    """Run a single test. Returns True if passed."""
    print(f"\n{'=' * 60}")
    print(f"TEST {name}: {label} (run #{run_num})")
    print(f"{'=' * 60}\n")

    try:
        result, failures = fn()
    except Exception as e:
        print(f"\n[CRASH] {label} raised: {e}")
        traceback.print_exc()
        return False

    concept_id = result.get("concept_id", "N/A") if isinstance(result, dict) else "N/A"
    status = result.get("status", "N/A") if isinstance(result, dict) else "N/A"

    print(f"\n{'=' * 60}")
    print(f"RESULT: {label} (run #{run_num})")
    print(f"{'=' * 60}")
    if concept_id != "N/A":
        print(f"Concept: {concept_id} | Status: {status}")

    if failures:
        print(f"\n[FAIL] {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        return False
    else:
        print(f"\n[PASS] All checks passed.")
        return True


def main():
    parser = argparse.ArgumentParser(description="Manifest-first refactor tests")
    parser.add_argument("--repeat", type=int, default=1, help="Consecutive runs")
    args = parser.parse_args()

    total_runs = args.repeat
    results = {name: [] for name, _, _ in TESTS}

    for run_num in range(1, total_runs + 1):
        for name, label, fn in TESTS:
            passed = run_test(name, label, fn, run_num)
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
    for name, label, _ in TESTS:
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
            print(f"\nAll tests PASSED.")
    else:
        print(f"\nSome tests FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    main()
