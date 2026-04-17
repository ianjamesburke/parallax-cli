"""
Tier registry for fal.ai media generation.

Tiers are cost-first quality ladders: low = cheapest credible model,
medium = balanced quality/cost, high = top-tier.

Pricing verified 2026-04-16 from fal.ai/pricing and model pages.

VIDEO TIER PICKS
  low:    fal-ai/ltx-2.3/text-to-video  $0.02/clip  (LTX-2.3, supports aspect_ratio param)
  medium: fal-ai/wan-t2v          $0.20/clip (480p) / $0.40/clip (720p)  (Wan 2.1, solid quality)
  high:   fal-ai/kling-video/v1.6/standard/text-to-video  ~$0.056/sec (Kling 1.6, cinematic quality)

IMAGE TIER PICKS
  low:    fal-ai/flux/schnell     ~$0.003/image  (FLUX.1 schnell, 1-4 steps, very fast)
  medium: fal-ai/flux/dev         ~$0.025/image  (FLUX.1 dev, better quality)
  high:   fal-ai/flux-pro/v1.1    ~$0.05/image   (FLUX.1 pro v1.1, highest fidelity)
"""

from dataclasses import dataclass
from typing import Literal

MediaKind = Literal["video", "image"]
Tier = Literal["low", "medium", "high"]

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


VIDEO_MODELS: dict[Tier, ModelSpec] = {
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


def get_video_model(tier: Tier) -> ModelSpec:
    return VIDEO_MODELS[tier]


def get_image_model(tier: Tier) -> ModelSpec:
    return IMAGE_MODELS[tier]


def all_models() -> list[dict]:
    """Return all tier→model entries as plain dicts for --json output."""
    rows = []
    for tier, spec in VIDEO_MODELS.items():
        rows.append({
            "kind": "video",
            "tier": tier,
            "model_id": spec.model_id,
            "description": spec.description,
            "price": spec.price_note,
        })
    for tier, spec in IMAGE_MODELS.items():
        rows.append({
            "kind": "image",
            "tier": tier,
            "model_id": spec.model_id,
            "description": spec.description,
            "price": spec.price_note,
        })
    return rows
