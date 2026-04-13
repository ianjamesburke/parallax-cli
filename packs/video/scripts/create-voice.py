#!/usr/bin/env python3
"""Clone or create an ElevenLabs voice from audio sample(s) and save to manifest.

Creates a voice clone on ElevenLabs, prints the voice_id, and optionally
writes the voice config into a project manifest so future VO generation
uses the same voice automatically.

Usage:
  create-voice.py --name "Brand Voice" --samples input/sample.mp3
  create-voice.py --name "Brand Voice" --samples input/s1.mp3 input/s2.mp3 --manifest manifest.yaml
  create-voice.py --name "Brand Voice" --samples input/sample.mp3 --description "Deep male, warm tone"
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api_config import get_elevenlabs_key


def create_voice(name: str, sample_paths: list[str], description: str = "", manifest_path: str = None):
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        print("ERROR: elevenlabs not installed. Run: pip install elevenlabs", file=sys.stderr)
        sys.exit(1)

    # Validate samples exist
    files = []
    for p in sample_paths:
        path = Path(p)
        if not path.exists():
            print(f"ERROR: Sample file not found: {p}", file=sys.stderr)
            sys.exit(1)
        files.append(path)

    print(f"Creating voice '{name}' from {len(files)} sample(s)...")

    client = ElevenLabs(api_key=get_elevenlabs_key())

    try:
        voice = client.clone(
            name=name,
            description=description or f"Cloned voice: {name}",
            files=[str(f) for f in files],
        )
    except Exception as e:
        print(f"ERROR: Voice creation failed: {e}", file=sys.stderr)
        sys.exit(1)

    voice_id = voice.voice_id
    print(f"Voice created: {name}")
    print(f"  voice_id: {voice_id}")

    # Write to manifest if provided
    if manifest_path:
        from manifest_schema import load_manifest, save_manifest, validate_manifest

        manifest = load_manifest(manifest_path)
        manifest["voice"] = {
            "voice_id": voice_id,
            "voice_name": name,
            "model_id": "eleven_v3",
            "stability": 0.5,
            "similarity_boost": 0.75,
        }
        save_manifest(manifest, manifest_path)
        validate_manifest(manifest, manifest_path)
        print(f"  Saved to manifest: {manifest_path}")

    print(f"\nUse with: generate-voiceover.py --manifest ... --voice {voice_id}")
    return voice_id


def main():
    parser = argparse.ArgumentParser(description="Clone/create an ElevenLabs voice from audio samples")
    parser.add_argument("--name", required=True, help="Name for the voice")
    parser.add_argument("--samples", nargs="+", required=True, help="Audio sample file(s) for cloning")
    parser.add_argument("--description", default="", help="Voice description")
    parser.add_argument("--manifest", default=None, help="Optional manifest.yaml to save voice config into")
    args = parser.parse_args()

    create_voice(args.name, args.samples, args.description, args.manifest)


if __name__ == "__main__":
    main()
