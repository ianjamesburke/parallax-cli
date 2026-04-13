#!/usr/bin/env python3
"""
Duck a music bed under speech using ffmpeg sidechain compression.

Takes a speech track and a music track, compresses the music whenever speech
is present, then mixes them together. The speech track drives the compressor
but is not compressed itself.

Presets:
  gentle  — subtle ducking, music stays present. Good for ambient beds.
  medium  — clear ducking, speech always dominant. Standard talking-head.
  heavy   — aggressive ducking, music nearly silent during speech. Ad/promo.
  podcast — fast attack, medium release. Tight duck for dialogue-heavy content.

Usage:
  apply-music-duck.py --speech vo.wav --music bed.mp3 --output mixed.wav --preset medium
  apply-music-duck.py --speech vo.wav --music bed.mp3 --output mixed.wav \\
    --threshold 0.02 --ratio 8 --attack 10 --release 300 --music-vol -18
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "gentle":  {"threshold": 0.05, "ratio": 4,  "attack": 20,  "release": 500, "music_vol_db": -15},
    "medium":  {"threshold": 0.03, "ratio": 6,  "attack": 15,  "release": 400, "music_vol_db": -18},
    "heavy":   {"threshold": 0.02, "ratio": 10, "attack": 10,  "release": 300, "music_vol_db": -20},
    "podcast": {"threshold": 0.03, "ratio": 8,  "attack": 5,   "release": 250, "music_vol_db": -16},
}


def build_duck_filter(threshold: float = 0.03, ratio: int = 6,
                      attack: int = 15, release: int = 400,
                      music_vol_db: int = -18) -> str:
    """
    filter_complex:
      [1:a] music → volume adjust → [music]
      [0:a] speech is the sidechain source
      [music][0:a] → sidechaincompress → [ducked]
      [0:a][ducked] → amix → output

    The speech audio drives the compressor on the music track.
    Then speech and ducked music are mixed together.
    """
    fc = (
        f"[1:a]volume={music_vol_db}dB[music];"
        f"[music][0:a]sidechaincompress="
        f"threshold={threshold}:"
        f"ratio={ratio}:"
        f"attack={attack}:"
        f"release={release}:"
        f"level_in=1:"
        f"level_sc=1[ducked];"
        f"[0:a][ducked]amix=inputs=2:duration=longest:dropout_transition=2"
    )
    return fc


def apply_music_duck(speech_path: str, music_path: str, output_path: str,
                     filter_complex: str):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", speech_path,
        "-i", music_path,
        "-filter_complex", filter_complex,
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]
    print(f"[apply-music-duck] Speech: {Path(speech_path).name}")
    print(f"[apply-music-duck] Music: {Path(music_path).name}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[apply-music-duck] ffmpeg failed: {e}", file=sys.stderr)
        raise
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"[apply-music-duck] Output: {output_path} ({size_mb:.1f}MB)")


def main():
    parser = argparse.ArgumentParser(description="Duck music bed under speech via sidechain compression")
    parser.add_argument("--speech", required=True, help="Speech/voiceover audio file")
    parser.add_argument("--music", required=True, help="Music bed audio file")
    parser.add_argument("--output", required=True, help="Output mixed audio path")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="Ducking preset")
    parser.add_argument("--threshold", type=float, help="Compressor threshold 0.0-1.0 (default 0.03)")
    parser.add_argument("--ratio", type=int, help="Compression ratio (default 6)")
    parser.add_argument("--attack", type=int, help="Attack time in ms (default 15)")
    parser.add_argument("--release", type=int, help="Release time in ms (default 400)")
    parser.add_argument("--music-vol", type=int, dest="music_vol_db",
                        help="Music volume in dB before ducking (default -18)")
    args = parser.parse_args()

    for label, path in [("speech", args.speech), ("music", args.music)]:
        abs_path = os.path.abspath(path)
        if not os.path.exists(abs_path):
            print(f"[apply-music-duck] {label} file not found: {abs_path}", file=sys.stderr)
            sys.exit(1)

    params = dict(PRESETS.get(args.preset or "medium", PRESETS["medium"]))
    if args.threshold is not None:
        params["threshold"] = args.threshold
    if args.ratio is not None:
        params["ratio"] = args.ratio
    if args.attack is not None:
        params["attack"] = args.attack
    if args.release is not None:
        params["release"] = args.release
    if args.music_vol_db is not None:
        params["music_vol_db"] = args.music_vol_db

    fc = build_duck_filter(**params)
    apply_music_duck(
        os.path.abspath(args.speech),
        os.path.abspath(args.music),
        args.output,
        fc,
    )


if __name__ == "__main__":
    main()
