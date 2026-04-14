"""
Shared helpers for parallax-web Playwright tests.

The tests each boot a fresh `parallax chat` inside a throwaway scratch
directory and drive the browser against the served URL. This module
factors out the startup, URL detection, and video-polling logic so each
test file can focus on the thing it's actually checking.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PARALLAX_BIN = REPO_ROOT / "bin" / "parallax"

_URL_RE = re.compile(r"url\s*=\s*(http://127\.0\.0\.1:\d+/)")


def start_server(scratch_dir: Path) -> tuple[subprocess.Popen, str]:
    """
    Launch `parallax chat` with `scratch_dir` as cwd and block until it
    prints its URL. Returns (proc, url). The caller is responsible for
    stopping the process via stop_server() on the way out.

    Scratch dir is wiped and recreated so each test starts from a clean
    master directory.
    """
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    scratch_dir.mkdir(parents=True)

    env = os.environ.copy()
    env["PARALLAX_WEB_NO_BROWSER"] = "1"
    env["PARALLAX_SKIP_CLARIFICATIONS"] = "1"
    # Force the parallax_web child to emit stdout unbuffered, otherwise its
    # "parallax-web: url = ..." line sits in a 4KB pipe buffer forever and
    # this helper hangs waiting for a URL that never arrives.
    env["PYTHONUNBUFFERED"] = "1"
    # Pin the subprocess binary to this worktree so test-mode patches run.
    # Without this, shutil.which("parallax") may resolve to a different
    # branch via ~/.local/bin symlinks.
    env["PARALLAX_BIN"] = str(PARALLAX_BIN)
    # The server trigger-phrase path decides test mode — don't pre-force it.
    env.pop("TEST_MODE", None)

    proc = subprocess.Popen(
        [sys.executable, "-u", str(PARALLAX_BIN), "chat"],
        cwd=str(scratch_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    url = ""
    deadline = time.time() + 30
    assert proc.stdout is not None
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("parallax chat exited before printing URL")
            continue
        print(f"[server] {line.rstrip()}")
        m = _URL_RE.search(line)
        if m:
            url = m.group(1)
            break
    if not url:
        proc.kill()
        raise RuntimeError("timed out waiting for parallax chat URL")

    # Drain the rest of the server output in a background thread so the
    # subprocess doesn't stall on a full stdout pipe.
    def _drain() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                print(f"[server] {raw.rstrip()}")
        except Exception:
            pass

    threading.Thread(target=_drain, daemon=True).start()
    return proc, url


def stop_server(proc: subprocess.Popen) -> None:
    """SIGINT first, kill after 5s. Safe to call twice."""
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def wait_for_output_video(scratch_dir: Path, timeout_s: float = 420.0) -> Path:
    """
    Poll for a real (non-symlink) .mp4 under scratch_dir matching the
    canonical beta layout `parallax/users/<user>/<project>/output/*.mp4`.
    Raises TimeoutError if nothing shows up within the deadline.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for pattern in (
            "parallax/users/*/*/output/*.mp4",
            "parallax/*/output/*.mp4",
            "**/output/*.mp4",
        ):
            hits = sorted(scratch_dir.glob(pattern))
            real = [
                p for p in hits
                if p.is_file() and not p.is_symlink() and p.stat().st_size > 1024
            ]
            if real:
                return real[-1]
        time.sleep(2.0)
    raise TimeoutError(f"no video appeared in {scratch_dir} within {timeout_s}s")
