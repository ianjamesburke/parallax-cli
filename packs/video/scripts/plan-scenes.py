#!/usr/bin/env python3
"""Break a script into scenes and write them to the manifest.

Uses Gemini to plan scene cuts, visual descriptions, and estimated timing.
Prints a scene table for review — always confirm before generating stills.

Usage:
  plan-scenes.py --manifest path/to/manifest.yaml
  plan-scenes.py --manifest path/to/manifest.yaml --wpm 180
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api_config import get_gemini_client
from config import get_model_name
from manifest_schema import load_manifest, save_manifest, validate_manifest, GENERATION_REF_WHITELIST

# Estimated words per minute at natural ad VO speed (~1.2–1.3x)
DEFAULT_WPM = 180

# Scenes at or under this duration get Ken Burns (no video gen needed)
KB_THRESHOLD_S = 2.6

SCENE_PLAN_PROMPT = """You are a short-form video editor breaking an ad script into scenes.

SCRIPT:
{script}

STYLE GUIDELINES:
{style}

REFERENCES: {refs}

ESTIMATED TOTAL DURATION: {total_s:.1f}s at {wpm} words/minute

RULES:

SCENE SPLITTING:
- One scene per sentence is the default.
- Split long sentences (25+ words) only at a clear clause boundary. Both halves must be complete thoughts.
- Two consecutive short sentences (<8 words each) sharing the same visual MAY be combined.
- Never split mid-clause. Never manufacture short scenes.
- Short scenes (≤{kb_threshold}s) will render as Ken Burns stills — mark ken_burns: true. Great for punchy one-liners.
- Scene 1 is always the hook — full sentence, high energy, ken_burns: false regardless of length.

HOOK PACING (NON-NEGOTIABLE):
- The first 5 seconds MUST contain at least 3 distinct scene cuts. A single slow establishing shot will lose the viewer.
- Scenes 1–3 should be ≤2.5s each. Short, punchy, high energy.
- If the opening VO lines are long (>2.5s each), split the longest at a clause boundary to get an additional cut.
- Do not open with a mood-setting shot. Open with action, tension, or a relatable problem.

PACING:
- Early scenes (1–3): bold imagery, strong emotion, high energy — SHORT cuts.
- Later scenes can breathe — longer holds, more detail.
- Scenes should feel dynamic through CHARACTER AND OBJECT ACTION, not camera orbits.
  Avoid: "camera orbits", "sweeping pan". Prefer: "hand slams down", "notification floods screen".

SCENE INDEPENDENCE:
- Every scene is a jump cut. No visual continuity between scenes.
- Each scene stands alone — different shot, angle, possibly different setting.
- Not every scene needs the main character. B-roll scenes (objects, environments) support the VO.
- Mark b_roll: true for scenes that don't feature the character.

DESCRIPTIONS (two fields per scene):
1. starting_frame: What the viewer sees at frame 1. Specific enough to generate an image.
   For ken_burns scenes, make starting_frame ALL-ENCOMPASSING — it IS the entire visual.
2. action: What happens dynamically — character motion, object movement, reveals.
   For ken_burns scenes, action = camera move only (e.g., "slow zoom in").

Keep descriptions concrete. Not "man looks worried" but "skeleton character stares at phone showing red alert, brow furrowed, dark room lit by screen glow."
Keep it grounded. Real phones, real rooms, real everyday objects. No holographic UI, no sci-fi unless the brand requires it.

