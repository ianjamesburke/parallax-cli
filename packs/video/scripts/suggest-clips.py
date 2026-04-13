#!/usr/bin/env python3
"""
suggest-clips.py — Analyze a clip-index manifest and recommend which clips to keep.

Flags three classes of bad clips:
  1. Isolated beats   — clips with fewer than --min-words words (default: 3)
                        Single words like "Hi" or "My" with long pauses become
                        hanging caption chunks and awkward edit beats.
  2. Retakes          — clips whose transcript substantially repeats an earlier
                        clip (threshold: --retake-threshold, default 0.65).
                        Detects "but then I found strawberry.me..." appearing
                        twice because the speaker stumbled and restarted.
  3. Self-directing   — clips containing phrases that indicate the speaker is
                        directing themselves rather than performing content.
                        e.g. "what's some other b-roll", "just grab a clip of me",
                        "let's try that again".

Usage:
  python3 suggest-clips.py --manifest _meta/IMG_9639.yaml
  python3 suggest-clips.py --manifest _meta/IMG_9639.yaml --min-words 2
  python3 suggest-clips.py --manifest _meta/IMG_9639.yaml --verbose
"""

import argparse
import sys
from pathlib import Path

import yaml

# Phrases that indicate the speaker is directing themselves rather than performing.
# Case-insensitive substring match against clip transcript.
SELF_DIRECTING_PHRASES = [
    "b-roll", "b roll", "broll",
    "what's some other",
    "just grab a clip",
    "from the top",
    "let's try",
    "one more time",
    "that's a cut",
    "did we get that",
    "okay for this next part",
    "cut that",
    "that was good",
    "stop recording",
    "okay so",
    "alright so",
    "with some website stuff",
]


def words_for_clip(all_words: list, source_start: float, source_end: float) -> list[str]:
    """Return word strings that fall within [source_start, source_end)."""
    return [
        w["word"].strip(".,!?;:")
        for w in all_words
        if source_start <= w["start"] < source_end
    ]


def is_retake(clip_words: list[str], earlier_word_lists: list[list[str]], threshold: float) -> bool:
    """Return True if clip_words substantially repeats any earlier clip."""
    if len(clip_words) < 3:
        return False  # Too short to reliably detect — let isolated-beat rule handle it
    clip_lower = [w.lower() for w in clip_words]
    for earlier_words in earlier_word_lists:
        if not earlier_words:
            continue
        earlier_lower = set(w.lower() for w in earlier_words)
        overlap = sum(1 for w in clip_lower if w in earlier_lower) / len(clip_lower)
        if overlap >= threshold:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suggest which clips to include in assembly based on transcript analysis"
    )
    parser.add_argument("--manifest", required=True, help="Path to clip-index manifest YAML")
    parser.add_argument(
        "--min-words",
        type=int,
        default=3,
        help="Flag clips with fewer than this many words as isolated beats (default: 3)",
    )
    parser.add_argument(
        "--retake-threshold",
        type=float,
        default=0.65,
        help="Fraction of words that must overlap an earlier clip to flag as retake (default: 0.65)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show full transcript for each clip",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    data = yaml.safe_load(manifest_path.read_text())
    clips = data.get("clips", [])
    all_words = data.get("words", [])

    if not clips:
        print("[suggest-clips] No clips found in manifest.", file=sys.stderr)
        sys.exit(1)

    include = []
    exclude_info = []   # (index, transcript, [reasons])
    earlier_word_lists = []

    for i, clip in enumerate(clips):
        src_start = clip.get("source_start", clip.get("start", 0))
        src_end = clip.get("source_end", clip.get("end", 0))
        clip_words = words_for_clip(all_words, src_start, src_end)
        transcript = " ".join(clip_words)
        transcript_lower = transcript.lower()

        reasons = []

        # 1. Isolated beat
        if len(clip_words) < args.min_words:
            reasons.append(
                f"isolated beat ({len(clip_words)} word{'s' if len(clip_words) != 1 else ''})"
            )

        # 2. Self-directing
        for phrase in SELF_DIRECTING_PHRASES:
            if phrase in transcript_lower:
                reasons.append(f"self-directing (\"{phrase}\")")
                break

        # 3. Retake (only check after the first few clips have been seen)
        if i > 0 and not reasons:
            if is_retake(clip_words, earlier_word_lists, args.retake_threshold):
                reasons.append("retake (transcript overlaps earlier clip)")

        if reasons:
            exclude_info.append((i, transcript, reasons))
        else:
            include.append(i)

        earlier_word_lists.append(clip_words)

    # Output
    print(f"\n[suggest-clips] {len(clips)} clips analyzed")
    print(f"  Source: {manifest_path.name}")

    if include:
        clips_flag = ",".join(str(i) for i in include)
        total_duration = sum(
            clips[i].get("duration", 0) for i in include
        )
        print(f"\nRECOMMENDED: --clips {clips_flag}  (~{total_duration:.1f}s)")
    else:
        print("\nRECOMMENDED: (no clips passed — check thresholds)")

    if exclude_info:
        print(f"\nEXCLUDED ({len(exclude_info)} clip{'s' if len(exclude_info) != 1 else ''}):")
        for idx, transcript, reasons in exclude_info:
            reason_str = "; ".join(reasons)
            preview = (transcript[:70] + "…") if len(transcript) > 70 else (transcript or "(empty)")
            print(f"  clip[{idx}]: {reason_str}")
            if args.verbose or not transcript:
                print(f"    \"{preview}\"")

    if not exclude_info:
        print("\n  No clips flagged — all look clean.")

    print()


if __name__ == "__main__":
    main()
