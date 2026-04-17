"""
Real-world Playwright test — full Ken Burns ad (SPENDS MONEY).

End-to-end smoke of the thing the product actually exists to produce:
three real Gemini stills, an ElevenLabs voiceover, a compose pass that
mixes audio + burns captions, and a final .mp4 under the per-user
workspace. This is the regression gate for "does parallax still ship
a real video?"

Estimated cost per run: ~$0.35–0.55 (3x Gemini images + ~20s of
ElevenLabs voice + Claude orchestration tokens).

Not part of CI. Requires ANTHROPIC_API_KEY, AI_VIDEO_GEMINI_KEY, and
ELEVENLABS_API_KEY (or AI_VIDEO_ELEVENLABS_KEY) exported in the shell.

Safety: refuses to run without --yes-spend.

Run:
    python3 test/playwright/real_full_ad.py --yes-spend
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _helpers import start_server, stop_server  # noqa: E402

SCRATCH_DIR = Path("/tmp/parallax-beta-real-full-ad")
TIMEOUT_S = 720.0  # 12 minutes — gives Claude room to plan + all three dispatches
EST_COST_USD = 0.45

# Tight, deterministic brief. Keeps Claude from meandering into extra
# manifest edits or asking clarifying questions. Matches the skeleton of
# a typical short-form ad.
PROMPT = (
    "Produce a 3-scene vertical Ken Burns ad (9:16) for a small-batch "
    "coffee roaster called Ember Coffee. "
    "Scene 1: close-up of dark coffee beans in a wooden scoop, warm light. "
    "Scene 2: steam rising from a black ceramic mug on a cafe counter. "
    "Scene 3: a bright cafe window with morning sun. "
    "Voiceover, 3 lines, one per scene, under 8 words each, warm and "
    "confident voice (use 'george'). Enable captions. Assemble the final "
    "video with audio mux. When the final .mp4 is on disk, reply with "
    "\"Ad ready.\" and stop."
)


def _probe_duration(mp4: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(mp4)],
            capture_output=True, text=True, check=True, timeout=15,
        )
        return float(out.stdout.strip() or "0")
    except Exception as e:
        print(f"[test] ffprobe failed: {e}", file=sys.stderr)
        return 0.0


def _wait_for_real_final(scratch_dir: Path, timeout_s: float) -> Path:
    """Poll for a real >=500 KB mp4 with a non-zero duration."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        hits = sorted(scratch_dir.glob("parallax/users/*/*/output/*.mp4"))
        real = [
            p for p in hits
            if p.is_file() and not p.is_symlink() and p.stat().st_size > 500 * 1024
        ]
        if real:
            latest = real[-1]
            # Make sure the write finished — duration > 0 and size stable.
            if _probe_duration(latest) > 0:
                return latest
        time.sleep(4.0)
    raise TimeoutError(f"no real mp4 appeared in {scratch_dir} within {timeout_s}s")


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
            "(3x Gemini images + ~20s ElevenLabs VO + Claude tokens).\n"
            "[test] re-run with:\n"
            "         python3 test/playwright/real_full_ad.py --yes-spend",
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
            print(f"[test] prompt submitted — this will take several minutes")

            video = _wait_for_real_final(SCRATCH_DIR, timeout_s=TIMEOUT_S)
            duration = _probe_duration(video)
            size_mb = video.stat().st_size / (1024 * 1024)
            rel = video.relative_to(SCRATCH_DIR)
            print(
                f"[test] OK: final video -> {rel} "
                f"({size_mb:.2f} MB, {duration:.2f}s)"
            )

            # Sanity checks on the full artifact:
            workspace = video.parent.parent
            stills = sorted((workspace / "stills").glob("*.png"))
            real_stills = [p for p in stills if p.stat().st_size > 100 * 1024]
            assert len(real_stills) >= 3, (
                f"expected >=3 real Gemini stills, got {len(real_stills)}"
            )
            vo_mp3 = workspace / "audio" / "voiceover.mp3"
            assert vo_mp3.is_file(), f"voiceover.mp3 missing at {vo_mp3}"
            assert vo_mp3.stat().st_size > 50 * 1024, (
                f"voiceover.mp3 too small — {vo_mp3.stat().st_size} bytes — "
                "probably a stub not a real ElevenLabs render"
            )
            vo_manifest = workspace / "audio" / "vo_manifest.json"
            assert vo_manifest.is_file(), "vo_manifest.json missing"
            print(
                f"[test] stills OK ({len(real_stills)} real), "
                f"voiceover OK ({vo_mp3.stat().st_size:,} bytes), "
                f"vo_manifest OK"
            )
            assert duration > 2.0, f"video duration too short: {duration}s"

            shot = SCRATCH_DIR / "real_full_ad_final.png"
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
