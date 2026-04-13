#!/usr/bin/env python3
"""
preview-sheet.py — Generate a tiled preview sheet (grid of frames) from a video or image directory.

Two modes:
  1. Video mode: extracts evenly-spaced frames from a .mp4 and tiles them with timecodes.
  2. Stills mode: tiles all images from a directory with filename labels.

Visual inspection tool for source footage and rendered output. Also called via inspect-media.py --preview.

Add --waveform <audio_or_video> to append a waveform strip below the frame grid, with silence
regions shaded and vertical tick marks aligned to each frame's sample position. This produces a
single image covering both visual and audio inspection — the model can cross-reference a frame
with the audio underneath it without switching between two images.

NOT the animatic tool. For a pre-render text wireframe (VO script + action per scene),
use: assemble.py --animatic

Usage:
    python3 preview-sheet.py --input video.mp4 --frames 24 --output sheet.jpg
    python3 preview-sheet.py --input video.mp4 --fps 4 --output sheet.jpg
    python3 preview-sheet.py --input video.mp4 --waveform audio.mp3 --output sheet.jpg
    python3 preview-sheet.py --input video.mp4 --waveform video.mp4 --min-silence 0.15 --output sheet.jpg
    python3 preview-sheet.py --stills path/to/stills/ --output stills_sheet.jpg
    python3 preview-sheet.py --stills path/to/stills/ --cols 3 --output stills_sheet.jpg
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow is required. Install with: pip3 install Pillow", file=sys.stderr)
    sys.exit(1)


def get_duration(input_path: str) -> float:
    """Get video duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "json", input_path
            ],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except (subprocess.CalledProcessError, KeyError, ValueError) as e:
        print(f"ERROR: Failed to get duration for {input_path}: {e}", file=sys.stderr)
        sys.exit(1)


def extract_frames(input_path: str, timestamps: list[float], tmpdir: str) -> list[str]:
    """Extract frames at specific timestamps. Returns list of image paths."""
    paths = []
    for i, ts in enumerate(timestamps):
        out = os.path.join(tmpdir, f"frame_{i:04d}.jpg")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-ss", str(ts), "-i", input_path,
                    "-frames:v", "1", "-q:v", "2", out, "-y"
                ],
                capture_output=True, check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"WARNING: Failed to extract frame at {ts:.2f}s: {e.stderr[:200] if e.stderr else 'unknown error'}", file=sys.stderr)
            continue
        if os.path.exists(out):
            paths.append(out)
    return paths


