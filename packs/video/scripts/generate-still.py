#!/usr/bin/env python3
"""
Generate a scene still from manifest using Gemini image generation.

Usage:
  generate-still.py --manifest path/to/manifest.json --scene N
  generate-still.py --manifest path/to/manifest.json --scene 1-5

Default multi-scene flow (anchor + parallel):
  1. Generate scene[0] (first anchor)
  2. Generate scene[mid] using scene[0] as reference (second anchor)
  3. Generate all remaining scenes in parallel, each receiving both anchors
     as visual continuity references

Flags:
  --parallel  Skip anchoring — fire all scenes fully concurrently with no
              cross-scene context.
  --chain     Sequential. Each scene receives the prior scene's output as a
              continuity reference, maintaining style/lighting/character
              consistency. Slowest option.
  (default)   Anchor + parallel (see above). Best balance of speed and
              consistency for multi-scene runs. Single-scene invocations
              are always stateless.

--chain and --parallel are mutually exclusive.
"""
import argparse
import string
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api_config import get_gemini_client
from config import get_model_name
from cost_tracker import track_still
from manifest_schema import load_manifest, save_manifest


def get_scene(manifest: dict, idx: int) -> dict:
    for s in manifest.get("scenes", []):
        sidx = s.get("index") or s.get("scene_index")
        if sidx == idx:
            return s
    raise ValueError(f"Scene {idx} not found in manifest")


def build_prompt(scene: dict, manifest: dict) -> str:
    """Build the image generation prompt from manifest + scene data."""
    style_block = manifest.get("style", {})
    style = style_block.get("guidelines", "") if isinstance(style_block, dict) else ""
    char_desc = style_block.get("character_description", "") if isinstance(style_block, dict) else manifest.get("character_description", "")
    frame_desc = scene.get("starting_frame", "")

    aspect_ratio = manifest.get("config", {}).get("aspect_ratio", "9:16")
    if aspect_ratio == "9:16":
        orientation = "vertical portrait 9:16 (taller than wide)"
    elif aspect_ratio == "16:9":
        orientation = "horizontal landscape 16:9 (wider than tall)"
    elif aspect_ratio == "1:1":
        orientation = "square 1:1"
    elif aspect_ratio == "4:5":
        orientation = "vertical portrait 4:5 (taller than wide)"
    else:
        orientation = f"{aspect_ratio} aspect ratio"

    parts = []
    if style:
        parts.append(f"ART STYLE (CRITICAL): {style}")
    if char_desc:
        parts.append(f"CHARACTER: {char_desc}")
    parts.append(f"SCENE: {frame_desc}")
    parts.append(f"RULES: {orientation} image. Single unified scene. No split screen. No text/labels/watermarks. No mirrors.")

    return "\n\n".join(parts)


