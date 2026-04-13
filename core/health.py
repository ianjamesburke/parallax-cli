"""
Health Check
============
Verifies all dependencies are available before Parallax runs.
Called on startup — reports what's ready, what's missing, what's degraded.
"""

import shutil
import subprocess
import sys
from pathlib import Path


# Bundled video-production scripts — parallax is self-contained.
SKILL_DIR = Path(__file__).resolve().parent.parent / "packs" / "video"
SCRIPTS_DIR = SKILL_DIR / "scripts"


def check_all() -> dict:
    """
    Run all health checks. Returns:
        {
            ready: bool,           # True if minimum viable set is available
            checks: list[dict],    # {name, status, detail}
            missing_critical: list, # names of critical failures
            missing_optional: list, # names of degraded capabilities
        }
    """
    checks = []
    checks.append(_check_binary("ffmpeg"))
    checks.append(_check_binary("ffprobe"))
    checks.append(_check_python_module("faster_whisper", "faster-whisper"))
    checks.append(_check_python_module("anthropic", "anthropic"))
    checks.append(_check_python_module("PIL", "Pillow"))
    checks.append(_check_skill_dir())
    checks.append(_check_python_module("google.genai", "google-genai", critical=False))
    checks.append(_check_python_module("elevenlabs", "elevenlabs", critical=False))
    checks.append(_check_binary("playwright", critical=False))

    missing_critical = [c["name"] for c in checks if c["status"] == "missing" and c.get("critical", True)]
    missing_optional = [c["name"] for c in checks if c["status"] == "missing" and not c.get("critical", True)]

    return {
        "ready": len(missing_critical) == 0,
        "checks": checks,
        "missing_critical": missing_critical,
        "missing_optional": missing_optional,
    }


def display(result: dict) -> str:
    """Format health check for terminal."""
    lines = ["[Parallax] Health Check"]
    for c in result["checks"]:
        icon = "ok" if c["status"] == "ok" else ("MISSING" if c.get("critical", True) else "optional")
        lines.append(f"  [{icon}] {c['name']}: {c['detail']}")

    if result["missing_critical"]:
        lines.append(f"\n  BLOCKED: {', '.join(result['missing_critical'])} must be installed")
    if result["missing_optional"]:
        lines.append(f"  Degraded: {', '.join(result['missing_optional'])} not available (some features disabled)")
    if result["ready"]:
        lines.append("\n  Ready to run.")
    return "\n".join(lines)


def _check_binary(name: str, critical: bool = True) -> dict:
    path = shutil.which(name)
    if path:
        try:
            version = subprocess.run([name, "-version" if name == "ffmpeg" else "--version"],
                                     capture_output=True, text=True, timeout=5)
            first_line = version.stdout.split("\n")[0][:80] if version.stdout else "installed"
        except Exception:
            first_line = "installed"
        return {"name": name, "status": "ok", "detail": first_line, "critical": critical}
    return {"name": name, "status": "missing", "detail": f"not found in PATH", "critical": critical}


def _check_python_module(module: str, pip_name: str, critical: bool = True) -> dict:
    try:
        __import__(module)
        return {"name": pip_name, "status": "ok", "detail": "importable", "critical": critical}
    except ImportError:
        return {"name": pip_name, "status": "missing",
                "detail": f"pip install {pip_name}", "critical": critical}


def _check_skill_dir() -> dict:
    if SCRIPTS_DIR.exists():
        count = len(list(SCRIPTS_DIR.glob("*.py")))
        return {"name": "bundled-video-scripts", "status": "ok",
                "detail": f"{count} scripts at {SCRIPTS_DIR}", "critical": True}
    return {"name": "bundled-video-scripts", "status": "missing",
            "detail": f"not found at {SKILL_DIR} — install looks corrupted", "critical": True}
