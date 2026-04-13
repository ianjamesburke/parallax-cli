#!/usr/bin/env python3
"""
Apply paper/film grain overlay to video.

This generates a separate grain layer from a neutral source and blends
it over the video using overlay composite mode — closer to how a paper
texture layer works in After Effects.

Presets:
  paper   — warm static-ish grain, low intensity. Aged paper / risograph feel.
  film    — cooler temporal grain, medium intensity. 35mm film feel.
  heavy   — strong grain, high texture. Xerox / photocopy look.

Usage:
  apply-grain.py --input video.mp4 --output grain.mp4 --preset paper
  apply-grain.py --input video.mp4 --output grain.mp4 --intensity 30 --warmth 0.05
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "paper": {
        "intensity": 18,
        "warmth": 0.06,    # warm tint on grain layer (0 = neutral grey grain)
        "opacity": 0.18,   # blend opacity of grain layer over video
        "temporal": False, # False = slow-drift grain; True = per-frame flicker
    },
    "film": {
        "intensity": 28,
        "warmth": 0.0,
        "opacity": 0.22,
        "temporal": True,
    },
    "heavy": {
        "intensity": 45,
        "warmth": -0.03,   # slight cool tint
        "opacity": 0.35,
        "temporal": True,
    },
}


def build_grain_filter(intensity: int = 18, warmth: float = 0.05,
                       opacity: float = 0.18, temporal: bool = False) -> str:
    """
    Build a filter_complex string that:
      1. Generates a mid-grey source (color=0x808080)
      2. Adds noise to it to create a grain texture
      3. Optionally shifts its color temperature for warm/cool paper feel
      4. Blends it over the input video with overlay composite at low opacity
    """
    allf = "t+u" if temporal else "u"
    noise = f"noise=alls={intensity}:allf={allf}"

    eq_parts = []
    if warmth > 0:
        # Warm grain: boost red slightly, reduce blue
        eq_parts.append(f"colorbalance=rs={warmth:.3f}:gs={warmth*0.3:.3f}:bs={-warmth*0.5:.3f}")
    elif warmth < 0:
        # Cool grain: reduce red, boost blue
        eq_parts.append(f"colorbalance=rs={warmth:.3f}:bs={-warmth*0.5:.3f}")

    grain_chain = noise
    if eq_parts:
        grain_chain += "," + ",".join(eq_parts)

    # Build filter_complex:
    # [0:v] = input video
    # Generate grain from a mid-grey solid source matched to input resolution
    # Blend grain over video using overlay at `opacity`
    fc = (
        f"[0:v]split[vid][ref];"
        f"[ref]scale=iw:ih,{grain_chain}[grain];"
        f"[vid][grain]blend=all_mode=overlay:all_opacity={opacity:.3f}"
    )
    return fc


def apply_grain(input_path: str, output_path: str, filter_complex: str, crf: int = 18):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]
    print(f"[apply-grain] Processing: {Path(input_path).name}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[apply-grain] ffmpeg failed: {e}", file=sys.stderr)
        raise
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-grain] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Apply paper/film grain overlay to video")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="Grain preset")
    parser.add_argument("--intensity", type=int, help="Grain intensity (0-60)")
    parser.add_argument("--warmth", type=float, help="Grain color warmth (-0.1 to 0.1, 0=neutral)")
    parser.add_argument("--opacity", type=float, help="Grain blend opacity (0.0-1.0)")
    parser.add_argument("--temporal", action="store_true",
                        help="Per-frame grain flicker (default: slow drift)")
    parser.add_argument("--crf", type=int, default=18,
                        help="H.264 CRF quality (0-51, default: 18). Lower = better quality / bigger files. "
                             "Grain defeats compression, so raise this (e.g. 23-28) to control file size.")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[apply-grain] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    params = dict(PRESETS.get(args.preset or "paper", PRESETS["paper"]))
    if args.intensity is not None:
        params["intensity"] = args.intensity
    if args.warmth is not None:
        params["warmth"] = args.warmth
    if args.opacity is not None:
        params["opacity"] = args.opacity
    if args.temporal:
        params["temporal"] = True

    fc = build_grain_filter(**params)
    apply_grain(input_path, args.output, fc, crf=args.crf)


if __name__ == "__main__":
    main()
