#!/usr/bin/env python3
"""Extend a scene's duration by inserting silence into the audio and shifting
all downstream VO word timestamps. Updates the manifest atomically.

When you manually want a scene to be longer — e.g. "add 1 second to scene 1"
— this script does all four steps in one command:
  1. Edits scene timing in manifest (duration_s, end_s, and shifts all later scenes)
  2. Inserts silence into the audio at the scene's VO boundary
  3. Shifts all VO manifest word timestamps that fall after the split point
  4. Updates manifest voiceover pointers to the new padded files
  5. Re-validates the manifest

Usage:
  extend-scene.py --manifest path/to/manifest.yaml --scene 1 --seconds 1.0
  extend-scene.py --manifest path/to/manifest.yaml --scene 2 --seconds 0.5
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manifest_schema import load_manifest, save_manifest, validate_manifest


def insert_silence(audio_path: Path, split_at: float, seconds: float, output_path: Path):
    """Insert `seconds` of silence into `audio_path` at `split_at` seconds.

    Uses ffmpeg filter_complex: splits the audio, inserts an anullsrc segment,
    then concatenates the three parts.
    """
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-f", "lavfi", "-t", str(seconds),
            "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            (
                f"[0]atrim=end={split_at},asetpts=PTS-STARTPTS[a1];"
                f"[1]atrim=duration={seconds},asetpts=PTS-STARTPTS[sil];"
                f"[0]atrim=start={split_at},asetpts=PTS-STARTPTS[a2];"
                f"[a1][sil][a2]concat=n=3:v=0:a=1[out]"
            ),
            "-map", "[out]",
            str(output_path),
        ]
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"[extend-scene] ffmpeg failed inserting silence into {audio_path} "
            f"at t={split_at}s (+{seconds}s): {e}"
        ) from e


def shift_vo_timestamps(vo: dict, split_at: float, shift: float) -> dict:
    """Return a copy of vo with all word timestamps >= split_at shifted by shift seconds."""
    import copy
    padded = copy.deepcopy(vo)
    for w in padded.get("words", []):
        if w["start"] >= split_at:
            w["start"] += shift
            w["end"] += shift
    padded["total_duration_s"] = padded.get("total_duration_s", 0) + shift
    return padded


def extend_scene(manifest_path: str, scene_index: int, seconds: float):
    manifest = load_manifest(manifest_path)
    project_dir = Path(manifest_path).parent

    scenes = manifest.get("scenes", [])
    if not scenes:
        print("ERROR: manifest has no scenes.", file=sys.stderr)
        sys.exit(1)

    # Find the target scene (1-based index)
    target = next((s for s in scenes if s.get("index") == scene_index), None)
    if target is None:
        indices = [s.get("index") for s in scenes]
        print(f"ERROR: scene {scene_index} not found. Available: {indices}", file=sys.stderr)
        sys.exit(1)

    # The split point in the audio is the current end of this scene
    split_at = float(target.get("end_s", target.get("duration_s", 0)))
    print(f"Extending scene {scene_index} by {seconds}s (audio split at t={split_at:.3f}s)")

    # --- 1. Locate audio + VO manifest ---
    vo_info = manifest.get("voiceover", {})
    audio_rel = vo_info.get("audio_file")
    vo_manifest_rel = vo_info.get("vo_manifest")

    if not audio_rel or not vo_manifest_rel:
        print("ERROR: manifest missing voiceover.audio_file or voiceover.vo_manifest.", file=sys.stderr)
        sys.exit(1)

    audio_path = project_dir / audio_rel
    vo_manifest_path = project_dir / "assets" / "audio" / vo_manifest_rel
    if not vo_manifest_path.exists():
        vo_manifest_path = project_dir / vo_manifest_rel
    if not audio_path.exists():
        print(f"ERROR: audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)
    if not vo_manifest_path.exists():
        print(f"ERROR: VO manifest not found: {vo_manifest_path}", file=sys.stderr)
        sys.exit(1)

    try:
        vo = json.loads(vo_manifest_path.read_text())
    except Exception as e:
        raise RuntimeError(f"[extend-scene] Could not read VO manifest: {e}") from e

    # --- 2. Insert silence into audio ---
    audio_dir = audio_path.parent
    padded_audio_name = "voiceover_padded.mp3"
    padded_audio_path = audio_dir / padded_audio_name
    insert_silence(audio_path, split_at, seconds, padded_audio_path)
    print(f"  Audio padded: {padded_audio_path.name}")

    # --- 3. Shift VO timestamps ---
    padded_vo = shift_vo_timestamps(vo, split_at, seconds)
    padded_vo["audio_file"] = f"audio/{padded_audio_name}"
    padded_vo_name = "vo_manifest_padded.json"
    padded_vo_path = audio_dir / padded_vo_name
    try:
        padded_vo_path.write_text(json.dumps(padded_vo, indent=2))
    except Exception as e:
        raise RuntimeError(f"[extend-scene] Could not write padded VO manifest: {e}") from e
    print(f"  VO timestamps shifted: {padded_vo_name}")

    # --- 4. Update scene timings in manifest ---
    for scene in scenes:
        idx = scene.get("index", 0)
        if idx == scene_index:
            scene["duration_s"] = round(scene["duration_s"] + seconds, 3)
            scene["end_s"] = round(scene.get("end_s", 0) + seconds, 3)
        elif idx > scene_index:
            scene["start_s"] = round(scene.get("start_s", 0) + seconds, 3)
            scene["end_s"] = round(scene.get("end_s", 0) + seconds, 3)

    # --- 5. Update voiceover pointers ---
    manifest["voiceover"]["audio_file"] = f"audio/{padded_audio_name}"
    manifest["voiceover"]["vo_manifest"] = padded_vo_name
    old_dur = vo_info.get("duration_s", 0)
    manifest["voiceover"]["duration_s"] = round(old_dur + seconds, 3)

    # --- 6. Save + validate ---
    try:
        save_manifest(manifest, manifest_path)
    except Exception as e:
        raise RuntimeError(f"[extend-scene] Could not save manifest: {e}") from e

    errors, warnings = validate_manifest(manifest, manifest_path)
    if errors:
        print("Manifest errors after update:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    total = scenes[-1].get("end_s", 0)
    print(f"\nDone. Scene {scene_index} extended by {seconds}s. Total video: {total:.3f}s")
    print(f"\nNext: reassemble and re-burn captions:")
    print(f"  python3 assemble.py --manifest {manifest_path} --draft")
    print(f"  python3 burn-captions.py --manifest {manifest_path} --video <draft.mp4>")


def main():
    parser = argparse.ArgumentParser(
        description="Extend a scene by inserting silence and shifting VO timestamps"
    )
    parser.add_argument("--manifest", required=True, help="Path to manifest.yaml")
    parser.add_argument("--scene", required=True, type=int, help="Scene index to extend (1-based)")
    parser.add_argument("--seconds", required=True, type=float, help="Seconds to add")
    args = parser.parse_args()

    if args.seconds <= 0:
        print("ERROR: --seconds must be positive.", file=sys.stderr)
        sys.exit(1)

    try:
        extend_scene(args.manifest, args.scene, args.seconds)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
