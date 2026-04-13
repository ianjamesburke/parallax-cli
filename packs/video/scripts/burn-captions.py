#!/usr/bin/env python3
"""
Burn word-by-word captions from VO manifest timestamps onto an assembled video.

Reads word timestamps from vo_manifest.json, groups them into small chunks,
and burns them as drawtext overlays using ffmpeg. Safe zones match generate-caption.py.

Usage:
  burn-captions.py --manifest path/to/manifest.yaml --video path/to/draft.mp4
  burn-captions.py --manifest path/to/manifest.yaml --video input.mp4 --output captioned.mp4
  burn-captions.py --manifest path/to/manifest.yaml --video input.mp4 --words-per-chunk 3
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from manifest_schema import load_manifest

# Social safe zones — match generate-caption.py
SAFE_BOTTOM = 640   # px from bottom where caption baseline sits
SAFE_SIDE = 100     # px from each side

# Drawtext font size as fraction of video width
FONTSIZE_RATIO = 0.055  # ~59px on 1080px wide

def _find_ffmpeg() -> str:
    """Find an ffmpeg binary that supports drawtext (needs libfreetype)."""
    # Prefer ffmpeg-full (homebrew tap with all filters)
    full = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
    if full.exists():
        return str(full)
    # Fall back to PATH ffmpeg
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise FileNotFoundError("No ffmpeg binary found on PATH or at /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")


FFMPEG = _find_ffmpeg()

FONT_PATHS = [
    # Prefer fonts without spaces in path — ffmpeg drawtext treats spaces as delimiters
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def find_font() -> str:
    for path in FONT_PATHS:
        if Path(path).exists():
            return path
    raise FileNotFoundError(f"No usable font found. Tried: {FONT_PATHS}")


def _escape_text(s: str) -> str:
    """Escape text for ffmpeg drawtext text= option (no wrapping quotes).

    In filter_complex/filter_script context, these chars must be escaped:
      \\ : ; ' [ ] ,
    We also escape = for safety.
    """
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\u2019")  # Replace apostrophes with Unicode right single quote
    s = s.replace(";", "\\;")
    s = s.replace(":", "\\:")
    s = s.replace("[", "\\[")
    s = s.replace("]", "\\]")
    s = s.replace(",", "\\,")
    s = s.replace("=", "\\=")
    return s


def chunk_words(words: list[dict], words_per_chunk: int) -> list[dict]:
    """Group word timestamps into caption chunks of N words each (legacy mode)."""
    chunks = []
    i = 0
    while i < len(words):
        group = words[i : i + words_per_chunk]
        text = " ".join(w["word"] for w in group)
        start = group[0]["start"]
        end = group[-1]["end"]
        chunks.append({"text": text, "start": start, "end": end})
        i += words_per_chunk
    return chunks


def _ends_sentence(word: str) -> bool:
    """Return True if this word closes a sentence (ends with . ? !)."""
    return word.rstrip().endswith((".", "?", "!"))


def _is_short(word: str) -> bool:
    """Return True if the word is short enough to group with the next word.

    Short = 3 chars or fewer after stripping punctuation. Covers articles,
    prepositions, conjunctions, and auxiliary verbs that look awkward alone
    (a, an, the, is, in, on, of, to, be, do, it, he, we, or, so, for, and, but…).
    """
    return len(word.strip(".,!?;:\"'")) <= 3


def smart_chunk_words(words: list[dict]) -> list[dict]:
    """Group words into per-word caption chunks with sentence-aware short-word grouping.

    Rules:
    - Default: one word per caption frame.
    - Exception: a short word (≤3 chars) that does NOT end a sentence is grouped
      with the following word into a single frame.
    - Hard constraint: sentence boundaries are never crossed. A word ending in
      . ? ! always closes its own chunk; the next word starts fresh.
    """
    chunks = []
    i = 0
    while i < len(words):
        w = words[i]
        word_text = w["word"]
        ends = _ends_sentence(word_text)

        # Group short non-sentence-ending word with next word
        if _is_short(word_text) and not ends and i + 1 < len(words):
            nw = words[i + 1]
            text = word_text + " " + nw["word"]
            chunks.append({"text": text, "start": w["start"], "end": nw["end"]})
            i += 2
        else:
            chunks.append({"text": word_text, "start": w["start"], "end": w["end"]})
            i += 1
    return chunks


def build_drawtext_filter(
    chunks: list[dict],
    video_width: int,
    video_height: int,
    font_path: str,
    block_bg: bool = False,
    block_color: str = "black@0.6",
    block_padding: int = 12,
    safe_bottom: int = SAFE_BOTTOM,
) -> str:
    fontsize = max(40, int(video_width * FONTSIZE_RATIO))
    # y position: bottom safe zone, offset up by two line heights to sit above platform chrome
    y_pos = video_height - safe_bottom

    # Build a filter_complex chain with labeled pads.
    # Each drawtext gets its own segment separated by semicolons to avoid
    # comma-escaping conflicts between filter separators and expression args.
    lines = []
    for i, chunk in enumerate(chunks):
        text = _escape_text(chunk["text"])
        start = round(chunk["start"], 3)
        end = round(chunk["end"], 3)
        in_label = "[0:v]" if i == 0 else f"[v{i}]"
        out_label = f"[v{i + 1}]"
        if block_bg:
            # Block background: box around each word, no stroke border
            lines.append(
                f"{in_label}drawtext=fontfile={font_path}"
                f":text={text}"
                f":fontsize={fontsize}"
                f":fontcolor=white"
                f":box=1"
                f":boxcolor={block_color}"
                f":boxborderw={block_padding}"
                f":x=(w-text_w)/2"
                f":y={y_pos}"
                f":enable='between(t,{start},{end})'"
                f"{out_label}"
            )
        else:
            lines.append(
                f"{in_label}drawtext=fontfile={font_path}"
                f":text={text}"
                f":fontsize={fontsize}"
                f":fontcolor=white"
                f":borderw=4"
                f":bordercolor=black"
                f":x=(w-text_w)/2"
                f":y={y_pos}"
                f":enable='between(t,{start},{end})'"
                f"{out_label}"
            )

    return ";\n".join(lines)


def probe_resolution(video_path: str) -> tuple[int, int]:
    """Probe video dimensions via ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
                video_path,
            ],
            capture_output=True, text=True, check=True,
        )
        parts = result.stdout.strip().split(",")
        w, h = parts[0], parts[1]
        return int(w), int(h)
    except Exception as e:
        raise RuntimeError(f"[burn-captions] ffprobe failed on {video_path}: {e}") from e


