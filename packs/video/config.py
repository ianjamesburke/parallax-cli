"""
Project-local config loader for Parallax.

Discovers .parallax/config.toml by walking up from cwd. Missing file is fine — defaults apply.
Invalid model IDs fail fast with a clear error.

Precedence (highest to lowest):
  1. CLI --model flag (not handled here — caller overrides after load)
  2. Env vars: PARALLAX_FAL_VIDEO_LOW, PARALLAX_FAL_VIDEO_MEDIUM, PARALLAX_FAL_VIDEO_HIGH,
               PARALLAX_FAL_IMAGE_LOW, PARALLAX_FAL_IMAGE_MEDIUM, PARALLAX_FAL_IMAGE_HIGH
  3. .parallax/config.toml [fal.video] / [fal.image] sections
  4. Built-in defaults from packs/video/fal/models.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Built-in defaults — mirrors VIDEO_MODELS / IMAGE_MODELS in fal/models.py
_DEFAULT_VIDEO: dict[str, str] = {
    "low":    "fal-ai/ltx-2.3/text-to-video",
    "medium": "fal-ai/wan-t2v",
    "high":   "fal-ai/kling-video/v1.6/standard/text-to-video",
}
_DEFAULT_IMAGE: dict[str, str] = {
    "low":    "fal-ai/flux/schnell",
    "medium": "fal-ai/flux/dev",
    "high":   "fal-ai/flux-pro/v1.1",
}

_VALID_TIERS = ("low", "medium", "high")
_VALID_MODEL_KINDS = ("video", "image")


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


def _env_var_name(kind: str, tier: str) -> str:
    return f"PARALLAX_FAL_{kind.upper()}_{tier.upper()}"


class EffectiveConfig:
    """Holds the resolved tier→model_id map with source attribution."""

    def __init__(
        self,
        video: dict[str, tuple[str, str]],
        image: dict[str, tuple[str, str]],
        config_path: Optional[Path],
    ) -> None:
        # Each value is (model_id, source) where source is "default", "config", or "env"
        self.video = video
        self.image = image
        self.config_path = config_path

    def get_video_model(self, tier: str) -> str:
        if tier not in _VALID_TIERS:
            raise ValueError(f"[parallax config] Invalid tier {tier!r}. Valid: {_VALID_TIERS}")
        return self.video[tier][0]

    def get_image_model(self, tier: str) -> str:
        if tier not in _VALID_TIERS:
            raise ValueError(f"[parallax config] Invalid tier {tier!r}. Valid: {_VALID_TIERS}")
        return self.image[tier][0]

    def as_rows(self) -> list[dict]:
        """Return all entries as plain dicts for --json / tabular display."""
        rows = []
        for tier in _VALID_TIERS:
            model_id, source = self.video[tier]
            rows.append({"kind": "video", "tier": tier, "model_id": model_id, "source": source})
        for tier in _VALID_TIERS:
            model_id, source = self.image[tier]
            rows.append({"kind": "image", "tier": tier, "model_id": model_id, "source": source})
        return rows


def load(start: Optional[Path] = None) -> EffectiveConfig:
    """
    Load effective config by merging defaults → config.toml → env vars.
    Fails fast if a config entry is not a non-empty string.
    """
    config_path = _find_config_file(start)
    toml_data: dict = {}
    if config_path is not None:
        toml_data = _load_toml(config_path)

    toml_video = toml_data.get("fal", {}).get("video", {})
    toml_image = toml_data.get("fal", {}).get("image", {})

    def _resolve(kind: str, tier: str, defaults: dict[str, str], toml_section: dict) -> tuple[str, str]:
        # env var wins over config
        env_key = _env_var_name(kind, tier)
        env_val = os.environ.get(env_key, "").strip()
        if env_val:
            if not isinstance(env_val, str) or "/" not in env_val:
                raise ValueError(
                    f"[parallax config] {env_key}={env_val!r} doesn't look like a valid model id "
                    f"(expected format: 'provider/model-name')"
                )
            return env_val, "env"

        # config.toml
        cfg_val = toml_section.get(tier, "")
        if cfg_val:
            if not isinstance(cfg_val, str) or not cfg_val.strip():
                raise ValueError(
                    f"[parallax config] {config_path}: [fal.{kind}].{tier} must be a non-empty string, "
                    f"got {cfg_val!r}"
                )
            cfg_val = cfg_val.strip()
            if "/" not in cfg_val:
                raise ValueError(
                    f"[parallax config] {config_path}: [fal.{kind}].{tier}={cfg_val!r} doesn't look "
                    f"like a valid model id (expected format: 'provider/model-name')"
                )
            return cfg_val, "config"

        return defaults[tier], "default"

    video = {tier: _resolve("video", tier, _DEFAULT_VIDEO, toml_video) for tier in _VALID_TIERS}
    image = {tier: _resolve("image", tier, _DEFAULT_IMAGE, toml_image) for tier in _VALID_TIERS}

    return EffectiveConfig(video=video, image=image, config_path=config_path)
