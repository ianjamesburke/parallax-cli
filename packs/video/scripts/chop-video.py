#!/usr/bin/env python3
"""
DEPRECATED — use index-clip.py + assemble-clips.py instead.

  index-clip.py --input video.mov      # analyze, write _meta/<stem>.yaml
  assemble-clips.py --manifests _meta/<stem>.yaml --output out.mp4

This script remains for reference but is no longer maintained.

---
Standalone silence-chopper for raw video files.

1. Extracts audio from the input video
2. Transcribes with OpenAI Whisper (saves transcript in manifest)
3. Detects silence regions in the audio
4. Drops keep-segments shorter than --min-clip (default 1.0s)
5. Removes silence + short clips from both video and audio streams
6. Writes a manifest.yaml alongside the output for later editing/reassembly

Usage:
  chop-video.py --input /path/to/video.mov
  chop-video.py --input /path/to/video.mov --output /path/to/out.mp4
  chop-video.py --input /path/to/video.mov --min-silence 0.4 --min-clip 0.5 --dry-run

Flags:
  --input         Input video file (required)
  --output        Output video path (default: <input>_chopped.mp4 next to input)
  --min-silence   Minimum silence duration to trim (default: 0.5s)
  --min-clip      Drop keep-segments shorter than this (default: 1.0s)
  --pad           Padding to preserve on each side of a silence cut (default: 0.15s)
  --threshold     Silence detection threshold in dB (default: -35)
  --model         Whisper model to use (default: base.en — fast, English)
  --language      Language hint for Whisper (default: en)
  --dry-run       Show what would be trimmed without writing output
"""
import argparse
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

def _dump_yaml(data: dict) -> str:
    try:
        import yaml
        return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except ImportError:
        import json
        return "# install pyyaml for proper YAML formatting\n" + json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

