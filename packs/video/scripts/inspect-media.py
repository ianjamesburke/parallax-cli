#!/usr/bin/env python3
"""
inspect-media.py — Quick media inspection: duration, size, streams.

Prints a human-readable summary of a video or audio file via ffprobe.
Optionally generates a preview sheet for visual frame inspection.

Usage:
    python3 inspect-media.py video.mp4
    python3 inspect-media.py video.mp4 --preview
    python3 inspect-media.py video.mp4 --preview --frames 24 --output sheet.jpg
"""

import argparse
import json
import os
import subprocess
import sys


def probe(input_path: str) -> dict:
    """Run ffprobe and return parsed JSON."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                input_path,
            ],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: ffprobe failed on {input_path}: {e.stderr[:300] if e.stderr else 'unknown error'}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse ffprobe output: {e}", file=sys.stderr)
        sys.exit(1)


def format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:05.2f} ({seconds:.1f}s)"
    if m > 0:
        return f"{m}:{s:05.2f} ({seconds:.1f}s / {seconds/60:.1f}min)"
    return f"{seconds:.2f}s"


def format_size(path: str) -> str:
    try:
        b = os.path.getsize(path)
        if b >= 1_000_000_000:
            return f"{b / 1_000_000_000:.1f}GB"
        if b >= 1_000_000:
            return f"{b / 1_000_000:.1f}MB"
        return f"{b / 1_000:.1f}KB"
    except OSError:
        return "unknown"


def summarise(input_path: str) -> None:
    data = probe(input_path)
    fmt = data.get("format", {})
    streams = data.get("streams", [])

    duration = float(fmt.get("duration", 0))
    size = format_size(input_path)

    print(f"File:     {os.path.basename(input_path)}")
    print(f"Size:     {size}")
    print(f"Duration: {format_duration(duration)}")

    for s in streams:
        codec_type = s.get("codec_type", "unknown")
        codec = s.get("codec_name", "?")
        if codec_type == "video":
            w = s.get("width", "?")
            h = s.get("height", "?")
            r_num, r_den = (s.get("r_frame_rate", "0/1").split("/") + ["1"])[:2]
            try:
                fps = float(r_num) / float(r_den)
                fps_str = f"{fps:.3g}"
            except (ValueError, ZeroDivisionError):
                fps_str = "?"
            pix_fmt = s.get("pix_fmt", "")
            print(f"Video:    {codec} {w}x{h} @ {fps_str} fps{f'  [{pix_fmt}]' if pix_fmt else ''}")

            # Detect Display Matrix rotation (common on iPhone portrait MOVs)
            rotation = None
            for sd in s.get("side_data_list", []):
                if sd.get("side_data_type") == "Display Matrix":
                    rotation = sd.get("rotation")
                    break
            if rotation is not None and int(rotation) != 0:
                deg = int(rotation)
                # Compute display dimensions after rotation
                if abs(deg) == 90 or abs(deg) == 270:
                    display = f"{h}x{w}"
                else:
                    display = f"{w}x{h}"
                print(f"⚠️  Display Matrix rotation: {deg}°  (display: {display})")
                print(f"   NLE export: re-encode first to bake in rotation →")
                print(f"   ffmpeg -i input.mov -c:v libx264 -preset fast -crf 18 -c:a aac output.mp4")
                print(f"   ffmpeg auto-applies the rotation on decode — no transpose filter needed.")

        elif codec_type == "audio":
            sr = s.get("sample_rate", "?")
            ch = s.get("channels", "?")
            print(f"Audio:    {codec} {sr}Hz {ch}ch")


def generate_preview(input_path: str, output_path: str, frames: int, cols: int) -> None:
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    preview_sheet = os.path.join(scripts_dir, "preview-sheet.py")

    if not os.path.exists(preview_sheet):
        print(f"ERROR: preview-sheet.py not found at {preview_sheet}", file=sys.stderr)
        sys.exit(1)

    cmd = [
        sys.executable, preview_sheet,
        "--input", input_path,
        "--frames", str(frames),
        "--cols", str(cols),
        "--output", output_path,
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        # preview-sheet.py prints JSON to stdout with output paths
        info = json.loads(result.stdout)
        pages = info.get("pages", [output_path])
        for p in pages:
            print(f"Preview:  {p}")
    except subprocess.CalledProcessError as e:
        print(f"ERROR: preview-sheet.py failed: {e.stderr[-500:] if e.stderr else 'unknown error'}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        # preview-sheet printed something unexpected; still usable
        print(f"Preview:  {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Inspect media file — streams, duration, size")
    parser.add_argument("input", help="Video or audio file to inspect")
    parser.add_argument("--preview", action="store_true", help="Generate a preview sheet of evenly-spaced frames")
    parser.add_argument("--frames", type=int, default=24, help="Number of frames in the preview sheet (default: 24)")
    parser.add_argument("--cols", type=int, default=5, help="Columns in the preview grid (default: 5)")
    parser.add_argument("--output", "-o", help="Output path for preview sheet (default: <input>_preview.jpg)")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    summarise(args.input)

    if args.preview:
        if args.output:
            out = args.output
        else:
            base = os.path.splitext(args.input)[0]
            out = f"{base}_preview.jpg"
        print()
        generate_preview(args.input, out, frames=args.frames, cols=args.cols)


if __name__ == "__main__":
    main()
