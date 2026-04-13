#!/usr/bin/env python3
"""
storyboard.py — contact sheet generator for parallax image directories.

Usage:
    python3 tools/storyboard.py [DIRECTORY] [--max N] [--out PATH]

Takes up to N images from DIRECTORY (default: stills/, max 8), arranges
them in a grid, and overlays each filename. Outputs a single PNG so an
agent can see all images and their names in one read_image call.

Output: prints the path to the generated PNG on stdout.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("error: Pillow not installed — run: pip install Pillow", file=sys.stderr)
    sys.exit(1)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
BG_COLOR = (11, 12, 16)       # #0b0c10 — matches app theme
LABEL_COLOR = (230, 232, 238) # #e6e8ee
CELL_W = 400
COLS = 4
PADDING = 12
LABEL_FONT_SIZE = 28
LABEL_H = LABEL_FONT_SIZE + 10


def load_font(size: int = 12) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def make_storyboard(directory: Path, max_images: int = 8, out_path: Path | None = None) -> Path:
    images = sorted(
        [f for f in directory.iterdir() if f.suffix.lower() in IMAGE_EXTS and f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:max_images]

    if not images:
        raise ValueError(f"no images found in {directory}")

    cols = min(COLS, len(images))
    rows = (len(images) + cols - 1) // cols

    # Calculate cell height from first image aspect ratio (approximate)
    try:
        with Image.open(images[0]) as probe:
            aspect = probe.height / probe.width
    except Exception:
        aspect = 4 / 3
    cell_h = int(CELL_W * aspect)

    total_w = cols * CELL_W + (cols + 1) * PADDING
    total_h = rows * (cell_h + LABEL_H) + (rows + 1) * PADDING

    canvas = Image.new("RGB", (total_w, total_h), BG_COLOR)
    draw = ImageDraw.Draw(canvas)
    font = load_font(LABEL_FONT_SIZE)

    for i, img_path in enumerate(images):
        col = i % cols
        row = i // cols
        x = PADDING + col * (CELL_W + PADDING)
        y = PADDING + row * (cell_h + LABEL_H + PADDING)

        try:
            with Image.open(img_path) as im:
                im = im.convert("RGB")
                im.thumbnail((CELL_W, cell_h), Image.LANCZOS)
                # Center in cell if smaller than cell
                paste_x = x + (CELL_W - im.width) // 2
                paste_y = y + (cell_h - im.height) // 2
                canvas.paste(im, (paste_x, paste_y))
        except Exception as e:
            # Draw error placeholder
            draw.rectangle([x, y, x + CELL_W, y + cell_h], fill=(40, 20, 20))
            draw.text((x + 4, y + 4), f"error: {e}", fill=(200, 80, 80), font=font)

        # Filename label below image
        label_y = y + cell_h + 2
        name = img_path.name
        draw.text((x + 4, label_y), name, fill=LABEL_COLOR, font=font)

    if out_path is None:
        ts = int(time.time())
        out_dir = directory / "storyboards"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / f"storyboard_{ts}.png"

    canvas.save(out_path, "PNG")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a contact-sheet storyboard from a directory of images."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default="stills",
        help="Directory containing images (default: stills/)",
    )
    parser.add_argument(
        "--max", "-n",
        type=int,
        default=8,
        metavar="N",
        help="Max images to include (default: 8)",
    )
    parser.add_argument(
        "--out", "-o",
        type=str,
        default=None,
        help="Output PNG path (default: <directory>/storyboards/storyboard_<ts>.png)",
    )
    args = parser.parse_args()

    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        sys.exit(1)

    max_images = max(1, min(args.max, 8))
    out_path = Path(args.out).resolve() if args.out else None

    try:
        result = make_storyboard(directory, max_images=max_images, out_path=out_path)
        print(result)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
