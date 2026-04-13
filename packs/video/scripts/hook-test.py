#!/usr/bin/env python3
"""
hook-test.py — Sample frames from a video's intro and build a storyboard for hook evaluation.

Generates a preview sheet from the first N seconds of a video (default: 5s at 2fps).
The storyboard is designed for visual inspection — either by the calling agent (default)
or by a configured vision API (e.g. Gemini) if models.image_analysis is set in skill-config.yaml.

Usage:
    python3 hook-test.py --input video.mp4
    python3 hook-test.py --manifest manifest.yaml
    python3 hook-test.py --input video.mp4 --duration 3 --fps 4
    python3 hook-test.py --input video.mp4 --output custom_storyboard.jpg
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# Sibling imports
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

import importlib.util  # noqa: E402

def _import_from(filename: str):
    """Import a module from a hyphenated filename in the same directory."""
    spec = importlib.util.spec_from_file_location(
        filename.replace("-", "_").replace(".py", ""),
        SCRIPT_DIR / filename,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_preview_sheet = _import_from("preview-sheet.py")
extract_frames = _preview_sheet.extract_frames
build_preview_sheets = _preview_sheet.build_preview_sheets
get_duration = _preview_sheet.get_duration

from config import get_model_provider, get_model_name  # noqa: E402


HOOK_PROMPT = """You are a social media video strategist evaluating the first few seconds of a video ad.

Below is a storyboard grid showing sequential frames from the video's opening.
Frames are in chronological order, left-to-right and top-to-bottom, with timecodes.

Evaluate the intro hook quality across these dimensions:
1. FIRST FRAME IMPACT — Does frame 1 alone grab attention in a scroll feed? Clear focal point, contrast, or intrigue?
2. VISUAL VARIETY — Do consecutive frames show meaningful change? Static intros lose viewers.
3. IMPLIED MOTION & ENERGY — Even from stills, can you sense movement, camera work, or dynamism?
4. PATTERN INTERRUPT — Anything unexpected, bold, or curiosity-provoking that would stop a thumb-scroller?
5. OPENING CLARITY — Within these frames, is the viewer given a reason to keep watching?

{criteria_block}

Return your evaluation as a JSON object with EXACTLY this structure:
{{
  "verdict": "PASS" or "FAIL",
  "score": <integer 1-10>,
  "reasoning": "<2-3 sentence overall assessment>",
  "dimensions": {{
    "first_frame_impact": <1-10>,
    "visual_variety": <1-10>,
    "motion_energy": <1-10>,
    "pattern_interrupt": <1-10>,
    "opening_clarity": <1-10>
  }},
  "suggestions": ["<actionable improvement 1>", "<actionable improvement 2>"]
}}

Scoring guide:
- 1-3: Weak hook, viewer likely scrolls past
- 4-5: Mediocre, might hold some viewers
- 6-7: Solid hook, good for most platforms
- 8-10: Excellent, strong scroll-stopper

PASS threshold: score >= 6. If score < 6, verdict MUST be "FAIL".

