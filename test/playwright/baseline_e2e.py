"""
Playwright E2E baseline — test mode, end-to-end pipeline.

Launches `parallax chat` inside a throwaway scratch workspace, opens the
served URL in headless Chromium, submits a prompt prefixed with TEST MODE
(so the server flips the session into TEST_MODE and every subprocess runs
without touching Gemini/ElevenLabs), waits for the dispatch pipeline to
finish, and asserts that a video file appears under the nested layout
`parallax/users/<user>/main/output/*.mp4`.

Run:
    python3 test/playwright/baseline_e2e.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import start_server, stop_server, wait_for_output_video

SCRATCH_DIR = Path("/tmp/parallax-beta-e2e-baseline")

PROMPT = (
    "TEST MODE — create three portrait stills of a quiet mountain cabin at "
    "dawn, then voice a short three-line narration and assemble a Ken Burns "
    "ad with captions."
)


def main() -> int:
    print(f"[test] starting parallax chat in {SCRATCH_DIR}")
    proc, url = start_server(SCRATCH_DIR)
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

            video = wait_for_output_video(SCRATCH_DIR, timeout_s=420.0)
            print(f"[test] OK: video produced -> {video} ({video.stat().st_size:,} bytes)")

            # Layout assertions — the new nested workspace must exist and
            # the master dir root must stay clean (only parallax/ + the
            # screenshot that lands there after this block).
            rel = video.relative_to(SCRATCH_DIR)
            parts = rel.parts
            assert parts[0] == "parallax", f"video not under parallax/: {rel}"
            assert parts[1] == "users", f"video not under users/: {rel}"
            assert parts[3] == "main", f"project should be 'main': {rel}"
            assert parts[4] == "output", f"fourth dir should be output/: {rel}"
            print(f"[test] layout OK: {'/'.join(parts[:5])}/")

            workspace = video.parent.parent
            for sub in ("stills", "input", "output", "drafts", "audio", "logs"):
                assert (workspace / sub).is_dir(), f"missing workspace subdir: {sub}"
            print(f"[test] workspace scaffold OK: {workspace}")

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
        stop_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())
