"""
fal.ai submit/poll/download client.

One code path for all tiers. Auth via FAL_KEY env var — fails fast if missing.
"""

import os
import sys
import json
import time
import urllib.request
from pathlib import Path
from typing import Callable

from .models import ModelSpec


def _require_fal_key() -> str:
    """Return FAL_KEY from env; fail fast with clear error if missing."""
    # Check AI_VIDEO_FAL_KEY first (project convention), then FAL_KEY
    key = os.environ.get("AI_VIDEO_FAL_KEY") or os.environ.get("FAL_KEY")
    if not key:
        print(
            "[parallax fal] ERROR: FAL_KEY not set.\n"
            "  export FAL_KEY='your-key'   # or AI_VIDEO_FAL_KEY\n"
            "  Keys live at: https://fal.ai/dashboard/keys",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _emit(use_json: bool, **kwargs) -> None:
    """Print a progress event — NDJSON when --json, plain text otherwise."""
    if use_json:
        print(json.dumps(kwargs), flush=True)
    else:
        event = kwargs.get("event", "")
        msg = kwargs.get("message") or kwargs.get("logs") or kwargs.get("path") or ""
        if event == "queued":
            print("[fal] queued…", flush=True)
        elif event == "in_progress":
            if msg:
                print(f"[fal] {msg}", flush=True)
        elif event == "done":
            print(f"[fal] done → {msg}", flush=True)
        elif event == "error":
            print(f"[fal] ERROR: {msg}", flush=True)


def _upload_image(image_path: Path) -> str:
    """Upload a local image file to fal storage and return the CDN URL.

    Uses fal_client.upload_file which handles auth via FAL_KEY env var.
    Raises RuntimeError on failure.
    """
    try:
        import fal_client
    except ImportError:
        raise RuntimeError("[parallax fal] fal-client not installed. Run: uv add fal-client")

    try:
        url = fal_client.upload_file(str(image_path))
        return url
    except Exception as e:
        raise RuntimeError(
            f"[parallax fal] image upload failed path={image_path!r}: {e}"
        ) from e


def _build_video_args(
    spec: ModelSpec,
    prompt: str,
    duration: int,
    aspect: str,
    seed: int | None,
) -> dict:
    """Build the model-specific arguments dict for text-to-video generation.

    Aspect support per model:
      ltx-2.3 (low):   aspect_ratio param — "9:16" or "16:9" only
      wan-t2v (medium): aspect_ratio param — "9:16" or "16:9" only
      kling-video (high): aspect_ratio param — "9:16", "16:9", or "1:1"
    """
    args: dict = {
        "prompt": prompt,
    }

    mid = spec.model_id

    if "ltx-2.3" in mid:
        # LTX-2.3 uses aspect_ratio enum + resolution (not width/height).
        # Supported: "9:16", "16:9". "1:1" is not supported — fail fast.
        if aspect not in ("9:16", "16:9"):
            raise ValueError(
                f"LTX-2.3 (low) does not support aspect ratio '{aspect}'. "
                "Supported: 9:16, 16:9"
            )
        args["aspect_ratio"] = aspect
        args["resolution"] = "1080p"
        # LTX-2.3 duration is an enum: 6, 8, or 10 (seconds, as string).
        # Snap requested duration to nearest valid value.
        _ltx_durations = [6, 8, 10]
        snapped = min(_ltx_durations, key=lambda d: abs(d - duration))
        args["duration"] = snapped

    elif "wan-t2v" in mid:
        # Wan 2.1 uses aspect_ratio enum. Supported: "9:16", "16:9" only.
        if aspect not in ("9:16", "16:9"):
            raise ValueError(
                f"Wan (medium) does not support aspect ratio '{aspect}'. "
                "Supported: 9:16, 16:9"
            )
        args["aspect_ratio"] = aspect
        # resolution key controls 480p vs 720p cost
        args["resolution"] = "480p"

    elif "kling-video" in mid:
        # Kling 1.6 supports "9:16", "16:9", "1:1".
        if aspect not in ("9:16", "16:9", "1:1"):
            raise ValueError(
                f"Kling (high) does not support aspect ratio '{aspect}'. "
                "Supported: 9:16, 16:9, 1:1"
            )
        args["aspect_ratio"] = aspect
        args["duration"] = str(duration)  # Kling takes string "5" or "10"

    if seed is not None:
        args["seed"] = seed

    return args


def _build_i2v_args(
    spec: ModelSpec,
    prompt: str,
    image_url: str,
    duration: int,
    aspect: str,
    seed: int | None,
    end_image_url: str | None,
    audio: bool | None,
) -> dict:
    """Build model-specific arguments for image-to-video generation.

    Parameter naming differs per model:
      ltx-2.3/image-to-video:          image_url, end_image_url, aspect_ratio, duration (snapped)
      wan-i2v:                          image_url, aspect_ratio, resolution
      kling-video/.../image-to-video:   image_url, tail_image_url, aspect_ratio, duration (str)

    audio: only valid on models with supports_audio=True. All current i2v models lack audio.
    Raises ValueError on unsupported flag/param combos.
    """
    if audio is not None and not spec.supports_audio:
        raise ValueError(
            f"Model {spec.model_id!r} does not support audio generation. "
            "Remove --audio / --no-audio for this tier."
        )

    if end_image_url and not spec.supports_end_frame:
        raise ValueError(
            f"Model {spec.model_id!r} (tier={spec.tier}) does not support --end-frame. "
            "Use low (LTX-2.3) or high (Kling) tier for end-frame anchoring."
        )

    args: dict = {
        "prompt": prompt,
        "image_url": image_url,
    }

    mid = spec.model_id

    if "ltx-2.3" in mid:
        if aspect not in ("9:16", "16:9", "auto"):
            raise ValueError(
                f"LTX-2.3 i2v does not support aspect ratio '{aspect}'. "
                "Supported: 9:16, 16:9 (or omit for auto)"
            )
        if aspect != "auto":
            args["aspect_ratio"] = aspect
        args["resolution"] = "1080p"
        _ltx_durations = [6, 8, 10]
        snapped = min(_ltx_durations, key=lambda d: abs(d - duration))
        args["duration"] = snapped
        if end_image_url:
            args["end_image_url"] = end_image_url

    elif "wan-i2v" in mid:
        if aspect not in ("9:16", "16:9", "1:1", "auto"):
            raise ValueError(
                f"Wan i2v does not support aspect ratio '{aspect}'. "
                "Supported: 9:16, 16:9, 1:1 (or omit for auto)"
            )
        if aspect != "auto":
            args["aspect_ratio"] = aspect
        args["resolution"] = "480p"

    elif "kling-video" in mid:
        if aspect not in ("9:16", "16:9", "1:1"):
            raise ValueError(
                f"Kling i2v does not support aspect ratio '{aspect}'. "
                "Supported: 9:16, 16:9, 1:1"
            )
        args["aspect_ratio"] = aspect
        args["duration"] = str(duration)
        if end_image_url:
            args["tail_image_url"] = end_image_url

    if seed is not None:
        args["seed"] = seed

    if audio is not None and spec.supports_audio:
        args["enable_audio"] = audio

    return args


def _build_image_args(
    spec: ModelSpec,
    prompt: str,
    aspect: str,
    seed: int | None,
) -> dict:
    size_val = spec.size_map.get(aspect, "portrait_9_16")
    args: dict = {
        "prompt": prompt,
        "image_size": size_val,
    }
    if seed is not None:
        args["seed"] = seed
    return args


def _download(url: str, dest: Path) -> None:
    """Download a URL to dest path via urllib (no extra deps)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310
            dest.write_bytes(resp.read())
    except Exception as e:
        raise RuntimeError(f"download failed url={url!r} dest={dest}: {e}") from e


def generate_video(
    spec: ModelSpec,
    prompt: str,
    duration: int,
    aspect: str,
    seed: int | None,
    output_path: Path,
    use_json: bool,
) -> int:
    """Submit a text-to-video job, poll to completion, download result. Returns exit code."""
    key = _require_fal_key()
    os.environ["FAL_KEY"] = key  # fal_client reads this on import

    try:
        import fal_client
    except ImportError:
        print("[parallax fal] ERROR: fal-client not installed. Run: uv add fal-client", file=sys.stderr)
        return 1

    try:
        model_args = _build_video_args(spec, prompt, duration, aspect, seed)
    except ValueError as e:
        print(f"[parallax fal] ERROR: {e}", file=sys.stderr)
        return 2

    _emit(use_json, event="queued", model=spec.model_id, tier=spec.tier)

    try:
        result = fal_client.subscribe(
            spec.model_id,
            arguments=model_args,
            with_logs=True,
            on_queue_update=lambda update: _on_update(update, use_json),
        )
    except Exception as e:
        _emit(use_json, event="error", message=str(e))
        print(f"[parallax fal] ERROR: API call failed: {e}", file=sys.stderr)
        return 1

    return _download_video_result(result, output_path, use_json)


def generate_i2v(
    spec: ModelSpec,
    prompt: str,
    image_path: Path,
    duration: int,
    aspect: str,
    seed: int | None,
    end_image_path: Path | None,
    audio: bool | None,
    output_path: Path,
    use_json: bool,
) -> int:
    """Submit an image-to-video job, poll to completion, download result. Returns exit code."""
    key = _require_fal_key()
    os.environ["FAL_KEY"] = key

    try:
        import fal_client
    except ImportError:
        print("[parallax fal] ERROR: fal-client not installed. Run: uv add fal-client", file=sys.stderr)
        return 1

    # Validate model supports i2v before uploading anything
    if not spec.supports_image_to_video:
        print(
            f"[parallax fal] ERROR: model {spec.model_id!r} (tier={spec.tier}) does not support "
            "image-to-video. Use --image only with i2v-capable tiers.",
            file=sys.stderr,
        )
        return 2

    # Upload start frame
    _emit(use_json, event="uploading", message=f"uploading start frame: {image_path.name}")
    try:
        image_url = _upload_image(image_path)
    except RuntimeError as e:
        print(f"[parallax fal] ERROR: {e}", file=sys.stderr)
        return 1

    # Upload end frame if provided
    end_image_url: str | None = None
    if end_image_path is not None:
        _emit(use_json, event="uploading", message=f"uploading end frame: {end_image_path.name}")
        try:
            end_image_url = _upload_image(end_image_path)
        except RuntimeError as e:
            print(f"[parallax fal] ERROR: {e}", file=sys.stderr)
            return 1

    try:
        model_args = _build_i2v_args(spec, prompt, image_url, duration, aspect, seed, end_image_url, audio)
    except ValueError as e:
        print(f"[parallax fal] ERROR: {e}", file=sys.stderr)
        return 2

    _emit(use_json, event="queued", model=spec.model_id, tier=spec.tier, mode="i2v")

    try:
        result = fal_client.subscribe(
            spec.model_id,
            arguments=model_args,
            with_logs=True,
            on_queue_update=lambda update: _on_update(update, use_json),
        )
    except Exception as e:
        _emit(use_json, event="error", message=str(e))
        print(f"[parallax fal] ERROR: API call failed: {e}", file=sys.stderr)
        return 1

    return _download_video_result(result, output_path, use_json)


def _download_video_result(result, output_path: Path, use_json: bool) -> int:
    """Extract video URL from a fal result dict, download it. Returns exit code."""
    video_url = None
    try:
        if isinstance(result, dict):
            if "video" in result and isinstance(result["video"], dict):
                video_url = result["video"]["url"]
            elif "videos" in result and result["videos"]:
                video_url = result["videos"][0].get("url")
            elif "output" in result:
                video_url = result["output"]
    except Exception as e:
        print(f"[parallax fal] WARNING: unexpected result shape: {e}\nresult={result!r}", file=sys.stderr)

    if not video_url:
        print(f"[parallax fal] ERROR: no video URL in result: {result!r}", file=sys.stderr)
        return 1

    _emit(use_json, event="downloading", url=video_url)
    try:
        _download(video_url, output_path)
    except RuntimeError as e:
        _emit(use_json, event="error", message=str(e))
        print(f"[parallax fal] ERROR: {e}", file=sys.stderr)
        return 1

    _emit(use_json, event="done", path=str(output_path))
    return 0


def generate_image(
    spec: ModelSpec,
    prompt: str,
    aspect: str,
    seed: int | None,
    output_path: Path,
    use_json: bool,
) -> int:
    """Submit a text-to-image job, poll to completion, download result. Returns exit code."""
    key = _require_fal_key()
    os.environ["FAL_KEY"] = key

    try:
        import fal_client
    except ImportError:
        print("[parallax fal] ERROR: fal-client not installed.", file=sys.stderr)
        return 1

    model_args = _build_image_args(spec, prompt, aspect, seed)

    _emit(use_json, event="queued", model=spec.model_id, tier=spec.tier)

    try:
        result = fal_client.subscribe(
            spec.model_id,
            arguments=model_args,
            with_logs=True,
            on_queue_update=lambda update: _on_update(update, use_json),
        )
    except Exception as e:
        _emit(use_json, event="error", message=str(e))
        print(f"[parallax fal] ERROR: API call failed: {e}", file=sys.stderr)
        return 1

    # Extract image URL — fal returns {"images": [{"url": "..."}]}
    image_url = None
    try:
        if isinstance(result, dict) and "images" in result and result["images"]:
            image_url = result["images"][0].get("url")
    except Exception as e:
        print(f"[parallax fal] WARNING: unexpected result shape: {e}", file=sys.stderr)

    if not image_url:
        print(f"[parallax fal] ERROR: no image URL in result: {result!r}", file=sys.stderr)
        return 1

    # Honor --output exactly when the caller specified a suffix; only fall back
    # to the URL's extension when output_path has none. Previously this always
    # overrode the user's suffix with the URL's, silently producing foo.jpg when
    # the caller asked for foo.png — which broke downstream manifest refs.
    if output_path.suffix:
        actual_output = output_path
    else:
        url_name = image_url.split("?")[0].split("/")[-1]
        ext = "." + url_name.rsplit(".", 1)[-1] if "." in url_name else ".png"
        actual_output = output_path.with_suffix(ext)

    _emit(use_json, event="downloading", url=image_url)
    try:
        _download(image_url, actual_output)
    except RuntimeError as e:
        _emit(use_json, event="error", message=str(e))
        print(f"[parallax fal] ERROR: {e}", file=sys.stderr)
        return 1

    _emit(use_json, event="done", path=str(actual_output))
    return 0


def _on_update(update, use_json: bool) -> None:
    """Callback for fal_client queue updates."""
    try:
        import fal_client
        if isinstance(update, fal_client.InProgress):
            logs = getattr(update, "logs", None)
            if logs:
                for log in logs:
                    msg = log.get("message", "") if isinstance(log, dict) else str(log)
                    if msg:
                        _emit(use_json, event="in_progress", logs=msg)
            else:
                _emit(use_json, event="in_progress", logs="processing…")
        else:
            _emit(use_json, event="in_progress", logs=f"status: {type(update).__name__}")
    except Exception:
        _emit(use_json, event="in_progress", logs="processing…")
