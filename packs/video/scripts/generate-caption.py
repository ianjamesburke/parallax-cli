#!/usr/bin/env python3
"""
Generate text overlay images (PNG) for video captions and banners.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manifest_schema import load_manifest
from PIL import Image, ImageDraw, ImageFont


_SCRIPT_DIR = Path(__file__).parent
_FONTS_DIR = _SCRIPT_DIR.parent / "fonts"

FONT_PATHS = {
    "display": [
        # Bundled Bangers (chunky, high-contrast social captions)
        str(_FONTS_DIR / "Bangers-Regular.ttf"),
    ],
    "rounded": [
        # Bundled Nunito SemiBold (closest free match to Proxima Nova SemiBold)
        str(_FONTS_DIR / "Nunito-SemiBold.ttf"),
    ],
    "sans": [
        # macOS
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        # Linux
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "serif": [
        # macOS
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        # Linux
        "/usr/share/fonts/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    ],
    "mono": [
        # macOS
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        # Linux
        "/usr/share/fonts/liberation/LiberationMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    ],
}

# Safe zone margins as fractions of canvas dimensions.
# Calibrated to 1080x1920 (TikTok/Reels/Shorts) platform chrome avoidance.
# Applied proportionally so they work at any resolution.
SAFE_TOP_FRAC = 0.167     # ~320px on 1920h
SAFE_BOTTOM_FRAC = 0.156  # ~300px on 1920h
SAFE_SIDE_FRAC = 0.093    # ~100px on 1080w


def safe_zones(width: int, height: int) -> tuple[int, int, int]:
    """Return (safe_top, safe_bottom, safe_side) in pixels for this canvas size."""
    return (
        int(height * SAFE_TOP_FRAC),
        int(height * SAFE_BOTTOM_FRAC),
        int(width * SAFE_SIDE_FRAC),
    )


def find_font(font_family: str) -> str:
    """Find the font file path for the given family."""
    paths = FONT_PATHS.get(font_family, FONT_PATHS["sans"])
    for path in paths:
        if Path(path).exists():
            return path
    raise FileNotFoundError(f"Could not find font for family '{font_family}'. Tried: {paths}")


def parse_resolution(resolution_str: str) -> tuple[int, int]:
    """Parse resolution string like '1080x1920' into (width, height)."""
    w, h = resolution_str.split("x")
    return int(w), int(h)


def load_resolution_from_manifest(manifest_path: str) -> tuple[int, int]:
    """Load resolution from manifest config block (YAML or JSON)."""
    manifest = load_manifest(manifest_path)
    if "config" in manifest and "resolution" in manifest["config"]:
        return parse_resolution(manifest["config"]["resolution"])
    return (1080, 1920)


def get_default_fontsize(width: int, height: int, style: str) -> int:
    """Calculate appropriate default font size based on resolution and style."""
    # Use the smaller dimension as the base
    base_dim = min(width, height)
    
    if style == "banner":
        # Banner should be LARGE — about 11-12% of base dimension
        return int(base_dim * 0.11)
    elif style == "title":
        # Title: ~11% of base dimension → 120px on 1080x1920 vertical
        return int(base_dim * 0.11)
    else:  # caption
        # Caption more modest
        return int(base_dim * 0.06)


def generate_banner(
    text: str,
    width: int,
    height: int,
    fontsize: int,
    font_path: str,
    fg_color: str = "white",
    bg_color: str = "black",
) -> Image.Image:
    """Generate a banner: centered bold text on solid background."""
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, fontsize)
    
    # Get text bounding box
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    # Center the text
    x = (width - text_width) // 2
    y = (height - text_height) // 2
    
    draw.text((x, y), text, fill=fg_color, font=font)
    return img


def wrap_text(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Word-wrap text to fit within max_width pixels."""
    words = text.split()
    lines = []
    line: list[str] = []
    for word in words:
        test = " ".join(line + [word])
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and line:
            lines.append(" ".join(line))
            line = [word]
        else:
            line.append(word)
    if line:
        lines.append(" ".join(line))
    return lines


def parse_accent_words(accent_words_str: str) -> set[str]:
    """Parse accent words string into a set of uppercase words for matching."""
    return {w.strip().upper() for w in accent_words_str.split(",") if w.strip()}


