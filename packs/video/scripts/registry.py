#!/usr/bin/env python3
"""Project registry — persistent index of all video production projects.

Stored at {paths.output}/project-registry.yaml alongside the projects themselves.
The agent reads this at session start to know what exists and updates it after
significant milestones (init, assembly, delivery).

Projects get sequential integer IDs (1, 2, 3...) so you can say "project 59"
unambiguously. The slug is optional — directories are named {id}-{slug} or just {id}.

Usage:
  registry.py list                          # show all projects
  registry.py register --manifest path      # register or update from manifest
  registry.py status <slug-or-id>           # show one project's details
  registry.py note <slug-or-id> "your note" # add a note to a project
  registry.py next-id                       # print next available project ID
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config
from manifest_schema import load_manifest

REGISTRY_FILENAME = "project-registry.yaml"

STATUS_ORDER = [
    "initialized",
    "scenes_planned",
    "voiceover_done",
    "stills_done",
    "assembled",
    "delivered",
]


def _get_registry_path() -> Path:
    cfg = load_config()
    output_root = Path(cfg.get("paths", {}).get("output", "~/Movies/VideoProduction")).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    return output_root / REGISTRY_FILENAME


def _load_registry(path: Path) -> dict:
    if not path.exists():
        return {"projects": [], "next_id": 1}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if "projects" not in data:
            data["projects"] = []
        # Ensure next_id is tracked
        if "next_id" not in data:
            max_id = max((p.get("id", 0) for p in data["projects"]), default=0)
            data["next_id"] = max_id + 1
        return data
    except Exception as e:
        print(f"Warning: could not read registry ({e}), starting fresh", file=sys.stderr)
        return {"projects": [], "next_id": 1}


def _save_registry(registry: dict, path: Path):
    try:
        with open(path, "w") as f:
            yaml.dump(registry, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception as e:
        print(f"ERROR: could not save registry: {e}", file=sys.stderr)
        sys.exit(1)


def _find_project(registry: dict, slug_or_id: str) -> dict | None:
    """Find a project by slug or numeric ID."""
    # Try numeric ID first
    try:
        target_id = int(slug_or_id)
        entry = next((p for p in registry["projects"] if p.get("id") == target_id), None)
        if entry:
            return entry
    except ValueError:
        pass
    # Fall back to slug match
    return next((p for p in registry["projects"] if p.get("slug") == slug_or_id), None)


def next_id() -> int:
    """Return the next available project ID and increment the counter."""
    registry_path = _get_registry_path()
    registry = _load_registry(registry_path)
    nid = registry.get("next_id", 1)
    registry["next_id"] = nid + 1
    _save_registry(registry, registry_path)
    return nid


def peek_next_id() -> int:
    """Return the next available project ID without incrementing."""
    registry_path = _get_registry_path()
    registry = _load_registry(registry_path)
    return registry.get("next_id", 1)


def backfill_ids():
    """Assign IDs to existing projects that don't have one, ordered by last_worked date."""
    registry_path = _get_registry_path()
    registry = _load_registry(registry_path)

    needs_id = [p for p in registry["projects"] if "id" not in p]
    if not needs_id:
        print("All projects already have IDs.")
        return

    needs_id.sort(key=lambda p: p.get("last_worked", "2000-01-01"))
    current_max = max((p.get("id", 0) for p in registry["projects"]), default=0)

    for i, project in enumerate(needs_id, start=current_max + 1):
        project["id"] = i
        print(f"  Assigned ID {i} to {project.get('slug', '?')}")

    registry["next_id"] = max(p.get("id", 0) for p in registry["projects"]) + 1
    _save_registry(registry, registry_path)
    print(f"Backfilled {len(needs_id)} projects. Next ID: {registry['next_id']}")


def _infer_status(manifest: dict, project_dir: Path) -> str:
    """Infer project status from manifest state and files on disk."""
    scenes = manifest.get("scenes", [])
    vo = manifest.get("voiceover", {}) or {}

    # Check for assembled outputs (output/ subfolder is canonical; root is legacy)
    output_dir = project_dir / "output"
    mp4s = list(output_dir.glob("*.mp4")) if output_dir.exists() else list(project_dir.glob("*.mp4"))
    non_animatic = [f for f in mp4s if "animatic" not in f.name]
    if any("draft" in f.name or "mixed" in f.name for f in non_animatic):
        return "assembled"

    # Check stills (all formats now use assets/stills/)
    stills_dir = project_dir / "assets" / "stills"
    if not stills_dir.exists():
        stills_dir = project_dir / "assets"  # fallback for older projects
    stills_done = stills_dir.exists() and any(stills_dir.glob("*.png"))
    all_stills = (
        stills_done
        and scenes
        and all(
            (project_dir / s.get("still_path", "")).exists()
            for s in scenes
            if s.get("still_path") and not s.get("text_overlay")
        )
    )
    if all_stills:
        return "stills_done"

    # Check voiceover
    if vo.get("audio_file") and (project_dir / vo["audio_file"]).exists():
        return "voiceover_done"

    # Check scenes planned
    if scenes:
        return "scenes_planned"

    return "initialized"


def _find_outputs(project_dir: Path) -> list[str]:
    # Look in output/ subfolder first (canonical), fall back to project root for old projects
    output_dir = project_dir / "output"
    if output_dir.exists():
        return sorted(f"output/{f.name}" for f in output_dir.glob("*.mp4"))
    return sorted(f.name for f in project_dir.glob("*.mp4"))


