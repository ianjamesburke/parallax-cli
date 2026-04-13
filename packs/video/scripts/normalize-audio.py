#!/usr/bin/env python3
"""
Normalize audio loudness in a video file using ffmpeg's two-pass loudnorm (EBU R128).

Targets -14 LUFS by default (social/streaming standard). Optionally mixes in a separate
music track at a specified gain offset so music sits under dialogue.

Usage:
  normalize-audio.py --input assembled.mp4 --output normalized.mp4
  normalize-audio.py --input assembled.mp4 --output normalized.mp4 --target -16
  normalize-audio.py --input assembled.mp4 --output normalized.mp4 --music music.mp3 --music-gain -12

Flags:
  --input         Input video file (required)
  --output        Output video file (required)
  --target        Target integrated loudness in LUFS (default: -14.0)
  --true-peak     Maximum true peak in dBTP (default: -1.5)
  --lra           Loudness range target in LU (default: 11.0)
  --music         Optional music track to mix under dialogue audio
  --music-gain    Gain to apply to music track in dB before mix (default: -12.0)
                  Negative = quieter. -6 is subtle duck, -12 is clearly background.
  --music-loop    Loop the music track to match video duration (default: true)
  --dry-run       Run loudness analysis only, print measured levels, no output written
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def probe_duration(path: str) -> float:
    """Return video duration in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception as e:
        raise RuntimeError(f"probe_duration failed for {path!r}: {e}") from e


def measure_loudness(path: str, target_lufs: float, true_peak: float, lra: float) -> dict:
    """
    Run the first pass of loudnorm to measure integrated loudness, true peak, and LRA.
    Returns the JSON stats block ffmpeg prints to stderr.
    """
    af = (
        f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}"
        ":print_format=json"
    )
    cmd = [
        "ffmpeg", "-hide_banner",
        "-i", path,
        "-af", af,
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        # loudnorm JSON is printed to stderr
        stderr = result.stderr
        # Extract the JSON block (between the last { ... })
        match = re.search(r'\{[^{}]+\}', stderr, re.DOTALL)
        if not match:
            raise RuntimeError(
                f"loudnorm analysis produced no JSON.\nstderr:\n{stderr}"
            )
        return json.loads(match.group())
    except Exception as e:
        raise RuntimeError(f"measure_loudness failed: {e}") from e


def build_loudnorm_filter(stats: dict, target_lufs: float, true_peak: float, lra: float) -> str:
    """Build the second-pass loudnorm filter string using first-pass measurements."""
    return (
        f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}"
        f":measured_I={stats['input_i']}"
        f":measured_TP={stats['input_tp']}"
        f":measured_LRA={stats['input_lra']}"
        f":measured_thresh={stats['input_thresh']}"
        f":offset={stats['target_offset']}"
        ":linear=true:print_format=summary"
    )


def normalize(
    input_path: str,
    output_path: str,
    target_lufs: float,
    true_peak: float,
    lra: float,
    music_path: str | None,
    music_gain_db: float,
    music_loop: bool,
    dry_run: bool,
) -> None:
    print(f"Pass 1 — measuring loudness of {input_path!r}...", flush=True)
    try:
        stats = measure_loudness(input_path, target_lufs, true_peak, lra)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    measured_lufs = stats.get("input_i", "?")
    measured_tp = stats.get("input_tp", "?")
    measured_lra = stats.get("input_lra", "?")
    print(f"  Measured:  {measured_lufs} LUFS  |  TP {measured_tp} dBTP  |  LRA {measured_lra} LU")
    print(f"  Target:    {target_lufs} LUFS  |  TP {true_peak} dBTP  |  LRA {lra} LU")

    if dry_run:
        print("Dry run — no output written.")
        return

    loudnorm_filter = build_loudnorm_filter(stats, target_lufs, true_peak, lra)

    print(f"Pass 2 — normalizing to {target_lufs} LUFS...", flush=True)

    if music_path:
        # Mix music under normalized dialogue
        # filter_complex:
        #   [0:a] → loudnorm → [speech]
        #   [1:a] → volume gain → [music]
        #   [speech][music] → amix (speech dominates) → [out]
        duration = probe_duration(input_path)
        music_input_flags = []
        if music_loop:
            music_input_flags = ["-stream_loop", "-1"]

        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-i", input_path,
            *music_input_flags,
            "-i", music_path,
            "-filter_complex",
            (
                f"[0:a]{loudnorm_filter}[speech];"
                f"[1:a]volume={music_gain_db}dB[music];"
                f"[speech][music]amix=inputs=2:duration=first:weights=1 0.3[out]"
            ),
            "-map", "0:v",
            "-map", "[out]",
            "-t", str(duration),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-i", input_path,
            "-af", loudnorm_filter,
            "-map", "0:v",
            "-map", "0:a",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-movflags", "+faststart",
            output_path,
        ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg exited {result.returncode}.\nstderr:\n{result.stderr[-2000:]}"
            )
    except Exception as e:
        print(f"ERROR: normalize failed: {e}", file=sys.stderr)
        sys.exit(1)

    out = Path(output_path)
    if not out.exists() or out.stat().st_size == 0:
        print(f"ERROR: output file missing or empty: {output_path}", file=sys.stderr)
        sys.exit(1)

    size_mb = out.stat().st_size / 1_000_000
    print(f"Done → {output_path}  ({size_mb:.1f} MB)")
    if music_path:
        print(f"  Music mixed at {music_gain_db:+.0f} dB")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize audio loudness (EBU R128 two-pass loudnorm).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="Input video file")
    parser.add_argument("--output", required=True, help="Output video file")
    parser.add_argument("--target", type=float, default=-14.0,
                        help="Target integrated loudness in LUFS (default: -14.0)")
    parser.add_argument("--true-peak", type=float, default=-1.5,
                        help="Max true peak in dBTP (default: -1.5)")
    parser.add_argument("--lra", type=float, default=11.0,
                        help="Loudness range in LU (default: 11.0)")
    parser.add_argument("--music", default=None,
                        help="Optional music track to mix under dialogue")
    parser.add_argument("--music-gain", type=float, default=-12.0,
                        help="Gain offset for music track in dB (default: -12.0)")
    parser.add_argument("--music-loop", action=argparse.BooleanOptionalAction, default=True,
                        help="Loop music to match video duration (default: on)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Measure and print loudness only, no output written")

    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"ERROR: input file not found: {args.input!r}", file=sys.stderr)
        sys.exit(1)
    if args.music and not Path(args.music).exists():
        print(f"ERROR: music file not found: {args.music!r}", file=sys.stderr)
        sys.exit(1)

    normalize(
        input_path=args.input,
        output_path=args.output,
        target_lufs=args.target,
        true_peak=args.true_peak,
        lra=args.lra,
        music_path=args.music,
        music_gain_db=args.music_gain,
        music_loop=args.music_loop,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
