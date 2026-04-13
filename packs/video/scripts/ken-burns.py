#!/usr/bin/env python3
"""Standalone Ken Burns clip from a single image.

Uses Pillow float-precision EXTENT transform — no ffmpeg zoompan, no integer
quantization jitter. This is the correct way to produce Ken Burns clips.

Usage:
    python3 ken-burns.py --input photo.png --output clip.mp4 --duration 8
    python3 ken-burns.py --input photo.png --output clip.mp4 --duration 6 --motion zoom_out
    python3 ken-burns.py --input photo.png --output clip.mp4 --motion pan_up --resolution 1920x1080
"""

import argparse
import subprocess
import sys

MOTION_PRESETS = {
    # Zoom only (centered)
    "zoom_in":        (1.0,  1.15,  0.0,  0.0),
    "zoom_out":       (1.15, 1.0,   0.0,  0.0),
    # Zoom + drift combo
    "zoom_drift_right": (1.0,  1.12,  0.4,  0.0),
    "zoom_drift_left":  (1.0,  1.12, -0.4,  0.0),
    "zoom_drift_down":  (1.0,  1.12,  0.0,  0.4),
    "zoom_drift_up":    (1.0,  1.12,  0.0, -0.4),
    # Pure slow pan (no zoom change — uses 1.15x crop for pan headroom)
    "pan_right":  (1.15, 1.15,  0.8,  0.0),
    "pan_left":   (1.15, 1.15, -0.8,  0.0),
    "pan_down":   (1.15, 1.15,  0.0,  0.8),
    "pan_up":     (1.15, 1.15,  0.0, -0.8),
}


def render(image_path: str, output_path: str, duration: float,
           motion: str = "zoom_in", resolution: str = "1080x1920",
           fps: int = 30, crf: int = 18):
    try:
        from PIL import Image
        _RESAMPLE = Image.Resampling.BICUBIC
        _LANCZOS = Image.Resampling.LANCZOS
        _EXTENT = Image.Transform.EXTENT
    except ImportError:
        print("ERROR: Pillow required. Run: pip install Pillow", file=sys.stderr)
        sys.exit(1)

    if motion not in MOTION_PRESETS:
        print(f"ERROR: Unknown motion '{motion}'. Options: {', '.join(MOTION_PRESETS)}", file=sys.stderr)
        sys.exit(1)

    start_zoom, end_zoom, pan_x, pan_y = MOTION_PRESETS[motion]
    out_w, out_h = [int(x) for x in resolution.split("x")]
    total_frames = max(1, round(duration * fps))

    # Prepare source: scale to 1.5x output for zoom headroom, center-crop
    src_w, src_h = round(out_w * 1.5), round(out_h * 1.5)
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:
        print(f"ERROR: Cannot open {image_path}: {e}", file=sys.stderr)
        sys.exit(1)

    scale = max(src_w / img.width, src_h / img.height)
    scaled = img.resize((round(img.width * scale), round(img.height * scale)), _LANCZOS)
    x0 = (scaled.width - src_w) // 2
    y0 = (scaled.height - src_h) // 2
    img = scaled.crop((x0, y0, x0 + src_w, y0 + src_h))
    cx, cy = src_w / 2.0, src_h / 2.0

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24", "-r", str(fps),
        "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-vframes", str(total_frames),
        output_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    stdin = proc.stdin
    assert stdin is not None

    try:
        for n in range(total_frames):
            t = n / max(total_frames - 1, 1)
            zoom = start_zoom + (end_zoom - start_zoom) * t

            crop_w = src_w / zoom
            crop_h = src_h / zoom

            avail_x = (src_w - crop_w) / 2
            avail_y = (src_h - crop_h) / 2

            left = cx - crop_w / 2 + pan_x * avail_x * t
            top  = cy - crop_h / 2 + pan_y * avail_y * t

            frame = img.transform(
                (out_w, out_h),
                _EXTENT,
                (left, top, left + crop_w, top + crop_h),
                _RESAMPLE,
            )
            stdin.write(frame.tobytes())
    except Exception as e:
        proc.kill()
        raise RuntimeError(f"Frame write failed for {image_path}: {e}") from e
    finally:
        stdin.close()
        proc.wait()

    if proc.returncode != 0:
        print(f"ERROR: ffmpeg exited with code {proc.returncode}", file=sys.stderr)
        sys.exit(1)

    print(f"Done: {output_path} ({duration}s, {motion}, {resolution})")


def main():
    p = argparse.ArgumentParser(description="Smooth Ken Burns clip from a single image (Pillow, no zoompan)")
    p.add_argument("--input", required=True, help="Source image path")
    p.add_argument("--output", required=True, help="Output .mp4 path")
    p.add_argument("--duration", type=float, default=8.0, help="Clip duration in seconds (default: 8)")
    p.add_argument("--motion", default="zoom_in", choices=list(MOTION_PRESETS.keys()),
                   help="Motion preset (default: zoom_in)")
    p.add_argument("--resolution", default="1080x1920", help="Output resolution WxH (default: 1080x1920)")
    p.add_argument("--fps", type=int, default=30, help="Frame rate (default: 30)")
    p.add_argument("--crf", type=int, default=18, help="H.264 CRF quality (default: 18, lower = better)")
    args = p.parse_args()

    render(args.input, args.output, args.duration, args.motion, args.resolution, args.fps, args.crf)


if __name__ == "__main__":
    main()
