#!/usr/bin/env python3
"""
Apply color grade to video via ffmpeg eq + colorbalance filters.

Presets:
  orange-teal      — warm shadows/mids, cool teal highlights. Classic cinematic.
  muted-film       — desaturated, warm, soft contrast. Analog/indie film feel.
  high-contrast    — punchy contrast, vivid saturation. Bold digital look.
  wes-anderson     — pastel warmth, lifted shadows, slight desat.
  noir             — near-greyscale, extreme contrast.

Custom params override preset defaults.

Usage:
  apply-grade.py --input video.mp4 --output graded.mp4 --preset orange-teal
  apply-grade.py --input video.mp4 --output graded.mp4 --preset muted-film --saturation 0.6
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "orange-teal": {
        "saturation": 1.3,
        "contrast": 1.15,
        "brightness": 0.0,
        # colorbalance: shadows (rs/gs/bs), mids (rm/gm/bm), highlights (rh/gh/bh)
        # Orange in shadows/mids = +red -blue; teal in highlights = -red +blue
        "balance": "colorbalance=rs=0.12:gs=-0.02:bs=-0.18:rm=0.06:gm=0.0:bm=-0.08:rh=-0.18:gh=0.08:bh=0.22",
    },
    "muted-film": {
        "saturation": 0.72,
        "contrast": 0.92,
        "brightness": 0.05,
        "balance": "colorbalance=rs=0.08:gs=0.02:bs=-0.06",
    },
    "high-contrast": {
        "saturation": 1.5,
        "contrast": 1.4,
        "brightness": -0.03,
        "balance": None,
    },
    "wes-anderson": {
        "saturation": 0.82,
        "contrast": 1.05,
        "brightness": 0.07,
        "balance": "colorbalance=rs=0.14:gs=0.08:bs=-0.06:rm=0.06:gm=0.04:bm=-0.02",
    },
    "noir": {
        "saturation": 0.08,
        "contrast": 1.65,
        "brightness": -0.05,
        "balance": None,
    },
}


def build_grade_filter(saturation: float = 1.0, contrast: float = 1.0,
                       brightness: float = 0.0, balance: str | None = None) -> str:
    eq = f"eq=saturation={saturation}:contrast={contrast}:brightness={brightness}"
    parts = [eq]
    if balance:
        parts.append(balance)
    return ",".join(parts)


def apply_grade(input_path: str, output_path: str, filter_str: str):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", filter_str,
        "-map", "0:v", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]
    print(f"[apply-grade] Processing: {Path(input_path).name}")
    print(f"[apply-grade] Filter: {filter_str}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[apply-grade] ffmpeg failed: {e}", file=sys.stderr)
        raise
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-grade] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Apply color grade to video")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()),
                        help="Grade preset")
    parser.add_argument("--saturation", type=float, help="Saturation multiplier (1.0 = unchanged)")
    parser.add_argument("--contrast", type=float, help="Contrast multiplier")
    parser.add_argument("--brightness", type=float, help="Brightness offset (-1.0 to 1.0)")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[apply-grade] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    params = dict(PRESETS.get(args.preset or "orange-teal", PRESETS["orange-teal"]))
    if args.saturation is not None:
        params["saturation"] = args.saturation
    if args.contrast is not None:
        params["contrast"] = args.contrast
    if args.brightness is not None:
        params["brightness"] = args.brightness

    filter_str = build_grade_filter(**params)
    apply_grade(input_path, args.output, filter_str)


if __name__ == "__main__":
    main()