def format_timecode(seconds: float) -> str:
    """Format seconds as HH:MM:SS.f or MM:SS.f."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m:02d}:{s:05.2f}"


def build_preview_sheets(
    frame_paths: list[str],
    timestamps: list[float],
    cols: int,
    thumb_width: int,
    max_height: int,
    output_path: str,
    waveform_path: str | None = None,
    min_silence: float = 0.15,
    noise_db: float = -40.0,
) -> list[str]:
    """Tile frames into preview sheet(s) with timecode labels. Returns output paths."""
    if not frame_paths:
        print("ERROR: No frames extracted.", file=sys.stderr)
        sys.exit(1)

    # Load first frame to get aspect ratio
    sample = Image.open(frame_paths[0])
    aspect = sample.height / sample.width
    thumb_height = int(thumb_width * aspect)
    sample.close()

    label_height = 32
    cell_height = thumb_height + label_height
    padding = 4

    # Try to load a monospace font
    font = None
    for font_name in ["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]:
        try:
            font = ImageFont.truetype(font_name, 20)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()

    # Calculate pagination
    total_frames = len(frame_paths)
    # When waveform is enabled, each row gets its own strip — factor that into page height
    waveform_row_h = 164 if waveform_path else 0  # STRIP_H(120) + LABEL_H(36)
    row_unit_h = cell_height + padding + waveform_row_h
    rows_per_page = max(1, (max_height - padding) // row_unit_h)
    frames_per_page = rows_per_page * cols

    pages = []
    page_idx = 0
    step = (timestamps[1] - timestamps[0]) if len(timestamps) > 1 else 1.0

    for page_start_idx in range(0, total_frames, frames_per_page):
        batch_frames = frame_paths[page_start_idx:page_start_idx + frames_per_page]
        batch_timestamps = timestamps[page_start_idx:page_start_idx + frames_per_page]
        n_rows = math.ceil(len(batch_frames) / cols)

        sheet_w = padding + cols * (thumb_width + padding)

        # Build each row as a separate image, optionally followed by its waveform strip
        row_images: list[Image.Image] = []

        for r in range(n_rows):
            row_frame_paths = batch_frames[r * cols:(r + 1) * cols]
            row_timestamps = batch_timestamps[r * cols:(r + 1) * cols]

            row_h = padding + cell_height + padding
            row_img = Image.new("RGB", (sheet_w, row_h), (30, 30, 30))
            draw = ImageDraw.Draw(row_img)

            for c, (fpath, ts) in enumerate(zip(row_frame_paths, row_timestamps)):
                x = padding + c * (thumb_width + padding)
                y = padding
                try:
                    thumb = Image.open(fpath)
                    thumb = thumb.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
                    row_img.paste(thumb, (x, y))
                    thumb.close()
                except (OSError, IOError) as e:
                    print(f"WARNING: Failed to load frame {fpath}: {e}", file=sys.stderr)
                    continue
                tc = format_timecode(ts)
                label_y = y + thumb_height + 2
                draw.rectangle([x, label_y, x + thumb_width, label_y + label_height], fill=(0, 0, 0))
                draw.text((x + 4, label_y + 2), tc, fill=(255, 255, 255), font=font)

            row_images.append(row_img)

            # Waveform strip for this row's time window
            if waveform_path and row_timestamps:
                row_t_start = row_timestamps[0] - step / 2
                row_t_end = row_timestamps[-1] + step / 2
                strip = _build_waveform_strip(
                    waveform_path, row_t_start, row_t_end, sheet_w,
                    min_silence, noise_db, list(row_timestamps),
                )
                if strip:
                    row_images.append(strip)

        # Stack all row images vertically
        total_h = sum(img.height for img in row_images)
        sheet = Image.new("RGB", (sheet_w, total_h), (18, 18, 24))
        y_cursor = 0
        for img in row_images:
            sheet.paste(img, (0, y_cursor))
            y_cursor += img.height

        # Save
        base, ext = os.path.splitext(output_path)
        if page_idx == 0 and frames_per_page >= total_frames:
            page_path = output_path
        else:
            page_path = f"{base}_page{page_idx + 1}{ext}"

        sheet.save(page_path, "JPEG", quality=92)
        pages.append(page_path)
        dims = f"{sheet.width}x{sheet.height}"
        print(f"Saved: {page_path} ({len(batch_frames)} frames, {dims})", file=sys.stderr)
        page_idx += 1

    return pages


STILL_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}


def collect_stills(stills_dir: str) -> tuple[list[str], list[str]]:
    """Collect image files from a directory, sorted by name. Returns (paths, labels)."""
    files = []
    for f in sorted(os.listdir(stills_dir)):
        if os.path.splitext(f)[1].lower() in STILL_EXTENSIONS:
            files.append((os.path.join(stills_dir, f), os.path.splitext(f)[0]))
    if not files:
        print(f"ERROR: No image files found in {stills_dir}", file=sys.stderr)
        sys.exit(1)
    paths, labels = zip(*files)
    return list(paths), list(labels)


def build_stills_preview_sheet(
    image_paths: list[str],
    labels: list[str],
    cols: int,
    thumb_width: int,
    max_height: int,
    output_path: str,
) -> list[str]:
    """Tile still images into preview sheet(s) with filename labels. Returns output paths."""
    if not image_paths:
        print("ERROR: No images provided.", file=sys.stderr)
        sys.exit(1)

    sample = Image.open(image_paths[0])
    aspect = sample.height / sample.width
    thumb_height = int(thumb_width * aspect)
    sample.close()

    label_height = 32
    cell_height = thumb_height + label_height
    padding = 4

    font = None
    for font_name in ["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]:
        try:
            font = ImageFont.truetype(font_name, 20)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()

    total = len(image_paths)
    rows_per_page = max(1, (max_height - padding) // (cell_height + padding))
    frames_per_page = rows_per_page * cols

    pages = []
    page_idx = 0

    for start in range(0, total, frames_per_page):
        batch_paths = image_paths[start:start + frames_per_page]
        batch_labels = labels[start:start + frames_per_page]
        rows = math.ceil(len(batch_paths) / cols)

        sheet_w = padding + cols * (thumb_width + padding)
        sheet_h = padding + rows * (cell_height + padding)

        sheet = Image.new("RGB", (sheet_w, sheet_h), (30, 30, 30))
        draw = ImageDraw.Draw(sheet)

        for i, (fpath, label) in enumerate(zip(batch_paths, batch_labels)):
            row = i // cols
            col = i % cols
            x = padding + col * (thumb_width + padding)
            y = padding + row * (cell_height + padding)

            try:
                thumb = Image.open(fpath)
                thumb = thumb.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
                sheet.paste(thumb, (x, y))
                thumb.close()
            except (OSError, IOError) as e:
                print(f"WARNING: Failed to load {fpath}: {e}", file=sys.stderr)
                continue

            label_y = y + thumb_height + 2
            draw.rectangle([x, label_y, x + thumb_width, label_y + label_height], fill=(0, 0, 0))
            draw.text((x + 4, label_y + 2), label, fill=(255, 255, 255), font=font)

        base, ext = os.path.splitext(output_path)
        if page_idx == 0 and frames_per_page >= total:
            page_path = output_path
        else:
            page_path = f"{base}_page{page_idx + 1}{ext}"

        sheet.save(page_path, "JPEG", quality=92)
        pages.append(page_path)
        print(f"Saved: {page_path} ({len(batch_paths)} stills, {sheet_w}x{sheet_h})", file=sys.stderr)
        page_idx += 1

    return pages


# ---------------------------------------------------------------------------
# Waveform strip helpers
# ---------------------------------------------------------------------------

def _detect_silences_for_strip(input_path: str, start: float, duration: float,
                                min_silence: float, noise_db: float) -> list[dict]:
    """Run silencedetect on a time range and return [{start, end, duration}] (absolute seconds)."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-t", str(duration),
        "-i", input_path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        stderr = result.stderr
    except Exception as e:
        print(f"WARNING: silencedetect failed: {e}", file=sys.stderr)
        return []

    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", stderr)]

    silences = []
    for s, e in zip(starts, ends):
        silences.append({"start": start + s, "end": start + e, "duration": round(e - s, 3)})
    if len(starts) > len(ends):
        silences.append({"start": start + starts[-1], "end": start + duration,
                         "duration": round(duration - starts[-1], 3)})
    return silences