def register(manifest_path: str, notes: str = "") -> dict:
    """Register or update a project from its manifest. Returns the entry."""
    manifest = load_manifest(manifest_path)
    project_dir = Path(manifest_path).parent

    proj = manifest.get("project", {})
    slug = proj.get("id") or project_dir.name
    fmt = proj.get("format", "unknown")
    brand = proj.get("brand", "")

    status = _infer_status(manifest, project_dir)
    outputs = _find_outputs(project_dir)
    scenes = manifest.get("scenes", [])

    # Find character ref if present
    char_ref = None
    for r in manifest.get("resources", {}).get("supplied", []):
        if r.get("type") == "character_reference":
            ref_path = project_dir / (r.get("path") or "")
            if ref_path.exists():
                char_ref = str(ref_path)
                break

    registry_path = _get_registry_path()
    registry = _load_registry(registry_path)

    # Find existing entry or create new
    existing = _find_project(registry, slug)
    entry = existing or {}

    entry.update({
        "slug": slug,
        "path": str(project_dir.resolve()),
        "format": fmt,
        "brand": brand,
        "status": status,
        "scene_count": len(scenes),
        "last_worked": str(date.today()),
        "outputs": outputs,
    })

    if char_ref:
        entry["character_ref"] = char_ref
    if notes:
        entry["notes"] = notes
    elif "notes" not in entry:
        entry["notes"] = ""

    # Assign ID to new entries
    if existing is None:
        if "id" not in entry:
            nid = registry.get("next_id", 1)
            entry["id"] = nid
            registry["next_id"] = nid + 1
        registry["projects"].insert(0, entry)
    else:
        idx = registry["projects"].index(existing)
        registry["projects"][idx] = entry

    _save_registry(registry, registry_path)
    print(f"Registered: {slug} [{status}] → {registry_path}")
    return entry


def list_projects(fmt_filter: str | None = None, status_filter: str | None = None):
    """Print all registered projects."""
    registry_path = _get_registry_path()
    registry = _load_registry(registry_path)
    projects = registry["projects"]

    if fmt_filter:
        projects = [p for p in projects if p.get("format") == fmt_filter]
    if status_filter:
        projects = [p for p in projects if p.get("status") == status_filter]

    if not projects:
        print("No projects found.")
        return

    print(f"\n{'ID':<5} {'Slug':<32} {'Brand':<18} {'Format':<16} {'Status':<16} {'Last worked':<12} Scenes")
    print("-" * 115)
    for p in projects:
        pid = str(p.get("id", "-"))[:4]
        slug = p.get("slug", "?")[:31]
        brand = p.get("brand", "")[:17]
        fmt = p.get("format", "")[:15]
        status = p.get("status", "")[:15]
        last = p.get("last_worked", "")[:11]
        scenes = p.get("scene_count", "?")
        print(f"{pid:<5} {slug:<32} {brand:<18} {fmt:<16} {status:<16} {last:<12} {scenes}")
        if p.get("outputs"):
            for out in p["outputs"]:
                print(f"  {'':37} → {out}")
    print()


def show_status(slug_or_id: str):
    """Show full details for one project (by slug or numeric ID)."""
    registry_path = _get_registry_path()
    registry = _load_registry(registry_path)
    entry = _find_project(registry, slug_or_id)

    if not entry:
        print(f"Project not found: {slug_or_id}")
        print("Run: registry.py list")
        sys.exit(1)

    pid = entry.get("id", "?")
    slug = entry.get("slug", "?")
    print(f"\n=== Project {pid}: {slug} ===")
    for k, v in entry.items():
        if isinstance(v, list):
            print(f"  {k}:")
            for item in v:
                print(f"    - {item}")
        else:
            print(f"  {k}: {v}")
    print()


def add_note(slug_or_id: str, note: str):
    registry_path = _get_registry_path()
    registry = _load_registry(registry_path)
    entry = _find_project(registry, slug_or_id)

    if not entry:
        print(f"Project not found: {slug_or_id}")
        sys.exit(1)

    entry["notes"] = note
    _save_registry(registry, registry_path)
    print(f"Note saved for {entry.get('slug', slug_or_id)}")


def main():
    parser = argparse.ArgumentParser(description="Video production project registry")
    sub = parser.add_subparsers(dest="cmd")

    list_p = sub.add_parser("list")
    list_p.add_argument("--format", dest="fmt", default=None)
    list_p.add_argument("--status", dest="status_filter", default=None)

    reg = sub.add_parser("register")
    reg.add_argument("--manifest", required=True)
    reg.add_argument("--notes", default="")
    reg.add_argument("--id", type=int, default=None, help="Assign specific project ID")

    status_p = sub.add_parser("status")
    status_p.add_argument("slug_or_id", help="Project slug or numeric ID")

    note_p = sub.add_parser("note")
    note_p.add_argument("slug_or_id", help="Project slug or numeric ID")
    note_p.add_argument("text")

    sub.add_parser("next-id", help="Print next available project ID")
    sub.add_parser("backfill-ids", help="Assign IDs to existing projects that lack them")

    args = parser.parse_args()

    if args.cmd == "list":
        list_projects(fmt_filter=args.fmt, status_filter=args.status_filter)
    elif args.cmd == "register":
        register(args.manifest, args.notes)
    elif args.cmd == "status":
        show_status(args.slug_or_id)
    elif args.cmd == "note":
        add_note(args.slug_or_id, args.text)
    elif args.cmd == "next-id":
        print(peek_next_id())
    elif args.cmd == "backfill-ids":
        backfill_ids()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
