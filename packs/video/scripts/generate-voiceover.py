#!/usr/bin/env python3
"""Generate voiceover audio from manifest using ElevenLabs.

Reads script and voice config from manifest. Calls ElevenLabs with
convert_with_timestamps to get word-level timing. Applies a fixed
atempo speedup (default 1.3x) via ffmpeg.

Outputs:
  voiceover.mp3        — the final audio file
  vo_manifest.json     — word-level timestamps (input to align-scenes.py)

Usage:
  generate-voiceover.py --manifest path/to/manifest.yaml
  generate-voiceover.py --manifest path/to/manifest.yaml --voice george --speed 1.2
  generate-voiceover.py --manifest path/to/manifest.yaml --no-speedup
"""

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api_config import get_elevenlabs_key
from cost_tracker import track_vo
from manifest_schema import load_manifest, save_manifest, validate_manifest

DEFAULT_SPEEDUP = 1.3
OUTPUT_FORMAT = "mp3_44100_128"

# Voice shortcuts → ElevenLabs voice IDs
VOICE_SHORTCUTS = {
    "george":  "JBFqnCBsd6RMkjVDRZzb",
    "rachel":  "21m00Tcm4TlvDq8ikWAM",
    "domi":    "AZnzlk1XvdvUeBnXmlld",
    "bella":   "EXAVITQu4vr4xnSDxMaL",
    "antoni":  "ErXwobaYiN019PkySvjV",
    "arnold":  "VR6AewLTigWG4xSOukaG",
}


def _extract_script(manifest: dict, project_dir: Path) -> str:
    """Pull script text from supplied resources or fall back to scene vo_text."""
    for r in manifest.get("resources", {}).get("supplied", []):
        if r.get("type") in ("script", "voiceover_recording"):
            p = project_dir / r["path"]
            if p.exists() and p.suffix in (".txt", ".md"):
                return p.read_text().strip()
    # Fall back to concatenated vo_text from scenes
    scenes = manifest.get("scenes", [])
    if scenes:
        return " ".join(s.get("vo_text", "") for s in scenes if s.get("vo_text"))
    return ""


def _derive_word_timestamps(alignment, audio_duration: float) -> list[dict]:
    """Convert ElevenLabs character-level alignment to word-level timestamps."""
    chars = list(alignment.characters)
    starts = list(alignment.character_start_times_seconds)

    words = []
    current_word = ""
    word_start = None

    for i, char in enumerate(chars):
        if char.strip() == "":
            if current_word and word_start is not None:
                words.append({"word": current_word, "start": word_start})
                current_word = ""
                word_start = None
        else:
            if word_start is None:
                word_start = starts[i]
            current_word += char

    if current_word and word_start is not None:
        words.append({"word": current_word, "start": word_start})

    # End of each word = start of next; last word ends at audio_duration
    result = []
    for i, w in enumerate(words):
        end = words[i + 1]["start"] if i + 1 < len(words) else audio_duration
        result.append({
            "word": w["word"],
            "start": round(w["start"], 3),
            "end": round(end, 3),
        })
    return result


def _apply_atempo(audio_path: Path, words: list[dict], speedup: float) -> tuple:
    """Speed up audio via ffmpeg atempo and scale word timestamps proportionally."""
    sped_path = audio_path.with_suffix(".sped.mp3")
    result = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(audio_path),
        "-af", f"atempo={speedup}",
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(sped_path),
    ], capture_output=True, text=True)

    if result.returncode != 0 or not sped_path.exists():
        print(f"  Warning: atempo failed, using original audio")
        return audio_path, words, words[-1]["end"] if words else 0.0

    sped_path.replace(audio_path)
    scale = 1.0 / speedup
    adjusted = [
        {"word": w["word"], "start": round(w["start"] * scale, 3), "end": round(w["end"] * scale, 3)}
        for w in words
    ]
    old_dur = words[-1]["end"] if words else 0
    new_dur = adjusted[-1]["end"] if adjusted else 0
    print(f"  Sped up: {old_dur:.1f}s → {new_dur:.1f}s ({speedup}x)")
    return audio_path, adjusted, new_dur