def load_words_from_vo_manifest(vo_manifest_path: str) -> list[dict]:
    """Load word timestamps from a vo_manifest.json (ElevenLabs format)."""
    try:
        vo = json.loads(Path(vo_manifest_path).read_text())
    except Exception as e:
        raise RuntimeError(f"[burn-captions] Could not read vo_manifest {vo_manifest_path}: {e}") from e
    words = vo.get("words", [])
    if not words:
        raise RuntimeError(f"[burn-captions] vo_manifest has no word timestamps: {vo_manifest_path}")
    # Clamp each word's end to the next word's start to fix ElevenLabs overlapping timestamps
    for i in range(len(words) - 1):
        if words[i]["end"] > words[i + 1]["start"]:
            words[i]["end"] = words[i + 1]["start"]
    return words


def load_words_from_clip_index(clip_index_path: str, start_s: float = 0.0) -> list[dict]:
    """Load word timestamps from a clip-index YAML produced by index-clip.py.

    The clip-index words[] array uses the same {word, start, end} shape as vo_manifest.json.
    start_s offsets timestamps so they align with the trimmed clip's timeline (pass the
    same value you used as --start-s / -ss when cutting the clip).
    """
    try:
        data = yaml.safe_load(Path(clip_index_path).read_text())
    except Exception as e:
        raise RuntimeError(f"[burn-captions] Could not read clip-index {clip_index_path}: {e}") from e

    raw_words = data.get("words", [])
    if not raw_words:
        raise RuntimeError(f"[burn-captions] clip-index has no words[] array: {clip_index_path}")

    # Filter to words that fall within the clip window and offset timestamps
    words = []
    for w in raw_words:
        w_start = w.get("start", 0.0)
        w_end = w.get("end", w_start)
        if start_s > 0 and w_end <= start_s:
            continue  # before clip window
        words.append({
            "word": w.get("word", ""),
            "start": round(w_start - start_s, 4),
            "end": round(w_end - start_s, 4),
        })

    if not words:
        raise RuntimeError(
            f"[burn-captions] No words found after start_s={start_s}s in {clip_index_path}"
        )
    # Clamp each word's end to the next word's start (same fix as vo_manifest path)
    for i in range(len(words) - 1):
        if words[i]["end"] > words[i + 1]["start"]:
            words[i]["end"] = words[i + 1]["start"]
    return words