def generate(manifest_path: str, scene_idx: int, anchor_stills: list[Path] | None = None,
             variant_label: str | None = None, cli_ref_image: Path | None = None):
    manifest = load_manifest(manifest_path)
    scene = get_scene(manifest, scene_idx)
    project_dir = Path(manifest_path).parent

    # Skip text overlay scenes
    if scene.get("type") == "text_overlay" or scene.get("text_overlay"):
        print(f"Scene {scene_idx}: text overlay, skipping image gen")
        return

    # Load reference image if available
    from google.genai import types
    ref_parts = []

    # CLI-supplied reference image (--ref-image) — injected first
    if cli_ref_image and cli_ref_image.exists():
        ref_bytes = cli_ref_image.read_bytes()
        mime = "image/png" if cli_ref_image.suffix.lower() == ".png" else "image/jpeg"
        ref_parts.append(types.Part.from_bytes(data=ref_bytes, mime_type=mime))

    # Support both old references[] and new resources.supplied[]
    resources = manifest.get("resources", {})
    all_refs = resources.get("supplied", []) + manifest.get("references", [])
    char_refs = [r for r in all_refs if r.get("type") in ("character_reference", "character") or r.get("category") == "character"]
    if char_refs:
        ref_path_str = char_refs[0].get("path") or ""
        if ref_path_str:
            ref_path = Path(ref_path_str) if Path(ref_path_str).is_absolute() else project_dir / ref_path_str
            if ref_path.exists():
                ref_bytes = ref_path.read_bytes()
                mime = "image/png" if ref_path.suffix.lower() == ".png" else "image/jpeg"
                ref_parts.append(types.Part.from_bytes(data=ref_bytes, mime_type=mime))

    # Load scene-specific generation refs (brand assets, app screenshots, etc.)
    for ref_path_str in (scene.get("generation_refs") or []):
        resolved = project_dir / ref_path_str if not Path(ref_path_str).is_absolute() else Path(ref_path_str)
        if resolved.exists():
            ref_bytes = resolved.read_bytes()
            mime = "image/png" if resolved.suffix.lower() == ".png" else "image/jpeg"
            ref_parts.append(types.Part.from_bytes(data=ref_bytes, mime_type=mime))

    # Inject anchor stills as visual continuity references
    valid_anchors = [p for p in (anchor_stills or []) if p and p.exists()]
    if valid_anchors:
        for anchor in valid_anchors:
            anchor_bytes = anchor.read_bytes()
            mime = "image/png" if anchor.suffix.lower() == ".png" else "image/jpeg"
            ref_parts.append(types.Part.from_bytes(data=anchor_bytes, mime_type=mime))
        ref_parts.append(
            "CONTINUITY REFERENCE: The above image(s) are anchor scenes from this video. "
            "Maintain visual consistency — same art style, color palette, lighting, and character appearance."
        )

    prompt = build_prompt(scene, manifest)
    ref_parts.append(prompt)

    client = get_gemini_client()

    # Pass aspect ratio via ImageConfig so Gemini generates the correct dimensions
    # (text-only aspect ratio instructions in the prompt are ignored by the API)
    aspect_ratio = manifest.get("config", {}).get("aspect_ratio", "9:16")
    response = client.models.generate_content(
        model=get_model_name("image_generation"),
        contents=ref_parts,
        config=types.GenerateContentConfig(
            response_modalities=["image", "text"],
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
        ),
    )

    # Default target resolutions per aspect ratio
    TARGET_RESOLUTION = {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
    }

    # Extract image
    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            # Use still_path from manifest if set, otherwise default
            still_path = scene.get("still_path")
            if still_path:
                out_path = project_dir / still_path if not Path(still_path).is_absolute() else Path(still_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                out_path = project_dir / f"scene_{scene_idx:03d}.png"
            if variant_label:
                out_path = out_path.with_stem(f"{out_path.stem}-{variant_label}")
            out_path.write_bytes(part.inline_data.data)

            # Upscale to target resolution if Gemini returned smaller dimensions
            config_res = manifest.get("config", {}).get("resolution")
            if config_res and "x" in str(config_res):
                target_w, target_h = (int(x) for x in str(config_res).split("x"))
            else:
                target_w, target_h = TARGET_RESOLUTION.get(aspect_ratio, (1080, 1920))
            from PIL import Image
            img = Image.open(out_path)
            if img.width < target_w or img.height < target_h:
                img = img.resize((target_w, target_h), Image.LANCZOS)
                img.save(out_path)
                print(f"Scene {scene_idx}: upscaled to {target_w}x{target_h}")

            label_tag = f" [{variant_label}]" if variant_label else ""
            print(f"Scene {scene_idx}{label_tag}: saved to {out_path} ({target_w}x{target_h})")
            track_still(project_dir, scene_idx, get_model_name("image_generation"))
            return out_path

    print(f"Scene {scene_idx}: no image in response", file=sys.stderr)
    sys.exit(1)


def parse_scenes(s: str) -> list[int]:
    scenes = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            scenes.extend(range(int(a), int(b) + 1))
        else:
            scenes.append(int(part))
    return scenes


def run_parallel(manifest_path: str, scene_idxs: list[int], anchor_stills: list[Path] | None = None,
                 cli_ref_image: Path | None = None):
    with ThreadPoolExecutor(max_workers=len(scene_idxs)) as executor:
        futures = {
            executor.submit(generate, manifest_path, idx, anchor_stills, None, cli_ref_image): idx
            for idx in scene_idxs
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"Scene {futures[future]}: failed — {e}", file=sys.stderr)
                sys.exit(1)


def run_variants(manifest_path: str, scene_idx: int, n: int, cli_ref_image: Path | None = None):
    """Generate N variants of a single scene concurrently, labelled A, B, C..."""
    labels = list(string.ascii_uppercase[:n])
    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = {
            executor.submit(generate, manifest_path, scene_idx, None, label, cli_ref_image): label
            for label in labels
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"Scene {scene_idx} [{label}]: failed — {e}", file=sys.stderr)
                sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--scene", required=True, help="Scene index or range (e.g. 1 or 1-5)")
    parser.add_argument("--chain", action="store_true", help="Sequential: each scene receives the prior scene as continuity reference")
    parser.add_argument("--parallel", action="store_true", help="Fully concurrent, no cross-scene context")
    parser.add_argument("--variants", type=int, default=1, metavar="N",
                        help="Generate N variants of the scene(s) concurrently, labelled A, B, C... (only with single-scene invocations)")
    parser.add_argument("--ref-image", metavar="PATH",
                        help="Reference image to include in the generation prompt (overrides nothing in manifest — both are sent)")
    args = parser.parse_args()

    if args.chain and args.parallel:
        print("Error: --chain and --parallel are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    cli_ref_image = Path(args.ref_image) if args.ref_image else None
    if cli_ref_image and not cli_ref_image.exists():
        print(f"Error: --ref-image path does not exist: {cli_ref_image}", file=sys.stderr)
        sys.exit(1)

    scenes = parse_scenes(args.scene)

    if args.variants > 1:
        if len(scenes) != 1:
            print("Error: --variants only works with a single scene index", file=sys.stderr)
            sys.exit(1)
        run_variants(args.manifest, scenes[0], args.variants, cli_ref_image=cli_ref_image)

    elif args.parallel:
        # Fully concurrent, no anchoring
        run_parallel(args.manifest, scenes, cli_ref_image=cli_ref_image)

    elif args.chain:
        # Sequential, each scene sees the prior scene's output
        prior_still: Path | None = None
        for idx in scenes:
            result = generate(args.manifest, idx, anchor_stills=[prior_still] if prior_still else None,
                              cli_ref_image=cli_ref_image)
            if isinstance(result, Path):
                prior_still = result

    elif len(scenes) == 1:
        # Single scene — stateless
        generate(args.manifest, scenes[0], cli_ref_image=cli_ref_image)

    else:
        # Default multi-scene: anchor + parallel
        # Step 1: generate first scene
        first_still = generate(args.manifest, scenes[0], cli_ref_image=cli_ref_image)

        # Step 2: generate middle scene using first as reference
        mid_idx = len(scenes) // 2
        anchors: list[Path] = [p for p in [first_still] if isinstance(p, Path)]
        mid_still = generate(args.manifest, scenes[mid_idx], anchor_stills=anchors or None,
                             cli_ref_image=cli_ref_image)

        # Step 3: generate remaining scenes in parallel with both anchors
        anchors = [p for p in [first_still, mid_still] if isinstance(p, Path)]
        remaining = [idx for i, idx in enumerate(scenes) if i != 0 and i != mid_idx]
        if remaining:
            run_parallel(args.manifest, remaining, anchor_stills=anchors or None,
                         cli_ref_image=cli_ref_image)


if __name__ == "__main__":
    main()