def _build_waveform_strip(
    audio_path: str,
    start: float,
    end: float,
    width: int,
    min_silence: float,
    noise_db: float,
    frame_timestamps: list[float],
) -> "Image.Image | None":
    """
    Generate a waveform strip for [start, end] at the given pixel width.
    Overlays silence regions (red) and per-frame tick marks (white).
    Returns a PIL Image or None on failure.
    """
    duration = end - start
    if duration <= 0:
        return None

    STRIP_H = 120   # waveform pixels
    LABEL_H = 44    # timecode row below waveform
    total_h = STRIP_H + LABEL_H
    BG = (18, 18, 24)
    WF_BG = (28, 28, 38)

    # Generate waveform via ffmpeg showwavespic
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    safe_start = max(0.0, round(start, 6))
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(safe_start), "-t", str(round(duration, 6)),
                "-i", audio_path,
                "-filter_complex",
                f"aformat=channel_layouts=mono,showwavespic=s={width}x{STRIP_H}:colors=#4fc3f7",
                "-frames:v", "1", "-update", "1", tmp_path,
            ],
            capture_output=True, check=True,
        )
        wf_img = Image.open(tmp_path).convert("RGBA")
    except Exception as e:
        print(f"WARNING: Waveform generation failed: {e}", file=sys.stderr)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Silence detection
    silences = _detect_silences_for_strip(audio_path, start, duration, min_silence, noise_db)

    # Compose strip
    strip = Image.new("RGBA", (width, total_h), BG + (255,))
    wf_bg = Image.new("RGBA", (width, STRIP_H), WF_BG + (255,))
    wf_bg.paste(wf_img, (0, 0))
    strip.paste(wf_bg, (0, 0))

    draw = ImageDraw.Draw(strip, "RGBA")

    font = None
    for font_path in ["/System/Library/Fonts/Menlo.ttc",
                       "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]:
        try:
            font = ImageFont.truetype(font_path, 13)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    def t_to_x(t: float) -> int:
        return int((t - start) / duration * width)

    # Silence regions
    for s in silences:
        x1 = max(0, t_to_x(s["start"]))
        x2 = min(width, t_to_x(s["end"]))
        if x2 > x1:
            draw.rectangle([x1, 0, x2, STRIP_H], fill=(220, 50, 50, 110))
            draw.line([(x1, 0), (x1, STRIP_H)], fill=(255, 80, 80, 180), width=1)
            draw.line([(x2, 0), (x2, STRIP_H)], fill=(255, 80, 80, 180), width=1)
            label = f"{s['duration']:.2f}s"
            if (x2 - x1) > 24:
                draw.text((x1 + 2, 2), label, fill=(255, 180, 180, 200), font=font)

    # Frame sample tick marks
    for ts in frame_timestamps:
        if start <= ts <= end:
            x = t_to_x(ts)
            draw.line([(x, STRIP_H - 12), (x, STRIP_H)], fill=(255, 255, 255, 160), width=1)

    # Timecode axis
    # Pick a human-friendly interval
    targets = [0.5, 1, 2, 5, 10, 15, 30, 60]
    tick_interval = next((t for t in targets if duration / t <= 30), 60.0)
    import math as _math
    t = _math.ceil(start / tick_interval) * tick_interval
    while t <= end:
        x = t_to_x(t)
        draw.line([(x, STRIP_H), (x, STRIP_H + 5)], fill=(130, 130, 160, 255), width=1)
        m = int(t // 60)
        s_val = t % 60
        tc = f"{m}:{s_val:05.2f}" if m > 0 else f"{s_val:.1f}s"
        draw.text((x + 2, STRIP_H + 6), tc, fill=(130, 130, 160, 255), font=font)
        t += tick_interval

    return strip.convert("RGB")


def _append_waveform_to_sheet(
    sheet: "Image.Image",
    audio_path: str,
    page_start: float,
    page_end: float,
    sheet_w: int,
    min_silence: float,
    noise_db: float,
    frame_timestamps: list[float],
) -> "Image.Image":
    """Append a waveform strip to the bottom of a preview sheet image."""
    strip = _build_waveform_strip(audio_path, page_start, page_end, sheet_w,
                                  min_silence, noise_db, frame_timestamps)
    if strip is None:
        return sheet

    combined = Image.new("RGB", (sheet_w, sheet.height + strip.height), (18, 18, 24))
    combined.paste(sheet, (0, 0))
    combined.paste(strip, (0, sheet.height))
    return combined


def main():
    parser = argparse.ArgumentParser(description="Generate tiled preview sheet from video or image directory")
    parser.add_argument("--input", "-i", help="Input video file (video mode)")
    parser.add_argument("--stills", help="Directory of still images (stills mode)")
    parser.add_argument("--output", "-o", required=True, help="Output JPEG path")
    parser.add_argument("--frames", "-n", type=int, help="Total number of frames to extract (video mode)")
    parser.add_argument("--fps", type=float, default=4, help="Frames per second to sample (default: 4). Ignored if --frames is set.")
    parser.add_argument("--cols", type=int, default=5, help="Columns in the grid (default: 5)")
    parser.add_argument("--thumb-width", type=int, default=320, help="Thumbnail width in pixels (default: 320)")
    parser.add_argument("--max-height", type=int, default=8000, help="Max sheet height before pagination (default: 8000)")
    parser.add_argument("--start", type=float, default=0, help="Start time in seconds (default: 0)")
    parser.add_argument("--end", type=float, help="End time in seconds (default: end of video)")
    parser.add_argument("--waveform", help="Audio or video file — appends a waveform strip with silence markers below the frame grid")
    parser.add_argument("--min-silence", type=float, default=0.15, help="Min silence duration to highlight on waveform strip, in seconds (default: 0.15)")
    parser.add_argument("--noise", type=float, default=-40.0, help="Silence dB threshold for waveform strip (default: -40)")
    args = parser.parse_args()

    if not args.input and not args.stills:
        parser.error("One of --input (video) or --stills (image directory) is required")
    if args.input and args.stills:
        parser.error("Use --input OR --stills, not both")

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    # --- Stills mode ---
    if args.stills:
        if not os.path.isdir(args.stills):
            print(f"ERROR: Not a directory: {args.stills}", file=sys.stderr)
            sys.exit(1)
        image_paths, labels = collect_stills(args.stills)
        print(f"Tiling {len(image_paths)} stills from {args.stills}", file=sys.stderr)
        pages = build_stills_preview_sheet(
            image_paths, labels,
            cols=args.cols,
            thumb_width=args.thumb_width,
            max_height=args.max_height,
            output_path=args.output,
        )
        print(json.dumps({"pages": pages, "frame_count": len(image_paths)}))
        return

    # --- Video mode ---
    if not os.path.exists(args.input):
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    duration = get_duration(args.input)
    start = args.start
    end = args.end if args.end else duration

    if args.frames:
        n_frames = args.frames
    else:
        n_frames = max(1, int((end - start) * args.fps))

    if n_frames == 1:
        timestamps = [(start + end) / 2]
    else:
        step = (end - start) / n_frames
        timestamps = [start + step * i + step / 2 for i in range(n_frames)]

    print(f"Extracting {n_frames} frames from {format_timecode(start)} to {format_timecode(end)} ({duration:.1f}s total)", file=sys.stderr)

    with tempfile.TemporaryDirectory() as tmpdir:
        frame_paths = extract_frames(args.input, timestamps, tmpdir)
        if not frame_paths:
            print("ERROR: No frames could be extracted.", file=sys.stderr)
            sys.exit(1)

        actual_timestamps = timestamps[:len(frame_paths)]

        pages = build_preview_sheets(
            frame_paths, actual_timestamps,
            cols=args.cols,
            thumb_width=args.thumb_width,
            max_height=args.max_height,
            output_path=args.output,
            waveform_path=args.waveform,
            min_silence=args.min_silence,
            noise_db=args.noise,
        )

    print(json.dumps({"pages": pages, "frame_count": len(frame_paths)}))


if __name__ == "__main__":
    main()
