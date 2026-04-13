#!/usr/bin/env python3
"""
Apply ASCII art visual effect to video.

Each frame is downsampled to an ASCII grid, each cell is mapped to a character
based on brightness, then the characters are rendered back at full resolution
using a monospace font — producing a genuine ASCII art video.

Modes:
  color    — characters use the original pixel color of each cell. Vivid.
  green    — classic terminal: green text on black background.
  white    — white text on black background. Clean/minimal.
  amber    — amber/orange text on black. Vintage CRT terminal.

Character sets:
  standard — 69-character ramp from space to @. Good balance of density.
  dense    — more characters, finer tonal gradation.
  blocks   — block elements only (█▓▒░ + space). Chunky/retro.

Presets:
  fine     — small cells (6×12px), fine grid. Dense ASCII detail.
  medium   — 8×16px cells. Readable and detailed. Good default.
  coarse   — 12×24px cells. Bold, readable characters at a distance.
  chunky   — 16×32px cells + block charset. Heavy retro terminal look.

Usage:
  apply-ascii.py --input video.mp4 --output ascii.mp4 --preset medium
  apply-ascii.py --input video.mp4 --output ascii.mp4 --preset coarse --color-mode green
  apply-ascii.py --input video.mp4 --output ascii.mp4 --cell-w 8 --cell-h 16 --color-mode color
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Character ramps — ordered dark (sparse) to bright (dense)
# ---------------------------------------------------------------------------

CHARSETS = {
    "standard": list(" .'`^\",:;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"),
    "dense":    list(" .-':_,^=;><+!rc*/z?sLTv)J7(|Fi{C}fI31tlu[neoZ5Yxjya]2ESwqkP6h9d4VpOGbUAKXHm8RD#$Bg0MNWQ%&@"),
    "blocks":   [' ', '░', '▒', '▓', '█'],
}

PRESETS = {
    "fine":   {"cell_w": 6,  "cell_h": 12, "charset": "standard", "mode": "color"},
    "medium": {"cell_w": 8,  "cell_h": 16, "charset": "standard", "mode": "color"},
    "coarse": {"cell_w": 12, "cell_h": 24, "charset": "standard", "mode": "color"},
    "chunky": {"cell_w": 16, "cell_h": 32, "charset": "blocks",   "mode": "color"},
}

MODE_COLORS = {
    "green": (0, 255, 70),
    "white": (255, 255, 255),
    "amber": (255, 176, 0),
}

BG_COLOR = (0, 0, 0)


def find_font(cell_w: int, cell_h: int) -> ImageFont.FreeTypeFont:
    """Find a monospace font and size it to fit cell_w × cell_h."""
    candidates = [
        "/System/Library/Fonts/Courier.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            # Binary search for the largest font size that fits within cell_w × cell_h
            lo, hi = 4, cell_h * 2
            font = None
            while lo <= hi:
                mid = (lo + hi) // 2
                try:
                    f = ImageFont.truetype(path, mid)
                    bbox = f.getbbox("M")
                    w = bbox[2] - bbox[0]
                    h = bbox[3] - bbox[1]
                    if w <= cell_w and h <= cell_h:
                        font = f
                        lo = mid + 1
                    else:
                        hi = mid - 1
                except Exception:
                    hi = mid - 1
            if font:
                return font
    # Fallback: PIL default bitmap font (tiny but always available)
    return ImageFont.load_default()


def frame_to_ascii(frame_rgb: np.ndarray, cell_w: int, cell_h: int,
                   charset: list, mode: str, font: ImageFont.FreeTypeFont) -> Image.Image:
    """Convert a single RGB frame (H×W×3 numpy array) to ASCII art image."""
    H, W, _ = frame_rgb.shape
    cols = W // cell_w
    rows = H // cell_h
    n_chars = len(charset)

    # Resize frame to grid resolution for fast cell sampling
    small = Image.fromarray(frame_rgb).resize((cols, rows), Image.LANCZOS)
    small_np = np.array(small)  # rows × cols × 3

    # Luma (brightness) for character selection
    luma = (
        0.2126 * small_np[:, :, 0].astype(float) +
        0.7152 * small_np[:, :, 1].astype(float) +
        0.0722 * small_np[:, :, 2].astype(float)
    )
    char_indices = (luma / 255.0 * (n_chars - 1)).astype(int).clip(0, n_chars - 1)

    # Create output canvas
    out_w = cols * cell_w
    out_h = rows * cell_h
    canvas = Image.new("RGB", (out_w, out_h), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # Determine text offset to center character in cell
    sample_bbox = font.getbbox("M")
    char_w = sample_bbox[2] - sample_bbox[0]
    char_h = sample_bbox[3] - sample_bbox[1]
    off_x = max(0, (cell_w - char_w) // 2)
    off_y = max(0, (cell_h - char_h) // 2)

    static_color = MODE_COLORS.get(mode)  # None if mode == "color"

    for row in range(rows):
        y = row * cell_h + off_y
        for col in range(cols):
            ch = charset[char_indices[row, col]]
            if ch == ' ':
                continue  # background already black — skip
            if static_color:
                color = static_color
            else:
                # color mode: use original pixel color
                r, g, b = int(small_np[row, col, 0]), int(small_np[row, col, 1]), int(small_np[row, col, 2])
                # Boost dim colors so characters are always legible
                lum = luma[row, col]
                if lum < 30:
                    continue  # too dark — skip (background)
                boost = max(1.0, 180.0 / max(lum, 1))
                color = (min(255, int(r * boost)), min(255, int(g * boost)), min(255, int(b * boost)))
            draw.text((col * cell_w + off_x, y), ch, fill=color, font=font)

    # Scale back to original resolution if needed (output matches input size)
    if out_w != W or out_h != H:
        canvas = canvas.resize((W, H), Image.LANCZOS)

    return canvas


def apply_ascii(input_path: str, output_path: str,
                cell_w: int, cell_h: int, charset: str, mode: str):
    input_path = os.path.abspath(input_path)
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input not found: {input_path}")

    chars = CHARSETS[charset]
    font = find_font(cell_w, cell_h)

    # Get video info
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-of", "default=noprint_wrappers=1", input_path],
        capture_output=True, text=True
    )
    info = dict(line.split("=") for line in probe.stdout.strip().splitlines() if "=" in line)
    W = int(info.get("width", 1080))
    H = int(info.get("height", 1920))
    fps_raw = info.get("r_frame_rate", "24/1")
    num, den = fps_raw.split("/")
    fps = float(num) / float(den)

    print(f"[apply-ascii] Input: {Path(input_path).name}  {W}×{H} @ {fps:.1f}fps")
    print(f"[apply-ascii] Grid: {W // cell_w}×{H // cell_h} cells ({cell_w}×{cell_h}px), charset={charset}, mode={mode}")

    with tempfile.TemporaryDirectory() as tmpdir:
        frames_dir = Path(tmpdir) / "frames"
        frames_dir.mkdir()
        ascii_dir = Path(tmpdir) / "ascii"
        ascii_dir.mkdir()

        # Extract frames as PNGs
        print(f"[apply-ascii] Extracting frames...")
        try:
            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", input_path,
                "-vf", f"scale={W}:{H}",
                str(frames_dir / "frame_%06d.png"),
            ], check=True)
        except subprocess.CalledProcessError as e:
            print(f"[apply-ascii] Frame extraction failed: {e}", file=sys.stderr)
            raise

        frame_files = sorted(frames_dir.glob("frame_*.png"))
        total = len(frame_files)
        print(f"[apply-ascii] Processing {total} frames...")

        for i, fp in enumerate(frame_files):
            if i % 24 == 0:
                print(f"[apply-ascii]   frame {i}/{total}")
            try:
                img = Image.open(fp).convert("RGB")
                frame_np = np.array(img)
                ascii_img = frame_to_ascii(frame_np, cell_w, cell_h, chars, mode, font)
                ascii_img.save(ascii_dir / fp.name, "PNG")
            except Exception as e:
                print(f"[apply-ascii] Frame {fp.name} failed: {e}", file=sys.stderr)
                raise

        # Re-encode from ASCII frames
        print(f"[apply-ascii] Encoding output...")
        try:
            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-framerate", str(fps),
                "-i", str(ascii_dir / "frame_%06d.png"),
                "-i", input_path,
                "-map", "0:v", "-map", "1:a?",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                output_path,
            ], check=True)
        except subprocess.CalledProcessError as e:
            print(f"[apply-ascii] Encode failed: {e}", file=sys.stderr)
            raise

    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-ascii] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Apply ASCII art effect to video")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="ASCII preset")
    parser.add_argument("--cell-w", type=int, dest="cell_w",
                        help="Cell width in pixels (default 8)")
    parser.add_argument("--cell-h", type=int, dest="cell_h",
                        help="Cell height in pixels (default 16)")
    parser.add_argument("--charset", choices=list(CHARSETS.keys()),
                        help="Character set (standard, dense, blocks)")
    parser.add_argument("--color-mode", dest="color_mode",
                        choices=["color", "green", "white", "amber"],
                        help="Color mode: color (original pixel colors), green/white/amber "
                             "(monochrome terminal look). Not a preset — presets control grid "
                             "size, this controls text color. Default: color")
    # Deprecated alias — kept so old invocations don't break
    parser.add_argument("--mode", dest="color_mode",
                        choices=["color", "green", "white", "amber"],
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"[apply-ascii] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    params = dict(PRESETS.get(args.preset or "medium", PRESETS["medium"]))
    if args.cell_w is not None:
        params["cell_w"] = args.cell_w
    if args.cell_h is not None:
        params["cell_h"] = args.cell_h
    if args.charset is not None:
        params["charset"] = args.charset
    if args.color_mode is not None:
        params["mode"] = args.color_mode

    try:
        apply_ascii(input_path, args.output, **params)
    except Exception as e:
        print(f"[apply-ascii] Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
