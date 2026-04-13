#!/usr/bin/env python3
"""
Compose a vertical video from layers: background region, foreground region, overlay PNG.

Takes one source video and extracts two regions — one for background (scaled/rotated to fill),
one for foreground (centered on top) — plus an optional static overlay (e.g. title PNG).

Usage:
  compose-vertical.py --input video.mp4 \
    --bg 1650,110,220,240 --bg-rotate 90 \
    --fg 1410,390,300,270 --fg-scale 700 \
    --overlay title.png \
    --duration 15 --output vertical.mp4

  # Simpler: just foreground with blurred-bg fill (same as extract-region --render)
  compose-vertical.py --input video.mp4 \
    --fg 1410,390,300,270 \
    --duration 15 --output vertical.mp4

Options:
  --bg X,Y,W,H         Background region crop coordinates
  --bg-rotate DEG       Rotate background (0, 90, 180, 270) (default: 0)
  --bg-blur AMOUNT      Blur the background (default: 0 = no blur)
  --fg X,Y,W,H         Foreground region crop coordinates
  --fg-scale WIDTH      Scale foreground to this width in pixels (default: auto 65% of output width)
  --fg-pos POSITION     Foreground position: center, top, bottom (default: center)
  --overlay PATH        Transparent PNG to composite on top (e.g. title card)
  --vertical WxH        Output resolution (default: 1080x1920)
  --duration SECONDS    Duration to extract (default: full video)
  --start SECONDS       Start time in source video
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_crop(crop_str: str) -> tuple:
    """Parse 'X,Y,W,H' into (x, y, w, h)."""
    parts = [int(p.strip()) for p in crop_str.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Crop must be X,Y,W,H — got {crop_str}")
    return tuple(parts)


def parse_resolution(res_str: str) -> tuple:
    parts = [int(p.strip()) for p in res_str.lower().split("x")]
    if len(parts) != 2:
        raise ValueError(f"Resolution must be WxH — got {res_str}")
    return tuple(parts)


def build_filter(bg_crop, bg_rotate, bg_blur, fg_crop, fg_scale, fg_pos,
                 out_w, out_h, has_overlay):
    """Build the ffmpeg filter_complex string."""
    filters = []
    input_idx = 0  # source video is always input 0

    if bg_crop:
        bx, by, bw, bh = bg_crop
        # Crop background region
        bg_chain = f"[{input_idx}:v]crop={bw}:{bh}:{bx}:{by}"

        # Rotate if needed
        if bg_rotate == 90:
            bg_chain += ",transpose=1"
        elif bg_rotate == 180:
            bg_chain += ",transpose=1,transpose=1"
        elif bg_rotate == 270:
            bg_chain += ",transpose=2"

        # Scale to fill output
        bg_chain += f",scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}"

        if bg_blur and bg_blur > 0:
            bg_chain += f",boxblur={bg_blur}:5"

        bg_chain += "[bg]"
        filters.append(bg_chain)
        current = "[bg]"
    else:
        # No explicit background — use foreground blurred as bg
        if fg_crop:
            fx, fy, fw, fh = fg_crop
            filters.append(
                f"[{input_idx}:v]crop={fw}:{fh}:{fx}:{fy},"
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h},boxblur=25:5[bg]"
            )
            current = "[bg]"
        else:
            # Solid black fallback
            filters.append(f"color=black:s={out_w}x{out_h}:r=30[bg]")
            current = "[bg]"

    if fg_crop:
        fx, fy, fw, fh = fg_crop
        # Determine foreground scale
        if fg_scale is None:
            fg_scale = int(out_w * 0.65)
        scale_h = int(fh * (fg_scale / fw))
        # Ensure even
        fg_scale = fg_scale + (fg_scale % 2)
        scale_h = scale_h + (scale_h % 2)

        filters.append(
            f"[{input_idx}:v]crop={fw}:{fh}:{fx}:{fy},"
            f"scale={fg_scale}:{scale_h}:flags=lanczos[fg]"
        )

        # Position
        x_expr = "(W-w)/2"
        if fg_pos == "top":
            y_expr = f"{int(out_h * 0.08)}"
        elif fg_pos == "bottom":
            y_expr = f"{int(out_h * 0.92)}-h"
        else:
            y_expr = "(H-h)/2"

        filters.append(f"{current}[fg]overlay={x_expr}:{y_expr}[comp]")
        current = "[comp]"

    if has_overlay:
        overlay_idx = 1  # overlay PNG is input 1
        filters.append(f"{current}[{overlay_idx}:v]overlay=0:0[v]")
        current = "[v]"
    else:
        # Rename final output
        last = filters[-1]
        # Replace the last label with [v]
        if last.endswith("[comp]"):
            filters[-1] = last[:-6] + "[v]"
        elif last.endswith("[bg]"):
            filters[-1] = last[:-4] + "[v]"
        current = "[v]"

    return ";".join(filters)


def compose(input_path: str, output_path: str, overlay_path: str | None,
            bg_crop: tuple | None, bg_rotate: int, bg_blur: int,
            fg_crop: tuple | None, fg_scale: int | None, fg_pos: str,
            out_w: int, out_h: int,
            start: float | None, duration: float | None):
    """Run the composition."""

    has_overlay = overlay_path is not None

    filter_complex = build_filter(
        bg_crop, bg_rotate, bg_blur, fg_crop, fg_scale, fg_pos,
        out_w, out_h, has_overlay
    )

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    # -ss and -t before -i = input-level seeking/duration (fast, no full decode)
    if start is not None:
        cmd += ["-ss", str(start)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-i", input_path]
    if has_overlay:
        cmd += ["-i", overlay_path]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        output_path,
    ]

    desc_parts = []
    if bg_crop:
        desc_parts.append(f"bg={bg_crop[2]}x{bg_crop[3]}@({bg_crop[0]},{bg_crop[1]}) rot={bg_rotate}")
    if fg_crop:
        desc_parts.append(f"fg={fg_crop[2]}x{fg_crop[3]}@({fg_crop[0]},{fg_crop[1]}) scale={fg_scale or 'auto'}")
    if has_overlay:
        desc_parts.append(f"overlay={Path(overlay_path).name}")

    print(f"[compose] Composing {out_w}x{out_h} vertical video...")
    for d in desc_parts:
        print(f"[compose]   {d}")

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[compose] Failed: {e}", file=sys.stderr)
        print(f"[compose] Filter: {filter_complex}", file=sys.stderr)
        raise

    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[compose] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Compose vertical video from layered regions")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--bg", help="Background crop: X,Y,W,H")
    parser.add_argument("--bg-rotate", type=int, default=0, choices=[0, 90, 180, 270],
                        help="Rotate background (default: 0)")
    parser.add_argument("--bg-blur", type=int, default=0,
                        help="Blur background (0 = no blur, 25 = heavy)")
    parser.add_argument("--fg", help="Foreground crop: X,Y,W,H")
    parser.add_argument("--fg-scale", type=int, help="Foreground width in px (default: 65%% of output)")
    parser.add_argument("--fg-pos", choices=["center", "top", "bottom"], default="center",
                        help="Foreground position (default: center)")
    parser.add_argument("--overlay", help="Transparent PNG to overlay (e.g. title card)")
    parser.add_argument("--vertical", default="1080x1920",
                        help="Output resolution (default: 1080x1920)")
    parser.add_argument("--start", type=float, help="Start time in seconds")
    parser.add_argument("--duration", type=float, help="Duration in seconds")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[compose] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    bg_crop = parse_crop(args.bg) if args.bg else None
    fg_crop = parse_crop(args.fg) if args.fg else None
    out_w, out_h = parse_resolution(args.vertical)

    if not bg_crop and not fg_crop:
        print("[compose] Need at least --bg or --fg", file=sys.stderr)
        sys.exit(1)

    overlay_path = None
    if args.overlay:
        overlay_path = os.path.abspath(args.overlay)
        if not os.path.exists(overlay_path):
            print(f"[compose] Overlay not found: {overlay_path}", file=sys.stderr)
            sys.exit(1)

    compose(input_path, args.output, overlay_path,
            bg_crop, args.bg_rotate, args.bg_blur,
            fg_crop, args.fg_scale, args.fg_pos,
            out_w, out_h, args.start, args.duration)


if __name__ == "__main__":
    main()
