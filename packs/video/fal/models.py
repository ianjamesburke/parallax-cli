"""
Tier registry for fal.ai media generation.

Tiers are cost-first quality ladders: low = cheapest credible model,
medium = balanced quality/cost, high = top-tier.

Pricing verified 2026-04-16 from fal.ai/pricing and model pages.

VIDEO TIER PICKS — TEXT-TO-VIDEO (t2v)
  low:    fal-ai/ltx-2.3/text-to-video  $0.02/clip  (LTX-2.3, supports aspect_ratio param)
  medium: fal-ai/wan-t2v          $0.20/clip (480p) / $0.40/clip (720p)  (Wan 2.1, solid quality)
  high:   fal-ai/kling-video/v1.6/standard/text-to-video  ~$0.056/sec (Kling 1.6, cinematic quality)

VIDEO TIER PICKS — IMAGE-TO-VIDEO (i2v)
  low:    fal-ai/ltx-2.3/image-to-video  ~$0.02/clip  (LTX-2.3, supports start + end frame)
  medium: fal-ai/wan-i2v                 ~$0.20/clip   (Wan i2v, 480p/720p)
  high:   fal-ai/kling-video/v1.6/standard/image-to-video  ~$0.056/sec (Kling, supports tail_image_url)

IMAGE TIER PICKS
  low:    fal-ai/flux/schnell     ~$0.003/image  (FLUX.1 schnell, 1-4 steps, very fast)
  medium: fal-ai/flux/dev         ~$0.025/image  (FLUX.1 dev, better quality)
  high:   fal-ai/flux-pro/v1.1    ~$0.05/image   (FLUX.1 pro v1.1, highest fidelity)
"""

from dataclasses import dataclass, field
from typing import Literal

MediaKind = Literal["video", "image"]
Tier = Literal["low", "medium", "high"]
VideoMode = Literal["t2v", "i2v"]

# Aspect ratio string → fal.ai video_size enum value (varies by model)
_ASPECT_TO_VIDEO_SIZE = {
    "9:16": "portrait_9_16",
    "16:9": "landscape_16_9",
    "1:1": "square",
}

# Aspect ratio string → fal.ai image_size enum (used by flux models)
# NOTE: fal.ai Flux uses portrait_16_9 for tall/portrait orientation (9:16 output),
# not portrait_9_16. The enum naming is aspect-ratio-of-the-longer-axis, not WxH.
_ASPECT_TO_IMAGE_SIZE = {
    "9:16": "portrait_16_9",
    "16:9": "landscape_16_9",
    "1:1": "square",
    "4:5": "portrait_4_5",
}


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    tier: Tier
    kind: MediaKind
    description: str
    price_note: str
    # Which aspect_ratio → size mapping to use
    size_map: dict
    # Capability flags
    supports_image_to_video: bool = False
    supports_start_frame: bool = False   # accepts an image as start anchor (image_url)
    supports_end_frame: bool = False     # accepts a tail/end image (tail_image_url / end_image_url)
    supports_audio: bool = False         # can generate audio natively


VIDEO_T2V_MODELS: dict[Tier, ModelSpec] = {
    "low": ModelSpec(
        model_id="fal-ai/ltx-2.3/text-to-video",
        tier="low",
        kind="video",
        description="LTX-2.3 — fast, cheap text-to-video with aspect_ratio support",
        price_note="$0.02/clip (flat)",
        size_map=_ASPECT_TO_VIDEO_SIZE,
    ),
    "medium": ModelSpec(
        model_id="fal-ai/wan-t2v",
        tier="medium",
        kind="video",
        description="Wan 2.1 — solid quality text-to-video at 480p/720p",
        price_note="$0.20/clip (480p) or $0.40/clip (720p)",
        size_map=_ASPECT_TO_VIDEO_SIZE,
    ),
    "high": ModelSpec(
        model_id="fal-ai/kling-video/v1.6/standard/text-to-video",
        tier="high",
        kind="video",
        description="Kling 1.6 Standard — cinematic quality text-to-video",
        price_note="~$0.056/sec generated",
        size_map=_ASPECT_TO_VIDEO_SIZE,
    ),
}

VIDEO_I2V_MODELS: dict[Tier, ModelSpec] = {
    "low": ModelSpec(
        model_id="fal-ai/ltx-2.3/image-to-video",
        tier="low",
        kind="video",
        description="LTX-2.3 — image-to-video with start + end frame anchoring",
        price_note="~$0.02/clip (flat)",
        size_map=_ASPECT_TO_VIDEO_SIZE,
        supports_image_to_video=True,
        supports_start_frame=True,
        supports_end_frame=True,     # end_image_url param
    ),
    "medium": ModelSpec(
        model_id="fal-ai/wan-i2v",
        tier="medium",
        kind="video",
        description="Wan i2v — image-to-video at 480p/720p",
        price_note="~$0.20/clip (480p) or ~$0.40/clip (720p)",
        size_map=_ASPECT_TO_VIDEO_SIZE,
        supports_image_to_video=True,
        supports_start_frame=True,
        supports_end_frame=False,    # no tail_image_url on Wan i2v
    ),
    "high": ModelSpec(
        model_id="fal-ai/kling-video/v1.6/standard/image-to-video",
        tier="high",
        kind="video",
        description="Kling 1.6 Standard — cinematic image-to-video with optional end frame",
        price_note="~$0.056/sec generated",
        size_map=_ASPECT_TO_VIDEO_SIZE,
        supports_image_to_video=True,
        supports_start_frame=True,
        supports_end_frame=True,     # tail_image_url param
    ),
}

