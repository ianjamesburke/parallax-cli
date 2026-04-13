#!/usr/bin/env python3
"""Interactive API key setup for the video-production skill.

Pops native macOS dialogs to collect keys — values are written directly
to the .env file and never pass through the agent or chat history.

Usage:
  python3 setup-keys.py           # guided setup for all keys
  python3 setup-keys.py --check   # show current key status only
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
ENV_PATH = SKILL_ROOT / ".env"
ENV_EXAMPLE_PATH = SKILL_ROOT / ".env.example"

SERVICES = [
    {
        "key": "AI_VIDEO_GEMINI_KEY",
        "name": "Gemini",
        "description": "Image generation + scene planning",
        "required": True,
        "how_to_get": (
            "1. Go to: https://aistudio.google.com/app/apikey\n"
            "2. Sign in with a Google account\n"
            "3. Click \"Create API key\"\n"
            "4. Copy the key and paste it below"
        ),
    },
    {
        "key": "AI_VIDEO_ELEVENLABS_KEY",
        "name": "ElevenLabs",
        "description": "Voiceover generation",
        "required": False,
        "how_to_get": (
            "1. Go to: https://elevenlabs.io and sign up (free tier available)\n"
            "2. Click your profile icon → API Keys\n"
            "3. Click \"Create API Key\", give it a name\n"
            "4. Copy the key and paste it below"
        ),
    },
    {
        "key": "AI_VIDEO_FAL_KEY",
        "name": "FAL.ai",
        "description": "Animated video scene generation",
        "required": False,
        "how_to_get": (
            "1. Go to: https://fal.ai and sign up\n"
            "2. Go to: https://fal.ai/dashboard/keys\n"
            "3. Click \"Add key\", copy it\n"
            "4. Paste it below"
        ),
    },
]
# Frame.io credentials are managed separately via the frameio skill.
# Run: python3 ~/.agents/skills/frameio/scripts/setup-keys.py


def _osascript(script: str) -> str | None:
    """Run an AppleScript and return stdout, or None if cancelled."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return None  # User cancelled
        return result.stdout.strip()
    except FileNotFoundError:
        return None  # osascript not available (non-macOS)


_SKIP = object()  # Sentinel: user skipped this key, continue to next
_ABORT = None     # Sentinel: user wants to exit setup entirely


def _prompt_key(service: dict, current_value: str | None):
    """Show a native macOS dialog to collect a key.

    Returns:
      str      — key value to save
      _SKIP    — user pressed Skip, move to next service
      None     — user pressed Escape / closed dialog, abort setup
    """
    label = service["name"]
    desc = service["description"]
    how_to_get = service.get("how_to_get", "")
    required = service["required"]
    optional_note = "" if required else " (optional)"

    if current_value:
        status_line = "\n\nStatus: Already set ✓ — enter a new value to replace, or Skip to keep."
    else:
        status_line = "\n\nStatus: Not set"

    message = (
        f"{label} — {desc}{optional_note}\n\n"
        f"How to get your key:\n{how_to_get}"
        f"{status_line}"
    )

    script = (
        f'display dialog {_osa_quote(message)} '
        f'default answer "" '
        f'with hidden answer '
        f'buttons {{"Skip", "Save"}} '
        f'default button "Save" '
        f'with title "Video Production — {label} API Key"'
    )

    raw = _osascript(script)
    if raw is None:
        return _ABORT  # Escape / window closed → exit setup

    # Parse: "button returned:Save, text returned:the-actual-key"
    try:
        button = raw.split("button returned:")[-1].split(",")[0].strip()
        if button == "Skip":
            return _SKIP
        text_part = raw.split("text returned:")[-1].strip()
        return text_part if text_part else _SKIP
    except Exception:
        return _SKIP