Return ONLY valid JSON:
{{
  "scenes": [
    {{
      "index": 1,
      "vo_text": "exact script text for this scene",
      "starting_frame": "detailed still image description",
      "action": "what happens dynamically",
      "estimated_start_s": 0.0,
      "estimated_end_s": 3.5,
      "duration_s": 3.5,
      "ken_burns": false,
      "b_roll": false
    }}
  ]
}}"""


def estimate_duration(text: str, wpm: int) -> float:
    return round((len(text.split()) / wpm) * 60, 1)


def main():
    parser = argparse.ArgumentParser(description="Plan scenes from manifest script")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--wpm", type=int, default=DEFAULT_WPM, help="Words per minute estimate")
    parser.add_argument("--force", action="store_true", help="Re-plan even if scenes already exist")
    parser.add_argument("--print-prompt", action="store_true",
                        help="Print the scene planning prompt to stdout and exit. Use when planning with the agent instead of Gemini.")
    parser.add_argument("--from-json", metavar="PATH",
                        help="Ingest pre-generated scenes from a JSON file (output of agent planning). Skips LLM call.")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    project_dir = Path(args.manifest).parent

    if manifest.get("scenes") and not args.force:
        print("Scenes already planned. Use --force to re-plan.")
        _print_table(manifest["scenes"])
        return

    # Extract script text
    script_text = ""
    for r in manifest.get("resources", {}).get("supplied", []):
        if r.get("type") == "script":
            script_path = project_dir / r["path"]
            if script_path.exists():
                script_text = script_path.read_text().strip()
                break
    if not script_text:
        # Fall back to vo_text fields if already partially filled
        scenes = manifest.get("scenes", [])
        if scenes:
            script_text = " ".join(s.get("vo_text", "") for s in scenes)
    if not script_text:
        print("ERROR: No script text found. Add a resource with type: script.")
        sys.exit(1)

    style = manifest.get("style", {})
    style_guidelines = style.get("guidelines", "") if isinstance(style, dict) else ""
    char_desc = style.get("character_description", "") if isinstance(style, dict) else ""
    if char_desc:
        style_guidelines = f"{style_guidelines}\nCHARACTER: {char_desc}".strip()

    refs = []
    for r in manifest.get("resources", {}).get("supplied", []):
        if r.get("type") in ("character_reference", "style_reference", "brand_asset"):
            refs.append(f"{r.get('type')}: {r.get('path', '')}")
    refs_str = ", ".join(refs) or "None"

    total_s = estimate_duration(script_text, args.wpm)

    prompt = SCENE_PLAN_PROMPT.format(
        script=script_text,
        style=style_guidelines or "(not set — edit manifest.yaml style.guidelines)",
        refs=refs_str,
        total_s=total_s,
        wpm=args.wpm,
        kb_threshold=KB_THRESHOLD_S,
    )

    # --print-prompt: output prompt and exit so the agent can plan scenes directly
    if args.print_prompt:
        print(prompt)
        print("\n# ---")
        print("# Agent: generate scenes JSON matching the schema in the prompt above.")
        print(f"# Then write to a file and run:")
        print(f"#   python3 plan-scenes.py --manifest {args.manifest} --from-json /path/to/scenes.json")
        return

    print(f"Planning scenes for {manifest.get('project', {}).get('id', 'project')}...")
    print(f"  Script: {len(script_text.split())} words, estimated {total_s:.1f}s")

    # --from-json: ingest agent-generated scenes, skip LLM call
    if args.from_json:
        try:
            raw = Path(args.from_json).read_text()
            data = json.loads(raw)
            # Accept either {"scenes": [...]} or a bare list
            scenes = data.get("scenes", data) if isinstance(data, dict) else data
            print(f"  Loaded {len(scenes)} scenes from {args.from_json}")
        except Exception as e:
            print(f"ERROR: Could not load scenes from {args.from_json}: {e}", file=sys.stderr)
            sys.exit(1)

    # Gemini planning
    else:
        from api_config import check_keys
        if not check_keys().get("gemini"):
            print("Gemini not configured. Options:")
            print("  1. Set AI_VIDEO_GEMINI_KEY and re-run")
            print(f"  2. Run with --print-prompt, plan scenes yourself, save JSON, re-run with --from-json")
            print(f"\nExample:")
            print(f"  python3 plan-scenes.py --manifest {args.manifest} --print-prompt")
            print(f"  # agent generates scenes.json")
            print(f"  python3 plan-scenes.py --manifest {args.manifest} --from-json scenes.json")
            sys.exit(1)

        try:
            from google.genai import types
            client = get_gemini_client()
            response = client.models.generate_content(
                model=get_model_name("scene_planning"),
                contents=[prompt],
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            text = response.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(text)
            scenes = result.get("scenes", [])
        except Exception as e:
            print(f"ERROR: Scene planning failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Collect whitelisted visual resources to assign as generation refs
    vis_ref_paths = [
        r["path"]
        for r in manifest.get("resources", {}).get("supplied", [])
        if r.get("type") in GENERATION_REF_WHITELIST and r.get("path")
    ]

    # Normalize scene fields
    for scene in scenes:
        idx = scene.get("index", 0)
        # type field: ken_burns flag → type, default normal
        if "type" not in scene:
            scene["type"] = "ken_burns" if scene.get("ken_burns") else "normal"
        scene.setdefault("still_path", f"assets/stills/scene_{idx:03d}.png")
        scene.setdefault("still_status", "pending")
        # Normalize timing: use start_s/end_s (remove estimated_ prefix)
        if "estimated_start_s" in scene and "start_s" not in scene:
            scene["start_s"] = scene.pop("estimated_start_s")
        if "estimated_end_s" in scene and "end_s" not in scene:
            scene["end_s"] = scene.pop("estimated_end_s")
        scene.setdefault("start_s", 0.0)
        scene.setdefault("end_s", scene.get("duration_s", 3.0))
        # Assign visual generation refs (can be edited per-scene in manifest)
        scene.setdefault("generation_refs", vis_ref_paths)

    manifest["scenes"] = scenes
    save_manifest(manifest, args.manifest)
    validate_manifest(manifest, args.manifest)

    # Update registry
    try:
        from registry import register
        register(args.manifest)
    except Exception:
        pass

    print(f"\nPlanned {len(scenes)} scenes:")
    _print_table(scenes)

    # Surface any unassigned footage/stills so the user can wire them up manually
    supplied = manifest.get("resources", {}).get("supplied", [])
    REF_TYPES = {"character_reference", "brand_asset", "style_reference",
                 "background_reference", "app_screenshot", "website_screenshot", "script"}
    footage = [r for r in supplied
               if r.get("path") and Path(r["path"]).suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}]
    stills  = [r for r in supplied
               if r.get("path") and Path(r["path"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
               and r.get("type") not in REF_TYPES]

    if footage or stills:
        print(f"\n── Pre-supplied assets (not yet assigned to scenes) ──────────────")
        for r in footage:
            print(f"  [clip]   {r['path']}")
        for r in stills:
            print(f"  [still]  {r['path']}")
        print(f"\n  → Review the scene table above and assign manually in manifest.yaml:")
        print(f"      scene N: video_clip: <path>   (for footage clips)")
        print(f"      scene N: still_path: <path>, still_status: ready   (for pre-made stills)")

    print(f"\nSTOP — review the scene table above before generating stills.")
    print(f"Next: python3 generate-voiceover.py --manifest {args.manifest}")
    print(f"  OR: python3 generate-still.py --manifest {args.manifest} --scene 1")


def _print_table(scenes: list):
    print(f"\n{'#':>3}  {'Dur':>5}  {'KB':>3}  {'VO text':<55}  Visual")
    print("-" * 120)
    for s in scenes:
        idx = s.get("index", "?")
        dur = s.get("duration_s", 0)
        kb = "KB" if s.get("ken_burns") else "  "
        vo = (s.get("vo_text", ""))[:54]
        visual = (s.get("starting_frame", ""))[:50]
        print(f"{idx:>3}  {dur:>5.1f}  {kb:>3}  {vo:<55}  {visual}")


if __name__ == "__main__":
    main()
