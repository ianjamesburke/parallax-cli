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


# ── Runner ────────────────────────────────────────────────────────────────────

TESTS = [
    ("projects lists or prints friendly message", test_projects_lists_or_friendly_message),
    ("project new creates structure", test_project_new_creates_structure),
    ("run produces manifest under .parallax/", test_run_produces_manifest),
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