def burn(
    video_path: str,
    words: list[dict],
    output_path: str,
    words_per_chunk: int | None,
    block_bg: bool = False,
    block_color: str = "black@0.6",
    block_padding: int = 12,
    safe_bottom: int = SAFE_BOTTOM,
):
    if not words:
        print("ERROR: no word timestamps provided — cannot burn captions.", file=sys.stderr)
        sys.exit(1)

    if words_per_chunk is not None:
        chunks = chunk_words(words, words_per_chunk)
        print(f"  {len(words)} words → {len(chunks)} caption chunks ({words_per_chunk} words/chunk, legacy mode)")
    else:
        chunks = smart_chunk_words(words)
        print(f"  {len(words)} words → {len(chunks)} caption chunks (smart per-word mode)")

    # Clamp each chunk's end to the next chunk's start to prevent on-screen overlap
    for i in range(len(chunks) - 1):
        if chunks[i]["end"] > chunks[i + 1]["start"]:
            chunks[i]["end"] = chunks[i + 1]["start"]

    font_path = find_font()
    width, height = probe_resolution(video_path)
    vf = build_drawtext_filter(chunks, width, height, font_path, block_bg, block_color, block_padding, safe_bottom)

    # Write filter to a temp file to avoid shell/ffmpeg escaping issues with
    # long drawtext chains. -filter_script:v reads the filter graph from a file.
    import tempfile
    filter_file = Path(tempfile.mktemp(suffix=".txt"))
    # The last drawtext produces label [vN] where N = len(chunks)
    final_label = f"[v{len(chunks)}]"
    try:
        filter_file.write_text(vf)
        subprocess.run(
            [
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-i", video_path,
                "-filter_complex_script", str(filter_file),
                "-map", final_label, "-map", "0:a",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                output_path,
            ],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"[burn-captions] ffmpeg failed burning captions onto {video_path}: {e}") from e
    finally:
        filter_file.unlink(missing_ok=True)

    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"  Captions burned: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Burn word-by-word captions onto assembled video")
    parser.add_argument("--manifest", required=True, help="Path to manifest.yaml")
    parser.add_argument("--video", required=True, help="Input video (assembled draft or mixed)")
    parser.add_argument("--output", default=None, help="Output path (default: replaces _draft.mp4 with _captioned.mp4)")
    parser.add_argument("--words-per-chunk", dest="words_per_chunk", type=int, default=None,
                        help="Words per caption chunk — forces legacy fixed-count mode (default: smart per-word mode)")
    parser.add_argument("--vo-manifest", dest="vo_manifest", default=None,
                        help="Path to vo_manifest.json (default: auto-detect from manifest)")
    parser.add_argument("--clip-index", dest="clip_index", default=None,
                        help="Path to clip-index YAML from index-clip.py — alternative to vo_manifest.json for raw-footage clips")
    parser.add_argument("--start-s", dest="start_s", type=float, default=0.0,
                        help="Clip start time in seconds — subtracts this offset from clip-index word timestamps (default: 0.0)")
    parser.add_argument("--block-bg", dest="block_bg", action="store_true", default=False,
                        help="Render captions with a solid block background (YouTube/TikTok style)")
    parser.add_argument("--block-color", dest="block_color", default="black@0.6",
                        help="Block background color in ffmpeg format, e.g. black@0.6 (default), white@0.5 (default: black@0.6)")
    parser.add_argument("--block-padding", dest="block_padding", type=int, default=12,
                        help="Padding in pixels around text inside the block background (default: 12)")
    parser.add_argument("--safe-bottom", dest="safe_bottom", type=int, default=SAFE_BOTTOM,
                        help=f"Pixels from the bottom of the frame where captions sit (default: {SAFE_BOTTOM})")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = load_manifest(str(manifest_path))
    project_dir = manifest_path.parent

    # Load caption style defaults from style_preset (CLI flags override)
    style_caption: dict = {}
    style_preset = manifest.get("style_preset")
    if style_preset:
        styles_dir = Path(__file__).parent.parent / "styles"
        style_path = styles_dir / f"{style_preset}.yaml"
        if style_path.exists():
            try:
                style_data = yaml.safe_load(style_path.read_text())
                style_caption = style_data.get("caption", {})
            except Exception as e:
                print(f"  Warning: could not load style '{style_preset}': {e}", file=sys.stderr)

    # Resolve block-bg settings: style provides base, CLI overrides
    effective_block_bg = args.block_bg or bool(style_caption.get("block_bg", False))
    if args.block_bg:
        # CLI explicitly requested — use CLI values for color/padding too
        effective_block_color = args.block_color
        effective_block_padding = args.block_padding
    else:
        # Use style values if present, fall back to CLI defaults
        effective_block_color = str(style_caption.get("block_color", args.block_color))
        effective_block_padding = int(style_caption.get("block_padding", args.block_padding))

    # Resolve word timestamps — clip-index YAML takes priority over vo_manifest
    if args.clip_index:
        try:
            words = load_words_from_clip_index(args.clip_index, args.start_s)
            print(f"  Word source: clip-index {Path(args.clip_index).name} (offset {args.start_s}s, {len(words)} words)")
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        if args.vo_manifest:
            vo_manifest_path = args.vo_manifest
        else:
            candidate = manifest.get("voiceover", {}).get("vo_manifest")
            if candidate:
                vo_path = project_dir / "assets" / "audio" / candidate
                if not vo_path.exists():
                    vo_path = project_dir / candidate
            elif (project_dir / "assets" / "audio" / "vo_manifest.json").exists():
                vo_path = project_dir / "assets" / "audio" / "vo_manifest.json"
            else:
                vo_path = project_dir / "vo_manifest.json"
            if not vo_path.exists():
                print(f"ERROR: vo_manifest not found at {vo_path}. Use --vo-manifest, --clip-index, or run generate-voiceover.py first.", file=sys.stderr)
                sys.exit(1)
            vo_manifest_path = str(vo_path)
        try:
            words = load_words_from_vo_manifest(vo_manifest_path)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    # Resolve output path
    video_path = Path(args.video)
    if args.output:
        output_path = args.output
    else:
        stem = video_path.stem.replace("_draft", "").replace("_mixed", "")
        output_path = str(video_path.parent / f"{stem}_captioned.mp4")

    if effective_block_bg and style_preset:
        print(f"  Caption style: block-bg from style '{style_preset}' (color={effective_block_color}, padding={effective_block_padding}px)")
    elif effective_block_bg:
        print(f"  Caption style: block-bg (color={effective_block_color}, padding={effective_block_padding}px)")

    print(f"Burning captions onto: {video_path.name}")
    burn(str(video_path), words, output_path, args.words_per_chunk,
         block_bg=effective_block_bg, block_color=effective_block_color, block_padding=effective_block_padding,
         safe_bottom=args.safe_bottom)
    print(f"\nNext: open {output_path}")


if __name__ == "__main__":
    main()
