#!/usr/bin/env python3
"""
Apply CRT/retro scanline overlay to video.

Uses ffmpeg geq filter to darken every Nth row by a configurable amount,
simulating the gaps between phosphor rows on a CRT monitor or an interlaced
screen recording.

Presets:
  subtle   — fine lines, barely visible. Modern LCD feel.
  crt      — classic CRT: 4px spacing, 2px dark rows, medium opacity.
  heavy    — pronounced lines, strong darkening. Arcade/old monitor look.
  wide     — wider gaps, like a low-res display upscaled.

Usage:
  apply-scanlines.py --input video.mp4 --output scan.mp4 --preset crt
  apply-scanlines.py --input video.mp4 --output scan.mp4 --spacing 4 --thickness 1 --opacity 0.25
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "subtle": {"spacing": 4, "thickness": 1, "opacity": 0.18},
    "crt":    {"spacing": 4, "thickness": 2, "opacity": 0.35},
    "heavy":  {"spacing": 3, "thickness": 2, "opacity": 0.55},
    "wide":   {"spacing": 8, "thickness": 2, "opacity": 0.30},
}


def build_scanline_filter(spacing: int = 4, thickness: int = 2, opacity: float = 0.35) -> str:
    """
    geq darkens rows where (Y mod spacing) < thickness.
    lum expression: original_luma * (1 - opacity) for scanline rows, original elsewhere.
    cb/cr pass through unchanged to preserve color.
    """
    factor = 1.0 - opacity
    lum = f"p(X,Y)*if(lt(mod(Y\\,{spacing})\\,{thickness})\\,{factor:.3f}\\,1)"
    return f"geq=lum='{lum}':cb='p(X,Y)':cr='p(X,Y)'"


def apply_scanlines(input_path: str, output_path: str, filter_str: str):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", filter_str,
        "-map", "0:v", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]
    print(f"[apply-scanlines] Processing: {Path(input_path).name}")
    print(f"[apply-scanlines] Filter: spacing={filter_str}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[apply-scanlines] ffmpeg failed: {e}", file=sys.stderr)
        raise
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-scanlines] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Apply CRT scanline overlay to video")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="Scanline preset")
    parser.add_argument("--spacing", type=int, help="Pixels between scanline centers (default 4)")
    parser.add_argument("--thickness", type=int, help="Dark row thickness in pixels (default 2)")
    parser.add_argument("--opacity", type=float, help="Scanline darkness 0.0-1.0 (default 0.35)")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[apply-scanlines] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    params = dict(PRESETS.get(args.preset or "crt", PRESETS["crt"]))
    if args.spacing is not None:
        params["spacing"] = args.spacing
    if args.thickness is not None:
        params["thickness"] = args.thickness
    if args.opacity is not None:
        params["opacity"] = args.opacity

    filter_str = build_scanline_filter(**params)
    apply_scanlines(input_path, args.output, filter_str)


if __name__ == "__main__":
    main()
