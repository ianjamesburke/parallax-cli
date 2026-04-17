"""
pricing.py — static cost table for every external model parallax touches.

Philosophy:
  - No runtime fetches. Providers don't publish a stable "current price"
    endpoint, and a network call on every dispatch is a latency bomb for
    no benefit. Update this file quarterly when provider pricing moves.
  - Every entry carries a `last_verified` date so stale rates are easy
    to spot.
  - Rates are conservative — if in doubt, round up. Under-estimating
    real spend is worse than over-estimating projected spend.
  - Shared by real-mode and test-mode emitters so TEST MODE doubles as
    a dry-run cost estimator for the same dispatch.

Units:
  - LLM models: $ / million tokens (input, output, cache_read, cache_write).
  - Image generation: $ / image.
  - TTS / voiceover: $ / character.

When this file grows beyond ~100 entries, promote it to a YAML file
loaded from disk — same schema, just a read at import time.
"""
from __future__ import annotations


LAST_VERIFIED = "2026-04-14"


# ── Anthropic (LLM) ─────────────────────────────────────────────────────────
# Source: https://www.anthropic.com/pricing
# Prompt caching reads are ~10% of base input, writes are ~125%.
ANTHROPIC: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {
        "input_per_mtok": 15.00,
        "output_per_mtok": 75.00,
        "cache_read_per_mtok": 1.50,
        "cache_write_per_mtok": 18.75,
    },
    "claude-sonnet-4-6": {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.30,
        "cache_write_per_mtok": 3.75,
    },
    "claude-haiku-4-5": {
        "input_per_mtok": 1.00,
        "output_per_mtok": 5.00,
        "cache_read_per_mtok": 0.10,
        "cache_write_per_mtok": 1.25,
    },
}


# ── Gemini (image generation) ──────────────────────────────────────────────
# Source: https://ai.google.dev/pricing — image-out entries.
# `gemini-3.1-flash-image-preview` is the model cmd_create uses today.
GEMINI: dict[str, dict[str, float]] = {
    "gemini-3.1-flash-image-preview": {
        "per_image": 0.04,  # TODO: re-verify — 3.1 preview pricing moves
    },
    "gemini-2.5-flash-image": {
        "per_image": 0.04,
    },
    "gemini-2.5-pro": {
        "input_per_mtok": 1.25,
        "output_per_mtok": 5.00,
    },
}


# ── ElevenLabs (voiceover) ──────────────────────────────────────────────────
# Source: https://elevenlabs.io/pricing
# ElevenLabs bills per character of input text. Rates here are the pay-as-
# you-go tier; subscription plans make real per-char cost cheaper.
ELEVENLABS: dict[str, dict[str, float]] = {
    "eleven_v3": {
        "per_char": 0.00030,  # ~$300 / million chars pay-as-you-go
    },
    "eleven_turbo_v2_5": {
        "per_char": 0.00015,
    },
    "eleven_flash_v2_5": {
        "per_char": 0.00010,
    },
}


# ── public helpers ──────────────────────────────────────────────────────────


def estimate_image_cost(model: str, count: int) -> float:
    """USD for generating `count` images with `model`. 0 if model unknown."""
    entry = GEMINI.get(model) or {}
    per_image = float(entry.get("per_image") or 0.0)
    return round(per_image * max(int(count or 0), 0), 6)


def estimate_voiceover_cost(model: str, char_count: int) -> float:
    """USD for synthesizing `char_count` characters with `model`."""
    entry = ELEVENLABS.get(model) or {}
    per_char = float(entry.get("per_char") or 0.0)
    return round(per_char * max(int(char_count or 0), 0), 6)


def estimate_llm_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD for a single Anthropic call with the given token breakdown."""
    entry = ANTHROPIC.get(model) or {}
    input_rate = float(entry.get("input_per_mtok") or 0.0)
    output_rate = float(entry.get("output_per_mtok") or 0.0)
    cache_read_rate = float(entry.get("cache_read_per_mtok") or 0.0)
    cache_write_rate = float(entry.get("cache_write_per_mtok") or 0.0)
    mtok = 1_000_000.0
    total = (
        (input_tokens / mtok) * input_rate
        + (output_tokens / mtok) * output_rate
        + (cache_read_tokens / mtok) * cache_read_rate
        + (cache_write_tokens / mtok) * cache_write_rate
    )
    return round(total, 6)


def model_known(provider: str, model: str) -> bool:
    """True if we have a rate for (provider, model) in the table."""
    table = {"anthropic": ANTHROPIC, "gemini": GEMINI, "elevenlabs": ELEVENLABS}.get(
        provider.lower(), {}
    )
    return model in table
