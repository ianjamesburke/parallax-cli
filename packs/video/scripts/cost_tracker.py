"""Cost tracking utility for video production pipeline.

Appends cost events to assets/costs.jsonl as scripts run.
report.py reads this file to generate session reports.

Update PRICES when provider rates change.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Price table — update when rates change
# All costs in USD.
# ---------------------------------------------------------------------------
PRICES = {
    # Gemini image generation (per image, Flash model)
    "gemini_image": 0.02,

    # ElevenLabs TTS — per character, eleven_v3 Creator tier
    "elevenlabs_per_char": 0.0003,

    # FAL — per second of output video
    "fal_omnihuman":  0.01,    # OmniHuman v1.5
    "fal_birefnet":   0.0067,  # BiRefNet video BG removal
    "fal_kling":      0.045,   # Kling 2.5 Turbo
    "fal_ltx":        0.04,    # LTX image-to-video
    "fal_veo":        0.15,    # Veo (Gemini)

    # FAL — fixed per call
    "fal_kling_5s":   0.225,   # Kling 5s fixed cost
    "fal_kling_10s":  0.45,    # Kling 10s fixed cost

    # Gemini Veo — per video (flat rate, ~8s clip)
    # Pricing approximate as of 2025-04 (preview tier)
    "veo_3_fast":     0.50,    # Veo 3 Fast per clip
    "veo_3":          0.80,    # Veo 3 standard per clip
    "veo_2":          0.40,    # Veo 2 per clip
}

COSTS_FILENAME = "assets/costs.jsonl"


def _costs_path(project_dir: Path) -> Path:
    p = project_dir / COSTS_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append(project_dir: Path, record: dict):
    record["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(_costs_path(project_dir), "a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Tracking calls — one per API type
# ---------------------------------------------------------------------------

def track_still(project_dir: Path, scene_idx: int, model: str = "gemini-2.5-flash-image"):
    """Track one Gemini image generation call."""
    cost = PRICES["gemini_image"]
    _append(project_dir, {
        "type": "still",
        "scene": scene_idx,
        "model": model,
        "cost": cost,
    })


def track_vo(project_dir: Path, char_count: int, provider: str, voice_name: str):
    """Track one ElevenLabs VO generation call."""
    if provider != "elevenlabs":
        # Recorded or other provider — no cost
        _append(project_dir, {
            "type": "vo",
            "provider": provider,
            "voice": voice_name,
            "chars": char_count,
            "cost": 0.0,
        })
        return

    cost = round(char_count * PRICES["elevenlabs_per_char"], 4)
    _append(project_dir, {
        "type": "vo",
        "provider": provider,
        "voice": voice_name,
        "chars": char_count,
        "cost": cost,
    })


def track_veo(project_dir: Path, model: str, duration_s: float = 8):
    """Track one Gemini Veo video generation call."""
    # Map model ID to price key
    if "fast" in model.lower():
        if "3.0" in model or "3.1" in model:
            key = "veo_3_fast"
        else:
            key = "veo_2"
    elif "3.0" in model or "3.1" in model:
        key = "veo_3"
    else:
        key = "veo_2"
    cost = PRICES.get(key, PRICES["veo_3_fast"])
    _append(project_dir, {
        "type": "veo",
        "model": model,
        "duration_s": round(duration_s, 2),
        "cost": cost,
    })


def track_fal(project_dir: Path, model: str, duration_s: float):
    """Track one FAL API call (video generation, lipsync, BG removal)."""
    key = f"fal_{model.lower().replace('-', '_').replace(' ', '_')}"
    if key not in PRICES:
        raise ValueError(f"Unknown FAL model: {model!r}. Add it to PRICES in cost_tracker.py")
    rate = PRICES[key]
    cost = round(rate * duration_s, 4)
    _append(project_dir, {
        "type": "fal",
        "model": model,
        "duration_s": round(duration_s, 2),
        "cost": cost,
    })


# ---------------------------------------------------------------------------
# Summary — read costs.jsonl and return structured data
# ---------------------------------------------------------------------------

def load_events(project_dir: Path) -> list[dict]:
    p = _costs_path(project_dir)
    if not p.exists():
        return []
    events = []
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    except Exception as e:
        print(f"Warning: could not read costs.jsonl: {e}", file=sys.stderr)
    return events


def summarize(project_dir: Path) -> dict:
    """Return cost summary dict from costs.jsonl."""
    events = load_events(project_dir)
    total = round(sum(e.get("cost", 0) for e in events), 4)

    by_type: dict[str, float] = {}
    for e in events:
        t = e["type"]
        by_type[t] = round(by_type.get(t, 0) + e.get("cost", 0), 4)

    services = []
    if any(e["type"] == "still" for e in events):
        n = sum(1 for e in events if e["type"] == "still")
        services.append(f"Gemini image gen ({n} still{'s' if n != 1 else ''})")
    if any(e["type"] == "vo" for e in events):
        vo = next(e for e in events if e["type"] == "vo")
        if vo["provider"] == "elevenlabs":
            services.append(f"ElevenLabs ({vo['voice']}, {vo['chars']} chars)")
        else:
            services.append(f"VO: {vo['provider']}")
    for e in events:
        if e["type"] == "fal":
            services.append(f"FAL {e['model']} ({e['duration_s']}s)")

    return {
        "total": total,
        "by_type": by_type,
        "services": services,
        "events": events,
    }