def draw_text_block(
    draw: ImageDraw.Draw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    width: int,
    y_start: int,
    fg_color: str,
    stroke_width: int,
    line_spacing: int = 8,
    accent_words: set[str] | None = None,
    accent_color: str = "#7FE040",
) -> None:
    """Draw multiple lines of centered text with stroke.

    If accent_words is provided, any word (case-insensitive) in that set is
    rendered in accent_color instead of fg_color.
    """
    for line in lines:
        if accent_words:
            _draw_line_with_accents(
                draw, line, font, width, y_start,
                fg_color, stroke_width, accent_words, accent_color,
            )
        else:
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x = (width - tw) // 2
            draw.text((x, y_start), line, fill=fg_color, font=font,
                      stroke_width=stroke_width, stroke_fill="black")
        bbox = draw.textbbox((0, 0), line, font=font)
        th = bbox[3] - bbox[1]
        y_start += th + line_spacing


def _draw_line_with_accents(
    draw: ImageDraw.Draw,
    line: str,
    font: ImageFont.FreeTypeFont,
    width: int,
    y: int,
    fg_color: str,
    stroke_width: int,
    accent_words: set[str],
    accent_color: str,
) -> None:
    """Draw a single line word-by-word, applying accent color to matched words."""
    words = line.split()
    space_w = draw.textbbox((0, 0), " ", font=font)[2]

    # Measure total line width to center it
    total_w = 0
    word_widths = []
    for i, word in enumerate(words):
        bbox = draw.textbbox((0, 0), word, font=font)
        ww = bbox[2] - bbox[0]
        word_widths.append(ww)
        total_w += ww
        if i < len(words) - 1:
            total_w += space_w

    x = (width - total_w) // 2
    for word, ww in zip(words, word_widths):
        color = accent_color if word.upper() in accent_words else fg_color
        draw.text((x, y), word, fill=color, font=font,
                  stroke_width=stroke_width, stroke_fill="black")
        x += ww + space_w


def resolve_y(position: str, height: int, total_h: int, safe_top: int, safe_bottom: int) -> int:
    """Resolve a named position to a y_start pixel value."""
    _pos = {"headline": "top", "caption": "bottom", "midscreen": "center"}.get(position, position)
    if _pos == "top":
        return safe_top
    elif _pos == "bottom":
        return height - total_h - safe_bottom
    else:
        return (height - total_h) // 2


def validate_bounds(x: int, y: int, block_w: int, block_h: int, canvas_w: int, canvas_h: int) -> None:
    """Raise ValueError if the text block extends outside the canvas."""
    errors = []
    if x < 0:
        errors.append(f"x={x} is left of canvas (min 0)")
    if y < 0:
        errors.append(f"y={y} is above canvas (min 0)")
    if x + block_w > canvas_w:
        errors.append(f"x={x} + block_width={block_w} = {x + block_w} exceeds canvas width={canvas_w}")
    if y + block_h > canvas_h:
        errors.append(f"y={y} + block_height={block_h} = {y + block_h} exceeds canvas height={canvas_h}")
    if errors:
        raise ValueError("[generate-caption] Text block out of bounds:\n  " + "\n  ".join(errors))


def generate_caption(
    text: str,
    width: int,
    height: int,
    fontsize: int,
    font_path: str,
    fg_color: str = "white",
    stroke_width: int = 2,
    position: str = "bottom",
    accent_words: set | None = None,
    accent_color: str = "#7FE040",
    custom_x: int | None = None,
    custom_y: int | None = None,
) -> Image.Image:
    """Generate a caption overlay with safe-zone margins.

    position: 'top'/'headline', 'bottom'/'caption', 'center'/'midscreen'
    custom_x/custom_y: override position with explicit pixel coordinates (validated against canvas)
    """
    safe_top, safe_bottom, safe_side = safe_zones(width, height)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, fontsize)

    max_w = width - safe_side * 2
    lines = wrap_text(draw, text, font, max_w)

    line_h = fontsize + 8
    total_h = len(lines) * line_h - 8

    if custom_x is not None or custom_y is not None:
        x_pos = custom_x if custom_x is not None else safe_side
        y_start = custom_y if custom_y is not None else resolve_y(position, height, total_h, safe_top, safe_bottom)
        validate_bounds(x_pos, y_start, max_w, total_h, width, height)
    else:
        x_pos = safe_side
        y_start = resolve_y(position, height, total_h, safe_top, safe_bottom)

    draw_text_block(draw, lines, font, width, y_start, fg_color, stroke_width,
                    accent_words=accent_words, accent_color=accent_color)
    return img


