"""
Real-world Playwright test — minimal still generation (SPENDS MONEY).

Drives the beta web UI with a tight, deterministic brief that asks for
just two portrait stills (no voiceover, no compose). Hits the real
Gemini image generation API and a handful of Claude turns to orchestrate
the tool calls. No ElevenLabs, no ffmpeg render.

Estimated cost per run: ~$0.10–0.15 (2x Gemini images + Claude tokens).

Not part of CI — run manually when you want to verify end-to-end against
the real services. Requires a live ANTHROPIC_API_KEY and AI_VIDEO_GEMINI_KEY
in the shell env, same as `parallax chat`.

Safety: refuses to run without --yes-spend. The flag is also required for
any automation wrapper so nobody burns credits by accident.

Run:
    python3 test/playwright/real_minimal_stills.py --yes-spend
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _helpers import start_server, stop_server  # noqa: E402

SCRATCH_DIR = Path("/tmp/parallax-beta-real-minimal")
TIMEOUT_S = 360.0  # 6 minutes — enough for Claude planning + 2 real stills
EST_COST_USD = 0.12

# Deterministic, tight brief: two stills, no voiceover, no compose.
# Phrasing pushes Claude to call parallax_create once and stop.
PROMPT = (
    "Generate exactly two portrait stills (9:16 aspect ratio) of a small "
    "wooden cabin in a foggy pine forest at dawn. Do not add voiceover, "
    "do not assemble a video, do not run compose. Only call parallax_create "
    "once with count=2. When the two stills are on disk, reply with "
    "\"Stills ready.\" and stop."
)


def _wait_for_two_real_stills(scratch_dir: Path, timeout_s: float) -> list[Path]:
    """
    Poll parallax/users/*/*/stills/*.png until we have >=2 files that
    look like real Gemini output (each >100 KB). The TEST MODE placeholder
    generator produces ~21 KB PNGs, so the size gate distinguishes them
    from a silently-stubbed run.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        hits = sorted(scratch_dir.glob("parallax/users/*/*/stills/*.png"))
        real = [p for p in hits if p.is_file() and p.stat().st_size > 100 * 1024]
        if len(real) >= 2:
            return real[:2]
        time.sleep(3.0)
    raise TimeoutError(
        f"no >=2 real stills appeared in {scratch_dir} within {timeout_s}s"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--yes-spend",
        action="store_true",
        help="acknowledge that this test spends real API credits",
    )
    args = ap.parse_args()
    if not args.yes_spend:
        print(
            "[test] refusing to run without --yes-spend.\n"
            f"[test] estimated cost: ~${EST_COST_USD:.2f} "
            "(2x Gemini image + Claude orchestration tokens).\n"
            "[test] re-run with:\n"
            "         python3 test/playwright/real_minimal_stills.py --yes-spend",
            file=sys.stderr,
        )
        return 2

    print(f"[test] starting parallax chat in {SCRATCH_DIR}")
    print(f"[test] estimated cost: ~${EST_COST_USD:.2f}")
    started = time.time()
    proc, url = start_server(SCRATCH_DIR)
    print(f"[test] server URL: {url}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            page = context.new_page()
            page.on("console", lambda m: print(f"[browser:{m.type}] {m.text}"))
            page.on("pageerror", lambda e: print(f"[browser:error] {e}"))

            print(f"[test] navigating to {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_selector("#composer-input", timeout=10000)
            page.fill("#composer-input", PROMPT)
            page.click("#send-btn")
            print(f"[test] prompt submitted, waiting for real Gemini stills...")

            stills = _wait_for_two_real_stills(SCRATCH_DIR, timeout_s=TIMEOUT_S)
            total_bytes = sum(p.stat().st_size for p in stills)
            print(
                f"[test] OK: {len(stills)} real stills "
                f"({total_bytes/1024:.0f} KB total)"
            )
            for p in stills:
                rel = p.relative_to(SCRATCH_DIR)
                print(f"        {rel} ({p.stat().st_size:,} bytes)")

            shot = SCRATCH_DIR / "real_minimal_final.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[test] screenshot: {shot}")
            browser.close()

        elapsed = time.time() - started
        print(f"[test] elapsed: {elapsed:.1f}s")
        return 0
    except Exception as e:
        import traceback
        print(f"[test] FAILED: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        print("[test] stopping server")
        stop_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())
