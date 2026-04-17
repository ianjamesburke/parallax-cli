"""
Project-local config loader for Parallax.

Discovers .parallax/config.toml by walking up from cwd. Missing file is fine — defaults apply.
Invalid model IDs fail fast with a clear error.

Precedence (highest to lowest):
  1. CLI --model flag (not handled here — caller overrides after load)
  2. Env vars: PARALLAX_FAL_VIDEO_T2V_LOW, PARALLAX_FAL_VIDEO_T2V_MEDIUM, ...
               PARALLAX_FAL_VIDEO_I2V_LOW, ...  (mode-scoped)
               PARALLAX_FAL_VIDEO_LOW, ...       (legacy, treated as t2v)
               PARALLAX_FAL_IMAGE_LOW, PARALLAX_FAL_IMAGE_MEDIUM, PARALLAX_FAL_IMAGE_HIGH
  3. .parallax/config.toml [fal.video.t2v] / [fal.video.i2v] sections
     Legacy flat [fal.video] with top-level tier keys treated as t2v + deprecation warning.
  4. Built-in defaults from packs/video/fal/models.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Built-in defaults — mirrors models.py registries
_DEFAULT_VIDEO_T2V: dict[str, str] = {
    "low":    "fal-ai/ltx-2.3/text-to-video",
    "medium": "fal-ai/wan-t2v",
    "high":   "fal-ai/kling-video/v1.6/standard/text-to-video",
}
_DEFAULT_VIDEO_I2V: dict[str, str] = {
    "low":    "fal-ai/ltx-2.3/image-to-video",
    "medium": "fal-ai/wan-i2v",
    "high":   "fal-ai/kling-video/v1.6/standard/image-to-video",
}
_DEFAULT_IMAGE: dict[str, str] = {
    "low":    "fal-ai/flux/schnell",
    "medium": "fal-ai/flux/dev",
    "high":   "fal-ai/flux-pro/v1.1",
}

_VALID_TIERS = ("low", "medium", "high")
_VALID_MODEL_KINDS = ("video", "image")
_VALID_VIDEO_MODES = ("t2v", "i2v")


def _find_config_file(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from start (default cwd) looking for .parallax/config.toml."""
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / ".parallax" / "config.toml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _load_toml(path: Path) -> dict:
    """Load a TOML file. Uses tomllib (3.11+) or falls back to tomli."""
    try:
        if sys.version_info >= (3, 11):
            import tomllib
            try:
                return tomllib.loads(path.read_text())
            except Exception as e:
                raise ValueError(f"[parallax config] TOML parse error in {path}: {e}") from e
        else:
            try:
                import tomli  # type: ignore[import]
                try:
                    return tomli.loads(path.read_text())
                except Exception as e:
                    raise ValueError(f"[parallax config] TOML parse error in {path}: {e}") from e
            except ImportError:
                raise ImportError(
                    "[parallax config] Python < 3.11 requires `tomli` for TOML parsing. "
                    "Run: pip install tomli"
                )
    except (ValueError, ImportError):
        raise
    except Exception as e:
        raise RuntimeError(f"[parallax config] Could not read {path}: {e}") from e


def _env_var_name(kind: str, tier: str, mode: Optional[str] = None) -> str:
    if mode:
        return f"PARALLAX_FAL_{kind.upper()}_{mode.upper()}_{tier.upper()}"
    return f"PARALLAX_FAL_{kind.upper()}_{tier.upper()}"


def _validate_model_id(value: str, context: str) -> str:
    """Validate that value looks like a fal model id (contains /). Return stripped value."""
    value = value.strip()
    if not value or "/" not in value:
        raise ValueError(
            f"[parallax config] {context}: {value!r} doesn't look like a valid model id "
            f"(expected format: 'provider/model-name')"
        )
    return value


