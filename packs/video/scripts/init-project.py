#!/usr/bin/env python3
"""Initialize a new video project — creates folder structure and manifest.yaml.

The agent (Claude) analyzes the script and refs before calling this script,
then passes its decisions as explicit flags. No AI calls happen here — this
script is a pure scaffolder.

Ad project usage (agent decides format/style/voice first):
  init-project.py --slug "Brand-Concept" --script path/to/script.txt
  init-project.py --slug "Brand-Concept" --script script.txt --refs ref.png logo.png
  init-project.py --slug "Brand-Concept" --script script.txt --refs input_folder/ \\
    --format character-ad --brand "MudWater" \\
    --style "Warm photorealistic lifestyle, natural lighting" \\
    --character "Skeleton in a suit, cartoon style" \\
    --voice-id JBFqnCBsd6RMkjVDRZzb --voice-name George

With presets (style defaults apply, CLI flags override):
  init-project.py --slug "Brand-Ad" --script script.txt \\
    --style-preset product-ad --character-preset pixel-demon
  init-project.py --slug "Short-Clip" --format video-project \\
    --style-preset vertical-social-raw

Creative / footage project (no script needed):
  init-project.py --slug "np1-collage" --format video-project
  init-project.py --slug "raw-edit" --format footage-project

Presets are loaded from styles/ and characters/ (custom/ subdirs checked first).
Style presets can declare required services — missing services trigger a warning.

--refs accepts files OR directories (recursively scanned).
Subfolder names (footage/, refs/, brand/, etc.) are used as classification hints.

Output: {paths.output}/{id}-{slug}/ (or {id}/ if no slug)
  manifest.yaml
  input/           ← source files (script, refs, footage, music)
  assets/          ← pipeline-generated intermediates
    assets/stills/ ← generated scene stills (ad formats)
    assets/audio/  ← voiceover and audio files (ad formats)
  output/          ← final deliverables, versioned: {slug}_v{n}.mp4, {slug}_v{n}.fcpxml

Use --from-inbox to move a file from the inbox into the new project's input/.
"""

import argparse
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config, SKILL_ROOT
from api_config import check_keys
from manifest_schema import save_manifest, validate_manifest

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".aiff", ".aif", ".m4a", ".ogg"}

FOLDER_HINTS = {
    "footage":    {"video": "scene_clip",        "image": "style_reference"},
    "clips":      {"video": "scene_clip",        "image": "style_reference"},
    "scenes":     {"video": "scene_clip",        "image": "style_reference"},
    "video":      {"video": "scene_clip",        "image": "style_reference"},
    "videos":     {"video": "scene_clip",        "image": "style_reference"},
    "refs":       {"video": None,                "image": "character_reference"},
    "references": {"video": None,                "image": "character_reference"},
    "character":  {"video": None,                "image": "character_reference"},
    "characters": {"video": None,                "image": "character_reference"},
    "char":       {"video": None,                "image": "character_reference"},
    "brand":      {"video": None,                "image": "brand_asset"},
    "assets":     {"video": None,                "image": "brand_asset"},
    "logo":       {"video": None,                "image": "brand_asset"},
    "music":      {"video": None,                "image": None},
    "audio":      {"video": None,                "image": None},
    "sounds":     {"video": None,                "image": None},
}

AD_FORMATS = {"stills-ad", "character-ad"}
CREATIVE_FORMATS = {"video-project", "footage-project"}

STYLES_DIR = SKILL_ROOT / "styles"
STYLES_CUSTOM_DIR = STYLES_DIR / "custom"
CHARACTERS_DIR = SKILL_ROOT / "characters"
CHARACTERS_CUSTOM_DIR = CHARACTERS_DIR / "custom"


def _resolve_preset(name: str, dirs: list[Path], kind: str) -> dict | None:
    """Find a preset YAML by name, checking custom/ first then shipped."""
    for d in dirs:
        path = d / f"{name}.yaml"
        if path.exists():
            try:
                with open(path) as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                print(f"  Warning: could not read {kind} preset {path}: {e}")
                return None
    return None


