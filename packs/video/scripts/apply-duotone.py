#!/usr/bin/env python3
"""
Apply duotone (two-color) effect to video.

Desaturates the source and maps the luma range to two colors:
  shadow_color → at luma 0 (darkest areas)
  highlight_color → at luma 255 (brightest areas)

This is the same as Photoshop's Duotone mode or an AE tritone effect
with only two stops.

Presets:
  midnight   — deep navy shadows → hot pink highlights. Club/editorial.
  sunset     — deep purple shadows → amber highlights. Warm cinematic.
  risograph  — dark forest green shadows → cream highlights. Zine/print.
  blueprint  — near-black navy shadows → light blue highlights. Technical.
  infra      — near-black shadows → orange-red highlights. Thermal camera.

Colors can be specified as hex (#rrggbb) to define custom duotones.

Usage:
  apply-duotone.py --input video.mp4 --output duo.mp4 --preset midnight
  apply-duotone.py --input video.mp4 --output duo.mp4 --shadow "#1a0030" --highlight "#ff8c00"
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "midnight":  {"shadow": "#0d1b4b", "highlight": "#ff3399"},
    "sunset":    {"shadow": "#1a0030", "highlight": "#ff8c00"},
    "risograph": {"shadow": "#1a3a2a", "highlight": "#f5e6c8"},
    "blueprint": {"shadow": "#0a1628", "highlight": "#7eb8f7"},
    "infra":     {"shadow": "#0d0a1e", "highlight": "#ff6b35"},
}


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def build_duotone_filter(shadow: str = "#0d1b4b", highlight: str = "#ff3399") -> str:
    """
    1. Desaturate: hue=s=0 (keeps RGB format, sets all channels to luma)
    2. geq: map luma linearly from shadow_color (at 0) to highlight_color (at 255)
       For each channel c:  output = shadow_c + (highlight_c - shadow_c) * luma/255
       Since input is greyscale after hue=s=0, r(X,Y) == g(X,Y) == b(X,Y) == luma.
    """
    r1, g1, b1 = hex_to_rgb(shadow)
    r2, g2, b2 = hex_to_rgb(highlight)

    # Linear interpolation per channel using luma (r channel after desat = luma)
    r_expr = f"{r1}+({r2}-{r1})*r(X\\,Y)/255"
    g_expr = f"{g1}+({g2}-{g1})*g(X\\,Y)/255"
    b_expr = f"{b1}+({b2}-{b1})*b(X\\,Y)/255"

    return f"hue=s=0,geq=r='{r_expr}':g='{g_expr}':b='{b_expr}'"


def apply_duotone(input_path: str, output_path: str, filter_str: str):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", filter_str,
        "-map", "0:v", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]
    print(f"[apply-duotone] Processing: {Path(input_path).name}")
    print(f"[apply-duotone] Shadow: {filter_str.split('hue')[0].strip() or 'from filter'}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[apply-duotone] ffmpeg failed: {e}", file=sys.stderr)
        raise
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-duotone] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Apply duotone two-color effect to video")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="Duotone preset")
    parser.add_argument("--shadow", help="Shadow color as hex (#rrggbb)")
    parser.add_argument("--highlight", help="Highlight color as hex (#rrggbb)")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[apply-duotone] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    params = dict(PRESETS.get(args.preset or "midnight", PRESETS["midnight"]))
    if args.shadow:
        params["shadow"] = args.shadow
    if args.highlight:
        params["highlight"] = args.highlight

    filter_str = build_duotone_filter(**params)
    apply_duotone(input_path, args.output, filter_str)


if __name__ == "__main__":
    main()
