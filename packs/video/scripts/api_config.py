"""API key resolution for the video-production skill.

Checks AI_VIDEO_* prefixed env vars first, then falls back to standard names.
This lets users use a single consistent naming convention across the skill.

Setup (add to ~/.zshrc or ~/.zsh_secrets):
  export AI_VIDEO_GEMINI_KEY="your-key"       # image gen, scene planning
  export AI_VIDEO_FAL_KEY="your-key"          # video generation
  export AI_VIDEO_ELEVENLABS_KEY="your-key"   # voiceover

Standard names also work as fallbacks:
  GEMINI_API_KEY, FAL_KEY, ELEVENLABS_API_KEY

See references/setup-api-keys.md for full setup instructions.
"""

import os
import sys
from pathlib import Path


_PLACEHOLDER_VALUES = {
    "false", "none", "null", "",
    "your-gemini-api-key-here",
    "your-elevenlabs-api-key-here",
    "your-fal-api-key-here",
    "your-frameio-token-here",
    "your-youtube-client-secrets-path-here",
}


def _load_dotenv():
    """Load .env from skill root, then cwd as override. Raw parse fallback if dotenv unavailable."""
    skill_root = Path(__file__).parent.parent
    paths = [skill_root / ".env", Path(".env")]

    try:
        from dotenv import load_dotenv
        for path in paths:
            if path.exists():
                load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    # Fallback: manual parse (no dotenv installed)
    for path in paths:
        if not path.exists():
            continue
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k not in os.environ:  # don't override existing env
                    os.environ[k] = v
        except Exception:
            pass


_load_dotenv()


def _clean(val: str | None) -> str | None:
    """Return None for missing, 'false', or placeholder values."""
    if not val:
        return None
    if val.lower() in _PLACEHOLDER_VALUES:
        return None
    return val


def _resolve(primary: str, fallback: str) -> str | None:
    return _clean(os.environ.get(primary)) or _clean(os.environ.get(fallback))


def get_gemini_key() -> str:
    key = _resolve("AI_VIDEO_GEMINI_KEY", "GEMINI_API_KEY")
    if not key:
        print("ERROR: Gemini API key not set.", file=sys.stderr)
        print("  export AI_VIDEO_GEMINI_KEY='your-key'", file=sys.stderr)
        print("  See: references/setup-api-keys.md", file=sys.stderr)
        sys.exit(1)
    return key


def get_gemini_client():
    """Return a configured google.genai Client."""
    try:
        from google import genai
    except ImportError:
        print("ERROR: google-genai not installed. Run: pip install google-genai", file=sys.stderr)
        sys.exit(1)
    return genai.Client(api_key=get_gemini_key())


def get_fal_key() -> str:
    key = _resolve("AI_VIDEO_FAL_KEY", "FAL_KEY")
    if not key:
        print("ERROR: FAL.ai API key not set.", file=sys.stderr)
        print("  export AI_VIDEO_FAL_KEY='your-key'", file=sys.stderr)
        print("  See: references/setup-api-keys.md", file=sys.stderr)
        sys.exit(1)
    return key


def get_elevenlabs_key() -> str:
    key = _resolve("AI_VIDEO_ELEVENLABS_KEY", "ELEVENLABS_API_KEY")
    if not key:
        print("ERROR: ElevenLabs API key not set.", file=sys.stderr)
        print("  export AI_VIDEO_ELEVENLABS_KEY='your-key'", file=sys.stderr)
        print("  See: references/setup-api-keys.md", file=sys.stderr)
        sys.exit(1)
    return key


def check_keys() -> dict[str, bool]:
    """Return which API keys and credentials are currently configured."""
    return {
        "gemini": bool(_resolve("AI_VIDEO_GEMINI_KEY", "GEMINI_API_KEY")),
        "elevenlabs": bool(_resolve("AI_VIDEO_ELEVENLABS_KEY", "ELEVENLABS_API_KEY")),
    }


if __name__ == "__main__":
    """Run as a quick setup check: python3 api_config.py"""
    keys = check_keys()
    print("\nAPI key status:")
    for service, ok in keys.items():
        status = "OK" if ok else "MISSING"
        print(f"  {service:<14} {status}")
    if not all(keys.values()):
        missing = [s for s, ok in keys.items() if not ok]
        print(f"\nSome keys missing: {', '.join(missing)}")
        print("See references/setup-api-keys.md for setup instructions.")
    else:
        print("\nAll keys configured.")