def generate_title(
    text: str,
    width: int,
    height: int,
    fontsize: int,
    font_path: str,
    fg_color: str = "white",
    position: str = "top",
    stroke_width: int = 8,
    block_bg: bool = False,
    block_color: str = "white",
    block_padding: int = 18,
    corner_radius: int = 0,
    accent_words: set | None = None,
    accent_color: str = "#7FE040",
    custom_x: int | None = None,
    custom_y: int | None = None,
) -> Image.Image:
    """Generate a title overlay with safe-zone margins and black stroke outline.

    position: 'top'/'headline', 'bottom'/'caption', 'center'/'midscreen'
    block_bg: draw a filled rectangle behind each text line (e.g. white block / black text)
    corner_radius: rounds block_bg rect corners (0 = square)
    custom_x/custom_y: override position with explicit pixel coordinates (validated against canvas)
    """
    safe_top, safe_bottom, safe_side = safe_zones(width, height)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, fontsize)

    max_w = width - safe_side * 2
    lines = wrap_text(draw, text, font, max_w)

    line_h = fontsize + 8
    total_h = len(lines) * line_h - 8

    if custom_x is not None or custom_y is not None:
        y_start = custom_y if custom_y is not None else resolve_y(position, height, total_h, safe_top, safe_bottom)
        x_offset = custom_x if custom_x is not None else safe_side
        validate_bounds(x_offset, y_start, max_w, total_h, width, height)
    else:
        y_start = resolve_y(position, height, total_h, safe_top, safe_bottom)
        x_offset = safe_side

    if block_bg:
        # Draw a filled box behind each line, then text on top (no stroke)
        from PIL import ImageColor
        try:
            rgba = ImageColor.getrgb(block_color)
            fill = rgba if len(rgba) == 4 else (*rgba, 255)
        except Exception:
            fill = (255, 255, 255, 255)
        y = y_start
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x = (width - tw) // 2
            pad = block_padding
            rect = [x - pad, y - pad, x + tw + pad, y + th + pad]
            if corner_radius > 0:
                draw.rounded_rectangle(rect, radius=corner_radius, fill=fill)
            else:
                draw.rectangle(rect, fill=fill)
            draw.text((x, y), line, fill=fg_color, font=font)
            y += th + 8
    else:
        draw_text_block(draw, lines, font, width, y_start, fg_color, stroke_width,
                        accent_words=accent_words, accent_color=accent_color)
    return img


def preview_composite(caption_img: Image.Image, video_path: str, output_path: str, timestamp: float = 5.0) -> None:
    """Extract a single frame from video and composite the caption onto it for instant position review."""
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        frame_path = tf.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(timestamp), "-i", video_path,
             "-vframes", "1", "-q:v", "2", frame_path],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"[generate-caption] Frame extract failed (input={video_path}, t={timestamp}): {e.stderr.decode()}") from e

    frame = Image.open(frame_path).convert("RGBA")
    overlay = caption_img.convert("RGBA").resize(frame.size, Image.LANCZOS)
    composited = Image.alpha_composite(frame, overlay).convert("RGB")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    composited.save(output_path, quality=92)
    print(f"[generate-caption] Preview composite: {output_path} (t={timestamp}s)")