def resolve_style(name: str) -> dict | None:
    return _resolve_preset(name, [STYLES_CUSTOM_DIR, STYLES_DIR], "style")


def resolve_character(name: str) -> dict | None:
    return _resolve_preset(name, [CHARACTERS_CUSTOM_DIR, CHARACTERS_DIR], "character")


def check_style_services(style: dict) -> list[str]:
    """Check if required services for a style are configured. Returns list of missing services."""
    requires = style.get("requires", [])
    if not requires:
        return []
    configured = check_keys()
    return [svc for svc in requires if not configured.get(svc, False)]


def _classify_ref(file_path: Path, rel_path: Path) -> str | None:
    ext = file_path.suffix.lower()
    parts = [p.lower() for p in rel_path.parts[:-1]]

    if ext in SUPPORTED_VIDEO_EXTS:
        for folder in reversed(parts):
            if folder in FOLDER_HINTS and FOLDER_HINTS[folder]["video"] is not None:
                return FOLDER_HINTS[folder]["video"]
        return "scene_clip"

    if ext in SUPPORTED_IMAGE_EXTS:
        for folder in reversed(parts):
            if folder in FOLDER_HINTS and FOLDER_HINTS[folder]["image"] is not None:
                return FOLDER_HINTS[folder]["image"]
        name_lower = file_path.name.lower()
        if any(k in name_lower for k in ("character", "char", "ref")):
            return "character_reference"
        if any(k in name_lower for k in ("logo", "brand", "product")):
            return "brand_asset"
        return "style_reference"

    if ext in SUPPORTED_AUDIO_EXTS:
        return "audio"

    return None


def _collect_refs(ref_args: list[str]) -> list[tuple[Path, Path]]:
    collected = []
    for arg in ref_args:
        src = Path(arg)
        if not src.exists():
            print(f"  Warning: ref not found, skipping: {src}")
            continue
        if src.is_dir():
            for child in sorted(src.rglob("*")):
                if child.is_file():
                    collected.append((child, child.relative_to(src)))
        else:
            collected.append((src, Path(src.name)))
    return collected


def _print_ingest_summary(supplied: list[dict], script_name: str | None) -> None:
    scene_clips = [r for r in supplied if r["type"] == "scene_clip"]
    char_refs   = [r for r in supplied if r["type"] == "character_reference"]
    brand_assets= [r for r in supplied if r["type"] == "brand_asset"]
    style_refs  = [r for r in supplied if r["type"] == "style_reference"]
    audio_files = [r for r in supplied if r["type"] == "audio"]

    print("\n── Ingest Review ─────────────────────────────────────")
    if script_name:
        print(f"  [script]     {script_name}")
    for r in char_refs:
        print(f"  [char_ref]   {r['path']}")
    for r in brand_assets:
        print(f"  [brand]      {r['path']}")
    for r in style_refs:
        print(f"  [style_ref]  {r['path']}")
    for r in audio_files:
        print(f"  [audio]      {r['path']}")
    for r in scene_clips:
        print(f"  [scene_clip] {r['path']}")

    if scene_clips:
        print("\n── Scene Clips Found ──────────────────────────────────")
        for r in scene_clips:
            print(f"  {Path(r['path']).name}")
        print("\n  ⚠  Assign each clip to its scene in the manifest before generating stills.")
    elif not script_name:
        print("\n  No input files — add source files to input/ before running pipeline.")

    print("──────────────────────────────────────────────────────\n")


