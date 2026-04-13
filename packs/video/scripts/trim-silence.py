#!/usr/bin/env python3
"""
Trim silence gaps in voiceover audio while preserving word-level timestamps.

Strategy:
1. Detect silence regions in the audio
2. For each silence > threshold, trim it down to a small pad (e.g. 0.15s each side)
3. Rebuild the VO manifest timestamps by shifting words that come after each trimmed region
4. Output trimmed audio + updated VO manifest

Usage:
  trim-silence.py --manifest path/to/manifest.json
  trim-silence.py --manifest path/to/manifest.json --min-silence 0.4 --pad 0.15
  trim-silence.py --manifest path/to/manifest.json --dry-run

Flags:
  --min-silence   Minimum silence duration to trim (default: 0.5s)
  --pad           Padding to keep on each side of a cut (default: 0.15s)
  --threshold     Silence detection threshold in dB (default: -35)
  --dry-run       Show what would be trimmed without modifying files
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manifest_schema import load_manifest, save_manifest


def detect_silences(audio_path: str, threshold_db: float = -35, min_duration: float = 0.5) -> list[dict]:
    """Detect silence regions using ffmpeg silencedetect."""
    cmd = [
        "ffmpeg", "-hide_banner", "-i", audio_path,
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr

    silences = []
    starts = re.findall(r'silence_start: ([\d.]+)', stderr)
    ends = re.findall(r'silence_end: ([\d.]+)', stderr)

    for i in range(min(len(starts), len(ends))):
        s = float(starts[i])
        e = float(ends[i])
        silences.append({"start": s, "end": e, "duration": round(e - s, 6)})

    return silences


def build_trim_regions(silences: list[dict], pad: float = 0.15) -> list[dict]:
    """Convert silence regions into trim regions (what to cut), preserving pad on each side."""
    trims = []
    for s in silences:
        trim_start = s["start"] + pad
        trim_end = s["end"] - pad
        if trim_end > trim_start:
            trims.append({
                "start": round(trim_start, 6),
                "end": round(trim_end, 6),
                "removed": round(trim_end - trim_start, 6),
            })
    return trims


def trim_audio(audio_path: str, output_path: str, trims: list[dict], original_duration: float) -> float:
    """
    Trim silence regions from audio using ffmpeg atrim concat filter.
    Returns new duration.
    """
    if not trims:
        subprocess.run(["cp", audio_path, output_path], check=True)
        return get_duration(output_path)

    # Build segments to KEEP (inverse of trim regions)
    segments = []
    prev = 0.0
    for t in trims:
        if t["start"] > prev:
            segments.append((prev, t["start"]))
        prev = t["end"]
    segments.append((prev, original_duration))

    # Build ffmpeg filter_complex with atrim per segment + concat
    filter_parts = []
    for i, (s, e) in enumerate(segments):
        filter_parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=N/SR/TB[s{i}]")
    concat_inputs = "".join(f"[s{i}]" for i in range(len(segments)))
    filter_parts.append(f"{concat_inputs}concat=n={len(segments)}:v=0:a=1[out]")
    filtergraph = "; ".join(filter_parts)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", audio_path,
        "-filter_complex", filtergraph,
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2",
        output_path,
    ]
    subprocess.run(cmd, check=True)
    return get_duration(output_path)


def get_duration(path: str) -> float:
    """Get audio duration via ffprobe."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


def shift_timestamps(vo_manifest: dict, trims: list[dict]) -> dict:
    """
    Shift word-level timestamps in VO manifest to account for removed silence.
    Each trim removes (trim.end - trim.start) seconds.
    Words after a trim get shifted back by the cumulative removed time.
    Uses high precision to avoid rounding errors.
    """
    updated = json.loads(json.dumps(vo_manifest))  # deep copy

    words = updated.get("words", updated.get("alignment", []))
    if not words:
        return updated

    for word in words:
        word_start = word.get("start", word.get("start_time", 0))
        word_end = word.get("end", word.get("end_time", 0))

        cumulative_shift = 0.0
        for t in trims:
            if word_start > t["end"]:
                # Word is entirely after this trim
                cumulative_shift += t["removed"]
            elif word_start > t["start"] and word_start <= t["end"]:
                # Word starts inside a trim region — snap to trim start
                cumulative_shift += (word_start - t["start"])

        # Apply shift with full precision, round at the end
        if "start" in word:
            word["start"] = round(word_start - cumulative_shift, 6)
        if "start_time" in word:
            word["start_time"] = round(word_start - cumulative_shift, 6)
        if "end" in word:
            word["end"] = round(word_end - cumulative_shift, 6)
        if "end_time" in word:
            word["end_time"] = round(word_end - cumulative_shift, 6)

    return updated


