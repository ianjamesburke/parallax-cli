#!/usr/bin/env python3
"""
Apply blurred background behind a foreground subject via ffmpeg.

Blurs the entire frame, then composites the original (optionally cropped/scaled)
on top. Common for talking-head vertical video where the subject is centered
and the edges are a soft blurred version of the same frame.

Presets:
  soft     — light blur, subtle depth effect. Good for clean content.
  medium   — standard talking-head blur. Clear separation without distraction.
  heavy    — strong blur, subject pops. High-energy / ad content.
  cinematic — heavy blur + slight darken on the bg. Moody, polished look.

Usage:
  apply-blur-bg.py --input video.mp4 --output blurred.mp4 --preset medium
  apply-blur-bg.py --input video.mp4 --output blurred.mp4 --radius 40 --darken 0.1
  apply-blur-bg.py --input video.mp4 --output blurred.mp4 --preset heavy --scale 0.7
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "soft":      {"radius": 15, "darken": 0.0, "scale": 0.85},
    "medium":    {"radius": 30, "darken": 0.0, "scale": 0.80},
    "heavy":     {"radius": 50, "darken": 0.05, "scale": 0.75},
    "cinematic": {"radius": 60, "darken": 0.15, "scale": 0.75},
}


def build_blur_bg_filter(radius: int = 30, darken: float = 0.0,
                         scale: float = 0.80) -> str:
    """
    filter_complex:
      [0:v] → split into [fg] and [bg_src]
      [bg_src] → boxblur(radius) → optional darken → [bg]
      [fg] → scale to (scale * input size), center over [bg]

    The foreground is scaled down and overlaid centered on the blurred bg.
    This avoids needing segmentation — just a scaled-down original on a blurred copy.
    """
    parts = [f"[0:v]split[fg][bg_src]"]

    # Background: blur + optional darken
    bg_filters = f"boxblur=luma_radius={radius}:luma_power=3"
    if darken > 0:
        brightness = -(darken)
        bg_filters += f",eq=brightness={brightness:.2f}"
    parts.append(f"[bg_src]{bg_filters}[bg]")

    # Foreground: scale down and center overlay
    parts.append(
        f"[fg]scale=iw*{scale:.2f}:ih*{scale:.2f}[fg_scaled]"
    )
    parts.append(
        "[bg][fg_scaled]overlay=(W-w)/2:(H-h)/2"
    )

    return ";".join(parts)


def apply_blur_bg(input_path: str, output_path: str, filter_complex: str):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]
    print(f"[apply-blur-bg] Processing: {Path(input_path).name}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[apply-blur-bg] ffmpeg failed: {e}", file=sys.stderr)
        raise
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-blur-bg] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Apply blurred background behind foreground subject")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="Blur preset")
    parser.add_argument("--radius", type=int, help="Blur radius in pixels (default 30)")
    parser.add_argument("--darken", type=float, help="Darken background 0.0-1.0 (default 0.0)")
    parser.add_argument("--scale", type=float, help="Foreground scale factor 0.5-1.0 (default 0.80)")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[apply-blur-bg] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    params = dict(PRESETS.get(args.preset or "medium", PRESETS["medium"]))
    if args.radius is not None:
        params["radius"] = args.radius
    if args.darken is not None:
        params["darken"] = args.darken
    if args.scale is not None:
        params["scale"] = args.scale

    fc = build_blur_bg_filter(**params)
    apply_blur_bg(input_path, args.output, fc)


if __name__ == "__main__":
    main()
