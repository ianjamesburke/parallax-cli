#!/usr/bin/env python3
"""
Manifest schema validation and I/O helpers.

Every pipeline script should import and use:
    from manifest_schema import load_manifest, save_manifest, validate_manifest

CLI usage:
    python3 manifest_schema.py validate manifest.yaml
    python3 manifest_schema.py convert old_manifest.json --output manifest.yaml
"""
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Valid options — single source of truth for all enums
# ---------------------------------------------------------------------------

SCENE_TYPES = ["normal", "ken_burns", "still", "text_overlay", "video", "effect_overlay"]

PROJECT_FORMATS = [
    "stills-ad",         # Script only → generate stills + Ken Burns, no VO required
    "character-ad",      # Script + character ref → generate VO, stills, assemble
    "vo-broll",          # Recorded VO + footage → align, cut, assemble
    "vo-generated",      # Recorded VO + style refs → align, generate visuals, assemble
    "footage-ad",        # Script + footage → generate VO, cut footage, assemble
    "explainer",         # Script + diagrams → generate VO, stills + text, assemble
    "video-project",     # Creative composition — layers, effects, audio, interactive steps
]

RESOURCE_TYPES = [
    "script",
    "character_reference",
    "brand_asset",
    "footage",
    "voiceover_recording",
    "background_reference",
    "style_reference",
    "diagram",
    "scene_clip",         # Pre-supplied video clip covering a specific scene
    "app_screenshot",     # App UI screenshot
    "website_screenshot", # Website / landing page screenshot
]

# Resource types valid as visual reference images for Gemini image generation.
# Character references are always included separately — these are per-scene additions.
GENERATION_REF_WHITELIST = {
    "brand_asset",
    "background_reference",
    "style_reference",
    "app_screenshot",
    "website_screenshot",
}

# ---------------------------------------------------------------------------
# video-project format — layer types, effect types, compose methods
# ---------------------------------------------------------------------------

LAYER_TYPES = [
    "animation",      # HTML template → render-animation.py
    "crop",           # Region extraction → extract-region.py (may need interactive refinement)
    "effect",         # Post-processing filter applied to another layer (e.g. bloom, grain)
    "caption",        # Text overlay → generate-caption.py
    "video",          # Raw video file or clip
    "image",          # Static image file
    "audio",          # Audio extraction or file
    # Raw footage / editing layer types (documented in manifest-spec.md)
    "index",          # Clip index → index-clip.py
    "assemble",       # Clip assembly → assemble-clips.py
    "extract-region", # Region extraction with bg → extract-region.py
    "composite",      # Multi-source composite (e.g. webcam + background)
]

EFFECT_TYPES = [
    "blur",         # Gaussian/box blur
    "freeze-zoom",  # Freeze-frame zoom → freeze-zoom-intro.py
]

COMPOSE_METHODS = [
    "vertical",      # Vertical video composition → compose-vertical.py
    "overlay",       # Simple layer stack (bottom to top)
    "concat",        # Sequential clips → assemble-clips.py
]

# What each format requires in resources.supplied
# stills-ad requires nothing — all inputs are optional
FORMAT_REQUIRED_RESOURCES = {
    "character-ad": ["script", "character_reference"],
    "vo-broll": ["voiceover_recording", "footage"],
    "vo-generated": ["voiceover_recording"],
    "footage-ad": ["script", "footage"],
    "explainer": ["script"],
}


# ---------------------------------------------------------------------------
# YAML helpers — preserve None as null, use clean indentation
# ---------------------------------------------------------------------------

