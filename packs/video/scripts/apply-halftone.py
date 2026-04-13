#!/usr/bin/env python3
"""
Apply halftone dot pattern to video.

Uses ffmpeg geq to render a dot-per-cell pattern where each dot's radius
is proportional to the local brightness — the same principle as offset
lithographic printing (newspapers, comic books, risograph).

The output is monochrome by default. Use --color for a two-channel
approximate color halftone (luma + chroma approximation — not true CMYK).

Presets:
  fine     — small cells (8px), fine dot grid. Newspaper print.
  medium   — 12px cells. Clear halftone, readable dots.
  coarse   — 20px cells. Bold comic-book / Ben-Day dots.
  risograph — 16px cells, slightly desaturated source. Risograph zine feel.

Usage:
  apply-halftone.py --input video.mp4 --output halftone.mp4 --preset coarse
  apply-halftone.py --input video.mp4 --output halftone.mp4 --cell-size 10 --invert
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "fine":      {"cell_size": 8,  "scale": 0.95, "invert": False, "desat": 0.0},
    "medium":    {"cell_size": 12, "scale": 0.95, "invert": False, "desat": 0.0},
    "coarse":    {"cell_size": 20, "scale": 0.95, "invert": False, "desat": 0.0},
    "risograph": {"cell_size": 16, "scale": 0.92, "invert": False, "desat": 0.3},
}


def build_halftone_filter(cell_size: int = 12, scale: float = 0.95,
                          invert: bool = False, desat: float = 0.0) -> str:
    """
    geq expression per pixel (X, Y):
      cx = (floor(X/R) * R) + R/2   — cell center x (integer)
      cy = (floor(Y/R) * R) + R/2   — cell center y (integer)
      luma at center = p(cx, cy)     — p() requires integer coords
      dot_radius = sqrt(luma/255) * (R/2) * scale
      pixel in dot if dist(pixel, center) < dot_radius → white, else black
    Note: p() needs plain integer expressions — no floats as arguments.
    """
    R = cell_size
    half_R = R // 2  # integer division for p() compatibility

    # cell center expressions (integer arithmetic only for p() args)
    cx = f"(trunc(X/{R})*{R}+{half_R})"
    cy = f"(trunc(Y/{R})*{R}+{half_R})"
    dist = f"sqrt(pow(X-{cx},2)+pow(Y-{cy},2))"
    dot_r = f"sqrt(p({cx},{cy})/255.0)*{half_R}*{scale}"

    on_val  = "0" if invert else "255"
    off_val = "255" if invert else "0"
    dot_expr = f"if(lte({dist},{dot_r}),{on_val},{off_val})"

    filters = []
    if desat > 0:
        filters.append(f"eq=saturation={1.0 - desat:.2f}")
    filters.append(f"format=gray")
    filters.append(f"geq=lum='{dot_expr}'")

    return ",".join(filters)


def apply_halftone(input_path: str, output_path: str, filter_str: str):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", filter_str,
        "-map", "0:v", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]
    print(f"[apply-halftone] Processing: {Path(input_path).name}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[apply-halftone] ffmpeg failed: {e}", file=sys.stderr)
        raise
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-halftone] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Apply halftone dot pattern to video")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="Halftone preset")
    parser.add_argument("--cell-size", type=int, dest="cell_size",
                        help="Halftone cell size in pixels (default 12)")
    parser.add_argument("--invert", action="store_true",
                        help="Invert: white background with dark dots")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[apply-halftone] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    params = dict(PRESETS.get(args.preset or "medium", PRESETS["medium"]))
    if args.cell_size is not None:
        params["cell_size"] = args.cell_size
    if args.invert:
        params["invert"] = True

    filter_str = build_halftone_filter(**params)
    apply_halftone(input_path, args.output, filter_str)


if __name__ == "__main__":
    main()
