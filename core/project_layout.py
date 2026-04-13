"""
Standard Parallax project layout and auto-scaffolding.

Every cwd is a Parallax project. The first time any `parallax` command runs in
a directory, we create the canonical subdirs and migrate any loose source files
into `input/`. Subsequent runs are a silent no-op.

Canonical layout:

    <cwd>/
    ├── input/     # source footage + reference images (user-provided)
    ├── output/    # finals only — latest deliverable per concept
    ├── drafts/    # version history: <concept>_v0.0.1.mp4, v0.0.2.mp4, ...
    ├── stills/    # generated stills from `parallax create`
    └── .parallax/ # internal state, manifests, concept history
"""

from __future__ import annotations

import re
from pathlib import Path

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
STANDARD_DIRS = ("input", "output", "drafts", "stills", ".parallax")


def _is_media_loose(p: Path, exts: tuple[str, ...]) -> bool:
    """True if p is a loose media file in the project root (not in a subdir)."""
    if not (p.is_file() or p.is_symlink()):
        return False
    if p.suffix.lower() not in exts:
        return False
    return True


def ensure_project_layout(cwd: Path) -> None:
    """
    Scaffold the standard Parallax project layout in cwd.

    Idempotent: if all dirs already exist, prints nothing. On first call in a
    given cwd (detected by absence of `.parallax/`), also migrates loose video
    and image files from the root into `input/`, announcing each move.
    """
    cwd = Path(cwd).resolve()
    first_time = not (cwd / ".parallax").exists()

    created_any = False
    for sub in STANDARD_DIRS:
        d = cwd / sub
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created_any = True

    if first_time and created_any:
        print(f"[parallax] initialized project layout in {cwd}")

    if not first_time:
        return  # silent no-op on subsequent runs

    # Migrate loose media from the project root to input/
    input_dir = cwd / "input"
    moved_videos: list[str] = []
    moved_images: list[str] = []

    for entry in sorted(cwd.iterdir()):
        # Skip anything that lives inside one of our dirs
        if entry.is_dir():
            continue
        # is_file() returns False for dangling symlinks; use lstat-aware check
        if not (entry.is_file() or entry.is_symlink()):
            continue
        ext = entry.suffix.lower()
        if ext in VIDEO_EXTS:
            target = input_dir / entry.name
            try:
                entry.rename(target)  # preserves symlinks (no resolve)
                moved_videos.append(entry.name)
            except Exception as e:
                print(f"[parallax] WARNING: could not move {entry.name} → input/: {e}")
        elif ext in IMAGE_EXTS:
            target = input_dir / entry.name
            try:
                entry.rename(target)
                moved_images.append(entry.name)
            except Exception as e:
                print(f"[parallax] WARNING: could not move {entry.name} → input/: {e}")

    if moved_videos:
        label = "video" if len(moved_videos) == 1 else "videos"
        print(f"[parallax] moved {len(moved_videos)} {label} to input/: {', '.join(moved_videos)}")
    if moved_images:
        label = "image" if len(moved_images) == 1 else "images"
        print(f"[parallax] moved {len(moved_images)} {label} to input/: {', '.join(moved_images)}")


def next_version(project_root: Path, concept_id: str) -> str:
    """
    Return the next unused version string for `concept_id` in project_root.

    Scans output/ and drafts/ for files matching `<concept>_v<X.Y.Z>*.mp4` and
    returns the smallest v0.0.N not yet used. Starts at v0.0.1.
    """
    project_root = Path(project_root)
    used: set[tuple[int, int, int]] = set()
    pattern = re.compile(
        rf"^{re.escape(concept_id)}_v(\d+)\.(\d+)\.(\d+)(?:_.*)?\.mp4$"
    )
    for sub in ("output", "drafts"):
        d = project_root / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            m = pattern.match(f.name)
            if m:
                used.add((int(m.group(1)), int(m.group(2)), int(m.group(3))))

    # Always use major=0, minor=0, bump patch
    patch = 1
    while (0, 0, patch) in used:
        patch += 1
    return f"0.0.{patch}"


def update_latest_symlink(project_root: Path, final_path: Path) -> None:
    """
    Point project_root/output/latest.mp4 at final_path. Symlink first;
    fall back to copy if the FS doesn't support symlinks.
    """
    import shutil
    project_root = Path(project_root)
    latest = project_root / "output" / "latest.mp4"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        # Relative symlink keeps the project portable.
        try:
            rel = final_path.relative_to(project_root / "output")
            latest.symlink_to(rel)
        except ValueError:
            latest.symlink_to(final_path)
    except (OSError, NotImplementedError):
        try:
            shutil.copy2(final_path, latest)
        except Exception as e:
            print(f"[parallax] WARNING: could not update latest.mp4: {e}")


def extract_abs_video_path(brief: str) -> str | None:
    """
    If the brief mentions an absolute path ending in a video extension, return
    it. Otherwise return None. Heuristic: a token starting with `/` and ending
    in `.mp4`/`.mov`/`.webm`/`.m4v`/`.mkv`, stripped of surrounding punctuation.
    """
    if not brief:
        return None
    # Match whitespace-delimited tokens that start with / and end with a known
    # video ext. Strip trailing punctuation (.,;:!?) so sentence endings don't
    # break the match.
    match = re.search(
        r"(/[^\s]+?\.(?:mp4|mov|webm|m4v|mkv))(?=[\s.,;:!?)\"']|$)",
        brief,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    return None