def _yaml_dump(data: dict) -> str:
    return yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_manifest(path: str) -> dict:
    """
    Load a manifest from YAML or JSON.
    If the file is .json, auto-converts to the new YAML schema structure.
    Validates after loading. Returns the manifest dict.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")

    text = p.read_text(encoding="utf-8")

    if p.suffix == ".json":
        raw = json.loads(text)
        manifest = _migrate_json(raw)
    else:
        manifest = yaml.safe_load(text)

    errors = validate_manifest(manifest, path=str(p))
    if errors:
        print(f"Manifest validation warnings for {path}:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)

    return manifest


def save_manifest(manifest: dict, path: str):
    """
    Validate then write manifest to YAML.
    Always writes YAML regardless of original format.
    """
    errors = validate_manifest(manifest, path=path)
    if errors:
        print(f"Manifest validation warnings (saving anyway):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)

    p = Path(path)
    # If path is .json, silently redirect to .yaml
    if p.suffix == ".json":
        p = p.with_suffix(".yaml")

    p.write_text(_yaml_dump(manifest), encoding="utf-8")


def validate_manifest(manifest: dict, path: str = "") -> list[str]:
    """
    Validate manifest structure. Returns list of error/warning strings.
    Empty list = valid. Dispatches to format-specific validator based on
    top-level 'format' field or project.format.
    """
    top_format = manifest.get("format", "")

    if top_format == "video-project":
        return _validate_video_project(manifest, path)
    elif top_format == "clip-index":
        return _validate_clip_index(manifest, path)
    else:
        return _validate_ad_manifest(manifest, path)


def _validate_ad_manifest(manifest: dict, path: str = "") -> list[str]:
    """Validate ad-pipeline manifest (character-ad, vo-broll, etc.)."""
    errors = []
    label = f"[{path}] " if path else ""

    # --- project block ---
    project = manifest.get("project")
    if not project:
        errors.append(f"{label}Missing 'project' block")
    else:
        if not project.get("id"):
            errors.append(f"{label}Missing project.id")

    # --- config block ---
    config = manifest.get("config")
    if not config:
        errors.append(f"{label}Missing 'config' block")
    else:
        resolution = config.get("resolution", "")
        if not re.match(r"^\d+x\d+$", str(resolution)):
            errors.append(f"{label}config.resolution must be WxH format (e.g. '1080x1920'), got: {resolution!r}")

    # --- format-specific resource check ---
    project_format = (project or {}).get("format", "")
    resources = manifest.get("resources", {})
    supplied = resources.get("supplied", [])
    supplied_types = [r.get("type") for r in supplied]

    if project_format and project_format not in PROJECT_FORMATS:
        errors.append(f"{label}project.format '{project_format}' not recognized — must be one of: {', '.join(PROJECT_FORMATS)}")

    if project_format in FORMAT_REQUIRED_RESOURCES:
        for req_type in FORMAT_REQUIRED_RESOURCES[project_format]:
            if req_type not in supplied_types:
                errors.append(f"{label}Format '{project_format}' requires '{req_type}' in resources.supplied — add a resource with type: {req_type}")

    # --- resource type validation ---
    for r in supplied:
        rtype = r.get("type")
        if rtype and rtype not in RESOURCE_TYPES:
            errors.append(f"{label}Unknown resource type: '{rtype}' — must be one of: {', '.join(RESOURCE_TYPES)}")

    # --- file path warnings ---
    manifest_dir = Path(path).parent if path else Path(".")
    for r in supplied:
        rpath = r.get("path")
        if rpath and not (manifest_dir / rpath).exists():
            errors.append(f"{label}WARNING: supplied resource not found: {rpath}")

    # --- scenes validation ---
    if "scenes_dir" in manifest:
        if not manifest.get("scene_count"):
            errors.append(f"{label}scenes_dir set but scene_count missing")
    elif "scenes" in manifest:
        scenes = manifest["scenes"]
        if not isinstance(scenes, list):
            errors.append(f"{label}scenes must be a list")
        else:
            for i, scene in enumerate(scenes):
                pos = f"scene[{i}]"
                stype = scene.get("type")
                if stype and stype not in SCENE_TYPES:
                    errors.append(f"{label}{pos} invalid type: '{stype}' — must be one of: {', '.join(SCENE_TYPES)}")
                for field in ("index", "vo_text", "starting_frame", "type"):
                    if scene.get(field) is None:
                        if field == "type":
                            errors.append(f"{label}{pos} missing required field: '{field}' — add `type: normal` (options: {', '.join(SCENE_TYPES)})")
                        elif field == "vo_text":
                            errors.append(f"{label}{pos} missing required field: '{field}' — add `vo_text: ''`")
                        elif field == "starting_frame":
                            errors.append(f"{label}{pos} missing required field: '{field}' — add `starting_frame: ''`")
                        elif field == "index":
                            errors.append(f"{label}{pos} missing required field: '{field}' — add `index: N` (sequential starting from 1)")
                        else:
                            errors.append(f"{label}{pos} missing required field: '{field}'")

            indices = [s.get("index") for s in scenes if s.get("index") is not None]
            if indices:
                expected = list(range(1, len(indices) + 1))
                if sorted(indices) != expected:
                    errors.append(f"{label}Scene indices are not sequential 1..N. Found: {sorted(indices)}")

    return errors


# ---------------------------------------------------------------------------
# video-project format validation
# ---------------------------------------------------------------------------

def _validate_video_project(manifest: dict, path: str = "") -> list[str]:
    """Validate a video-project manifest — layers, effects, audio, compose."""
    errors = []
    label = f"[{path}] " if path else ""

    # --- project block ---
    project = manifest.get("project")
    if not project:
        errors.append(f"{label}Missing 'project' block")
    else:
        if not project.get("id"):
            errors.append(f"{label}Missing project.id")

    # --- config block ---
    config = manifest.get("config")
    if not config:
        errors.append(f"{label}Missing 'config' block")
    else:
        resolution = config.get("resolution", "")
        if not re.match(r"^\d+x\d+$", str(resolution)):
            errors.append(f"{label}config.resolution must be WxH format, got: {resolution!r}")
        if "duration" not in config:
            errors.append(f"{label}config.duration is required for video-project (use null if unknown at manifest-write time)")

    # --- sources ---
    sources = manifest.get("sources", [])
    if not isinstance(sources, list):
        errors.append(f"{label}sources must be a list")
    else:
        source_ids = set()
        for i, src in enumerate(sources):
            pos = f"sources[{i}]"
            sid = src.get("id")
            if not sid:
                errors.append(f"{label}{pos} missing 'id'")
            elif sid in source_ids:
                errors.append(f"{label}{pos} duplicate source id: '{sid}'")
            else:
                source_ids.add(sid)
            if not src.get("type"):
                errors.append(f"{label}{pos} missing 'type'")
            if not src.get("path"):
                errors.append(f"{label}{pos} missing 'path'")

    # --- layers ---
    layers = manifest.get("layers", [])
    if not isinstance(layers, list):
        errors.append(f"{label}layers must be a list")
    elif not layers:
        errors.append(f"{label}layers is empty — need at least one layer")
    else:
        layer_ids = set()
        for i, layer in enumerate(layers):
            pos = f"layers[{i}]"
            lid = layer.get("id")
            if not lid:
                errors.append(f"{label}{pos} missing 'id'")
            elif lid in layer_ids:
                errors.append(f"{label}{pos} duplicate layer id: '{lid}'")
            else:
                layer_ids.add(lid)

            ltype = layer.get("type")
            if not ltype:
                errors.append(f"{label}{pos} missing 'type'")
            elif ltype not in LAYER_TYPES:
                errors.append(f"{label}{pos} unknown type: '{ltype}' — must be one of: {', '.join(LAYER_TYPES)}")

            # Type-specific checks
            if ltype == "effect":
                if not layer.get("target"):
                    errors.append(f"{label}{pos} effect layer needs 'target' (id of layer to apply to)")
                elif layer.get("target") not in layer_ids:
                    errors.append(f"{label}{pos} target '{layer.get('target')}' not found in preceding layers")
                effect = layer.get("effect")
                if not effect:
                    errors.append(f"{label}{pos} effect layer needs 'effect' field")
                elif effect not in EFFECT_TYPES:
                    errors.append(f"{label}{pos} unknown effect: '{effect}' — must be one of: {', '.join(EFFECT_TYPES)}")

            if ltype == "crop":
                if not layer.get("source"):
                    errors.append(f"{label}{pos} crop layer needs 'source' (source id)")

            if ltype == "animation":
                if not layer.get("template"):
                    errors.append(f"{label}{pos} animation layer needs 'template'")

    # --- compose (optional but validated if present) ---
    compose = manifest.get("compose")
    if compose:
        method = compose.get("method")
        if method and method not in COMPOSE_METHODS:
            errors.append(f"{label}compose.method '{method}' — must be one of: {', '.join(COMPOSE_METHODS)}")
    return errors


# ---------------------------------------------------------------------------
# clip-index format validation (basic)
# ---------------------------------------------------------------------------

def _validate_clip_index(manifest: dict, path: str = "") -> list[str]:
    """Validate a clip-index manifest."""
    errors = []
    label = f"[{path}] " if path else ""

    if not manifest.get("source"):
        errors.append(f"{label}Missing 'source' field")
    if manifest.get("clips") is not None and not isinstance(manifest.get("clips"), list):
        errors.append(f"{label}clips must be a list")
    if manifest.get("silences") is not None and not isinstance(manifest.get("silences"), list):
        errors.append(f"{label}silences must be a list")

    return errors


# ---------------------------------------------------------------------------
# JSON → YAML migration
# ---------------------------------------------------------------------------

def _migrate_json(raw: dict) -> dict:
    """
    Convert old JSON manifest format to new YAML schema.
    Handles the pre-YAML manifest structure gracefully.
    """
    manifest = {}

    # --- project block ---
    config_raw = raw.get("config", {})
    manifest["project"] = {
        "id": raw.get("project_id", config_raw.get("brand", "unknown") + "-project"),
        "version": raw.get("version", 1),
        "format": config_raw.get("format", raw.get("format", "character-ad")),
        "client": config_raw.get("client", raw.get("client")),
        "brand": config_raw.get("brand", raw.get("brand")),
    }

    # --- config block ---
    manifest["config"] = {
        "resolution": config_raw.get("resolution", raw.get("resolution", "1080x1920")),
        "aspect_ratio": config_raw.get("aspect_ratio", raw.get("aspect_ratio", "9:16")),
        "fps": config_raw.get("fps", raw.get("fps", 30)),
    }

    # --- style block ---
    style_guidelines = raw.get("style", {}).get("guidelines") or raw.get("style_guidelines")
    char_desc = raw.get("character_description")
    if style_guidelines or char_desc:
        manifest["style"] = {}
        if style_guidelines:
            manifest["style"]["guidelines"] = style_guidelines
        if char_desc:
            manifest["style"]["character_description"] = char_desc

    # --- resources ---
    old_refs = raw.get("references", [])
    if old_refs:
        supplied = []
        for r in old_refs:
            rtype = r.get("category", r.get("type", "reference"))
            # Normalize category names to new type names
            type_map = {
                "character": "character_reference",
                "product": "brand_asset",
                "background": "background_reference",
                "reference": "reference",
            }
            supplied.append({
                "path": r.get("path", ""),
                "type": type_map.get(rtype, rtype),
            })
        manifest["resources"] = {"supplied": supplied}

    # --- voice ---
    voice_raw = raw.get("voice", {})
    if voice_raw:
        manifest["voice"] = {
            "voice_id": voice_raw.get("voice_id"),
            "voice_name": voice_raw.get("voice_name"),
            "provider": voice_raw.get("provider", "elevenlabs"),
        }

    # --- voiceover ---
    vo_raw = raw.get("voiceover", {})
    if vo_raw:
        manifest["voiceover"] = {
            "audio_file": vo_raw.get("audio_file"),
            "vo_manifest": vo_raw.get("vo_manifest"),
            "duration_s": vo_raw.get("duration_s"),
        }
        if vo_raw.get("trimmed_from"):
            manifest["voiceover"]["original_duration_s"] = None  # unknown unless stored
            manifest["voiceover"]["trimmed_from"] = vo_raw.get("trimmed_from")

    # --- frameio / delivery ---
    frameio_raw = raw.get("frameio", {})
    if frameio_raw:
        manifest["delivery"] = {
            "frameio": {
                "upload_folder_id": frameio_raw.get("upload_folder_id"),
                "uploads": [],
            }
        }
        last = frameio_raw.get("last_upload", {})
        if last:
            manifest["delivery"]["frameio"]["uploads"].append({
                "version": raw.get("version", 1),
                "file_id": last.get("file_id"),
                "view_url": last.get("view_url"),
                "filename": last.get("filename"),
                "uploaded_at": last.get("uploaded_at"),
            })

    # --- scenes ---
    old_scenes = raw.get("scenes", [])
    if old_scenes:
        new_scenes = []
        for s in old_scenes:
            # Determine type
            if s.get("text_overlay"):
                stype = "text_overlay"
            elif s.get("still"):
                stype = "still"
            elif s.get("ken_burns"):
                stype = "ken_burns"
            else:
                stype = "normal"

            scene_idx = s.get("scene_index", s.get("index"))
            new_scene = {
                "index": scene_idx,
                "vo_text": s.get("voiceover_text", s.get("vo_text", "")),
                "starting_frame": s.get("starting_frame", ""),
                "action": s.get("action"),
                "type": stype,
                "start_s": s.get("start_s"),
                "end_s": s.get("end_s"),
                "still_path": s.get("still_path", f"output/scene_{scene_idx:03d}.png" if scene_idx else None),
                "video_clip": s.get("video_clip"),
            }

            # Carry over overlay_text for text_overlay scenes
            if stype == "text_overlay" and "text_overlay" in s and isinstance(s["text_overlay"], str):
                new_scene["overlay_text"] = s["text_overlay"]
            elif stype == "text_overlay":
                # vo_text is the overlay text
                new_scene["overlay_text"] = new_scene["vo_text"]

            new_scenes.append(new_scene)
        manifest["scenes"] = new_scenes

    return manifest


# ---------------------------------------------------------------------------
# CLI: validate / convert
# ---------------------------------------------------------------------------

def cmd_validate(args):
    path = args[0] if args else None
    if not path:
        print("Usage: manifest_schema.py validate <manifest.yaml>")
        sys.exit(1)

    try:
        manifest = load_manifest(path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate_manifest(manifest, path=path)
    errors_only = [e for e in errors if not e.startswith(f"[{path}] WARNING")]
    warnings = [e for e in errors if "WARNING" in e]

    top_format = manifest.get("format", "")

    if top_format == "video-project":
        project = manifest.get("project", {})
        config = manifest.get("config", {})
        layer_count = len(manifest.get("layers", []))
        source_count = len(manifest.get("sources", []))
        print(f"Project:  {project.get('id', 'unknown')}")
        print(f"Format:   video-project")
        print(f"Config:   {config.get('resolution', '?')} @ {config.get('fps', '?')}fps, {config.get('duration', '?')}s")
        print(f"Sources:  {source_count}")
        print(f"Layers:   {layer_count}")
        layer_summary = [f"{l.get('id')}({l.get('type')})" for l in manifest.get("layers", [])]
        if layer_summary:
            print(f"  Stack:  {' → '.join(layer_summary)}")
    elif top_format == "clip-index":
        clip_count = len(manifest.get("clips", []))
        silence_count = len(manifest.get("silences", []))
        print(f"Format:   clip-index")
        print(f"Source:   {manifest.get('source', '?')}")
        print(f"Clips:    {clip_count}")
        print(f"Silences: {silence_count}")
    else:
        project = manifest.get("project", {})
        scene_count = len(manifest.get("scenes", []))
        print(f"Project:  {project.get('id', 'unknown')} v{project.get('version', '?')}")
        print(f"Format:   {project.get('format', 'unknown')}")
        print(f"Scenes:   {scene_count}")
        print(f"Config:   {manifest.get('config', {}).get('resolution', '?')} @ {manifest.get('config', {}).get('fps', '?')}fps")

        supplied = manifest.get("resources", {}).get("supplied", [])
        if supplied:
            manifest_dir = Path(path).parent
            print(f"\nSupplied resources:")
            for r in supplied:
                rpath = r.get("path") or ""
                rtype = r.get("type", "?")
                exists = (manifest_dir / rpath).exists() if rpath else False
                status = "found" if exists else "MISSING"
                fname = Path(rpath).name if rpath else "(no path)"
                print(f"  [{status}]  {rtype:<22}  {fname}")

    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for w in warnings:
            print(f"  {w}")

    if errors_only:
        print(f"\nERRORS ({len(errors_only)}):")
        for e in errors_only:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("\nOK - manifest is valid")


def cmd_convert(args):
    if not args:
        print("Usage: manifest_schema.py convert <old.json> [--output <new.yaml>]")
        sys.exit(1)

    src = args[0]
    output = None
    if "--output" in args:
        idx = args.index("--output")
        output = args[idx + 1]

    if not output:
        output = str(Path(src).with_suffix(".yaml"))

    if not Path(src).exists():
        print(f"ERROR: Source file not found: {src}", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest(src)
    save_manifest(manifest, output)
    print(f"Converted: {src} → {output}")

    # Print quick summary
    project = manifest.get("project", {})
    scene_count = len(manifest.get("scenes", []))
    print(f"  Project: {project.get('id')} v{project.get('version')}")
    print(f"  Scenes:  {scene_count}")
    print(f"  Format:  {project.get('format')}")


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage:")
        print("  manifest_schema.py validate <manifest.yaml>")
        print("  manifest_schema.py convert <old.json> [--output <new.yaml>]")
        sys.exit(0)

    cmd = args[0]
    rest = args[1:]

    if cmd == "validate":
        cmd_validate(rest)
    elif cmd == "convert":
        cmd_convert(rest)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: validate, convert")
        sys.exit(1)


if __name__ == "__main__":
    main()
