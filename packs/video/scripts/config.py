"""Shared config loader for video-production skill scripts.

Reads skill-config.yaml from the skill root. CLI flags always take precedence
over config values — this is just the fallback layer.
"""

import os
import yaml
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
CONFIG_PATH = SKILL_ROOT / "skill-config.yaml"
LOCAL_CONFIG_PATH = Path.home() / ".agents" / "config" / "video-production.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict:
    """Load skill-config.yaml, then deep-merge ~/.agents/config/video-production.yaml on top.

    The local override file wins on any key it defines. Expanding ~ in path values.
    Returns empty dict if neither file is found.
    """
    raw = {}

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                raw = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[config] Warning: could not read {CONFIG_PATH}: {e}")

    if LOCAL_CONFIG_PATH.exists():
        try:
            with open(LOCAL_CONFIG_PATH) as f:
                local = yaml.safe_load(f) or {}
            raw = _deep_merge(raw, local)
        except Exception as e:
            print(f"[config] Warning: could not read {LOCAL_CONFIG_PATH}: {e}")

    return _expand_paths(raw)


def _expand_paths(obj):
    """Recursively expand ~ in string values."""
    if isinstance(obj, dict):
        return {k: _expand_paths(v) for k, v in obj.items()}
    if isinstance(obj, str):
        return os.path.expanduser(obj)
    return obj


def get(key_path: str, default=None):
    """Get a nested config value by dot-separated path, e.g. 'paths.obs_clips'."""
    cfg = load_config()
    parts = key_path.split(".")
    try:
        for part in parts:
            cfg = cfg[part]
        return cfg
    except (KeyError, TypeError):
        return default


def get_model_provider(capability: str, default: str = "agent") -> str:
    """Get configured provider for a capability (image_analysis, image_generation, video_generation, scene_planning).

    Returns 'agent' if not configured — meaning the calling LLM handles it at runtime.
    """
    return get(f"models.{capability}", default)


# Maps capability to the config key for its model name and a sensible default.
_MODEL_DEFAULTS = {
    "image_generation": ("models.gemini_image_model", "gemini-2.5-flash-image"),
    "scene_planning": ("models.gemini_text_model", "gemini-2.5-flash"),
    "image_analysis": ("models.gemini_vision_model", "gemini-2.5-flash-preview-05-20"),
}


def get_model_name(capability: str) -> str:
    """Get the configured model name for a capability.

    Reads from skill-config.yaml model name overrides, falling back to built-in defaults.
    """
    key, default = _MODEL_DEFAULTS.get(capability, (None, None))
    if key is None:
        raise ValueError(f"Unknown capability: {capability}")
    return get(key, default)
