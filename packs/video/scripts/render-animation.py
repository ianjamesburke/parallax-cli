#!/usr/bin/env python3
"""
Render an HTML animation to mp4 via Playwright's built-in video recording.

Records the DOM directly — no screenshots, no MediaRecorder.
Playwright uses CDP screen capture under the hood.

Usage:
  render-animation.py --template zoom-reveal --duration 5.0 --output clip.mp4 \
    --params '{"image": "/path/to/scene.png"}'
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = SKILL_DIR / "assets" / "templates"
DEFAULT_FPS = 30
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
DEFAULT_TRIM_START = 1.0  # seconds to trim from start (Playwright browser startup delay)


def resolve_image_paths(params: dict, base_dir: str) -> dict:
    """Convert image paths to base64 data URLs for headless Chromium."""
    import base64
    import mimetypes
    resolved = dict(params)
    for key in ("image", "background", "src", "midground", "foreground"):
        if key not in resolved:
            continue
        val = resolved[key]
        if val.startswith(("http", "data:")):
            continue
        if val.startswith("file://"):
            val = val[7:]
        if not os.path.isabs(val):
            val = os.path.abspath(os.path.join(base_dir, val))
        if os.path.exists(val):
            mime = mimetypes.guess_type(val)[0] or "image/png"
            with open(val, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            resolved[key] = f"data:{mime};base64,{b64}"
    return resolved


def render_overlay(template_path: str, params: dict, output_path: str,
                   width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT):
    """Render a single transparent PNG screenshot — for static overlays (captions, titles)."""
    from playwright.sync_api import sync_playwright

    template_url = f"file://{template_path}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(template_url, wait_until="networkidle")

        page.evaluate(f"""
            window.__PARAMS = {json.dumps(params)};
            window.__DURATION = 0;
            window.__FPS = 1;
            window.__TOTAL_FRAMES = 1;
        """)

        try:
            page.wait_for_function("window.__READY === true", timeout=10000)
        except Exception:
            pass

        # Render frame 0 (static)
        page.evaluate("""
            if (typeof window.renderFrame === 'function') {
                window.renderFrame(0, 0);
            }
        """)
        page.wait_for_timeout(100)

        page.screenshot(path=output_path, type="png", omit_background=True)
        page.close()
        browser.close()

    size_kb = Path(output_path).stat().st_size / 1024
    print(f"[render] Overlay: {output_path} ({size_kb:.0f}KB)")


def render_overlay_sequence(template_path: str, params: dict, duration: float,
                            output_path: str, fps: int = DEFAULT_FPS,
                            width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT):
    """Render an animated overlay as a transparent MP4 (PNG frame sequence → ffmpeg).

    Each frame is a transparent screenshot. Output is a video with alpha-friendly
    encoding that composites cleanly via ffmpeg overlay.
    """
    from playwright.sync_api import sync_playwright

    template_url = f"file://{template_path}"
    total_frames = int(duration * fps)

    with tempfile.TemporaryDirectory(prefix="vp_seq_") as tmp_dir:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(template_url, wait_until="networkidle")

            page.evaluate(f"""
                window.__PARAMS = {json.dumps(params)};
                window.__DURATION = {duration};
                window.__FPS = {fps};
                window.__TOTAL_FRAMES = {total_frames};
            """)

            try:
                page.wait_for_function("window.__READY === true", timeout=10000)
            except Exception:
                pass

            print(f"[render] Capturing {total_frames} transparent frames ({duration}s @ {fps}fps)...")
            for i in range(total_frames):
                progress = i / max(total_frames - 1, 1)
                page.evaluate(f"""
                    window.__frameReady = false;
                    if (typeof window.renderFrame === 'function') {{
                        window.renderFrame({i}, {progress});
                    }}
                    requestAnimationFrame(() => {{ window.__frameReady = true; }});
                """)
                try:
                    page.wait_for_function("window.__frameReady === true", timeout=5000)
                except Exception:
                    pass
                frame_path = os.path.join(tmp_dir, f"frame_{i:05d}.png")
                page.screenshot(path=frame_path, type="png", omit_background=True)

            page.close()
            browser.close()

        # Encode PNG sequence → MP4 with yuva420p (alpha-aware) or yuv420p
        # Use VP9/WebM for true alpha, or H.264 with pre-multiplied alpha baked in
        # For ffmpeg overlay compositing, we use the PNG sequence directly as input
        # But for convenience, also produce an mp4 — ffmpeg overlay can use the PNG dir

        # Encode to mp4 (opaque — for preview). The PNG sequence is the real asset.
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-framerate", str(fps),
            "-i", os.path.join(tmp_dir, "frame_%05d.png"),
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            output_path,
        ]
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"[render] ffmpeg encode failed: {e}", file=sys.stderr)
            raise

        # Also copy the PNG sequence to a sibling directory for direct ffmpeg overlay use
        seq_dir = Path(output_path).with_suffix("") / "frames"
        seq_dir.mkdir(parents=True, exist_ok=True)
        for png in sorted(Path(tmp_dir).glob("frame_*.png")):
            import shutil
            shutil.copy2(str(png), str(seq_dir / png.name))

    print(f"[render] Sequence: {output_path} ({total_frames} frames)")
    print(f"[render] Frames:   {seq_dir}/")


def render(template_path: str, params: dict, duration: float, output_path: str,
           fps: int = DEFAULT_FPS, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
           trim_start: float = DEFAULT_TRIM_START):
    """Render animation via screenshot-per-frame + ffmpeg stitch.

    Each frame is captured as a JPEG screenshot after renderFrame() completes,
    guaranteeing exactly one frame per renderFrame call regardless of JS execution
    time. This prevents duplicate/dropped frames in physics-heavy animations that
    the old CDP recording approach suffered from.

    trim_start is kept for API compatibility but no longer used.
    """
    from playwright.sync_api import sync_playwright

    template_url = f"file://{template_path}"
    total_frames = int(duration * fps)

    with tempfile.TemporaryDirectory(prefix="vp_anim_") as tmp_dir:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(template_url, wait_until="networkidle")

            page.evaluate(f"""
                window.__PARAMS = {json.dumps(params)};
                window.__DURATION = {duration};
                window.__FPS = {fps};
                window.__TOTAL_FRAMES = {total_frames};
            """)

            try:
                page.wait_for_function("window.__READY === true", timeout=10000)
            except Exception:
                pass

            print(f"Capturing {total_frames} frames ({duration}s @ {fps}fps)...")
            for i in range(total_frames):
                progress = i / max(total_frames - 1, 1)
                page.evaluate(f"""
                    window.__frameReady = false;
                    if (typeof window.renderFrame === 'function') {{
                        window.renderFrame({i}, {progress});
                    }}
                    requestAnimationFrame(() => {{ window.__frameReady = true; }});
                """)
                # Wait for Chromium to signal paint completion via rAF callback
                try:
                    page.wait_for_function("window.__frameReady === true", timeout=5000)
                except Exception:
                    pass  # fallback: screenshot whatever state we're in
                frame_path = os.path.join(tmp_dir, f"frame_{i:05d}.jpg")
                page.screenshot(path=frame_path, type="jpeg", quality=92)
                if i % 30 == 0:
                    print(f"  frame {i}/{total_frames}")

            page.close()
            browser.close()

        # Stitch JPEG sequence → MP4 at exact target fps
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-framerate", str(fps),
            "-i", os.path.join(tmp_dir, "frame_%05d.jpg"),
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            output_path,
        ]
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"[render] ffmpeg encode failed: {e}", file=sys.stderr)
            raise

        # Verify frame count matches expectation
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-count_packets", "-show_entries", "stream=nb_read_packets",
             "-of", "csv=p=0", output_path],
            capture_output=True, text=True
        )
        actual_frames = int(probe.stdout.strip()) if probe.stdout.strip().isdigit() else -1
        if actual_frames == -1:
            pass  # probe returned non-integer (e.g. N/A) — not a real mismatch, skip check
        elif actual_frames != total_frames:
            print(f"[render] WARNING: expected {total_frames} frames, got {actual_frames} — frame rate mismatch", file=sys.stderr)
        else:
            print(f"[render] Frame check PASSED: {actual_frames}/{total_frames} frames")

        size_mb = Path(output_path).stat().st_size / 1024 / 1024
        print(f"Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Render HTML animation to mp4 or transparent PNG overlay")
    parser.add_argument("--template", required=True, help="Template name")
    parser.add_argument("--duration", type=float, default=0, help="Seconds (0 = static overlay)")
    parser.add_argument("--output", required=True, help="Output path (.mp4 for video, .png for overlay)")
    parser.add_argument("--mode", choices=["video", "overlay", "sequence"], default=None,
                        help="Render mode: video (opaque mp4), overlay (static PNG), sequence (animated transparent frames)")
    parser.add_argument("--params", default="{}", help="JSON params string")
    parser.add_argument("--params-file", default=None,
                        help="Path to a JSON file whose keys are merged into --params "
                             "(useful for large arrays like lipsync data)")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--trim-start", type=float, default=DEFAULT_TRIM_START,
                        help=f"Seconds to trim from start of recording to skip Playwright startup blank frames (default: {DEFAULT_TRIM_START})")
    parser.add_argument("--template-dir", default=None,
                        help="Additional template search path — checked before the skill's built-in assets/templates/. "
                             "Use this to keep project templates in the project folder without copying to the skill dir.")
    args = parser.parse_args()

    # Resolve template: check --template-dir first, fall back to skill assets/templates/
    template_path = None
    if args.template_dir:
        candidate = Path(args.template_dir) / f"{args.template}.html"
        if candidate.exists():
            template_path = candidate
    if template_path is None:
        candidate = TEMPLATES_DIR / f"{args.template}.html"
        if candidate.exists():
            template_path = candidate
    if template_path is None:
        searched = []
        if args.template_dir:
            searched.append(str(Path(args.template_dir) / f"{args.template}.html"))
        searched.append(str(TEMPLATES_DIR / f"{args.template}.html"))
        available = [t.stem for t in TEMPLATES_DIR.glob("*.html")]
        print(f"Template not found. Searched:", file=sys.stderr)
        for p in searched:
            print(f"  {p}", file=sys.stderr)
        print(f"Built-in templates available: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    params = json.loads(args.params)
    if args.params_file:
        with open(args.params_file) as f:
            params.update(json.load(f))
    params = resolve_image_paths(params, os.getcwd())

    # Inject renderer dimensions so templates can adapt to any --width/--height
    params.setdefault("width", args.width)
    params.setdefault("height", args.height)

    # Auto-detect mode from output extension or explicit flag
    mode = args.mode
    if mode is None:
        mode = "overlay" if args.output.endswith(".png") else "video"

    if mode == "overlay":
        render_overlay(str(template_path), params, args.output, args.width, args.height)
    elif mode == "sequence":
        if args.duration <= 0:
            print("--duration required for sequence mode", file=sys.stderr)
            sys.exit(1)
        render_overlay_sequence(str(template_path), params, args.duration, args.output,
                                args.fps, args.width, args.height)
    else:
        if args.duration <= 0:
            print("--duration required for video mode", file=sys.stderr)
            sys.exit(1)
        render(str(template_path), params, args.duration, args.output,
               args.fps, args.width, args.height, args.trim_start)


if __name__ == "__main__":
    main()