def _scaffold_dirs(project_dir: Path, fmt: str) -> None:
    """Create standard project directory structure.

    All formats use the same top-level layout:
      input/         ← source files (script, refs, footage, music)
      assets/        ← pipeline-generated intermediates
        assets/stills/   ← generated scene stills (ad formats)
        assets/audio/    ← voiceover and audio files (ad formats)
      output/        ← final deliverables (versioned mp4, fcpxml)
    """
    base_dirs = ["input", "output", "assets"]
    for d in base_dirs:
        (project_dir / d).mkdir(parents=True, exist_ok=True)
    if fmt in AD_FORMATS:
        (project_dir / "assets" / "stills").mkdir(exist_ok=True)
        (project_dir / "assets" / "audio").mkdir(exist_ok=True)


def _build_ad_manifest(args, slug: str, supplied: list[dict],
                       style_preset: str | None = None,
                       character_preset: str | None = None) -> dict:
    w, h = args.resolution.split("x")
    aspect = "9:16" if int(w) < int(h) else "16:9"
    project = {
        "id": slug,
        "version": 1,
        "format": args.format,
        "brand": args.brand or slug.split("-")[0],
    }
    if style_preset:
        project["style_preset"] = style_preset
    if character_preset:
        project["character_preset"] = character_preset
    return {
        "project": project,
        "config": {
            "resolution": args.resolution,
            "aspect_ratio": aspect,
            "fps": args.fps,
        },
        "style": {
            "guidelines": args.style or "",
            "character_description": args.character or "",
        },
        "resources": {
            "supplied": supplied,
            "generated": [{"type": "voiceover", "status": "pending", "path": None}],
        },
        "voice": {
            "voice_id": args.voice_id or "JBFqnCBsd6RMkjVDRZzb",
            "voice_name": args.voice_name or "George",
            "provider": "elevenlabs",
        },
        "voiceover": {
            "audio_file": None,
            "vo_manifest": None,
            "duration_s": None,
        },
        "scenes": [],
    }


