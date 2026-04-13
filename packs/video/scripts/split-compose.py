#!/usr/bin/env python3
"""
Compose a vertical video from two cropped layers of the same source.

Designed for screen recordings with webcam overlays. Each layer gets its own
crop region, fill mode, and position. The script handles scaling, padding,
and stacking.

Usage:
  # 50/50 split — webcam top, screen bottom
  split-compose.py --input clip.mp4 \
    --top-crop 1370,400,520,380 --top-fill pad \
    --bot-crop 0,45,1920,1000 --bot-fill cover \
    --start 14.4 --duration 45 \
    --output vertical.mp4

  # Preview a single frame (no encode)
  split-compose.py --input clip.mp4 \
    --top-crop 1370,400,520,380 --bot-crop 0,45,1920,1000 \
    --preview --preview-time 30

  # Adjust split ratio (default 50/50)
  split-compose.py --input clip.mp4 \
    --top-crop ... --bot-crop ... \
    --split 60 --output vertical.mp4   # 60% top, 40% bottom
"""
import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_BG_COLOR = "0x1a1a2e"


def parse_crop(crop_str: str) -> tuple:
    """Parse 'X,Y,W,H' into (x, y, w, h)."""
    parts = crop_str.split(",")
    if len(parts) != 4:
        print(f"Invalid crop format '{crop_str}' — expected X,Y,W,H", file=sys.stderr)
        sys.exit(1)
    return tuple(int(p) for p in parts)


def build_filter(top_crop, bot_crop, top_fill, bot_fill,
                 split_pct, out_w, out_h, bg_color):
    """Build the ffmpeg filter_complex string."""
    top_h = int(out_h * split_pct / 100)
    bot_h = out_h - top_h

    tx, ty, tw, th = top_crop
    bx, by, bw, bh = bot_crop

    parts = ["[0:v]split=2[_top][_bot]"]

    # Top layer
    if top_fill == "cover":
        parts.append(f"[_top]crop={tw}:{th}:{tx}:{ty},scale=-1:{top_h},crop={out_w}:{top_h}[top]")
    else:  # pad
        parts.append(
            f"[_top]crop={tw}:{th}:{tx}:{ty},"
            f"scale={out_w}:-1,scale='min({out_w},iw)':'min({top_h},ih)',"
            f"pad={out_w}:{top_h}:({out_w}-iw)/2:({top_h}-ih)/2:color={bg_color}[top]"
        )

    # Bottom layer
    if bot_fill == "cover":
        parts.append(f"[_bot]crop={bw}:{bh}:{bx}:{by},scale=-1:{bot_h},crop={out_w}:{bot_h}[bot]")
    else:  # pad
        parts.append(
            f"[_bot]crop={bw}:{bh}:{bx}:{by},"
            f"scale={out_w}:-1,scale='min({out_w},iw)':'min({bot_h},ih)',"
            f"pad={out_w}:{bot_h}:({out_w}-iw)/2:({bot_h}-ih)/2:color={bg_color}[bot]"
        )

    parts.append("[top][bot]vstack[out]")
    return ";".join(parts)


def run(args):
    top_crop = parse_crop(args.top_crop)
    bot_crop = parse_crop(args.bot_crop)

    filter_str = build_filter(
        top_crop, bot_crop,
        args.top_fill, args.bot_fill,
        args.split, args.width, args.height,
        args.bg_color,
    )

    if args.preview:
        out_path = args.output or str(
            Path(args.input).parent / "edits" / "_split_preview.png"
        )
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(args.preview_time),
            "-i", args.input,
            "-frames:v", "1",
            "-filter_complex", filter_str,
            "-map", "[out]",
            out_path,
        ]
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"[split-compose] Preview failed: {e}", file=sys.stderr)
            sys.exit(1)

        size_kb = Path(out_path).stat().st_size / 1024
        print(f"[split-compose] Preview: {out_path} ({size_kb:.0f}KB)")
        print(f"[split-compose]   top: {args.top_crop} fill={args.top_fill}")
        print(f"[split-compose]   bot: {args.bot_crop} fill={args.bot_fill}")
        print(f"[split-compose]   split: {args.split}/{100 - args.split}")
        return

    # Full render
    if not args.output:
        print("--output required for full render", file=sys.stderr)
        sys.exit(1)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
    ]
    if args.start is not None:
        cmd += ["-ss", str(args.start)]
    if args.duration is not None:
        cmd += ["-t", str(args.duration)]
    cmd += [
        "-i", args.input,
        "-filter_complex", filter_str,
        "-map", "[out]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-r", "30",
        args.output,
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[split-compose] Render failed: {e}", file=sys.stderr)
        sys.exit(1)

    size_mb = Path(args.output).stat().st_size / 1024 / 1024
    print(f"[split-compose] Output: {args.output} ({size_mb:.1f}MB)")
    print(f"[split-compose]   top: {args.top_crop} fill={args.top_fill}")
    print(f"[split-compose]   bot: {args.bot_crop} fill={args.bot_fill}")
    print(f"[split-compose]   split: {args.split}/{100 - args.split}")


def main():
    p = argparse.ArgumentParser(description="Compose vertical video from two cropped layers")
    p.add_argument("--input", required=True, help="Source video")
    p.add_argument("--output", help="Output path")

    p.add_argument("--top-crop", required=True, help="Top layer crop: X,Y,W,H")
    p.add_argument("--top-fill", choices=["cover", "pad"], default="pad",
                    help="Top fill mode (default: pad)")
    p.add_argument("--bot-crop", required=True, help="Bottom layer crop: X,Y,W,H")
    p.add_argument("--bot-fill", choices=["cover", "pad"], default="cover",
                    help="Bottom fill mode (default: cover)")

    p.add_argument("--split", type=int, default=50,
                    help="Top layer percentage (default: 50 = even split)")
    p.add_argument("--bg-color", default=DEFAULT_BG_COLOR,
                    help=f"Padding color (default: {DEFAULT_BG_COLOR})")

    p.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    p.add_argument("--height", type=int, default=DEFAULT_HEIGHT)

    p.add_argument("--start", type=float, help="Start time in seconds")
    p.add_argument("--duration", type=float, help="Duration in seconds")

    p.add_argument("--preview", action="store_true", help="Single frame preview (no encode)")
    p.add_argument("--preview-time", type=float, default=0,
                    help="Timestamp for preview frame (default: 0)")

    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
