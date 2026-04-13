#!/usr/bin/env python3
"""
Analyze a source video clip and write a clip index manifest to _meta/<stem>.yaml.
No video is exported. The manifest is the edit — change pad values and --recompute
to get different cuts without re-running transcription.

Usage:
  index-clip.py --input /path/to/footage/video.mov
  index-clip.py --input /path/to/video.mov --min-silence 0.4 --min-clip 0.5
  index-clip.py --input /path/to/video.mov --force          # full re-index
  index-clip.py --input /path/to/video.mov --recompute      # re-derive clips from silences (instant)
  index-clip.py --input /path/to/video.mov --no-transcript  # silence chop only, skip Whisper
  index-clip.py --input /path/to/video.mov --output /tmp/my-index.yaml

Flags:
  --input           Source video file (required)
  --output          Output path for clip index YAML (default: _meta/<stem>.yaml next to source)
  --min-silence     Minimum silence duration to cut (default: 0.5s)
  --min-clip        Drop keep-segments shorter than this (default: 1.0s)
  --pad             Padding to keep on each side of a silence cut (default: 0.15s)
  --threshold       Silence detection threshold in dB (default: -35)
  --model           Whisper model (default: base.en)
  --language        Whisper language (default: en)
  --backend         Transcription backend: auto|whisperx|faster-whisper|whisper-cli (default: auto)
                    auto tries whisperx → faster-whisper → whisper-cli in order.
                    whisperx uses phoneme-level forced alignment for more accurate word starts.
  --refine-onsets   After transcription, scan amplitude in a ±1s window around each word start
                    to find the actual silence→speech transition (default: on)
  --no-refine-onsets  Disable onset refinement
  --onset-lookback  Seconds to look back from word start when refining onsets (default: 1.0)
  --force           Full re-index even if manifest exists
  --recompute       Re-derive clips[] from silences[] using current pad values — no Whisper
  --no-transcript   Skip transcription entirely (silence-chop only)

Manifest authority rule:
  silences[] is the source of truth for cut points.
  clips[] is derived from silences[] via --recompute.
  If you edit clips[] directly, do NOT run --recompute (it overwrites manual edits).
  If you edit pad_before/pad_after on silences, run --recompute then assemble.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def _dump_yaml(data: dict) -> str:
    try:
        import yaml
        return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except ImportError:
        return "# install pyyaml for proper YAML formatting\n" + json.dumps(data, indent=2)


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(path.read_text()) or {}
    except ImportError:
        return json.loads(path.read_text())


def get_duration(path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        print(f"[index-clip] ffprobe failed on {path}: {e}", file=sys.stderr)
        raise


def extract_audio(video_path: str, audio_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[index-clip] Audio extraction failed: {e}", file=sys.stderr)
        raise


def transcribe_whisperx(audio_path: str, model: str, language: str) -> tuple[str, list[dict]]:
    """Transcribe using WhisperX (phoneme-level forced alignment). Returns (transcript, words)."""
    import whisperx
    print(f"[index-clip] Transcribing with whisperx model={model} (phoneme alignment)...")
    device = "cpu"
    wx_model = whisperx.load_model(model, device, compute_type="int8", language=language)
    audio = whisperx.load_audio(audio_path)
    result = wx_model.transcribe(audio, language=language)

    # Phoneme-level forced alignment
    align_model, metadata = whisperx.load_align_model(language_code=language, device=device)
    result = whisperx.align(result["segments"], align_model, metadata, audio, device)

    transcript_parts = [s["text"] for s in result.get("segments", [])]
    words = []
    for w in result.get("word_segments", []):
        word_text = w.get("word", "").strip()
        if not word_text:
            continue
        words.append({
            "word": word_text,
            "start": round(float(w.get("start", 0)), 3),
            "end": round(float(w.get("end", 0)), 3),
        })

    return " ".join(transcript_parts).strip(), words


def transcribe_faster_whisper(audio_path: str, model: str, language: str) -> tuple[str, list[dict]]:
    """Transcribe using faster-whisper (preferred). Returns (transcript, words)."""
    from faster_whisper import WhisperModel
    print(f"[index-clip] Transcribing with faster-whisper model={model}...")
    fw_model = WhisperModel(model, device="cpu", compute_type="int8")
    segments, _ = fw_model.transcribe(audio_path, language=language, word_timestamps=True)

    transcript_parts = []
    words = []
    for segment in segments:
        transcript_parts.append(segment.text)
        if segment.words:
            for w in segment.words:
                words.append({
                    "word": w.word.strip(),
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                })

    return " ".join(transcript_parts).strip(), words


def transcribe_whisper_cli(audio_path: str, model: str, language: str, out_dir: str) -> tuple[str, list[dict]]:
    """Transcribe using whisper CLI (fallback). Returns (transcript, words)."""
    cmd = [
        "whisper", audio_path,
        "--model", model,
        "--language", language,
        "--output_format", "json",
        "--output_dir", out_dir,
        "--word_timestamps", "True",
        "--fp16", "False",
        "--verbose", "False",
    ]
    print(f"[index-clip] Transcribing with whisper CLI model={model}...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[index-clip] Whisper CLI failed: {e}", file=sys.stderr)
        raise

    json_file = Path(out_dir) / f"{Path(audio_path).stem}.json"
    if not json_file.exists():
        print(f"[index-clip] WARNING: whisper JSON output not found at {json_file}", file=sys.stderr)
        return "", []

    data = json.loads(json_file.read_text())
    transcript = data.get("text", "").strip()
    words = []
    for segment in data.get("segments", []):
        for w in segment.get("words", []):
            words.append({
                "word": w.get("word", "").strip(),
                "start": round(w.get("start", 0), 3),
                "end": round(w.get("end", 0), 3),
            })
    return transcript, words


def transcribe(audio_path: str, model: str, language: str, out_dir: str,
               backend: str = "auto") -> tuple[str, list[dict]]:
    """
    Dispatch to the requested transcription backend.
    auto: whisperx → faster-whisper → whisper-cli
    Phoneme-level alignment (whisperx) gives the most accurate word start times.
    """
    def try_whisperx():
        try:
            return transcribe_whisperx(audio_path, model, language)
        except ImportError:
            return None
        except Exception as e:
            print(f"[index-clip] whisperx failed ({e}), falling back...", file=sys.stderr)
            return None

    def try_faster_whisper():
        try:
            return transcribe_faster_whisper(audio_path, model, language)
        except ImportError:
            return None
        except Exception as e:
            print(f"[index-clip] faster-whisper failed ({e}), falling back...", file=sys.stderr)
            return None

    if backend == "whisperx":
        result = try_whisperx()
        if result is None:
            raise RuntimeError("whisperx backend requested but failed — is whisperx installed?")
        return result
    elif backend == "faster-whisper":
        result = try_faster_whisper()
        if result is None:
            raise RuntimeError("faster-whisper backend requested but failed")
        return result
    elif backend == "whisper-cli":
        return transcribe_whisper_cli(audio_path, model, language, out_dir)
    else:  # auto
        result = try_whisperx()
        if result is not None:
            return result
        result = try_faster_whisper()
        if result is not None:
            return result
        return transcribe_whisper_cli(audio_path, model, language, out_dir)


def refine_word_onset(audio: np.ndarray, sr: int, word_start: float,
                      lookback: float = 1.0, frame_ms: float = 5.0) -> float:
    """
    Refine a word's start timestamp by scanning amplitude in a lookback window.

    Searches backward from word_start to find the last silence→speech transition:
    the frame just after the noise floor ends. This corrects for models that place
    word starts at the vowel nucleus or phoneme model midpoint rather than the
    actual acoustic onset.

    Returns the refined start time, clamped to [word_start - lookback, word_start].
    If no improvement is found (no silence in the window), returns word_start unchanged.
    """
    frame_len = max(1, int(sr * frame_ms / 1000))
    win_start = max(0.0, word_start - lookback)
    win_end = min(len(audio) / sr, word_start + 0.1)

    s0 = int(win_start * sr)
    s1 = int(win_end * sr)
    chunk = audio[s0:s1]
    if len(chunk) < frame_len * 4:
        return word_start

    # Per-frame RMS energy
    n_frames = len(chunk) // frame_len
    rms = np.array([
        np.sqrt(np.mean(chunk[i * frame_len:(i + 1) * frame_len] ** 2))
        for i in range(n_frames)
    ])

    # Noise floor: 75th percentile of the first 20% of frames (pre-word region)
    pre_n = max(1, n_frames // 5)
    noise_floor = float(np.percentile(rms[:pre_n], 75))
    threshold = max(noise_floor * 3.0, 1e-4)

    # Word start in frame index — search backward from here
    word_frame = min(int((word_start - win_start) / (frame_ms / 1000)), n_frames - 1)

    # Find the last frame below threshold before word_start
    last_silence_frame = None
    for i in range(word_frame, -1, -1):
        if rms[i] < threshold:
            last_silence_frame = i
            break

    if last_silence_frame is None:
        return word_start  # continuous speech up to word — keep original

    onset_frame = last_silence_frame + 1
    onset_time = win_start + onset_frame * (frame_ms / 1000)
    return round(max(win_start, min(word_start, onset_time)), 3)


def refine_words(words: list[dict], audio_path: str,
                 lookback: float = 1.0) -> tuple[list[dict], int]:
    """
    Run onset refinement on all word timestamps. Returns (refined_words, n_adjusted).
    Loads audio once, processes all words in a single pass.
    """
    try:
        try:
            import soundfile as sf
            audio, sr = sf.read(audio_path, dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
        except ImportError:
            import wave, array
            with wave.open(audio_path, "rb") as wf:
                sr = wf.getframerate()
                n_frames = wf.getnframes()
                raw = wf.readframes(n_frames)
                sampwidth = wf.getsampwidth()
                typecode = "h" if sampwidth == 2 else "b"
                samples = array.array(typecode, raw)
                audio = np.array(samples, dtype=np.float32) / (2 ** (sampwidth * 8 - 1))
    except Exception as e:
        print(f"[index-clip] onset refinement: could not load audio ({e}) — skipping",
              file=sys.stderr)
        return words, 0

    refined = []
    n_adjusted = 0
    for w in words:
        original = w["start"]
        improved = refine_word_onset(audio, sr, original, lookback=lookback)
        if improved < original - 0.005:  # only count meaningful shifts
            n_adjusted += 1
        refined.append({**w, "start": improved})
    return refined, n_adjusted


def detect_silences(audio_path: str, threshold_db: float, min_duration: float) -> list[dict]:
    cmd = ["ffmpeg", "-hide_banner", "-i", audio_path,
           "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    starts = re.findall(r"silence_start: ([\d.]+)", result.stderr)
    ends = re.findall(r"silence_end: ([\d.]+)", result.stderr)
    return [
        {"start": float(s), "end": float(e), "duration": round(float(e) - float(s), 6)}
        for s, e in zip(starts, ends)
    ]


def clips_from_silences(
    silences: list[dict], min_clip: float, duration: float
) -> tuple[list[dict], list[dict]]:
    """
    Derive keep-segments from silences[] using per-silence pad_before/pad_after.
    Returns (kept_clips, dropped_clips) as manifest-ready dicts.
    """
    cuts = []
    for s in silences:
        cut_start = s["start"] + s.get("pad_before", 0.0)
        cut_end = s["end"] - s.get("pad_after", 0.0)
        if cut_end > cut_start:
            cuts.append((round(cut_start, 6), round(cut_end, 6)))

    segments: list[tuple[float, float]] = []
    prev = 0.0
    for cut_s, cut_e in cuts:
        if cut_s > prev:
            segments.append((prev, cut_s))
        prev = cut_e
    if prev < duration:
        segments.append((prev, duration))

    kept_clips = []
    dropped_clips = []
    clip_idx = 0
    for s, e in segments:
        dur = round(e - s, 3)
        entry = {"source_start": round(s, 3), "source_end": round(e, 3), "duration": dur}
        if dur >= min_clip:
            entry["index"] = clip_idx
            kept_clips.append(entry)
            clip_idx += 1
        else:
            dropped_clips.append(entry)

    return kept_clips, dropped_clips


def main():
    parser = argparse.ArgumentParser(description="Index a source clip — no video export")
    parser.add_argument("source", nargs="?", default=None, help="Source video file (positional)")
    parser.add_argument("--input", default=None, help="Source video file")
    parser.add_argument("--min-silence", type=float, default=0.5)
    parser.add_argument("--min-clip", type=float, default=1.0)
    parser.add_argument("--pad", type=float, default=0.15)
    parser.add_argument("--threshold", type=float, default=-35)
    parser.add_argument("--model", default="base.en")
    parser.add_argument("--language", default="en")
    parser.add_argument("--backend", default="auto",
                        choices=["auto", "whisperx", "faster-whisper", "whisper-cli"],
                        help="Transcription backend (default: auto = whisperx → faster-whisper → whisper-cli)")
    parser.add_argument("--refine-onsets", dest="refine_onsets", action="store_true", default=True,
                        help="Scan amplitude to find actual onset before each word start (default: on)")
    parser.add_argument("--no-refine-onsets", dest="refine_onsets", action="store_false",
                        help="Disable onset refinement")
    parser.add_argument("--onset-lookback", type=float, default=1.0,
                        help="Seconds to look back when refining onsets (default: 1.0)")
    parser.add_argument("--force", action="store_true", help="Full re-index, ignore cached manifest")
    parser.add_argument("--recompute", action="store_true",
                        help="Re-derive clips[] from silences[] using current pad values — no Whisper")
    parser.add_argument("--no-transcript", action="store_true",
                        help="Skip transcription entirely (silence-chop only)")
    parser.add_argument("--output", default=None,
                        help="Output path for clip index YAML (default: _meta/<stem>.yaml next to source)")
    parser.add_argument("--vo-manifest", default=None,
                        help="Also write a vo_manifest.json at this path — compatible with burn-captions.py and align-scenes.py")
    args = parser.parse_args()

    raw_input = args.input or args.source
    if not raw_input:
        parser.error("a source video is required — pass it as a positional arg or with --input")

    input_path = Path(raw_input).resolve()
    if not input_path.exists():
        print(f"[index-clip] Input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        manifest_path = Path(args.output).resolve()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        meta_dir = input_path.parent / "_meta"
        meta_dir.mkdir(exist_ok=True)
        manifest_path = meta_dir / f"{input_path.stem}.yaml"

    # -----------------------------------------------------------------------
    # --recompute: fast path — re-derive clips from silences, no audio/Whisper
    # -----------------------------------------------------------------------
    if args.recompute:
        if not manifest_path.exists():
            print(f"[index-clip] No manifest at {manifest_path} — run without --recompute first",
                  file=sys.stderr)
            sys.exit(1)

        manifest = _load_yaml(manifest_path)
        silences = manifest.get("silences")
        if not silences:
            print("[index-clip] Manifest has no silences[] — run without --recompute to full-index",
                  file=sys.stderr)
            sys.exit(1)

        min_clip = manifest.get("index_params", {}).get("min_clip", args.min_clip)
        duration = manifest.get("duration_s", 0.0)

        kept, dropped = clips_from_silences(silences, min_clip, duration)
        manifest["clips"] = kept
        manifest["dropped_clips"] = dropped
        manifest_path.write_text(_dump_yaml(manifest))

        total_kept = sum(c["duration"] for c in kept)
        print(f"[index-clip] Recomputed {len(kept)} clip(s) → {total_kept:.2f}s")
        for c in kept:
            print(f"  [{c['index']}] {c['source_start']:.3f}s – {c['source_end']:.3f}s ({c['duration']:.3f}s)")
        if dropped:
            print(f"[index-clip] Dropped {len(dropped)} short clip(s)")
        print(f"[index-clip] Manifest updated: {manifest_path}")
        return

    # -----------------------------------------------------------------------
    # Cache check — skip if manifest already has silences[] and transcript
    # -----------------------------------------------------------------------
    if manifest_path.exists() and not args.force:
        existing = _load_yaml(manifest_path)
        has_silences = bool(existing.get("silences"))
        has_transcript = existing.get("transcript") is not None
        if has_silences and has_transcript:
            print(f"[index-clip] Using cached manifest: {manifest_path}")
            print(f"[index-clip] (Run with --force to re-index, --recompute to adjust clips)")
            print(f"\n--- Transcript ---\n{existing['transcript']}\n------------------")
            print(f"[index-clip] {len(existing.get('clips', []))} clip(s) in manifest.")
            return
        elif has_silences and args.no_transcript:
            print(f"[index-clip] Manifest exists with silences. Use --recompute to adjust clips.")
            return

    print(f"[index-clip] Source:   {input_path}")
    print(f"[index-clip] Manifest: {manifest_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = str(Path(tmpdir) / "audio.wav")

        print("[index-clip] Extracting audio...")
        try:
            extract_audio(str(input_path), audio_path)
        except Exception:
            sys.exit(1)

        duration = get_duration(audio_path)
        print(f"[index-clip] Duration: {duration:.2f}s")

        # Transcription
        transcript = ""
        words: list[dict] = []
        if not args.no_transcript:
            try:
                transcript, words = transcribe(audio_path, args.model, args.language, tmpdir,
                                               backend=args.backend)
            except Exception as e:
                print(f"[index-clip] Transcription failed ({e}) — continuing without transcript",
                      file=sys.stderr)

        if transcript:
            print(f"\n--- Transcript ---\n{transcript}\n------------------\n")
        elif args.no_transcript:
            print("[index-clip] Skipping transcription (--no-transcript)")
        else:
            print("[index-clip] No transcript produced.")

        # Onset refinement — scan amplitude around each word start
        if words and args.refine_onsets:
            words, n_adj = refine_words(words, audio_path, lookback=args.onset_lookback)
            print(f"[index-clip] Onset refinement: {n_adj}/{len(words)} word(s) adjusted")

        # Silence detection
        silences_raw = detect_silences(audio_path, args.threshold, args.min_silence)
        if not silences_raw:
            fallback = args.threshold + 5
            print(f"[index-clip] No silences at {args.threshold}dB, retrying at {fallback}dB...")
            silences_raw = detect_silences(audio_path, fallback, args.min_silence)

        print(f"[index-clip] {len(silences_raw)} silence region(s) detected")

    # Build silences[] with per-silence pad fields (editable in manifest)
    silences_manifest = [
        {
            "index": i,
            "start": round(s["start"], 3),
            "end": round(s["end"], 3),
            "duration": round(s["duration"], 3),
            "pad_before": args.pad,
            "pad_after": args.pad,
        }
        for i, s in enumerate(silences_raw)
    ]

    # Derive clips from silences
    kept_clips, dropped_clips = clips_from_silences(silences_manifest, args.min_clip, duration)

    total_kept = sum(c["duration"] for c in kept_clips)
    print(f"[index-clip] {len(kept_clips)} clip(s) kept → {total_kept:.2f}s of {duration:.2f}s")
    if dropped_clips:
        print(f"[index-clip] Dropped {len(dropped_clips)} short segment(s) (< {args.min_clip}s)")

    manifest = {
        "format": "clip-index",
        "source": str(input_path),
        "indexed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(duration, 3),
        "index_params": {
            "min_silence": args.min_silence,
            "min_clip": args.min_clip,
            "pad": args.pad,
            "threshold": args.threshold,
            "whisper_model": args.model,
            "backend": args.backend,
            "refine_onsets": args.refine_onsets,
            "onset_lookback": args.onset_lookback,
        },
        "transcript": transcript,
        "words": words,
        "silences": silences_manifest,
        "clips": kept_clips,
        "dropped_clips": dropped_clips,
    }

    manifest_path.write_text(_dump_yaml(manifest))
    print(f"\n[index-clip] Manifest written: {manifest_path}")
    print(f"  Clips:    {len(kept_clips)}")
    print(f"  Words:    {len(words)}")
    print(f"  Silences: {len(silences_manifest)}")

    # --vo-manifest: emit a vo_manifest.json compatible with burn-captions.py / align-scenes.py
    if args.vo_manifest and words:
        vo_manifest_path = Path(args.vo_manifest).resolve()
        vo_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        vo_data = {
            "audio_file": str(input_path),
            "total_duration_s": round(duration, 3),
            "words": [w for w in words if w.get("word")],
        }
        vo_manifest_path.write_text(json.dumps(vo_data, indent=2))
        print(f"[index-clip] vo_manifest written: {vo_manifest_path} ({len(vo_data['words'])} words)")


if __name__ == "__main__":
    main()