def get_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        print(f"[chop-video] ffprobe failed on {path}: {e}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------

def extract_audio(video_path: str, audio_path: str) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        audio_path,
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[chop-video] Audio extraction failed: {e}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# Transcription via Whisper
# ---------------------------------------------------------------------------

def transcribe(audio_path: str, model: str, language: str, out_dir: str) -> str:
    """Run Whisper and return the plain-text transcript."""
    cmd = [
        "whisper", audio_path,
        "--model", model,
        "--language", language,
        "--output_format", "txt",
        "--output_dir", out_dir,
        "--fp16", "False",
        "--verbose", "False",
    ]
    print(f"[chop-video] Transcribing with Whisper model={model}...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[chop-video] Whisper transcription failed: {e}", file=sys.stderr)
        raise

    stem = Path(audio_path).stem
    txt_file = Path(out_dir) / f"{stem}.txt"
    if not txt_file.exists():
        print(f"[chop-video] WARNING: transcript file not found at {txt_file}", file=sys.stderr)
        return ""

    return txt_file.read_text().strip()


# ---------------------------------------------------------------------------
# Silence detection + segment building
# ---------------------------------------------------------------------------

def detect_silences(audio_path: str, threshold_db: float, min_duration: float) -> list[dict]:
    cmd = [
        "ffmpeg", "-hide_banner",
        "-i", audio_path,
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        print(f"[chop-video] silencedetect failed: {e}", file=sys.stderr)
        raise

    starts = re.findall(r"silence_start: ([\d.]+)", result.stderr)
    ends = re.findall(r"silence_end: ([\d.]+)", result.stderr)

    silences = []
    for i in range(min(len(starts), len(ends))):
        s, e = float(starts[i]), float(ends[i])
        silences.append({"start": s, "end": e, "duration": round(e - s, 6)})
    return silences


def build_segments(silences: list[dict], pad: float, duration: float) -> list[tuple[float, float]]:
    """Convert silence list into keep-segments (inverse of silence, with padding)."""
    trims = []
    for s in silences:
        cut_start = s["start"] + pad
        cut_end = s["end"] - pad
        if cut_end > cut_start:
            trims.append((round(cut_start, 6), round(cut_end, 6)))

    segments: list[tuple[float, float]] = []
    prev = 0.0
    for cut_s, cut_e in trims:
        if cut_s > prev:
            segments.append((prev, cut_s))
        prev = cut_e
    if prev < duration:
        segments.append((prev, duration))

    return segments


def filter_short_segments(
    segments: list[tuple[float, float]], min_clip: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Split segments into kept and dropped based on min_clip duration."""
    kept, dropped = [], []
    for s, e in segments:
        (kept if (e - s) >= min_clip else dropped).append((s, e))
    return kept, dropped


# ---------------------------------------------------------------------------
# Video chopping
# ---------------------------------------------------------------------------

def chop_video(
    input_path: str,
    output_path: str,
    segments: list[tuple[float, float]],
) -> None:
    """Cut video+audio to the given keep-segments using ffmpeg filter_complex."""
    if not segments:
        print("[chop-video] No segments to keep — nothing to write.", file=sys.stderr)
        raise ValueError("Empty segment list")

    v_parts = [f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}]" for i, (s, e) in enumerate(segments)]
    a_parts = [f"[0:a]atrim=start={s}:end={e},asetpts=N/SR/TB[a{i}]" for i, (s, e) in enumerate(segments)]

    n = len(segments)
    interleaved = "".join(f"[v{i}][a{i}]" for i in range(n))
    concat_filter = f"{interleaved}concat=n={n}:v=1:a=1[vout][aout]"
    filter_complex = "; ".join(v_parts + a_parts + [concat_filter])

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]
    print(f"[chop-video] Assembling {n} segment(s)...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[chop-video] ffmpeg chop failed: {e}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def write_manifest(
    manifest_path: Path,
    input_path: Path,
    output_path: Path,
    transcript: str,
    segments: list[tuple[float, float]],
    dropped: list[tuple[float, float]],
    args,
) -> None:
    clips = [
        {
            "index": i,
            "source_start": round(s, 3),
            "source_end": round(e, 3),
            "duration": round(e - s, 3),
        }
        for i, (s, e) in enumerate(segments)
    ]

    manifest = {
        "format": "footage-chop",
        "source": str(input_path),
        "output": str(output_path),
        "chopped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chop_params": {
            "min_silence": args.min_silence,
            "min_clip": args.min_clip,
            "pad": args.pad,
            "threshold": args.threshold,
            "whisper_model": args.model,
        },
        "transcript": transcript or "",
        "clips": clips,
        "dropped_clips": [
            {"source_start": round(s, 3), "source_end": round(e, 3), "duration": round(e - s, 3)}
            for s, e in dropped
        ],
    }

    manifest_path.write_text(_dump_yaml(manifest))
    print(f"[chop-video] Manifest saved: {manifest_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Transcribe + silence-chop a video file")
    parser.add_argument("--input", required=True, help="Input video file")
    parser.add_argument("--output", help="Output video path (default: <input>_chopped.mp4)")
    parser.add_argument("--min-silence", type=float, default=0.5, help="Min silence to trim (s)")
    parser.add_argument("--min-clip", type=float, default=1.0,
                        help="Drop keep-segments shorter than this (default: 1.0s)")
    parser.add_argument("--pad", type=float, default=0.15, help="Padding to keep each side of silence (s)")
    parser.add_argument("--threshold", type=float, default=-35, help="Silence threshold (dB)")
    parser.add_argument("--model", default="base.en", help="Whisper model (default: base.en)")
    parser.add_argument("--language", default="en", help="Whisper language (default: en)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no output written")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"[chop-video] Input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output).resolve() if args.output else (
        input_path.parent / f"{input_path.stem}_chopped.mp4"
    )
    manifest_path = output_path.with_suffix(".yaml")

    print(f"[chop-video] Input:    {input_path}")
    print(f"[chop-video] Output:   {output_path}")
    print(f"[chop-video] Manifest: {manifest_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = str(Path(tmpdir) / "audio.wav")

        # Step 1: Extract audio
        print("[chop-video] Extracting audio...")
        try:
            extract_audio(str(input_path), audio_path)
        except Exception:
            sys.exit(1)

        duration = get_duration(audio_path)
        print(f"[chop-video] Duration: {duration:.2f}s")

        # Step 2: Transcribe
        try:
            transcript = transcribe(audio_path, args.model, args.language, tmpdir)
        except Exception:
            print("[chop-video] Transcription failed, continuing with silence chop only...", file=sys.stderr)
            transcript = ""

        if transcript:
            print(f"\n--- Transcript ---\n{transcript}\n------------------\n")
        else:
            print("[chop-video] No transcript produced.")

        # Step 3: Detect silences
        silences = detect_silences(audio_path, args.threshold, args.min_silence)
        if not silences:
            fallback_db = args.threshold + 5
            print(f"[chop-video] No silences at {args.threshold}dB, retrying at {fallback_db}dB...")
            silences = detect_silences(audio_path, fallback_db, args.min_silence)

        if silences:
            print(f"[chop-video] Found {len(silences)} silence region(s)")
        else:
            print("[chop-video] No significant silences detected.")

        # Step 4: Build keep-segments
        all_segments = build_segments(silences, args.pad, duration)

        # Step 5: Filter short segments
        segments, dropped = filter_short_segments(all_segments, args.min_clip)

        if dropped:
            print(f"[chop-video] Dropping {len(dropped)} short segment(s) (< {args.min_clip}s):")
            for s, e in dropped:
                print(f"  {s:.3f}s – {e:.3f}s ({e-s:.3f}s)")

        total_kept = sum(e - s for s, e in segments)
        total_removed = duration - total_kept
        print(f"\n[chop-video] Keeping {len(segments)} segment(s) → {total_kept:.3f}s (removing {total_removed:.3f}s from {duration:.3f}s)")
        for i, (s, e) in enumerate(segments):
            print(f"  [{i}] {s:.3f}s – {e:.3f}s ({e-s:.3f}s)")

        if args.dry_run:
            print("\n[chop-video] --dry-run: no files written.")
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Step 6: Write manifest
        write_manifest(manifest_path, input_path, output_path, transcript, segments, dropped, args)

        # Step 7: Chop video
        try:
            chop_video(str(input_path), str(output_path), segments)
        except Exception:
            sys.exit(1)

    chopped_dur = get_duration(str(output_path))
    print(f"\n[chop-video] Done.")
    print(f"  Video:    {output_path} ({chopped_dur:.2f}s)")
    print(f"  Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