Return ONLY the JSON object, no markdown fences, no commentary."""


def find_video_from_manifest(manifest_path: str) -> str:
    """Find the assembled video from a manifest file."""
    try:
        from manifest_schema import load_manifest
    except ImportError:
        print("ERROR: manifest_schema.py not found in scripts/", file=sys.stderr)
        sys.exit(1)

    try:
        manifest = load_manifest(manifest_path)
    except Exception as e:
        print(f"ERROR: Failed to load manifest {manifest_path}: {e}", file=sys.stderr)
        sys.exit(1)

    project_dir = Path(manifest_path).parent

    # 1. Check compose.output (video-project format)
    compose = manifest.get("compose", {})
    if isinstance(compose, dict) and compose.get("output"):
        candidate = project_dir / compose["output"]
        if candidate.exists():
            return str(candidate)

    # 2. Check layer output fields for .mp4
    for layer in manifest.get("layers", []):
        if isinstance(layer, dict) and layer.get("output"):
            candidate = project_dir / layer["output"]
            if candidate.exists() and candidate.suffix == ".mp4":
                return str(candidate)

    # 3. Search output/ and _work/ for newest .mp4
    search_dirs = [project_dir / "output", project_dir / "_work", project_dir]
    mp4s = []
    for d in search_dirs:
        if d.is_dir():
            mp4s.extend(d.glob("*.mp4"))

    if not mp4s:
        print(f"ERROR: No .mp4 files found near manifest at {project_dir}", file=sys.stderr)
        sys.exit(1)

    newest = max(mp4s, key=lambda p: p.stat().st_mtime)
    print(f"Auto-selected: {newest}", file=sys.stderr)
    return str(newest)


def build_hook_prompt(criteria: str | None) -> str:
    """Build the Gemini vision prompt with optional custom criteria."""
    if criteria:
        criteria_block = f"ADDITIONAL CRITERIA (factor into your evaluation):\n{criteria}"
    else:
        criteria_block = ""
    return HOOK_PROMPT.format(criteria_block=criteria_block)


def evaluate_with_gemini(storyboard_path: str, prompt: str) -> dict:
    """Send storyboard to Gemini vision for hook evaluation."""
    try:
        from api_config import get_gemini_client
        from google.genai import types
    except ImportError as e:
        print(f"ERROR: Gemini dependencies not available: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        client = get_gemini_client()
    except Exception as e:
        print(f"ERROR: Failed to initialize Gemini client: {e}", file=sys.stderr)
        sys.exit(1)

    with open(storyboard_path, "rb") as f:
        image_data = f.read()

    try:
        response = client.models.generate_content(
            model=get_model_name("image_analysis"),
            contents=[
                types.Part.from_bytes(data=image_data, mime_type="image/jpeg"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_modalities=["text"],
                thinking_config=types.ThinkingConfig(thinking_budget=1024),
            ),
        )
    except Exception as e:
        print(f"ERROR: Gemini API call failed: {e}", file=sys.stderr)
        sys.exit(1)

    text = response.text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"WARNING: Gemini returned non-JSON response:", file=sys.stderr)
        print(text, file=sys.stderr)
        return {"verdict": "ERROR", "score": 0, "reasoning": text, "suggestions": []}


def main():
    parser = argparse.ArgumentParser(
        description="QA hook test — sample video intro frames into a storyboard for evaluation"
    )
    parser.add_argument("--input", "-i", help="Input video file")
    parser.add_argument("--manifest", "-m", help="Manifest YAML (auto-finds assembled video)")
    parser.add_argument("--duration", type=float, default=5.0, help="Seconds to sample from start (default: 5)")
    parser.add_argument("--fps", type=float, default=2.0, help="Sample rate in fps (default: 2)")
    parser.add_argument("--criteria", help="Custom evaluation criteria (only used with vision API)")
    parser.add_argument("--output", "-o", help="Storyboard output path (default: auto-named)")
    parser.add_argument("--cols", type=int, default=5, help="Grid columns (default: 5)")
    args = parser.parse_args()

    if not args.input and not args.manifest:
        parser.error("One of --input or --manifest is required")
    if args.input and args.manifest:
        parser.error("Use --input OR --manifest, not both")

    # Resolve video path
    if args.input:
        video_path = args.input
    else:
        video_path = find_video_from_manifest(args.manifest)

    if not os.path.exists(video_path):
        print(f"ERROR: Video not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    # Calculate timestamps for first N seconds
    video_duration = get_duration(video_path)
    sample_duration = min(args.duration, video_duration)
    n_frames = max(1, int(sample_duration * args.fps))
    step = sample_duration / n_frames
    timestamps = [step * i + step / 2 for i in range(n_frames)]

    # Determine output path
    if args.output:
        storyboard_path = args.output
    else:
        base = os.path.splitext(video_path)[0]
        storyboard_path = f"{base}_hook_storyboard.jpg"
    os.makedirs(os.path.dirname(os.path.abspath(storyboard_path)), exist_ok=True)

    print(f"Sampling {n_frames} frames from first {sample_duration:.1f}s of {video_path}", file=sys.stderr)

    # Extract frames and build storyboard
    with tempfile.TemporaryDirectory() as tmpdir:
        frame_paths = extract_frames(video_path, timestamps, tmpdir)
        if not frame_paths:
            print("ERROR: No frames could be extracted", file=sys.stderr)
            sys.exit(1)

        actual_timestamps = timestamps[:len(frame_paths)]
        pages = build_preview_sheets(
            frame_paths, actual_timestamps,
            cols=args.cols, thumb_width=320, max_height=4000,
            output_path=storyboard_path,
        )

    print(f"Storyboard: {pages[0]}", file=sys.stderr)

    # Build result
    result = {
        "storyboard": pages[0],
        "video": video_path,
        "frames_sampled": len(frame_paths),
        "duration_sampled": round(sample_duration, 2),
    }

    # Check if a vision API is configured for evaluation
    provider = get_model_provider("image_analysis", default="agent")

    if provider == "gemini":
        prompt = build_hook_prompt(args.criteria)
        verdict = evaluate_with_gemini(pages[0], prompt)
        result["evaluation"] = verdict
        print(f"Verdict: {verdict.get('verdict', '?')} (score: {verdict.get('score', '?')}/10)", file=sys.stderr)
    elif provider != "agent":
        print(f"WARNING: Unknown image_analysis provider '{provider}', skipping evaluation", file=sys.stderr)
    # agent mode: just output the storyboard, the calling agent reads it

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
