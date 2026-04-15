#!/usr/bin/env python3.11
"""
CLI smoke tests for bin/parallax.

Usage:
    TEST_MODE=true python3.11 test/test_cli.py

All tests set TEST_MODE=true so no external API calls are made.
"""

import os
import sys
import subprocess
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin" / "parallax"


def _run_cli(args, cwd, extra_env=None):
    env = os.environ.copy()
    env["TEST_MODE"] = "true"
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


# ── Test 1: `parallax projects` ────────────────────────────────────────────────

def test_projects_lists_or_friendly_message():
    """`parallax projects` exits 0 and prints something sensible."""
    with tempfile.TemporaryDirectory() as tmp:
        r = _run_cli(["projects"], cwd=tmp)
    failures = []
    if r.returncode != 0:
        failures.append(f"exit={r.returncode}, stderr={r.stderr!r}")
    if not (r.stdout.strip() or r.stderr.strip()):
        failures.append("no output")
    return failures


# ── Test 2: `parallax project new <name>` ─────────────────────────────────────

def test_project_new_creates_structure():
    """`parallax project new` creates the expected directory layout."""
    # Use a unique name under a sandbox HOME so we don't pollute the real one.
    with tempfile.TemporaryDirectory() as tmp:
        fake_home = Path(tmp) / "home"
        fake_home.mkdir()
        name = "test-cli-project"
        r = _run_cli(
            ["project", "new", name],
            cwd=tmp,
            extra_env={"HOME": str(fake_home)},
        )
        failures = []
        if r.returncode != 0:
            failures.append(f"exit={r.returncode}, stderr={r.stderr!r}")
            return failures
        proj = fake_home / "Documents" / "parallax-projects" / name
        for sub in ("input", "output", "stills", "audio", "logs"):
            if not (proj / sub).is_dir():
                failures.append(f"missing subdir: {sub}")
        # stdout may show /private/var/... (macOS) while proj is /var/... — compare resolved.
        if proj.resolve().as_posix() not in r.stdout and str(proj) not in r.stdout:
            failures.append(f"expected absolute path in stdout, got {r.stdout!r}")
        return failures


# ── Test 3: `parallax run "test brief"` ────────────────────────────────────────

def test_run_produces_manifest():
    """`parallax run` under TEST_MODE exits 0 and creates a manifest under cwd/logs/<concept>/."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        r = _run_cli(["run", "test brief"], cwd=cwd)
        failures = []
        if r.returncode != 0:
            failures.append(f"exit={r.returncode}")
            failures.append(f"stdout={r.stdout[-2000:]}")
            failures.append(f"stderr={r.stderr[-2000:]}")
            return failures

        parallax_dir = cwd / "logs"
        if not parallax_dir.is_dir():
            failures.append("no logs/ created")
            return failures

        manifests = list(parallax_dir.glob("*/manifest.yaml"))
        if not manifests:
            failures.append(f"no manifest.yaml found under {parallax_dir}")
            failures.append(f"stdout={r.stdout[-1000:]}")
        return failures


# ── Test 4: V2 Mode 1 — `parallax generate still` ─────────────────────────────

def test_v2_generate_still_creates_png():
    """`parallax generate still` in TEST_MODE creates a real PNG and a manifest."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        r = _run_cli(["generate", "still", "cold brew coffee brand, moody lighting"], cwd=cwd)
        failures = []
        if r.returncode != 0:
            failures.append(f"exit={r.returncode}")
            failures.append(f"stdout={r.stdout[-1000:]}")
            failures.append(f"stderr={r.stderr[-1000:]}")
            return failures

        stills_dir = cwd / "stills"
        if not stills_dir.is_dir():
            failures.append("no stills/ directory created")
            return failures

        pngs = list(stills_dir.glob("*.png"))
        if not pngs:
            failures.append("no PNG files in stills/")
        else:
            # Verify real PNG (not just a placeholder byte string)
            png_bytes = pngs[0].read_bytes()
            if len(png_bytes) < 100:
                failures.append(f"PNG too small ({len(png_bytes)} bytes) — expected a real image")
            if not png_bytes.startswith(b"\x89PNG"):
                failures.append("file does not have a PNG header")

        manifest = cwd / "manifest.yaml"
        if not manifest.exists():
            failures.append("no manifest.yaml created")

        return failures


# ── Test 5: V2 Mode 3 — `parallax script write` ────────────────────────────────

def test_v2_script_write_produces_script():
    """`parallax script write` in TEST_MODE writes a structured script to --out file."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        out_file = cwd / "script.txt"
        r = _run_cli(
            ["script", "write", "a 30-second ad for a dog grooming business", "--out", str(out_file)],
            cwd=cwd,
        )
        failures = []
        if r.returncode != 0:
            failures.append(f"exit={r.returncode}")
            failures.append(f"stderr={r.stderr[-500:]}")
            return failures

        if not out_file.exists():
            failures.append("--out file not created")
            return failures

        content = out_file.read_text()
        if len(content.strip()) < 50:
            failures.append(f"script too short ({len(content)} chars)")
        if "Scene" not in content and "scene" not in content:
            failures.append("expected 'Scene' in script content")
        if "Brief" not in content and "brief" not in content.lower():
            failures.append("expected brief echo in script content")

        return failures


# ── Runner ────────────────────────────────────────────────────────────────────

TESTS = [
    ("projects lists or prints friendly message", test_projects_lists_or_friendly_message),
    ("project new creates structure", test_project_new_creates_structure),
    ("run produces manifest under .parallax/", test_run_produces_manifest),
    ("V2 Mode 1: generate still creates real PNG", test_v2_generate_still_creates_png),
    ("V2 Mode 3: script write produces structured script", test_v2_script_write_produces_script),
]


def main():
    os.environ["TEST_MODE"] = "true"
    if not CLI.exists():
        print(f"FAIL: CLI not found at {CLI}")
        return 1
    if not os.access(CLI, os.X_OK):
        print(f"FAIL: {CLI} is not executable")
        return 1

    passed = 0
    failed = 0
    for name, fn in TESTS:
        try:
            failures = fn()
        except Exception:
            print(f"FAIL  {name}")
            traceback.print_exc()
            failed += 1
            continue
        if failures:
            print(f"FAIL  {name}")
            for f in failures:
                print(f"      {f}")
            failed += 1
        else:
            print(f"PASS  {name}")
            passed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
