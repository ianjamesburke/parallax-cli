"""
PIL-based transparent-PNG text overlay renderer for parallax video pipeline.

Replaces ffmpeg drawtext with Python-rendered RGBA PNGs overlaid via
`-filter_complex "[0:v][1:v]overlay=0:0"`. No font path escaping, no
libfreetype dependency, any TTF/OTF works.

Public API:
    render_caption(text, style, video_size) -> Path
    render_headline(text, style, video_size) -> Path
    list_styles() -> list[str]
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

# Repo root is two levels up from this file (packs/video/text_render.py)
_REPO_ROOT = Path(__file__).parent.parent.parent
_FONTS_DIR = _REPO_ROOT / "assets" / "fonts"

# Safe zone constants — fraction of video height from bottom for caption baseline.
# Calibrated so text clears TikTok/Reels platform chrome.
_SAFE_BOTTOM_FRAC = 0.333   # ~640px on 1920h

# Padding used in block_background style (px at 1080px wide; scales with width)
_BLOCK_PAD_BASE = 12
_BLOCK_PAD_REF_W = 1080


def _font_path(filename: str) -> Path:
    p = _FONTS_DIR / filename
    if not p.exists():
        raise FileNotFoundError(
            f"[text_render] Bundled font not found: {p}. "
            "Run the font-bundle setup step or check assets/fonts/."
        )
    return p


def _load_font(filename: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(_font_path(filename)), size)
    except Exception as e:
        raise RuntimeError(
            f"[text_render] Failed to load font {filename} at size {size}: {e}"
        ) from e


def _pt_to_px(pt: int, video_width: int, ref_width: int = 1080) -> int:
    """Scale a point size proportionally to the actual video width."""
    return max(20, int(pt * video_width / ref_width))


@dataclass
class _StyleSpec:
    """Internal style definition. render() produces an RGBA PIL.Image."""

    name: str
    render_fn: Callable[[str, tuple[int, int]], Image.Image]

    def render(self, text: str, video_size: tuple[int, int]) -> Image.Image:
        try:
            return self.render_fn(text, video_size)
        except Exception as e:
            raise RuntimeError(
                f"[text_render] Style '{self.name}' render failed for text={text!r}: {e}"
            ) from e


# ---------------------------------------------------------------------------
# Style implementations
# ---------------------------------------------------------------------------

def _render_outline_black_on_white(text: str, video_size: tuple[int, int]) -> Image.Image:
    """Black text, white stroke, Inter SemiBold, bottom-center, 72pt."""
    w, h = video_size
    pt = _pt_to_px(72, w)
    stroke_w = max(3, int(4 * w / 1080))

    font = _load_font("Inter-SemiBold.ttf", pt)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_w)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (w - text_w) // 2 - bbox[0]
    y_bottom = h - int(h * _SAFE_BOTTOM_FRAC)
    y = y_bottom - text_h - bbox[1]

    draw.text(
        (x, y), text, font=font,
        fill=(0, 0, 0, 255),
        stroke_width=stroke_w,
        stroke_fill=(255, 255, 255, 255),
    )
    return img


def _render_outline_white_on_black(text: str, video_size: tuple[int, int]) -> Image.Image:
    """White text, black stroke, Inter SemiBold, bottom-center, 72pt."""
    w, h = video_size
    pt = _pt_to_px(72, w)
    stroke_w = max(3, int(4 * w / 1080))

    font = _load_font("Inter-SemiBold.ttf", pt)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_w)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (w - text_w) // 2 - bbox[0]
    y_bottom = h - int(h * _SAFE_BOTTOM_FRAC)
    y = y_bottom - text_h - bbox[1]

    draw.text(
        (x, y), text, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=stroke_w,
        stroke_fill=(0, 0, 0, 255),
    )
    return img


def _render_block_background(text: str, video_size: tuple[int, int]) -> Image.Image:
    """White text on solid black per-word rectangles, Anton Regular, center, 84pt.

    Words are split and rendered as individual blocks arranged horizontally.
    Each block has a solid black filled rectangle as background with padding.
    Blocks are centered horizontally and positioned in the upper-center area
    (suitable for headline-style overlays).
    """
    w, h = video_size
    pt = _pt_to_px(84, w)
    pad = max(8, int(_BLOCK_PAD_BASE * w / _BLOCK_PAD_REF_W))
    gap = max(4, int(8 * w / _BLOCK_PAD_REF_W))
    bg_color = (0, 0, 0, 230)   # near-opaque black

    font = _load_font("Anton-Regular.ttf", pt)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    words = text.split()
    if not words:
        return img

    # Measure each word block (text_w, text_h) for layout
    blocks: list[tuple[str, int, int]] = []
    for word in words:
        bbox = draw.textbbox((0, 0), word, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        blocks.append((word, tw, th))

    # Total row width
    block_h = max(th for _, _, th in blocks) + pad * 2
    total_w = sum(tw + pad * 2 for _, tw, _ in blocks) + gap * (len(blocks) - 1)

    # Center horizontally; position at top-center safe zone (~20% from top)
    start_x = (w - total_w) // 2
    y_top = int(h * 0.20)

    x = start_x
    for word, tw, th in blocks:
        rect_x0 = x
        rect_y0 = y_top
        rect_x1 = x + tw + pad * 2
        rect_y1 = y_top + block_h

        draw.rectangle([rect_x0, rect_y0, rect_x1, rect_y1], fill=bg_color)

        # Text inside the block — baseline-align vertically
        bbox = draw.textbbox((0, 0), word, font=font)
        text_offset_y = (block_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text(
            (rect_x0 + pad - bbox[0], rect_y0 + text_offset_y),
            word, font=font,
            fill=(255, 255, 255, 255),
        )
        x += tw + pad * 2 + gap

    return img


# ---------------------------------------------------------------------------
# Style registry
# ---------------------------------------------------------------------------

_STYLES: dict[str, _StyleSpec] = {
    "outline_black_on_white": _StyleSpec(
        name="outline_black_on_white",
        render_fn=_render_outline_black_on_white,
    ),
    "outline_white_on_black": _StyleSpec(
        name="outline_white_on_black",
        render_fn=_render_outline_white_on_black,
    ),
    "block_background": _StyleSpec(
        name="block_background",
        render_fn=_render_block_background,
    ),
}

_DEFAULT_CAPTION_STYLE = "outline_white_on_black"
_DEFAULT_HEADLINE_STYLE = "block_background"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_styles() -> list[str]:
    """Return all registered style names."""
    return list(_STYLES.keys())


def _save_to_tempfile(img: Image.Image, suffix: str = "_overlay.png") -> Path:
    """Save RGBA PIL image to a temp PNG and return its Path."""
    try:
        fd, path_str = tempfile.mkstemp(suffix=suffix)
        p = Path(path_str)
        import os
        os.close(fd)
        img.save(p, format="PNG")
        return p
    except Exception as e:
        raise RuntimeError(f"[text_render] Failed to write temp PNG: {e}") from e


def render_caption(
    text: str,
    style: str,
    video_size: tuple[int, int],
) -> Path:
    """Render a caption as a transparent PNG overlay sized to video_size.

    Args:
        text: Caption text (single word or short phrase).
        style: One of list_styles(). Defaults to outline_white_on_black if unknown.
        video_size: (width, height) in pixels.

    Returns:
        Path to a temporary PNG file (caller is responsible for cleanup).
    """
    if style not in _STYLES:
        raise ValueError(
            f"[text_render] Unknown caption style '{style}'. "
            f"Available: {list_styles()}"
        )
    spec = _STYLES[style]
    img = spec.render(text, video_size)
    return _save_to_tempfile(img, suffix=f"_caption_{style}.png")


def render_headline(
    text: str,
    style: str,
    video_size: tuple[int, int],
) -> Path:
    """Render a headline as a transparent PNG overlay sized to video_size.

    Args:
        text: Headline text.
        style: One of list_styles(). Defaults to block_background if unknown.
        video_size: (width, height) in pixels.

    Returns:
        Path to a temporary PNG file (caller is responsible for cleanup).
    """
    if style not in _STYLES:
        raise ValueError(
            f"[text_render] Unknown headline style '{style}'. "
            f"Available: {list_styles()}"
        )
    spec = _STYLES[style]
    img = spec.render(text, video_size)
    return _save_to_tempfile(img, suffix=f"_headline_{style}.png")
