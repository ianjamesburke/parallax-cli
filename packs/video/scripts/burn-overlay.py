#!/usr/bin/env python3
"""
Burn a single persistent text overlay onto a video via ffmpeg drawtext.

Unlike burn-captions.py (word-level from a VO manifest) or generate-caption.py
(PNG headline over the opening seconds), this tool draws one arbitrary text
string across the whole video (or a time-bounded window). Intended for
lower-thirds, brand tags, and brief-requested persistent labels.

Usage:
  burn-overlay.py --input in.mp4 --output out.mp4 --text "vibe coding a vibe editor"
  burn-overlay.py --input in.mp4 --output out.mp4 --text "hello" \\
    --position lower-third --fontcolor black --stroke-color white --stroke-width 3
  burn-overlay.py --input in.mp4 --output out.mp4 --text "intro" --start 0 --end 3
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


# Named vertical positions → drawtext y= expressions.
POSITION_Y = {
    "lower-third": "h-120",
    "upper-third": "120",
    "top": "60",
    "bottom": "h-60",
    "center": "(h-text_h)/2",
}


def _find_ffmpeg() -> str:
    """Prefer ffmpeg-full (homebrew tap with drawtext/libfreetype) over stock PATH ffmpeg."""
    full = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
    if full.exists():
        return str(full)
    found = shutil.which("ffmpeg")
    if found:
        print(
            "[burn-overlay] WARNING: /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg not found; "
            "falling back to PATH ffmpeg — drawtext may not work if libfreetype is missing.",
            file=sys.stderr,
        )
        return found
    raise FileNotFoundError(
        "No ffmpeg binary found on PATH or at /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
    )


FFMPEG = _find_ffmpeg()


# Font name → file path. drawtext needs a file; --font maps to one of these.
FONT_NAME_PATHS = {
    "Helvetica": "/System/Library/Fonts/Helvetica.ttc",
    "Arial": "/System/Library/Fonts/Supplemental/Arial.ttf",
    "LiberationSans-Bold": "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
}

FONT_FALLBACKS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def resolve_font(font_name: str, font_file: str | None) -> str:
    if font_file:
        if not Path(font_file).exists():
            raise FileNotFoundError(f"--font-file does not exist: {font_file}")
        return font_file
    mapped = FONT_NAME_PATHS.get(font_name)
    if mapped and Path(mapped).exists():
        return mapped
    for candidate in FONT_FALLBACKS:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError(
        f"No usable font found for '{font_name}'. Tried mapped path and {FONT_FALLBACKS}"
    )


def _escape_drawtext(s: str) -> str:
    """Escape text for ffmpeg drawtext text='...' option.

    Inside single-quoted text='...', ffmpeg's filter parser treats these as
    specials:
        \\ : ; ' [ ] , =
    We escape every one of them. Apostrophes need the single-quote-escape
    dance '\\'' to break out of the surrounding quotes, insert a literal
    quote, and reopen.
    """
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace(";", "\\;")
    s = s.replace("[", "\\[")
    s = s.replace("]", "\\]")
    s = s.replace(",", "\\,")
    s = s.replace("=", "\\=")
    s = s.replace("'", "'\\''")
    return s


def probe_duration(video_path: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except Exception as e:
        raise RuntimeError(f"[burn-overlay] ffprobe failed on {video_path}: {e}") from e


def build_drawtext(
    text: str,
    font_path: str,
    position: str,
    fontcolor: str,
    stroke_color: str,
    stroke_width: int,
    fontsize: int,
    start: float,
    end: float | None,
) -> str:
    escaped = _escape_drawtext(text)
    y_expr = POSITION_Y.get(position)
    if y_expr is None:
        raise ValueError(
            f"Unknown --position '{position}'. Valid: {sorted(POSITION_Y.keys())}"
        )

    parts = [
        f"drawtext=fontfile={font_path}",
        f"text='{escaped}'",
        f"fontsize={fontsize}",
        f"fontcolor={fontcolor}",
        f"borderw={stroke_width}",
        f"bordercolor={stroke_color}",
        "x=(w-text_w)/2",
        f"y={y_expr}",
    ]

    # Only add an enable clause when the window is bounded — otherwise always-on.
    if start > 0 or end is not None:
        if end is None:
            # start-only: treat as from start until end-of-video
            parts.append(f"enable='gte(t,{start})'")
        else:
            parts.append(f"enable='between(t,{start},{end})'")

    return ":".join(parts)


def burn_overlay(
    input_path: str,
    output_path: str,
    text: str,
    position: str = "lower-third",
    fontcolor: str = "black",
    stroke_color: str = "white",
    stroke_width: int = 3,
    fontsize: int = 42,
    font: str = "Helvetica",
    start: float = 0.0,
    end: float | None = None,
    font_file: str | None = None,
) -> str:
    if not Path(input_path).exists():
        raise FileNotFoundError(f"--input does not exist: {input_path}")

    font_path = resolve_font(font, font_file)
    drawtext = build_drawtext(
        text=text,
        font_path=font_path,
        position=position,
        fontcolor=fontcolor,
        stroke_color=stroke_color,
        stroke_width=stroke_width,
        fontsize=fontsize,
        start=start,
        end=end,
    )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", drawtext,
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"[burn-overlay] ffmpeg failed burning overlay onto {input_path}: "
            f"{(e.stderr or '')[:500]}"
        ) from e

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Burn a persistent text overlay onto a video via ffmpeg drawtext."
    )
    parser.add_argument("--input", required=True, help="Input video path")
    parser.add_argument("--output", required=True, help="Output video path")
    parser.add_argument("--text", required=True, help="Text to burn")
    parser.add_argument(
        "--position", default="lower-third",
        choices=sorted(POSITION_Y.keys()),
        help="Named vertical position (default: lower-third)",
    )
    parser.add_argument("--fontcolor", default="black", help="Text fill color (default: black)")
    parser.add_argument("--stroke-color", dest="stroke_color", default="white",
                        help="Text stroke/border color (default: white)")
    parser.add_argument("--stroke-width", dest="stroke_width", type=int, default=3,
                        help="Stroke/border width in pixels (default: 3)")
    parser.add_argument("--fontsize", type=int, default=42, help="Font size in px (default: 42)")
    parser.add_argument("--font", default="Helvetica", help="Font name (default: Helvetica)")
    parser.add_argument("--start", type=float, default=0.0,
                        help="Overlay start time in seconds (default: 0)")
    parser.add_argument("--end", type=float, default=None,
                        help="Overlay end time in seconds (default: full duration / always on)")
    parser.add_argument("--font-file", dest="font_file", default=None,
                        help="Explicit TTF/TTC path (overrides --font name lookup)")
    args = parser.parse_args()

    try:
        out = burn_overlay(
            input_path=args.input,
            output_path=args.output,
            text=args.text,
            position=args.position,
            fontcolor=args.fontcolor,
            stroke_color=args.stroke_color,
            stroke_width=args.stroke_width,
            fontsize=args.fontsize,
            font=args.font,
            start=args.start,
            end=args.end,
            font_file=args.font_file,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(out)


if __name__ == "__main__":
    main()
