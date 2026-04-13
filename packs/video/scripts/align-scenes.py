#!/usr/bin/env python3
"""Align scene timecodes to actual voiceover word timestamps.

Replaces estimated start_s/end_s/duration_s with actual values derived
from the vo_manifest word timestamps. Must run after generate-voiceover.py
and after any silence trimming.

Usage:
  align-scenes.py --manifest path/to/manifest.yaml
  align-scenes.py --manifest path/to/manifest.yaml --vo-manifest path/to/vo_manifest.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manifest_schema import load_manifest, save_manifest, validate_manifest


def align_scenes(manifest_path: str, vo_manifest_path: str = None):
    manifest = load_manifest(manifest_path)
    project_dir = Path(manifest_path).parent

    # Find vo_manifest — check manifest path, then audio/ subfolder, then project root
    if not vo_manifest_path:
        candidate = manifest.get("voiceover", {}).get("vo_manifest")
        if candidate:
            vo_path = project_dir / candidate
        elif (project_dir / "assets" / "audio" / "vo_manifest.json").exists():
            vo_path = project_dir / "assets" / "audio" / "vo_manifest.json"
        else:
            vo_path = project_dir / "vo_manifest.json"
    else:
        vo_path = Path(vo_manifest_path)

    if not vo_path.exists():
        print(f"ERROR: vo_manifest not found: {vo_path}", file=sys.stderr)
        print("Run generate-voiceover.py first.", file=sys.stderr)
        sys.exit(1)

    try:
        vo = json.loads(vo_path.read_text())
    except Exception as e:
        print(f"ERROR: Could not read vo_manifest: {e}", file=sys.stderr)
        sys.exit(1)

    words = vo.get("words", [])
    if not words:
        print("ERROR: vo_manifest has no word timestamps.", file=sys.stderr)
        sys.exit(1)

    scenes = manifest.get("scenes", [])
    if not scenes:
        print("ERROR: No scenes in manifest. Run plan-scenes.py first.", file=sys.stderr)
        sys.exit(1)

    cursor = 0
    print(f"Aligning {len(scenes)} scenes to {len(words)} words...")
    print(f"\n{'#':>3}  {'Start':>7}  {'End':>7}  {'Dur':>5}  {'Words':>5}  VO text")
    print("-" * 90)

    for scene in scenes:
        vo_text = scene.get("vo_text", "").strip()
        if not vo_text:
            print(f"  Scene {scene.get('index', '?')}: no vo_text, skipping alignment")
            continue

        scene_word_count = len(vo_text.split())

        if cursor + scene_word_count > len(words):
            print(
                f"ERROR: Scene {scene.get('index', '?')} needs {scene_word_count} words "
                f"but only {len(words) - cursor} remain.",
                file=sys.stderr,
            )
            sys.exit(1)

        first_word = words[cursor]
        last_word = words[cursor + scene_word_count - 1]

        start_s = round(first_word["start"], 3)
        end_s = round(last_word["end"], 3)
        duration_s = round(end_s - start_s, 3)

        scene["start_s"] = start_s
        scene["end_s"] = end_s
        scene["duration_s"] = duration_s
        # Remove estimated fields if present
        scene.pop("estimated_start_s", None)
        scene.pop("estimated_end_s", None)

        idx = scene.get("index", "?")
        vo_preview = vo_text[:45]
        print(f"{idx:>3}  {start_s:>7.3f}  {end_s:>7.3f}  {duration_s:>5.3f}  {scene_word_count:>5}  {vo_preview}")

        cursor += scene_word_count

    leftover = len(words) - cursor
    if leftover > 0:
        print(f"\nWARNING: {leftover} word(s) in vo_manifest not assigned to any scene")

    # Post-alignment: close inter-scene gaps.
    # Word boundaries leave natural gaps between scenes (inter-sentence silence).
    # assemble.py concatenates by duration_s, so gaps cause progressive drift.
    # Fix: extend each scene's end_s to the next scene's start_s.
    gaps_closed = 0
    for i in range(len(scenes) - 1):
        next_start = scenes[i + 1]["start_s"]
        if scenes[i]["end_s"] < next_start:
            gaps_closed += 1
            scenes[i]["end_s"] = next_start
            scenes[i]["duration_s"] = round(scenes[i]["end_s"] - scenes[i]["start_s"], 3)

    # Extend last scene to cover full audio duration so assembler doesn't truncate.
    audio_duration = vo.get("total_duration_s", 0)
    if scenes and audio_duration > scenes[-1]["end_s"]:
        scenes[-1]["end_s"] = round(audio_duration, 3)
        scenes[-1]["duration_s"] = round(scenes[-1]["end_s"] - scenes[-1]["start_s"], 3)

    if gaps_closed > 0 or audio_duration > 0:
        print(f"\nPost-alignment: closed {gaps_closed} inter-scene gap(s), last scene extends to {scenes[-1]['end_s']:.3f}s")

    total_aligned = scenes[-1].get("end_s", 0) if scenes else 0
    print(f"\nAligned: {len(scenes)} scenes, {total_aligned:.3f}s total (contiguous)")

    manifest["scenes"] = scenes
    manifest["voiceover"]["vo_manifest"] = vo_path.name
    save_manifest(manifest, manifest_path)
    validate_manifest(manifest, manifest_path)

    print(f"\nNext: python3 generate-still.py --manifest {manifest_path} --scene 1")


def main():
    parser = argparse.ArgumentParser(description="Align scene timecodes to VO word timestamps")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--vo-manifest", default=None, help="Path to vo_manifest.json (default: auto-detect from manifest)")
    args = parser.parse_args()

    align_scenes(args.manifest, args.vo_manifest)


if __name__ == "__main__":
    main()