def _build_creative_manifest(args, slug: str, supplied: list[dict],
                             style_preset: str | None = None,
                             character_preset: str | None = None) -> dict:
    w, h = args.resolution.split("x")
    aspect = "9:16" if int(w) < int(h) else "16:9"
    project = {
        "id": slug,
        "description": "",
    }
    if style_preset:
        project["style_preset"] = style_preset
    if character_preset:
        project["character_preset"] = character_preset
    return {
        "format": args.format,
        "project": project,
        "config": {
            "resolution": args.resolution,
            "aspect_ratio": aspect,
            "fps": args.fps,
            "duration": None,
        },
        "sources": [
            {"id": r["path"].split("/")[-1].rsplit(".", 1)[0], "type": "video", "path": r["path"]}
            for r in supplied if r["type"] in ("scene_clip", "audio")
        ],
        "layers": [],
        "compose": {
            "method": "overlay",
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Initialize a new video project.")
    parser.add_argument("--slug",       default=None,   help="Project slug (optional — project gets a numeric ID either way)")
    parser.add_argument("--format",     default=None,   help="Project format (stills-ad, character-ad, video-project, footage-project)")
    parser.add_argument("--script",     default=None,   help="Path to script file (.txt or .md) — required for ad formats")
    parser.add_argument("--refs",       nargs="*", default=[], help="Reference files or directories")
    parser.add_argument("--from-inbox", nargs="*", default=[], dest="from_inbox",
                        help="Files to move from inbox into the new project's input/")

    # Agent-supplied creative decisions (replaces Gemini analysis)
    parser.add_argument("--brand",      default=None,   help="Brand name")
    parser.add_argument("--style",      default=None,   help="Visual style guidelines (agent-written)")
    parser.add_argument("--character",  default=None,   help="Character description (agent-written)")
    parser.add_argument("--voice-id",   default=None,   help="ElevenLabs voice ID")
    parser.add_argument("--voice-name", default=None,   help="ElevenLabs voice name")

    parser.add_argument("--style-preset",     default=None,   help="Style preset name (from styles/ or styles/custom/)")
    parser.add_argument("--character-preset", default=None,   help="Character preset name (from characters/ or characters/custom/)")

    parser.add_argument("--resolution", default=None)
    parser.add_argument("--fps",        type=int, default=None)
    args = parser.parse_args()

    # Resolve style preset — apply defaults before CLI overrides
    style_data = None
    if args.style_preset:
        style_data = resolve_style(args.style_preset)
        if style_data is None:
            available = [p.stem for d in [STYLES_CUSTOM_DIR, STYLES_DIR] if d.exists() for p in d.glob("*.yaml")]
            print(f"ERROR: Style preset '{args.style_preset}' not found.")
            print(f"  Available: {', '.join(sorted(set(available)))}")
            sys.exit(1)

        # Check service dependencies
        missing = check_style_services(style_data)
        if missing:
            print(f"WARNING: Style '{args.style_preset}' requires services not configured: {', '.join(missing)}")
            print("  The project will be created, but some pipeline steps will fail.")
            print("  Run: python3 scripts/api_config.py  to check your setup.")
            print()

        # Apply style config as defaults (CLI flags override)
        style_cfg = style_data.get("config", {})
        if args.resolution is None and "resolution" in style_cfg:
            args.resolution = style_cfg["resolution"]
        if args.fps is None and "fps" in style_cfg:
            args.fps = style_cfg["fps"]

    # Final defaults if nothing set
    if args.resolution is None:
        args.resolution = "1080x1920"
    if args.fps is None:
        args.fps = 30

    # Resolve character preset
    character_data = None
    if args.character_preset:
        character_data = resolve_character(args.character_preset)
        if character_data is None:
            available = [p.stem for d in [CHARACTERS_CUSTOM_DIR, CHARACTERS_DIR] if d.exists() for p in d.glob("*.yaml")]
            print(f"ERROR: Character preset '{args.character_preset}' not found.")
            print(f"  Available: {', '.join(sorted(set(available)))}")
            sys.exit(1)

    # Resolve format
    fmt = args.format or "stills-ad"
    is_ad = fmt in AD_FORMATS

    # Validate: ad formats need a script OR inbox files (inbox = stills-only, no VO script needed)
    # Also defer this check until after inbox auto-scan so auto-scanned files count
    _script_check_deferred = is_ad and not args.script

    if args.script and not Path(args.script).exists():
        print(f"ERROR: Script not found: {args.script}")
        sys.exit(1)

    # Warn if agent hasn't filled in style (ad projects only)
    if is_ad and not args.style:
        print("  Warning: --style not provided. Edit manifest.yaml style.guidelines before generating stills.")

    # Resolve output root
    cfg = load_config()
    output_root = Path(cfg.get("paths", {}).get("output", "~/Movies/VideoProduction")).expanduser()

    # Auto-scan inbox — if paths.inbox is configured and has files, pull them all in
    inbox_path = cfg.get("paths", {}).get("inbox")
    if inbox_path and not args.from_inbox:
        inbox_dir = Path(inbox_path).expanduser()
        if inbox_dir.exists():
            inbox_files = sorted(f for f in inbox_dir.rglob("*") if f.is_file())
            if inbox_files:
                print(f"\n── Inbox ({inbox_dir}) ────────────────────────────────")
                for f in inbox_files:
                    print(f"  {f.relative_to(inbox_dir)}")
                print(f"  → Moving {len(inbox_files)} file(s) into project input/")
                print("──────────────────────────────────────────────────────")
                args.from_inbox = [str(f) for f in inbox_files]

    # Now check script requirement — after inbox auto-scan has had a chance to populate from_inbox
    if _script_check_deferred and not args.from_inbox:
        print(f"ERROR: --script is required for format '{fmt}' (unless inbox has files)")
        print("  Provide --script <path> for VO-driven ads, or drop files in the inbox.")
        sys.exit(1)

    # Get next project ID from registry
    try:
        from registry import next_id as get_next_id
        project_id = get_next_id()
    except Exception as e:
        print(f"Warning: could not get next ID from registry ({e}), using 0")
        project_id = 0

    # Build directory name: {id}-{slug} or just {id}
    slug = args.slug
    if slug:
        dir_name = f"{project_id}-{slug}"
    else:
        dir_name = str(project_id)
        slug = dir_name  # use ID as slug in manifest
    args.slug = slug

    project_dir = output_root / dir_name

    if project_dir.exists():
        print(f"Project directory already exists: {project_dir}")
        print("This shouldn't happen with auto-assigned IDs. Check the registry.")
        sys.exit(1)

    # Scaffold directories
    _scaffold_dirs(project_dir, fmt)
    input_dir = project_dir / "input"

    # Copy script
    supplied = []
    script_name = None
    if args.script:
        script_src = Path(args.script)
        script_text = script_src.read_text().strip()
        shutil.copy(script_src, input_dir / script_src.name)
        supplied.append({"path": f"input/{script_src.name}", "type": "script"})
        script_name = script_src.name

    # Move inbox files into input/
    # Resolve bare filenames against the configured inbox path
    inbox_dir_cfg = cfg.get("paths", {}).get("inbox")
    for inbox_file in args.from_inbox:
        src = Path(inbox_file).expanduser()
        if not src.exists() and inbox_dir_cfg:
            # Try resolving as a filename within the configured inbox
            candidate = Path(inbox_dir_cfg).expanduser() / Path(inbox_file).name
            if candidate.exists():
                src = candidate
        if not src.exists():
            print(f"  Warning: inbox file not found, skipping: {src}")
            continue
        dest = input_dir / src.name
        try:
            shutil.move(str(src), str(dest))
            rtype = _classify_ref(dest, Path(dest.name))
            if rtype:
                supplied.append({"path": f"input/{dest.name}", "type": rtype})
            print(f"  Moved from inbox: {src.name}")
        except Exception as e:
            print(f"  Warning: could not move {src.name} from inbox: {e}")

    # Collect and copy refs
    for abs_path, rel_path in _collect_refs(args.refs):
        rtype = _classify_ref(abs_path, rel_path)
        if rtype is None:
            print(f"  Skipping unsupported file type: {abs_path.name}")
            continue
        dest = input_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy(abs_path, dest)
        except Exception as e:
            print(f"  Warning: could not copy {abs_path.name}: {e}")
            continue
        supplied.append({"path": f"input/{rel_path}", "type": rtype})

    # Build manifest
    print(f"Initializing project {project_id}: {args.slug} (format: {fmt})")
    if is_ad:
        manifest = _build_ad_manifest(args, args.slug, supplied,
                                      style_preset=args.style_preset,
                                      character_preset=args.character_preset)
    else:
        manifest = _build_creative_manifest(args, args.slug, supplied,
                                            style_preset=args.style_preset,
                                            character_preset=args.character_preset)

    manifest_path = project_dir / "manifest.yaml"
    save_manifest(manifest, str(manifest_path))
    validate_manifest(manifest, str(manifest_path))

    # Register
    try:
        from registry import register
        register(str(manifest_path))
    except Exception as e:
        print(f"  Note: could not update registry ({e})")

    print(f"\nProject {project_id} created: {project_dir}")
    print(f"  id:        {project_id}")
    print(f"  format:    {fmt}")
    if args.style_preset:
        print(f"  style:     {args.style_preset}")
    if args.character_preset:
        print(f"  character: {args.character_preset}")
    if is_ad:
        brand = args.brand or args.slug.split("-")[0]
        voice_name = args.voice_name or "George"
        print(f"  brand:     {brand}")
        print(f"  voice:     {voice_name}")
        print(f"  style set: {'yes' if args.style else 'NO — edit manifest.yaml before generating stills'}")

    _print_ingest_summary(supplied, script_name)

    if is_ad:
        print(f"Next: python3 plan-scenes.py --manifest {manifest_path}")
    else:
        print(f"Next: edit manifest.yaml sources/layers, then run your pipeline scripts.")
        print(f"  Add source files to: {project_dir}/input/")


if __name__ == "__main__":
    main()