def _osa_quote(s: str) -> str:
    """Escape a string for use in AppleScript."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _prompt_fallback(service: dict, current_value: str | None) -> str | None:
    """Terminal fallback when osascript is unavailable (non-macOS)."""
    import getpass
    label = service["name"]
    desc = service["description"]
    required = service["required"]
    optional = "" if required else " (optional, Enter to skip)"
    status = " [currently set]" if current_value else ""

    print(f"\n{label} — {desc}{status}{optional}")
    print(f"  Get your key at: {service['url']}")
    try:
        value = getpass.getpass("  Paste key: ").strip()
        return value if value else None
    except (KeyboardInterrupt, EOFError):
        return None


def _load_env() -> dict[str, str]:
    """Load current .env values (raw strings, no interpretation)."""
    env = {}
    if not ENV_PATH.exists():
        return env
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception as e:
        print(f"Warning: could not read {ENV_PATH}: {e}", file=sys.stderr)
    return env


def _save_env(env: dict[str, str]):
    """Write all key=value pairs to .env, preserving comments from .env.example."""
    lines = []

    # Start from .env.example to preserve comments and ordering
    if ENV_EXAMPLE_PATH.exists():
        for line in ENV_EXAMPLE_PATH.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                lines.append(line)
                continue
            if "=" in stripped:
                k = stripped.split("=")[0].strip()
                if k in env:
                    lines.append(f"{k}={env[k]}")
                    continue
            lines.append(line)
    else:
        # No example file — write flat
        for k, v in env.items():
            lines.append(f"{k}={v}")

    # Append any keys not in .env.example
    example_keys = set()
    if ENV_EXAMPLE_PATH.exists():
        for line in ENV_EXAMPLE_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                example_keys.add(line.split("=")[0].strip())
    for k, v in env.items():
        if k not in example_keys:
            lines.append(f"{k}={v}")

    try:
        ENV_PATH.write_text("\n".join(lines) + "\n")
    except Exception as e:
        print(f"ERROR: could not write {ENV_PATH}: {e}", file=sys.stderr)
        sys.exit(1)


def _is_falsy(val: str | None) -> bool:
    return not val or val.lower() in ("false", "none", "null", "", "your-gemini-api-key-here",
                                       "your-elevenlabs-api-key-here", "your-fal-api-key-here")


def _check_status():
    """Print current key status — checks .env file and shell env vars."""
    env = _load_env()

    print("\nAPI key status:")
    for svc in SERVICES:
        k = svc["key"]
        raw = env.get(k) or os.environ.get(k, "")
        if _is_falsy(raw):
            status = "MISSING"
            source = ""
        else:
            status = "OK"
            source = " (shell)" if not env.get(k) or _is_falsy(env.get(k, "")) else " (.env)"
        req = " (required)" if svc["required"] else " (optional)"
        print(f"  {svc['name']:<14} {status}{source}{req}")

    print()
    if all(not _is_falsy(env.get(s["key"]) or os.environ.get(s["key"], "")) for s in SERVICES if s["required"]):
        print("Required keys configured. You're ready to generate stills.")
    else:
        print("Run: python3 scripts/setup-keys.py   to add missing keys.")


def main():
    parser = argparse.ArgumentParser(description="Set up API keys for video production")
    parser.add_argument("--check", action="store_true", help="Show current key status and exit")
    parser.add_argument("--service", help="Set up one specific service by name (e.g. gemini)")
    args = parser.parse_args()

    if args.check:
        _check_status()
        return

    # Load current .env
    env = _load_env()

    # Determine which services to configure
    services = SERVICES
    if args.service:
        services = [s for s in SERVICES if s["name"].lower() == args.service.lower()]
        if not services:
            names = ", ".join(s["name"].lower() for s in SERVICES)
            print(f"Unknown service '{args.service}'. Available: {names}")
            sys.exit(1)

    # Check if osascript is available
    use_dialogs = _osascript('return "ok"') == "ok"
    if not use_dialogs:
        print("(Native dialogs unavailable — using terminal prompts)")

    # Gating confirmation — don't launch key dialogs without explicit consent
    if use_dialogs:
        confirm_msg = (
            "Video Production — API Key Setup\n\n"
            "This will walk you through setting up API keys for:\n"
            "  • Gemini — image generation (required)\n"
            "  • ElevenLabs — voiceover (optional)\n"
            "  • FAL.ai — animated video (optional)\n\n"
            "Keys are saved directly to disk. They will not be visible\n"
            "in the chat or stored in conversation history.\n\n"
            "For Frame.io, run the frameio skill setup separately:\n"
            "  python3 ~/.agents/skills/frameio/scripts/setup-keys.py\n\n"
            "You can skip any key by pressing Cancel on its dialog."
        )
        confirm = _osascript(
            f'display dialog {_osa_quote(confirm_msg)} '
            f'buttons {{"Cancel", "Set Up Keys"}} '
            f'default button "Set Up Keys" '
            f'with title "Video Production — Setup"'
        )
        if confirm is None:
            print("Setup cancelled.")
            return
    else:
        print(f"\nVideo Production — API Key Setup")
        print(f"Keys will be saved to: {ENV_PATH}")
        print(f"Values are written directly to disk and do not pass through the agent.")
        print(f"Press Ctrl+C at any time to cancel.\n")

    changed = False
    for svc in services:
        current_raw = env.get(svc["key"], "")
        current_value = None if _is_falsy(current_raw) else current_raw

        if use_dialogs:
            result = _prompt_key(svc, current_value)
        else:
            result = _prompt_fallback(svc, current_value)

        if result is _SKIP or result is None and use_dialogs:
            # _SKIP = pressed Skip button; None from dialogs = Escape/close = abort
            if result is None:
                print(f"\nSetup cancelled at {svc['name']}.")
                break
            if current_value:
                print(f"  {svc['name']}: kept existing")
            else:
                print(f"  {svc['name']}: skipped")
        elif isinstance(result, str) and not _is_falsy(result):
            env[svc["key"]] = result
            print(f"  {svc['name']}: saved")
            changed = True
        else:
            if current_value:
                print(f"  {svc['name']}: kept existing")
            else:
                print(f"  {svc['name']}: skipped")

    if changed:
        _save_env(env)
        print(f"\nSaved to {ENV_PATH}")
    else:
        print("\nNo changes made.")

    print()
    _check_status()


if __name__ == "__main__":
    main()
