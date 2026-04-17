"""
parallax fal — argparse handlers for the fal subcommand group.

Subcommands:
  parallax fal video <low|medium|high> --prompt TEXT [--image PATH] [--end-frame PATH]
                                        [--audio/--no-audio] [--duration N] [--aspect ...]
                                        [--seed N] [--output PATH] [--json]
  parallax fal image <low|medium|high> --prompt TEXT [--aspect ...] [--seed N] [--output PATH] [--json]
  parallax fal models [--json]

When --image is provided, routes to the i2v endpoint for the chosen tier.
--end-frame requires --image. --audio/--no-audio only valid on models with supports_audio=True.

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


def _load_config_model(kind: str, tier: str, mode: str = "t2v"):
    """Return (model_id_override, source) from project config. None override = use default."""
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
    try:
        from packs.video.config import load as _load_config
        cfg = _load_config()
        if kind == "video":
            model_id, source = cfg.get_video_model_sourced(tier, mode)
        else:
            model_id, source = cfg.image[tier]
        # Only treat as override if source isn't default
        if source != "default":
            return model_id, source
        return None, "default"
    except Exception:
        return None, "default"


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
    model_flag = getattr(args, "model", None)
    image_flag = getattr(args, "image", None)
    end_frame_flag = getattr(args, "end_frame", None)
    audio_flag = getattr(args, "audio", None)  # True / False / None

    # Validate --end-frame requires --image
    if end_frame_flag and not image_flag:
        print("[parallax fal] ERROR: --end-frame requires --image", file=sys.stderr)
        return 2

    # Choose mode based on --image flag
    mode = "i2v" if image_flag else "t2v"

    # Precedence: --model flag > env/config > default
    if model_flag:
        model_override, override_source = model_flag, "cli"
    else:
        model_override, override_source = _load_config_model("video", tier, mode)

    spec = get_video_model(tier, mode=mode, model_id_override=model_override)
    if model_override and not use_json:
        print(f"[fal] model override ({override_source}): {model_override}")

    output_path = Path(output) if output else _default_output("video", tier, "mp4")

    if TEST_MODE:
        _emit(use_json, event="queued", model=spec.model_id, tier=tier, mode=mode, test_mode=True)
        _emit(use_json, event="in_progress", logs="TEST_MODE: skipping API call")
        try:
            _write_test_video(output_path)
        except RuntimeError as e:
            _emit(use_json, event="error", message=str(e))
            print(f"[parallax fal] ERROR: {e}", file=sys.stderr)
            return 1
        _emit(use_json, event="done", path=str(output_path))
        if not use_json:
            print(f"[fal] TEST_MODE video ({mode}) → {output_path}")
        return 0

    if mode == "i2v":
        from .client import generate_i2v
        image_path = Path(image_flag)
        if not image_path.exists():
            print(f"[parallax fal] ERROR: --image file not found: {image_path}", file=sys.stderr)
            return 2
        end_image_path = Path(str(end_frame_flag)) if end_frame_flag is not None else None
        if end_image_path and not end_image_path.exists():
            print(f"[parallax fal] ERROR: --end-frame file not found: {end_image_path}", file=sys.stderr)
            return 2
        return generate_i2v(
            spec, prompt, image_path, duration, aspect, seed,
            end_image_path, audio_flag, output_path, use_json,
        )
    else:
        # Validate --audio not used on t2v models without audio support
        if audio_flag is not None and not spec.supports_audio:
            print(
                f"[parallax fal] ERROR: model {spec.model_id!r} does not support audio generation. "
                "Remove --audio / --no-audio.",
                file=sys.stderr,
            )
            return 2
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
    model_flag = getattr(args, "model", None)

    if model_flag:
        model_override, override_source = model_flag, "cli"
    else:
        model_override, override_source = _load_config_model("image", tier)

    spec = get_image_model(tier, model_id_override=model_override)
    if model_override and not use_json:
        print(f"[fal] model override ({override_source}): {model_override}")
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
    from .models import all_models_with_config

    use_json = getattr(args, "json", False)
    rows = all_models_with_config()

    if use_json:
        for row in rows:
            print(json.dumps(row), flush=True)
    else:
        print(f"{'KIND':<7} {'MODE':<5} {'TIER':<8} {'SOURCE':<9} {'MODEL ID':<55} PRICE")
        print("-" * 120)
        for r in rows:
            flags = []
            if r.get("supports_start_frame"):
                flags.append("start-frame")
            if r.get("supports_end_frame"):
                flags.append("end-frame")
            if r.get("supports_audio"):
                flags.append("audio")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            print(
                f"{r['kind']:<7} {r.get('mode','n/a'):<5} {r['tier']:<8} "
                f"{r.get('source','default'):<9} {r['model_id']:<55} {r['price']}{flag_str}"
            )
    return 0