def generate_voiceover(manifest_path: str, voice_override: str = None, speedup: float = DEFAULT_SPEEDUP, no_speedup: bool = False):
    try:
        from elevenlabs.client import ElevenLabs
        from elevenlabs.types import VoiceSettings
    except ImportError:
        print("ERROR: elevenlabs not installed. Run: pip install elevenlabs", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest(manifest_path)
    project_dir = Path(manifest_path).parent
    audio_dir = project_dir / "assets" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    script_text = _extract_script(manifest, project_dir)
    if not script_text:
        print("ERROR: No script text found in manifest or supplied resources.", file=sys.stderr)
        sys.exit(1)

    # Resolve voice
    voice_cfg = manifest.get("voice", {})
    voice_id = voice_cfg.get("voice_id", VOICE_SHORTCUTS["george"])
    voice_name = voice_cfg.get("voice_name", "George")
    if voice_override:
        resolved = VOICE_SHORTCUTS.get(voice_override.lower(), voice_override)
        voice_id = resolved
        voice_name = voice_override.title()

    model_id = voice_cfg.get("model_id", "eleven_v3")
    stability = voice_cfg.get("stability", 0.5)
    similarity_boost = voice_cfg.get("similarity_boost", 0.75)

    print(f"Script: {len(script_text.split())} words")
    print(f"Voice: {voice_name} ({voice_id})")
    print(f"Model: {model_id}")

    client = ElevenLabs(api_key=get_elevenlabs_key())

    print("Calling ElevenLabs convert_with_timestamps...")
    try:
        response = client.text_to_speech.convert_with_timestamps(
            text=script_text,
            voice_id=voice_id,
            model_id=model_id,
            output_format=OUTPUT_FORMAT,
            voice_settings=VoiceSettings(
                stability=stability,
                similarity_boost=similarity_boost,
            ),
        )
    except Exception as e:
        print(f"ERROR: ElevenLabs call failed: {e}", file=sys.stderr)
        sys.exit(1)

    audio_bytes = base64.b64decode(response.audio_base_64)
    audio_path = audio_dir / "voiceover.mp3"
    audio_path.write_bytes(audio_bytes)
    print(f"Audio saved: {audio_path} ({len(audio_bytes):,} bytes)")
    track_vo(project_dir, len(script_text), "elevenlabs", voice_name)

    # Probe actual duration
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path)],
            capture_output=True, text=True, check=True,
        )
        audio_duration = float(json.loads(probe.stdout)["format"]["duration"])
    except Exception as e:
        print(f"  Warning: ffprobe failed ({e}), estimating duration from timestamps")
        audio_duration = 0

    # Build word timestamps from alignment
    words = []
    alignment = response.alignment
    if alignment and hasattr(alignment, "characters"):
        words = _derive_word_timestamps(alignment, audio_duration)
        print(f"Timestamps: {len(words)} words, {words[-1]['end']:.2f}s raw")
    else:
        print("WARNING: No alignment data returned from ElevenLabs")

    total_duration = words[-1]["end"] if words else audio_duration

    # Apply speedup
    if not no_speedup and words and speedup != 1.0:
        audio_path, words, total_duration = _apply_atempo(audio_path, words, speedup)

    vo_manifest = {
        "audio_file": "voiceover.mp3",
        "total_duration_s": round(total_duration, 3),
        "voice_id": voice_id,
        "voice_name": voice_name,
        "model_id": model_id,
        "speedup": speedup if not no_speedup else 1.0,
        "words": words,
    }

    vo_manifest_path = audio_dir / "vo_manifest.json"
    vo_manifest_path.write_text(json.dumps(vo_manifest, indent=2))
    print(f"VO manifest saved: {vo_manifest_path}")
    print(f"Total duration: {total_duration:.2f}s")

    # Persist voice config so future sessions can regenerate lines with the same voice
    manifest["voice"] = {
        "voice_id": voice_id,
        "voice_name": voice_name,
        "model_id": model_id,
        "stability": stability,
        "similarity_boost": similarity_boost,
    }

    # Update manifest with voiceover info
    manifest["voiceover"] = {
        "audio_file": "assets/audio/voiceover.mp3",
        "vo_manifest": "assets/audio/vo_manifest.json",
        "duration_s": round(total_duration, 3),
    }
    save_manifest(manifest, manifest_path)
    validate_manifest(manifest, manifest_path)

    print(f"\nNext: python3 align-scenes.py --manifest {manifest_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate voiceover from manifest")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--voice", default=None, help=f"Voice name or ID. Shortcuts: {', '.join(VOICE_SHORTCUTS)}")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEEDUP, help=f"Atempo speedup factor (default: {DEFAULT_SPEEDUP})")
    parser.add_argument("--no-speedup", action="store_true", help="Skip atempo speedup, use raw EL audio")
    args = parser.parse_args()

    generate_voiceover(args.manifest, args.voice, args.speed, args.no_speedup)


if __name__ == "__main__":
    main()