def main():
    parser = argparse.ArgumentParser(description="Trim silence from voiceover")
    parser.add_argument("--manifest", required=True, help="Path to project manifest JSON")
    parser.add_argument("--min-silence", type=float, default=0.5, help="Min silence duration to trim (s)")
    parser.add_argument("--pad", type=float, default=0.15, help="Padding to preserve on each side (s)")
    parser.add_argument("--threshold", type=float, default=-35, help="Silence threshold (dB)")
    parser.add_argument("--dry-run", action="store_true", help="Show trims without modifying files")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = load_manifest(str(manifest_path))
    project_dir = manifest_path.parent

    # Find VO audio
    vo = manifest.get("voiceover", {})
    audio_file = vo.get("audio_file")
    vo_manifest_file = vo.get("vo_manifest")

    if not audio_file:
        print("No voiceover.audio_file in manifest", file=sys.stderr)
        sys.exit(1)

    audio_path = str(project_dir / audio_file)
    if not Path(audio_path).exists():
        print(f"Audio not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    original_duration = get_duration(audio_path)
    print(f"Original VO: {original_duration:.3f}s")

    # Detect silences
    silences = detect_silences(audio_path, args.threshold, args.min_silence)

    if not silences:
        # Try once more with a more forgiving threshold
        print(f"No silences at {args.threshold}dB, trying {args.threshold + 5}dB...")
        silences = detect_silences(audio_path, args.threshold + 5, args.min_silence)

    if not silences:
        print("No significant silences detected. Audio is clean.")
        sys.exit(0)

    print(f"Found {len(silences)} silence region(s):")
    for s in silences:
        print(f"  {s['start']:.3f}s - {s['end']:.3f}s ({s['duration']:.3f}s)")

    # Build trim regions (with padding preserved)
    trims = build_trim_regions(silences, args.pad)
    total_removed = sum(t["removed"] for t in trims)

    if not trims:
        print(f"All silences shorter than 2x pad ({args.pad * 2}s). Nothing to trim.")
        sys.exit(0)

    print(f"\nWill trim {len(trims)} region(s), removing {total_removed:.3f}s:")
    for t in trims:
        print(f"  Cut {t['start']:.3f}s - {t['end']:.3f}s ({t['removed']:.3f}s)")

    expected_duration = original_duration - total_removed
    print(f"\nExpected new duration: {expected_duration:.3f}s (was {original_duration:.3f}s)")

    if args.dry_run:
        print("\n--dry-run: no files modified.")
        return

    # Trim audio
    trimmed_name = audio_file.replace(".mp3", "_trimmed.mp3")
    trimmed_path = str(project_dir / trimmed_name)
    new_duration = trim_audio(audio_path, trimmed_path, trims, original_duration)
    print(f"\nTrimmed audio: {trimmed_path} ({new_duration:.3f}s)")

    # Verify duration is close to expected (within 0.1s tolerance)
    drift = abs(new_duration - expected_duration)
    if drift > 0.1:
        print(f"WARNING: Duration drift of {drift:.3f}s — check timestamps carefully", file=sys.stderr)

    # Update VO manifest timestamps
    if vo_manifest_file:
        vo_manifest_path = project_dir / vo_manifest_file
        if vo_manifest_path.exists():
            vo_data = json.loads(vo_manifest_path.read_text())
            updated_vo = shift_timestamps(vo_data, trims)

            # Save updated VO manifest
            new_vo_name = vo_manifest_file.replace(".json", "_trimmed.json")
            new_vo_path = project_dir / new_vo_name
            new_vo_path.write_text(json.dumps(updated_vo, indent=2))
            updated_vo["audio_file"] = trimmed_name
            new_vo_path.write_text(json.dumps(updated_vo, indent=2))
            print(f"Updated VO manifest: {new_vo_path}")

            # Update project manifest to point to trimmed files
            manifest["voiceover"]["audio_file"] = trimmed_name
            manifest["voiceover"]["vo_manifest"] = new_vo_name
            manifest["voiceover"]["duration_s"] = round(new_duration, 3)
            manifest["voiceover"]["trimmed_from"] = audio_file
            # Save as YAML (manifest_path may be .json or .yaml)
            out_path = str(manifest_path.with_suffix(".yaml")) if manifest_path.suffix == ".json" else str(manifest_path)
            save_manifest(manifest, out_path)
            print(f"Manifest updated to use trimmed audio")
        else:
            print(f"VO manifest not found: {vo_manifest_path} — timestamps not shifted")
    else:
        print("No vo_manifest in manifest — timestamps not shifted")

    print(f"\nDone. Run align-scenes.py next to update scene timing.")


if __name__ == "__main__":
    main()
