#!/usr/bin/env python3
"""
Apply motion blur to video.

Two modes:
  temporal   — blends N consecutive frames together (ghosting/trailing effect).
               Good for slow-moving content — makes motion feel dreamy/fluid.
               Uses ffmpeg `tmix` filter.

  directional — applies a gaussian blur in a fixed direction (angle in degrees).
                Simulates camera movement or AE-style motion smear on a layer.
                Uses ffmpeg `gblur` with angle parameter.
                angle=0: horizontal, angle=90: vertical, angle=45: diagonal

Presets:
  subtle     — temporal, 3 frames, light blend. Barely perceptible.
  dreamy     — temporal, 5 frames, heavier blend. Slow-motion feel.
  smear-h    — directional horizontal. As if camera panned left/right.
  smear-v    — directional vertical. As if camera tilted up/down.
  smear-d    — directional diagonal (45°). Cinematic sweep feel.

Usage:
  apply-motion-blur.py --input video.mp4 --output blur.mp4 --preset dreamy
  apply-motion-blur.py --input video.mp4 --output blur.mp4 --mode directional --angle 45 --sigma 6
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "subtle": {
        "mode": "temporal",
        "frames": 3,
        "weights": "1 2 1",
        "sigma": None,
        "angle": None,
    },
    "dreamy": {
        "mode": "temporal",
        "frames": 5,
        "weights": "1 2 3 2 1",
        "sigma": None,
        "angle": None,
    },
    "smear-h": {
        "mode": "directional",
        "frames": None,
        "weights": None,
        "sigma": 5.0,
        "angle": 0,
    },
    "smear-v": {
        "mode": "directional",
        "frames": None,
        "weights": None,
        "sigma": 5.0,
        "angle": 90,
    },
    "smear-d": {
        "mode": "directional",
        "frames": None,
        "weights": None,
        "sigma": 5.0,
        "angle": 45,
    },
}


def build_motion_blur_filter(mode: str = "temporal", frames: int = 3,
                              weights: str = "1 2 1", sigma: float = 5.0,
                              angle: int = 0) -> str:
    if mode == "temporal":
        return f"tmix=frames={frames}:weights='{weights}'"
    elif mode == "directional":
        # gblur: sigma = blur radius, steps = quality (1-6), angle = degrees
        return f"gblur=sigma={sigma}:steps=2:angle={angle}"
    else:
        raise ValueError(f"Unknown mode: {mode}. Use 'temporal' or 'directional'.")


def apply_motion_blur(input_path: str, output_path: str, filter_str: str):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", filter_str,
        "-map", "0:v", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]
    print(f"[apply-motion-blur] Processing: {Path(input_path).name}")
    print(f"[apply-motion-blur] Filter: {filter_str}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[apply-motion-blur] ffmpeg failed: {e}", file=sys.stderr)
        raise
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-motion-blur] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Apply motion blur to video")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="Blur preset")
    parser.add_argument("--mode", choices=["temporal", "directional"],
                        help="Blur mode (temporal=frame blend, directional=angle blur)")
    parser.add_argument("--frames", type=int, help="Frames to blend (temporal mode, default 3)")
    parser.add_argument("--sigma", type=float, help="Blur radius (directional mode, default 5)")
    parser.add_argument("--angle", type=int, help="Blur angle in degrees (directional mode, 0=horizontal)")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[apply-motion-blur] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    params = dict(PRESETS.get(args.preset or "subtle", PRESETS["subtle"]))
    if args.mode is not None:
        params["mode"] = args.mode
    if args.frames is not None:
        params["frames"] = args.frames
    if args.sigma is not None:
        params["sigma"] = args.sigma
    if args.angle is not None:
        params["angle"] = args.angle

    filter_str = build_motion_blur_filter(**params)
    apply_motion_blur(input_path, args.output, filter_str)


if __name__ == "__main__":
    main()
