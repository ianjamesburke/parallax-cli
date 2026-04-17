"""
parallax fal — argparse handlers for the fal subcommand group.

Subcommands:
  parallax fal video <low|medium|high> --prompt TEXT [--duration N] [--aspect ...] [--seed N] [--output PATH] [--json]
  parallax fal image <low|medium|high> --prompt TEXT [--aspect ...] [--seed N] [--output PATH] [--json]
  parallax fal models [--json]

TEST_MODE: set TEST_MODE=1 to skip API calls entirely. Writes a 1s black mp4 (video)
or a blank PNG (image) via ffmpeg to the output path and emits fake NDJSON events.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TEST_MODE = os.environ.get("TEST_MODE", "false").lower() in ("1", "true", "yes", "on")

_DEFAULT_ASPECT = "9:16"
_DEFAULT_DURATION = 5


def _default_output(kind: str, tier: str, ext: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path.cwd() / "parallax-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{ts}-{tier}.{ext}"


def _emit(use_json: bool, **kwargs) -> None:
    if use_json:
        print(json.dumps(kwargs), flush=True)
    else:
        event = kwargs.get("event", "")
        msg = kwargs.get("message") or kwargs.get("path") or ""
        if msg:
            print(f"[fal] {event}: {msg}", flush=True)
        else:
            print(f"[fal] {event}", flush=True)


def _write_test_video(output_path: Path) -> None:
    """Write a 1-second black mp4 via ffmpeg for TEST_MODE."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-f", "lavfi", "-i", "color=black:s=576x1024:r=25:d=1",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", "1", "-shortest", "-y", str(output_path),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg test video failed: {e.stderr.decode()[:200]}") from e


def _write_test_image(output_path: Path) -> None:
    """Write a blank 576x1024 black PNG via ffmpeg for TEST_MODE."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-f", "lavfi", "-i", "color=black:s=576x1024:r=1",
                "-vframes", "1", "-y", str(output_path),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg test image failed: {e.stderr.decode()[:200]}") from e


def cmd_fal_video(args) -> int:
    """Handler for: parallax fal video <tier> --prompt TEXT [flags]"""
    from .models import get_video_model

    tier = args.tier
    prompt = args.prompt
    duration = getattr(args, "duration", _DEFAULT_DURATION)
    aspect = getattr(args, "aspect", _DEFAULT_ASPECT)
    seed = getattr(args, "seed", None)
    use_json = getattr(args, "json", False)
    output = getattr(args, "output", None)

    spec = get_video_model(tier)
    output_path = Path(output) if output else _default_output("video", tier, "mp4")

    if TEST_MODE:
        _emit(use_json, event="queued", model=spec.model_id, tier=tier, test_mode=True)
        _emit(use_json, event="in_progress", logs="TEST_MODE: skipping API call")
        try:
            _write_test_video(output_path)
        except RuntimeError as e:
            _emit(use_json, event="error", message=str(e))
            print(f"[parallax fal] ERROR: {e}", file=sys.stderr)
            return 1
        _emit(use_json, event="done", path=str(output_path))
        if not use_json:
            print(f"[fal] TEST_MODE video → {output_path}")
        return 0

    from .client import generate_video
    return generate_video(spec, prompt, duration, aspect, seed, output_path, use_json)


def cmd_fal_image(args) -> int:
    """Handler for: parallax fal image <tier> --prompt TEXT [flags]"""
    from .models import get_image_model

    tier = args.tier
    prompt = args.prompt
    aspect = getattr(args, "aspect", _DEFAULT_ASPECT)
    seed = getattr(args, "seed", None)
    use_json = getattr(args, "json", False)
    output = getattr(args, "output", None)

    spec = get_image_model(tier)
    output_path = Path(output) if output else _default_output("image", tier, "png")

    if TEST_MODE:
        _emit(use_json, event="queued", model=spec.model_id, tier=tier, test_mode=True)
        _emit(use_json, event="in_progress", logs="TEST_MODE: skipping API call")
        try:
            _write_test_image(output_path)
        except RuntimeError as e:
            _emit(use_json, event="error", message=str(e))
            print(f"[parallax fal] ERROR: {e}", file=sys.stderr)
            return 1
        _emit(use_json, event="done", path=str(output_path))
        if not use_json:
            print(f"[fal] TEST_MODE image → {output_path}")
        return 0

    from .client import generate_image
    return generate_image(spec, prompt, aspect, seed, output_path, use_json)


def cmd_fal_models(args) -> int:
    """Handler for: parallax fal models [--json]"""
    from .models import all_models

    use_json = getattr(args, "json", False)
    rows = all_models()

    if use_json:
        for row in rows:
            print(json.dumps(row), flush=True)
    else:
        print(f"{'KIND':<8} {'TIER':<8} {'MODEL ID':<55} PRICE")
        print("-" * 100)
        for r in rows:
            print(f"{r['kind']:<8} {r['tier']:<8} {r['model_id']:<55} {r['price']}")
    return 0