def main():
    parser = argparse.ArgumentParser(description="Generate text overlay images for videos")
    parser.add_argument("--text", required=True, help="Text to render")
    parser.add_argument("--output", required=True, help="Output PNG path (or JPEG for --preview)")
    parser.add_argument("--style", default="banner", choices=["banner", "caption", "title"], help="Text style")
    parser.add_argument("--resolution", default="1080x1920", help="Resolution (e.g., 1080x1920)")
    parser.add_argument("--manifest", help="Optional manifest.json to read resolution from")
    parser.add_argument("--fontsize", type=int, help="Font size (auto-calculated if omitted)")
    parser.add_argument("--font", default="sans", choices=["sans", "serif", "mono", "display", "rounded"],
                        help="Font family. 'display'=Bangers, 'rounded'=Nunito SemiBold (≈Proxima Nova)")
    parser.add_argument("--uppercase", action="store_true", default=False, help="Convert text to uppercase before rendering")
    parser.add_argument("--fg-color", dest="fg_color", default="white", help="Foreground color")
    parser.add_argument("--bg-color", dest="bg_color", default="black", help="Background color (banner only)")
    parser.add_argument("--stroke-width", dest="stroke_width", type=int, default=5, help="Stroke width (caption/title)")
    parser.add_argument("--accent-words", dest="accent_words", default=None,
                        help="Comma-separated words to render in accent color (e.g. 'FREE,NOW')")
    parser.add_argument("--accent-color", dest="accent_color", default="#7FE040",
                        help="Color for accented words (default: bright green #7FE040)")
    parser.add_argument("--block-bg", dest="block_bg", action="store_true", default=False,
                        help="Draw a filled rectangle behind each text line (title style only)")
    parser.add_argument("--block-color", dest="block_color", default="white",
                        help="Fill color for --block-bg (default: white)")
    parser.add_argument("--position", default=None,
                        choices=["top", "bottom", "center", "headline", "midscreen", "caption"],
                        help="Text position. 'headline'=top, 'caption'=bottom, 'midscreen'=center")
    parser.add_argument("--corner-radius", dest="corner_radius", type=int, default=0,
                        help="Corner radius for --block-bg rectangles in pixels (0=square, 20=pill)")
    parser.add_argument("--x", dest="custom_x", type=int, default=None,
                        help="Custom X pixel coordinate for text block left edge (overrides safe-zone side margin)")
    parser.add_argument("--y", dest="custom_y", type=int, default=None,
                        help="Custom Y pixel coordinate for text block top edge (overrides --position). Error if out of bounds.")
    parser.add_argument("--preview", metavar="VIDEO", default=None,
                        help="Composite caption onto a single frame of VIDEO and save as JPEG — instant position check, no encode")
    parser.add_argument("--preview-time", dest="preview_time", type=float, default=5.0,
                        help="Timestamp (seconds) to extract for --preview (default: 5.0)")

    args = parser.parse_args()

    # Determine resolution
    if args.manifest:
        width, height = load_resolution_from_manifest(args.manifest)
    else:
        width, height = parse_resolution(args.resolution)

    # Determine font size
    fontsize = args.fontsize or get_default_fontsize(width, height, args.style)

    # Find font
    font_path = find_font(args.font)

    # Apply uppercase if requested
    text = args.text.upper() if args.uppercase else args.text

    # Parse accent words
    accent_words = parse_accent_words(args.accent_words) if args.accent_words else None

    # Resolve position defaults per style
    position = args.position
    if position is None:
        position = "top" if args.style in ("title", "caption") else "top"

    # Generate the image
    if args.style == "banner":
        img = generate_banner(text, width, height, fontsize, font_path, args.fg_color, args.bg_color)
    elif args.style == "caption":
        img = generate_caption(text, width, height, fontsize, font_path, args.fg_color, args.stroke_width, position,
                               accent_words=accent_words, accent_color=args.accent_color,
                               custom_x=args.custom_x, custom_y=args.custom_y)
    else:  # title
        img = generate_title(text, width, height, fontsize, font_path, args.fg_color, position, args.stroke_width,
                             args.block_bg, args.block_color, args.corner_radius,
                             accent_words=accent_words, accent_color=args.accent_color,
                             custom_x=args.custom_x, custom_y=args.custom_y)

    # Save PNG
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)
    print(f"Generated {args.style} overlay: {output_path} ({width}x{height}, {fontsize}px)")

    # Optionally composite onto a video frame for instant preview
    if args.preview:
        preview_out = output_path.with_suffix(".preview.jpg")
        preview_composite(img, args.preview, str(preview_out), args.preview_time)
        print(f"[generate-caption] Open preview: open {preview_out}")


if __name__ == "__main__":
    main()