# Backward-compatible alias — existing code that imports VIDEO_MODELS still works
VIDEO_MODELS = VIDEO_T2V_MODELS

IMAGE_MODELS: dict[Tier, ModelSpec] = {
    "low": ModelSpec(
        model_id="fal-ai/flux/schnell",
        tier="low",
        kind="image",
        description="FLUX.1 schnell — 1-4 step generation, very fast and cheap",
        price_note="~$0.003/image",
        size_map=_ASPECT_TO_IMAGE_SIZE,
    ),
    "medium": ModelSpec(
        model_id="fal-ai/flux/dev",
        tier="medium",
        kind="image",
        description="FLUX.1 dev — better quality, balanced cost",
        price_note="~$0.025/image",
        size_map=_ASPECT_TO_IMAGE_SIZE,
    ),
    "high": ModelSpec(
        model_id="fal-ai/flux-pro/v1.1",
        tier="high",
        kind="image",
        description="FLUX.1 pro v1.1 — highest fidelity, sharpest detail",
        price_note="~$0.05/image",
        size_map=_ASPECT_TO_IMAGE_SIZE,
    ),
}


def get_video_model(tier: Tier, mode: VideoMode = "t2v", model_id_override=None) -> ModelSpec:
    """Return the ModelSpec for the given tier and mode, optionally overriding the model_id."""
    registry = VIDEO_I2V_MODELS if mode == "i2v" else VIDEO_T2V_MODELS
    spec = registry[tier]
    if model_id_override:
        spec = ModelSpec(
            model_id=model_id_override,
            tier=spec.tier,
            kind=spec.kind,
            description=f"(overridden) {model_id_override}",
            price_note="custom",
            size_map=spec.size_map,
            supports_image_to_video=spec.supports_image_to_video,
            supports_start_frame=spec.supports_start_frame,
            supports_end_frame=spec.supports_end_frame,
            supports_audio=spec.supports_audio,
        )
    return spec


def get_image_model(tier: Tier, model_id_override=None) -> ModelSpec:
    """Return the ModelSpec for the given tier, optionally overriding the model_id."""
    spec = IMAGE_MODELS[tier]
    if model_id_override:
        spec = ModelSpec(
            model_id=model_id_override,
            tier=spec.tier,
            kind=spec.kind,
            description=f"(overridden) {model_id_override}",
            price_note="custom",
            size_map=spec.size_map,
        )
    return spec


def all_models() -> list[dict]:
    """Return all tier→model entries as plain dicts for --json output.

    Call all_models_with_config() to get source attribution from .parallax/config.toml.
    """
    rows = []
    for mode, registry in (("t2v", VIDEO_T2V_MODELS), ("i2v", VIDEO_I2V_MODELS)):
        for tier, spec in registry.items():
            rows.append({
                "kind": "video",
                "mode": mode,
                "tier": tier,
                "model_id": spec.model_id,
                "description": spec.description,
                "price": spec.price_note,
                "supports_start_frame": spec.supports_start_frame,
                "supports_end_frame": spec.supports_end_frame,
                "supports_audio": spec.supports_audio,
            })
    for tier, spec in IMAGE_MODELS.items():
        rows.append({
            "kind": "image",
            "mode": "n/a",
            "tier": tier,
            "model_id": spec.model_id,
            "description": spec.description,
            "price": spec.price_note,
            "supports_start_frame": False,
            "supports_end_frame": False,
            "supports_audio": False,
        })
    return rows


def all_models_with_config() -> list[dict]:
    """Return effective tier→model rows with source attribution (default/config/env)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
    try:
        from packs.video.config import load as _load_config
        cfg = _load_config()
    except Exception:
        cfg = None

    rows = []
    for mode, registry in (("t2v", VIDEO_T2V_MODELS), ("i2v", VIDEO_I2V_MODELS)):
        for tier, spec in registry.items():
            if cfg:
                model_id, source = cfg.get_video_model_sourced(tier, mode)
            else:
                model_id, source = spec.model_id, "default"
            rows.append({
                "kind": "video",
                "mode": mode,
                "tier": tier,
                "model_id": model_id,
                "source": source,
                "description": spec.description if model_id == spec.model_id else f"(overridden) {model_id}",
                "price": spec.price_note,
                "supports_start_frame": spec.supports_start_frame,
                "supports_end_frame": spec.supports_end_frame,
                "supports_audio": spec.supports_audio,
            })
    for tier, spec in IMAGE_MODELS.items():
        if cfg:
            model_id, source = cfg.image[tier]
        else:
            model_id, source = spec.model_id, "default"
        rows.append({
            "kind": "image",
            "mode": "n/a",
            "tier": tier,
            "model_id": model_id,
            "source": source,
            "description": spec.description if model_id == spec.model_id else f"(overridden) {model_id}",
            "price": spec.price_note,
            "supports_start_frame": False,
            "supports_end_frame": False,
            "supports_audio": False,
        })
    return rows
