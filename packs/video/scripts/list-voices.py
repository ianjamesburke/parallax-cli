#!/usr/bin/env python3
"""List available ElevenLabs voices with ID, category, and labels.

By default shows only premade (official ElevenLabs) voices.
Use --all to include cloned and generated voices.

Usage:
  list-voices.py
  list-voices.py --all
  list-voices.py --category generated
  list-voices.py --json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api_config import get_elevenlabs_key


def list_voices(category_filter: str = "premade", output_json: bool = False):
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        print("ERROR: elevenlabs not installed. Run: pip install elevenlabs", file=sys.stderr)
        sys.exit(1)

    client = ElevenLabs(api_key=get_elevenlabs_key())

    try:
        response = client.voices.get_all()
    except Exception as e:
        print(f"ERROR: Failed to fetch voices: {e}", file=sys.stderr)
        sys.exit(1)

    voices = response.voices

    if category_filter != "all":
        voices = [v for v in voices if getattr(v, "category", "") == category_filter]

    voices = sorted(voices, key=lambda v: v.name or "")

    if output_json:
        out = []
        for v in voices:
            labels = dict(v.labels) if v.labels else {}
            out.append({
                "name": v.name,
                "voice_id": v.voice_id,
                "category": getattr(v, "category", ""),
                "labels": labels,
            })
        print(json.dumps(out, indent=2))
        return

    print(f"\n{'Name':<24} {'ID':<28} {'Category':<12} Labels")
    print("-" * 90)
    for v in voices:
        labels = dict(v.labels) if v.labels else {}
        label_str = ", ".join(f"{k}: {val}" for k, val in labels.items())
        category = getattr(v, "category", "")
        print(f"{v.name:<24} {v.voice_id:<28} {category:<12} {label_str}")

    print(f"\n{len(voices)} voices listed.")
    print("Use --voice <name_or_id> with generate-voiceover.py to select.")


def main():
    parser = argparse.ArgumentParser(description="List available ElevenLabs voices")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Include cloned and generated voices")
    group.add_argument("--category", default=None, help="Filter by category (premade, cloned, generated, professional)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.all:
        category_filter = "all"
    elif args.category:
        category_filter = args.category
    else:
        category_filter = "premade"

    list_voices(category_filter, args.json)


if __name__ == "__main__":
    main()
