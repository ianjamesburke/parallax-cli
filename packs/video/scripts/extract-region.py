#!/usr/bin/env python3
"""
Extract one or more regions from a video — probe, test crops, render vertical.

Iterative workflow:
  1. --probe:   Extract frame with coordinate grid for accurate crop guessing
  2. --test:    Crop one or more regions from a single frame to verify coordinates
  3. --render:  Crop a region into a standalone vertical video

Usage:
  # Step 1: grab a frame with coordinate grid
  extract-region.py --input video.mp4 --probe

  # Step 2: test one or more named crops (single ffmpeg call, all outputs at once)
  extract-region.py --input video.mp4 --test --crop webcam=1410,390,300,270 --crop bg=1650,110,220,240

  # Step 3: render one region as vertical video
  extract-region.py --input video.mp4 --crop webcam=1410,390,300,270 --render

Options:
  --crop [NAME=]X,Y,W,H  Crop coordinates. NAME is optional (default: "region").
                          Can specify multiple --crop flags for --test mode.
  --output PATH           Output path (default: auto-generated)
  --bg blur|black         Background fill mode (default: blur)
  --vertical WxH          Target vertical resolution (default: 1080x1920)
  --start SECONDS         Start time in source video
  --duration SECONDS      Duration to extract (default: full video)
  --timestamp SECONDS     Timestamp for probe/test frame (default: 0)
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


def probe_frame(input_path: str, output_dir: str, timestamp: float = 0) -> str:
    """Extract a frame with coordinate grid overlay for accurate crop guessing.

    Outputs two files:
      - probe_frame.jpg — clean frame
      - probe_frame_grid.jpg — same frame with grid lines and pixel coordinates
    """
    clean_out = os.path.join(output_dir, "probe_frame.jpg")
    grid_out = os.path.join(output_dir, "probe_frame_grid.jpg")

    # Get video dimensions
    probe_cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "csv=p=0",
        "-select_streams", "v:0", "-show_entries", "stream=width,height",
        input_path,
    ]
    try:
        result = subprocess.run(probe_cmd, check=True, capture_output=True, text=True)
        w, h = [int(x) for x in result.stdout.strip().split(",")]
    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"[extract-region] Failed to probe video dimensions: {e}", file=sys.stderr)
        raise

    # Extract clean frame
    cmd_clean = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(timestamp),
        "-i", input_path,
        "-vframes", "1", "-q:v", "2",
        clean_out,
    ]
    try:
        subprocess.run(cmd_clean, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[extract-region] Failed to extract probe frame: {e}", file=sys.stderr)
        raise

    # Build grid overlay using ffmpeg drawbox (no libfreetype needed)
    # Draw lines at every quarter and third, with coordinate labels via drawbox markers
    grid_filters = []

    # Vertical lines at 1/4, 1/3, 1/2, 2/3, 3/4
    for frac in [0.25, 1/3, 0.5, 2/3, 0.75]:
        px = int(w * frac)
        grid_filters.append(f"drawbox=x={px}:y=0:w=1:h={h}:color=red@0.5:t=fill")
        # Small marker box at top with x-coordinate
        grid_filters.append(f"drawbox=x={px-1}:y=0:w=3:h=18:color=red@0.8:t=fill")

    # Horizontal lines at 1/4, 1/3, 1/2, 2/3, 3/4
    for frac in [0.25, 1/3, 0.5, 2/3, 0.75]:
        py = int(h * frac)
        grid_filters.append(f"drawbox=x=0:y={py}:w={w}:h=1:color=red@0.5:t=fill")
        # Small marker box at left with y-coordinate
        grid_filters.append(f"drawbox=x=0:y={py-1}:w=18:h=3:color=red@0.8:t=fill")

    filter_str = ",".join(grid_filters)

    cmd_grid = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(timestamp),
        "-i", input_path,
        "-vframes", "1",
        "-vf", filter_str,
        "-q:v", "2",
        grid_out,
    ]
    try:
        subprocess.run(cmd_grid, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[extract-region] Grid overlay failed, using clean frame only: {e}", file=sys.stderr)
        grid_out = clean_out

    size_kb = Path(clean_out).stat().st_size / 1024
    print(f"[extract-region] Resolution: {w}x{h}")
    print(f"[extract-region] Probe frame: {clean_out} ({size_kb:.0f}KB)")
    print(f"[extract-region] Grid frame:  {grid_out}")
    print(f"[extract-region] Grid lines at:")
    for frac in [0.25, 1/3, 0.5, 2/3, 0.75]:
        print(f"  x={int(w*frac):4d} ({frac:.0%})   y={int(h*frac):4d} ({frac:.0%})")
    return clean_out


def test_crops(input_path: str, crops: dict, output_dir: str, timestamp: float = 0) -> list:
    """Test one or more named crops — outputs a cropped frame for each.

    Args:
        crops: dict mapping name → (x, y, w, h)

    Returns list of output paths.
    """
    outputs = []
    for name, (x, y, w, h) in crops.items():
        out = os.path.join(output_dir, f"crop_test_{name}.jpg")
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(timestamp),
            "-i", input_path,
            "-vframes", "1",
            "-vf", f"crop={w}:{h}:{x}:{y}",
            "-q:v", "2",
            out,
        ]
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"[extract-region] Crop test '{name}' failed: {e}", file=sys.stderr)
            raise
        size_kb = Path(out).stat().st_size / 1024
        print(f"[extract-region] {name}: {out} ({size_kb:.0f}KB) — crop={w}x{h} @ ({x},{y})")
        outputs.append(out)
    return outputs


def render_vertical(input_path: str, crop: tuple, output_path: str,
                    bg_mode: str = "blur", target_w: int = 1080, target_h: int = 1920,
                    start: float | None = None, duration: float | None = None) -> str:
    """Crop region and render as vertical video with background fill."""
    x, y, w, h = crop

    # Calculate scaled foreground size (fit width, maintain aspect ratio)
    scale_factor = target_w / w
    fg_h = int(h * scale_factor)
    # Ensure even dimensions
    fg_h = fg_h + (fg_h % 2)

    if bg_mode == "blur":
        filter_complex = (
            f"[0:v]crop={w}:{h}:{x}:{y},scale={target_w}:{fg_h}:flags=lanczos[fg];"
            f"[fg]split[fg1][fg2];"
            f"[fg1]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},boxblur=25:5[bg];"
            f"[bg][fg2]overlay=(W-w)/2:(H-h)/2[v]"
        )
    else:
        # Black background — just pad
        filter_complex = (
            f"[0:v]crop={w}:{h}:{x}:{y},"
            f"scale={target_w}:{fg_h}:flags=lanczos,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black[v]"
        )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
    ]
    if start is not None:
        cmd += ["-ss", str(start)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-i", input_path]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        output_path,
    ]

    print(f"[extract-region] Rendering vertical video...")
    print(f"[extract-region] Crop: {w}x{h} @ ({x},{y}) → {target_w}x{target_h} ({bg_mode} bg)")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[extract-region] Render failed: {e}", file=sys.stderr)
        raise

    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[extract-region] Output: {output_path} ({size_mb:.1f}MB)")
    return output_path


def parse_crop(crop_str: str) -> tuple:
    """Parse '[NAME=]X,Y,W,H' into (name, (x, y, w, h))."""
    if "=" in crop_str:
        name, coords = crop_str.split("=", 1)
    else:
        name = "region"
        coords = crop_str
    parts = [int(p.strip()) for p in coords.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Crop must be [NAME=]X,Y,W,H — got {crop_str}")
    return name, tuple(parts)


def parse_resolution(res_str: str) -> tuple:
    """Parse 'WxH' into (w, h) tuple."""
    parts = [int(p.strip()) for p in res_str.lower().split("x")]
    if len(parts) != 2:
        raise ValueError(f"Resolution must be WxH — got {res_str}")
    return tuple(parts)


def main():
    parser = argparse.ArgumentParser(description="Extract a region from video into vertical format")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--crop", action="append", help="Crop: [NAME=]X,Y,W,H (repeatable)")
    parser.add_argument("--output", help="Output path (default: auto-generated)")
    parser.add_argument("--probe", action="store_true", help="Extract frame with coordinate grid")
    parser.add_argument("--test", action="store_true", help="Test crop(s) on single frame")
    parser.add_argument("--render", action="store_true", help="Render full vertical video")
    parser.add_argument("--bg", choices=["blur", "black"], default="blur",
                        help="Background fill mode (default: blur)")
    parser.add_argument("--vertical", default="1080x1920",
                        help="Target vertical resolution (default: 1080x1920)")
    parser.add_argument("--start", type=float, help="Start time in seconds")
    parser.add_argument("--duration", type=float, help="Duration in seconds")
    parser.add_argument("--timestamp", type=float, default=0,
                        help="Timestamp for probe/test frame (default: 0)")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[extract-region] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.dirname(input_path)

    if args.probe:
        probe_frame(input_path, output_dir, args.timestamp)
        return

    if args.test:
        if not args.crop:
            print("[extract-region] --test requires --crop [NAME=]X,Y,W,H", file=sys.stderr)
            sys.exit(1)
        crops = {}
        for c in args.crop:
            name, coords = parse_crop(c)
            crops[name] = coords
        test_crops(input_path, crops, output_dir, args.timestamp)
        return

    if args.render:
        if not args.crop:
            print("[extract-region] --render requires --crop [NAME=]X,Y,W,H", file=sys.stderr)
            sys.exit(1)
        name, crop = parse_crop(args.crop[0])
        target_w, target_h = parse_resolution(args.vertical)

        if args.output:
            output_path = args.output
        else:
            stem = Path(input_path).stem
            output_path = os.path.join(output_dir, f"{stem}_{name}_vertical.mp4")

        render_vertical(input_path, crop, output_path, args.bg, target_w, target_h,
                        args.start, args.duration)
        return

    # Default: show usage
    parser.print_help()


if __name__ == "__main__":
    main()
