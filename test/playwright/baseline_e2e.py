"""
Playwright E2E baseline — test mode.

Launches `parallax chat` inside a throwaway scratch workspace, opens the
served URL in headless Chromium, submits a prompt prefixed with TEST MODE
(so the server flips the session into TEST_MODE and every subprocess runs
without touching Gemini/ElevenLabs), waits for the dispatch pipeline to
finish, and asserts that a video file appears under output/.

Run:
    python3 test/playwright/baseline_e2e.py
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
PARALLAX_BIN = REPO_ROOT / "bin" / "parallax"
SCRATCH_DIR = Path("/tmp/parallax-beta-e2e-baseline")

PROMPT = (
    "TEST MODE — create three portrait stills of a quiet mountain cabin at "
    "dawn, then voice a short three-line narration and assemble a Ken Burns "
    "ad with captions."
)

URL_RE = re.compile(r"url\s*=\s*(http://127\.0\.0\.1:\d+/)")


def _start_server() -> tuple[subprocess.Popen, str]:
    """Launch parallax chat in SCRATCH_DIR, block until it prints its URL."""
    if SCRATCH_DIR.exists():
        shutil.rmtree(SCRATCH_DIR)
    SCRATCH_DIR.mkdir(parents=True)

    env = os.environ.copy()
    env["PARALLAX_WEB_NO_BROWSER"] = "1"
    env["PARALLAX_SKIP_CLARIFICATIONS"] = "1"
    # Force the child python (cmd_chat -> parallax_web) to emit stdout
    # unbuffered, otherwise its "parallax-web: url = ..." print sits in a
    # 4KB buffer forever and _start_server hangs waiting for a line.
    env["PYTHONUNBUFFERED"] = "1"
    # Pin the subprocess parallax binary to *this* worktree, so our TEST_MODE
    # patches actually run. Without this, shutil.which("parallax") resolves
    # to ~/.local/bin/parallax -> main branch bin/parallax (no test-mode
    # stubs), the Gemini calls fire, and the test looks broken.
    env["PARALLAX_BIN"] = str(PARALLAX_BIN)
    # We rely on the server's TEST MODE trigger phrase, not env forcing,
    # so DON'T set TEST_MODE here — that's the whole point of the test.
    env.pop("TEST_MODE", None)

    proc = subprocess.Popen(
        [sys.executable, "-u", str(PARALLAX_BIN), "chat"],
        cwd=str(SCRATCH_DIR),
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
        m = URL_RE.search(line)
        if m:
            url = m.group(1)
            break
    if not url:
        proc.kill()
        raise RuntimeError("timed out waiting for parallax chat URL")

    # Drain server output in a background thread so subprocess doesn't stall.
    import threading
    def _drain():
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                print(f"[server] {raw.rstrip()}")
        except Exception:
            pass
    threading.Thread(target=_drain, daemon=True).start()

    return proc, url


def _wait_for_output_video(timeout_s: float = 240.0) -> Path:
    """Poll SCRATCH_DIR/.../output/*.mp4 until a real video appears."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        # Workspace layout under scratch dir:
        #   SCRATCH_DIR/<user>/main/output/*.mp4  (per-user mode)
        #   SCRATCH_DIR/output/*.mp4              (single-user mode)
        for pattern in ("*/main/output/*.mp4", "output/*.mp4", "**/output/*.mp4"):
            hits = sorted(SCRATCH_DIR.glob(pattern))
            # Ignore symlinks like output/latest.mp4
            real = [p for p in hits if p.is_file() and not p.is_symlink() and p.stat().st_size > 1024]
            if real:
                return real[-1]
        time.sleep(2.0)
    raise TimeoutError(f"no video appeared in {SCRATCH_DIR} within {timeout_s}s")


def main() -> int:
    print(f"[test] starting parallax chat in {SCRATCH_DIR}")
    proc, url = _start_server()
    print(f"[test] server URL: {url}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            page = context.new_page()
            page.on("console", lambda msg: print(f"[browser:{msg.type}] {msg.text}"))
            page.on("pageerror", lambda err: print(f"[browser:error] {err}"))

            print(f"[test] navigating to {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=15000)

            page.wait_for_selector("#composer-input", timeout=10000)
            page.fill("#composer-input", PROMPT)
            page.click("#send-btn")
            print(f"[test] prompt submitted, waiting for server pipeline...")

            # Wait for output video on disk (pipeline actually finished)
            video = _wait_for_output_video(timeout_s=420.0)
            print(f"[test] OK: video produced -> {video} ({video.stat().st_size:,} bytes)")

            # Screenshot for visual confirmation
            shot = SCRATCH_DIR / "baseline_final.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[test] screenshot: {shot}")

            browser.close()
        return 0
    except Exception as e:
        print(f"[test] FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        print("[test] stopping server")
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
