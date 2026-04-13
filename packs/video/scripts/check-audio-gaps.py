#!/usr/bin/env python3
"""
Scan a lipsync JSON file for silence gaps between consecutive words.

Exits 0 if all gaps are within threshold, 1 if any gaps exceed it.
Use as a gate before starting renders:

    python3 check-audio-gaps.py --lipsync clean.lipsync.json || exit 1

Usage:
    check-audio-gaps.py --lipsync <path> [--threshold 1.5] [--warn-only]
"""
import argparse
import json
import sys
from pathlib import Path


def check_gaps(lipsync_path: str, threshold: float, warn_only: bool) -> int:
    try:
        data = json.loads(Path(lipsync_path).read_text())
    except FileNotFoundError:
        print(f"[check-audio-gaps] ERROR: file not found: {lipsync_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"[check-audio-gaps] ERROR: invalid JSON in {lipsync_path}: {e}", file=sys.stderr)
        return 1

    words = data.get("words", [])
    if len(words) < 2:
        print(f"[check-audio-gaps] WARNING: fewer than 2 words in {lipsync_path} — nothing to check")
        return 0

    gaps = []
    for i in range(1, len(words)):
        gap = words[i]["start"] - words[i - 1]["end"]
        if gap > threshold:
            gaps.append({
                "gap_s": round(gap, 2),
                "after_word": words[i - 1]["word"],
                "after_end": round(words[i - 1]["end"], 2),
                "before_word": words[i]["word"],
                "before_start": round(words[i]["start"], 2),
            })

    duration = round(words[-1]["end"], 2)

    if gaps:
        label = "WARNING" if warn_only else "FAIL"
        print(f"[check-audio-gaps] {label} — {len(gaps)} gap(s) > {threshold}s in {Path(lipsync_path).name} (total duration: {duration}s)")
        for g in gaps:
            print(
                f"  {g['gap_s']:.2f}s gap: "
                f"after \"{g['after_word']}\" ({g['after_end']:.2f}s) "
                f"→ \"{g['before_word']}\" ({g['before_start']:.2f}s)"
            )
        return 0 if warn_only else 1

    print(f"[check-audio-gaps] OK — no gaps > {threshold}s  ({len(words)} words, {duration}s total)")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Gate check: fail if any word-gap exceeds threshold"
    )
    parser.add_argument("--lipsync", required=True, help="Path to lipsync JSON file")
    parser.add_argument(
        "--threshold", type=float, default=1.5,
        help="Gap threshold in seconds (default: 1.5)"
    )
    parser.add_argument(
        "--warn-only", action="store_true",
        help="Print gaps but exit 0 regardless (informational mode)"
    )
    args = parser.parse_args()

    sys.exit(check_gaps(args.lipsync, args.threshold, args.warn_only))


if __name__ == "__main__":
    main()
