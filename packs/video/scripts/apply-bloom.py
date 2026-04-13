#!/usr/bin/env python3
"""
Apply bloom / glow post-effect to video via ffmpeg.

Real bloom (not canvas): splits the video, blurs one copy, then screen-composites
the blurred copy back over the sharp original. Bright areas bleed light outward.
Dark areas are unaffected (screen mode never darkens).

Presets:
  subtle   — light bloom, preserves sharpness. Good for clean animations.
  medium   — visible glow around bright edges. AE Glow effect equivalent.
  heavy    — strong bloom, softens the whole image. Dream/ethereal look.
  neon     — saturated bloom, pumps colors. Good for dark backgrounds with bright lines.

Usage:
  apply-bloom.py --input video.mp4 --output bloom.mp4 --preset medium
  apply-bloom.py --input video.mp4 --output bloom.mp4 --radius 25 --strength 0.6 --saturation 1.4
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "subtle": {"radius": 12, "strength": 0.35, "saturation": 1.1, "brightness": 1.1},
    "medium": {"radius": 20, "strength": 0.50, "saturation": 1.3, "brightness": 1.4},
    "heavy":  {"radius": 30, "strength": 0.65, "saturation": 1.2, "brightness": 1.6},
    "neon":   {"radius": 22, "strength": 0.55, "saturation": 1.8, "brightness": 1.8},
}


def build_bloom_filter(radius: int = 20, strength: float = 0.50,
                       saturation: float = 1.3, brightness: float = 1.4) -> str:
    """
    filter_complex:
      [0:v] → split into [sharp] and [blur_src]
      [blur_src] → boxblur(radius, power=3) → eq(brightness, saturation) → [bloom]
      [sharp][bloom] → blend(screen, opacity=strength) → output

    Screen blend: result = 1 - (1-a)*(1-b). Where a and b are both in [0,1].
    At strength=1, full screen blend. At strength=0, only the sharp source.
    """
    fc = (
        f"[0:v]split[sharp][blur_src];"
        f"[blur_src]boxblur=luma_radius={radius}:luma_power=3,"
        f"eq=brightness={brightness - 1:.2f}:saturation={saturation:.2f}[bloom];"
        f"[sharp][bloom]blend=all_mode=screen:all_opacity={strength:.2f}"
    )
    return fc


def apply_bloom(input_path: str, output_path: str, filter_complex: str):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]
    print(f"[apply-bloom] Processing: {Path(input_path).name}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[apply-bloom] ffmpeg failed: {e}", file=sys.stderr)
        raise
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-bloom] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Apply bloom/glow post-effect to video")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="Bloom preset")
    parser.add_argument("--radius", type=int, help="Blur radius in pixels (default 20)")
    parser.add_argument("--strength", type=float, help="Bloom opacity 0.0-1.0 (default 0.5)")
    parser.add_argument("--saturation", type=float, help="Bloom color saturation (default 1.3)")
    parser.add_argument("--brightness", type=float, help="Bloom brightness boost (default 1.4)")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[apply-bloom] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    params = dict(PRESETS.get(args.preset or "medium", PRESETS["medium"]))
    if args.radius is not None:
        params["radius"] = args.radius
    if args.strength is not None:
        params["strength"] = args.strength
    if args.saturation is not None:
        params["saturation"] = args.saturation
    if args.brightness is not None:
        params["brightness"] = args.brightness

    fc = build_bloom_filter(**params)
    apply_bloom(input_path, args.output, fc)


if __name__ == "__main__":
    main()