class EffectiveConfig:
    """Holds the resolved tier→model_id map with source attribution."""

    def __init__(
        self,
        video_t2v: dict[str, tuple[str, str]],
        video_i2v: dict[str, tuple[str, str]],
        image: dict[str, tuple[str, str]],
        config_path: Optional[Path],
    ) -> None:
        # Each value is (model_id, source) where source is "default", "config", or "env"
        self.video_t2v = video_t2v
        self.video_i2v = video_i2v
        # Legacy attribute — points at t2v for backward compat
        self.video = video_t2v
        self.image = image
        self.config_path = config_path

    def get_video_model(self, tier: str, mode: str = "t2v") -> str:
        if tier not in _VALID_TIERS:
            raise ValueError(f"[parallax config] Invalid tier {tier!r}. Valid: {_VALID_TIERS}")
        if mode == "i2v":
            return self.video_i2v[tier][0]
        return self.video_t2v[tier][0]

    def get_video_model_sourced(self, tier: str, mode: str = "t2v") -> tuple[str, str]:
        if tier not in _VALID_TIERS:
            raise ValueError(f"[parallax config] Invalid tier {tier!r}. Valid: {_VALID_TIERS}")
        if mode == "i2v":
            return self.video_i2v[tier]
        return self.video_t2v[tier]

    def get_image_model(self, tier: str) -> str:
        if tier not in _VALID_TIERS:
            raise ValueError(f"[parallax config] Invalid tier {tier!r}. Valid: {_VALID_TIERS}")
        return self.image[tier][0]

    def as_rows(self) -> list[dict]:
        """Return all entries as plain dicts for --json / tabular display."""
        rows = []
        for mode, registry in (("t2v", self.video_t2v), ("i2v", self.video_i2v)):
            for tier in _VALID_TIERS:
                model_id, source = registry[tier]
                rows.append({"kind": "video", "mode": mode, "tier": tier, "model_id": model_id, "source": source})
        for tier in _VALID_TIERS:
            model_id, source = self.image[tier]
            rows.append({"kind": "image", "mode": "n/a", "tier": tier, "model_id": model_id, "source": source})
        return rows


def load(start: Optional[Path] = None) -> EffectiveConfig:
    """
    Load effective config by merging defaults → config.toml → env vars.
    Fails fast if a config entry is not a valid model id.

    Config file format (new, mode-scoped):
      [fal.video.t2v]
      low = "fal-ai/..."
      [fal.video.i2v]
      low = "fal-ai/..."

    Legacy format (flat [fal.video] with tier keys) is accepted as t2v + emits a deprecation warning.
    """
    config_path = _find_config_file(start)
    toml_data: dict = {}
    if config_path is not None:
        toml_data = _load_toml(config_path)

    fal_section = toml_data.get("fal", {})
    video_section = fal_section.get("video", {})
    toml_image = fal_section.get("image", {})

    # Detect legacy flat [fal.video] format: keys are tier names directly
    # New format: keys are mode names ("t2v", "i2v") pointing to sub-dicts
    toml_video_t2v: dict = {}
    toml_video_i2v: dict = {}
    if video_section:
        if any(k in video_section for k in ("t2v", "i2v")):
            # New mode-scoped format
            toml_video_t2v = video_section.get("t2v", {})
            toml_video_i2v = video_section.get("i2v", {})
        elif any(k in video_section for k in _VALID_TIERS):
            # Legacy flat format — treat as t2v + warn
            print(
                f"[parallax config] DEPRECATION: {config_path}: [fal.video] with flat tier keys "
                "is deprecated. Use [fal.video.t2v] and [fal.video.i2v] subsections.",
                file=sys.stderr,
            )
            toml_video_t2v = video_section

    def _resolve(
        kind: str,
        tier: str,
        defaults: dict[str, str],
        toml_section: dict,
        mode: Optional[str] = None,
    ) -> tuple[str, str]:
        # Mode-scoped env var wins over legacy env var
        if mode:
            env_key_scoped = _env_var_name(kind, tier, mode)
            env_val = os.environ.get(env_key_scoped, "").strip()
            if env_val:
                return _validate_model_id(env_val, env_key_scoped), "env"

        # Legacy env var (treated as t2v for video)
        if kind == "video" and mode in (None, "t2v"):
            env_key_legacy = _env_var_name(kind, tier)
            env_val = os.environ.get(env_key_legacy, "").strip()
            if env_val:
                return _validate_model_id(env_val, env_key_legacy), "env"
        elif kind == "image":
            env_key = _env_var_name(kind, tier)
            env_val = os.environ.get(env_key, "").strip()
            if env_val:
                return _validate_model_id(env_val, env_key), "env"

        # config.toml
        cfg_val = toml_section.get(tier, "")
        if cfg_val:
            ctx = f"{config_path}: [fal.{kind}{('.' + mode) if mode else ''}].{tier}"
            return _validate_model_id(cfg_val, ctx), "config"

        return defaults[tier], "default"

    video_t2v = {
        tier: _resolve("video", tier, _DEFAULT_VIDEO_T2V, toml_video_t2v, mode="t2v")
        for tier in _VALID_TIERS
    }
    video_i2v = {
        tier: _resolve("video", tier, _DEFAULT_VIDEO_I2V, toml_video_i2v, mode="i2v")
        for tier in _VALID_TIERS
    }
    image = {
        tier: _resolve("image", tier, _DEFAULT_IMAGE, toml_image)
        for tier in _VALID_TIERS
    }

    return EffectiveConfig(video_t2v=video_t2v, video_i2v=video_i2v, image=image, config_path=config_path)
