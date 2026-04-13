#!/usr/bin/env python3
"""Startup context router for the video-production skill.

Run this at the start of every session. Checks which services are configured
and prints exactly which reference docs to load and which to skip.

The agent reads this output instead of statically loading all references.
Unconfigured services get a one-line setup note — not the full usage docs.

Usage:
  python3 {baseDir}/scripts/skill-status.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Load .env if present
try:
    from dotenv import load_dotenv
    skill_root = Path(__file__).parent.parent
    for p in [skill_root / ".env", Path(".env")]:
        if p.exists():
            load_dotenv(p)
            break
except ImportError:
    pass


def _key(*names: str) -> bool:
    return any(os.environ.get(n) for n in names)


def check_services() -> dict:
    return {
        "gemini": _key("AI_VIDEO_GEMINI_KEY", "GEMINI_API_KEY"),
        "elevenlabs": _key("AI_VIDEO_ELEVENLABS_KEY", "ELEVENLABS_API_KEY"),
    }


# What to load when a service IS configured
ACTIVE_REFS = {
    "gemini": [
        "references/image-gen.md",
        "references/ad-pipeline.md",
    ],
    "elevenlabs": [
        "references/voiceover.md",
    ],
}

# One-line setup note when a service is NOT configured
SETUP_NOTES = {
    "gemini": (
        "Gemini (image gen + scene planning) not configured. "
        "Set AI_VIDEO_GEMINI_KEY to enable stills generation. "
        "See references/setup-api-keys.md."
    ),
    "elevenlabs": (
        "ElevenLabs (voiceover) not configured. "
        "Set AI_VIDEO_ELEVENLABS_KEY to enable VO generation. "
        "Animatic and stills-only workflows still work without it. "
        "See references/setup-api-keys.md."
    ),
}

# Always-load references (core pipeline, no service required)
ALWAYS_LOAD = [
    "references/getting-started.md",
    "references/manifest-spec.md",
    "references/qa-checklist.md",
    "references/character-refs.md",
]


def main():
    services = check_services()
    active = [s for s, ok in services.items() if ok]
    inactive = [s for s, ok in services.items() if not ok]

    print("=== SKILL STATUS ===")
    print()

    # Always-load docs
    print("ALWAYS LOAD:")
    for ref in ALWAYS_LOAD:
        print(f"  {ref}")
    print()

    # Active services — load full docs
    if active:
        print("ACTIVE SERVICES (load full docs):")
        loaded = set()
        for service in active:
            print(f"  [{service}]")
            for ref in ACTIVE_REFS.get(service, []):
                if ref not in loaded:
                    print(f"    → {ref}")
                    loaded.add(ref)
        print()

    # Inactive services — setup note only, skip full docs
    if inactive:
        print("INACTIVE SERVICES (setup note only — do NOT load their full docs):")
        for service in inactive:
            print(f"  [{service}] {SETUP_NOTES[service]}")
        print()

    # Capability summary
    print("CAPABILITY SUMMARY:")
    caps = []
    caps.append("  ✓ Animatic wireframe (always available)")
    caps.append("  ✓ Footage editing (always available)")
    if services["gemini"]:
        caps.append("  ✓ Still generation (Gemini configured)")
        caps.append("  ✓ Scene planning (Gemini configured)")
        caps.append("  ✓ Character reference generation (Gemini configured)")
    else:
        caps.append("  ✗ Still generation (needs Gemini)")
    if services["elevenlabs"]:
        caps.append("  ✓ Voiceover generation (ElevenLabs configured)")
    else:
        caps.append("  ✗ Voiceover generation (needs ElevenLabs)")
    for c in caps:
        print(c)
    print()
    print("===================")


if __name__ == "__main__":
    main()
