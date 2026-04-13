#!/usr/bin/env python3
"""
Generate lipsync data from an audio file for use with render-animation.py.

Runs WhisperX (phoneme-level forced alignment) for accurate word boundaries,
then computes per-frame mouth and speaking arrays for animation templates.
Falls back to faster-whisper if whisperx is not installed.

WhisperX is preferred because it uses wav2vec2 forced alignment — word boundaries
are tight and silence is not attributed to the nearest word. This matters for
silence detection: a 5s pause after "one" shows as a real gap, not "one" having
a 5s duration.

Output is a JSON sidecar with:
  mouth    — per-frame 0-1 open amount (primary mouth driver)
  speaking — per-frame 0-1 envelope for body animation
  words    — list of {start, end, word} dicts with phoneme-aligned timestamps
  duration — clip duration in seconds
  fps      — frames per second
  mode     — which envelope was computed

Usage:
  python3 generate-lipsync.py --audio vo.wav
  python3 generate-lipsync.py --input vo.wav --output vo.lipsync.json --mode word
  python3 generate-lipsync.py --audio vo.wav --mode amplitude

Flags --audio and --input are aliases; both accepted.
"""
import argparse
import json
import math
import os
import struct
import sys
import warnings
import wave
from pathlib import Path

# Suppress WhisperX / torchcodec FFmpeg version mismatch warning (40+ lines, non-fatal)
os.environ["TORCHAUDIO_NO_BACKEND_CHECK"] = "1"
warnings.filterwarnings("ignore", message=".*torchcodec.*")
warnings.filterwarnings("ignore", message=".*FFmpeg.*version.*mismatch.*")

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402


DEFAULT_FPS        = 30
DEFAULT_LOOK_AHEAD = 2    # frames — shift envelope earlier to compensate render lag
DEFAULT_ATTACK     = 4    # frames to ramp open at word start
DEFAULT_CLOSE      = 5    # frames to ramp closed at word end
DEFAULT_BOUNDARY   = 3    # frames of forced dip between back-to-back words


def read_amplitude(wav_path: str, fps: int) -> list[float]:
    """Read per-frame RMS amplitude from a WAV file, normalized 0-1."""
    f = wave.open(wav_path)
    sr = f.getframerate()
    spvf = sr // fps  # samples per video frame
    n = f.getnframes()
    n_frames = int(n / spvf)

    raw = []
    for _ in range(n_frames):
        data = f.readframes(spvf)
        samples = struct.unpack(f"<{len(data)//2}h", data)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0
        raw.append(rms)
    f.close()

    mn, mx = min(raw), max(raw)
    return [(v - mn) / (mx - mn) if mx > mn else 0.0 for v in raw]


def amplitude_envelope(amp: list[float], look_ahead: int) -> tuple[list[float], list[float]]:
    """Envelope follower: fast attack, slow decay. Good for dense/continuous speech."""
    n = len(amp)
    env = [0.0] * n
    env[0] = amp[0]
    for i in range(1, n):
        if amp[i] > env[i - 1]:
            env[i] = 0.5 * env[i - 1] + 0.5 * amp[i]   # fast attack
        else:
            env[i] = 0.80 * env[i - 1] + 0.20 * amp[i]  # slow decay

    # Apply sqrt curve — opens mouth more aggressively at mid values
    mouth_raw = [min(1.0, math.sqrt(v) * 1.2) for v in env]

    # Shift earlier to compensate render lag
    mouth = mouth_raw[look_ahead:] + [0.0] * look_ahead

    # Speaking: heavily smoothed version of mouth for body animation
    body = [0.0] * n
    body[0] = mouth[0]
    for i in range(1, n):
        if mouth[i] > body[i - 1]:
            body[i] = 0.4 * body[i - 1] + 0.6 * mouth[i]
        else:
            body[i] = 0.88 * body[i - 1]

    return ([round(v, 3) for v in mouth],
            [round(v, 3) for v in body])


def word_envelope(words: list[dict], n_frames: int, fps: int,
                  attack: int, close: int, boundary: int,
                  look_ahead: int) -> tuple[list[float], list[float]]:
    """Word-triggered open/close cycles. Each word gets its own ramp-up + hold + decay.

    Consecutive words get a forced brief dip between them so each word reads as
    a distinct mouth movement even when there's no gap in the audio.
    """
    # Build adjusted word schedule (insert boundary dips between adjacent words)
    schedule = []
    for i, w in enumerate(words):
        fs = int(w["start"] * fps)
        fe = int(w["end"]   * fps)
        if i > 0:
            prev_end = int(words[i - 1]["end"] * fps)
            if fs - prev_end < boundary:
                fs = prev_end + boundary  # push start forward to create dip
        schedule.append((fs, fe))

    env = [0.0] * n_frames

    for fs, fe in schedule:
        for fi in range(n_frames):
            frames_in  = fi - fs
            frames_out = fi - fe
            if fi >= fs and fi < fs + attack:
                # Ramp up
                env[fi] = max(env[fi], frames_in / attack)
            elif fi >= fs + attack and fi <= fe:
                # Hold open
                env[fi] = max(env[fi], 1.0)
            elif fi > fe and fi <= fe + close:
                # Decay (only if next word hasn't started)
                next_start = schedule[schedule.index((fs, fe)) + 1][0] \
                    if (fs, fe) != schedule[-1] else n_frames + 1
                if fi < next_start:
                    env[fi] = max(env[fi], 1.0 - (frames_out / close))

    # Shift earlier to compensate render lag
    mouth = env[look_ahead:] + [0.0] * look_ahead

    # Speaking: smoothed for body animation
    body = [0.0] * n_frames
    body[0] = mouth[0]
    for i in range(1, n_frames):
        if mouth[i] > body[i - 1]:
            body[i] = 0.4 * body[i - 1] + 0.6 * mouth[i]
        else:
            body[i] = 0.85 * body[i - 1]

    return ([round(v, 3) for v in mouth],
            [round(v, 3) for v in body])


def transcribe_whisperx(wav_path: str) -> list[dict]:
    """Run WhisperX with phoneme-level forced alignment — accurate word boundaries, no silence
    attributed to the nearest word. Falls back to faster-whisper if not installed."""
    try:
        import whisperx
        print("[lipsync] Transcribing with whisperx (phoneme alignment)...")
        device = "cpu"
        whisper_model = config.get("transcription.model", "base.en")
        model = whisperx.load_model(whisper_model, device, compute_type="int8", language="en")
        audio = whisperx.load_audio(wav_path)
        result = model.transcribe(audio, language="en")

        align_model, metadata = whisperx.load_align_model(language_code="en", device=device)
        result = whisperx.align(result["segments"], align_model, metadata, audio, device)

        words = []
        for w in result.get("word_segments", []):
            word_text = w.get("word", "").strip()
            if not word_text:
                continue
            words.append({
                "word":  word_text,
                "start": round(float(w.get("start", 0)), 3),
                "end":   round(float(w.get("end",   0)), 3),
            })
        print(f"[lipsync] whisperx: {len(words)} words with phoneme-aligned boundaries")
        return words
    except ImportError:
        print("[lipsync] whisperx not installed — falling back to faster-whisper", file=sys.stderr)
        return transcribe_faster_whisper(wav_path)
    except Exception as e:
        print(f"[lipsync] whisperx failed ({e}) — falling back to faster-whisper", file=sys.stderr)
        return transcribe_faster_whisper(wav_path)


def transcribe_faster_whisper(wav_path: str) -> list[dict]:
    """Fallback: faster-whisper with cross-attention word timestamps."""
    try:
        from faster_whisper import WhisperModel
        print("[lipsync] Transcribing with faster-whisper...")
        whisper_model = config.get("transcription.model", "base.en")
        model = WhisperModel(whisper_model, device="cpu", compute_type="int8")
        segs, _ = model.transcribe(wav_path, word_timestamps=True)
        words = []
        for seg in segs:
            for w in seg.words:
                words.append({"start": round(w.start, 3),
                               "end":   round(w.end,   3),
                               "word":  w.word.strip()})
        return words
    except ImportError:
        print("[lipsync] faster_whisper not installed — skipping transcription", file=sys.stderr)
        return []


def transcribe(wav_path: str) -> list[dict]:
    """Transcribe audio and return word-level timestamps. Uses WhisperX by default."""
    return transcribe_whisperx(wav_path)


def main():
    parser = argparse.ArgumentParser(description="Generate lipsync data from audio")
    parser.add_argument("--audio", "--input", dest="audio", required=True, help="Input WAV file")
    parser.add_argument("--output",     default=None,   help="Output JSON path (default: <input>.lipsync.json)")
    parser.add_argument("--mode",       default="word", choices=["word", "amplitude"],
                        help="word = word-triggered cycles (better for clear speech); "
                             "amplitude = envelope follower (better for music/dense speech)")
    parser.add_argument("--fps",        type=int,   default=DEFAULT_FPS)
    parser.add_argument("--look-ahead", type=int,   default=DEFAULT_LOOK_AHEAD,
                        help="Frames to shift envelope earlier (compensates render lag, default: 2)")
    parser.add_argument("--attack",     type=int,   default=DEFAULT_ATTACK,
                        help="Frames to ramp mouth open at word start (word mode, default: 4)")
    parser.add_argument("--close",      type=int,   default=DEFAULT_CLOSE,
                        help="Frames to ramp mouth closed after word end (word mode, default: 5)")
    parser.add_argument("--boundary",   type=int,   default=DEFAULT_BOUNDARY,
                        help="Forced dip frames between adjacent words (word mode, default: 3)")
    parser.add_argument("--manifest",   default=None,
                        help="Path to clip-index YAML manifest — uses pre-computed word timestamps "
                             "instead of re-transcribing (skips faster-whisper)")
    parser.add_argument("--print",      action="store_true",
                        help="Print arrays to stdout (for quick inspection)")
    parser.add_argument("--vo-manifest", default=None,
                        help="Also write a vo_manifest.json at this path (compatible with align-scenes.py). "
                             "Optionally pass --vo-manifest-ref to inherit voice metadata from an existing manifest.")
    parser.add_argument("--vo-manifest-ref", default=None,
                        help="Path to existing project manifest.yaml — copies voice_id, voice_name, model_id "
                             "into the vo_manifest output. If omitted, voice fields are left empty.")
    args = parser.parse_args()

    wav_path = args.audio
    out_path = args.output or str(Path(wav_path).with_suffix(".lipsync.json"))

    # Get clip duration
    with wave.open(wav_path) as f:
        n_frames_audio = f.getnframes()
        sr = f.getframerate()
        duration = n_frames_audio / sr
    n_frames = int(duration * args.fps)

    print(f"[lipsync] {wav_path}: {duration:.2f}s → {n_frames} frames @ {args.fps}fps")

    # Always read amplitude (needed for amplitude mode; word mode uses whisper)
    amp = read_amplitude(wav_path, args.fps)

    words = []
    if args.mode == "word":
        if args.manifest:
            import yaml
            with open(args.manifest) as f:
                manifest = yaml.safe_load(f)
            raw_words = manifest.get("words", [])
            if not raw_words:
                print("[lipsync] No words in manifest — falling back to amplitude mode", file=sys.stderr)
                args.mode = "amplitude"
            else:
                words = [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in raw_words]
                print(f"[lipsync] Using {len(words)} word(s) from manifest (skipping transcription)")
                for w in words:
                    print(f"  {w['start']:.3f}-{w['end']:.3f}: {w['word']!r}")
        else:
            print("[lipsync] Transcribing with faster-whisper...")
            words = transcribe(wav_path)
            if not words:
                print("[lipsync] No words found — falling back to amplitude mode", file=sys.stderr)
                args.mode = "amplitude"
            else:
                for w in words:
                    print(f"  {w['start']:.3f}-{w['end']:.3f}: {w['word']!r}")

    if args.mode == "word":
        mouth, speaking = word_envelope(
            words, n_frames, args.fps,
            args.attack, args.close, args.boundary, args.look_ahead
        )
    else:
        mouth, speaking = amplitude_envelope(amp, args.look_ahead)

    result = {
        "mouth":    mouth,
        "speaking": speaking,
        "words":    words,
        "duration": round(duration, 3),
        "fps":      args.fps,
        "mode":     args.mode,
    }

    with open(out_path, "w") as f:
        json.dump(result, f)
    print(f"[lipsync] Written: {out_path}")

    # Optionally emit a vo_manifest.json (align-scenes.py compatible)
    if args.vo_manifest and words:
        voice_meta = {"voice_id": "", "voice_name": "", "model_id": ""}
        if args.vo_manifest_ref:
            try:
                import yaml
                with open(args.vo_manifest_ref) as mf:
                    ref = yaml.safe_load(mf)
                vc = ref.get("voice", {})
                voice_meta["voice_id"] = vc.get("voice_id", "")
                voice_meta["voice_name"] = vc.get("voice_name", "")
                voice_meta["model_id"] = vc.get("model_id", "")
            except Exception as e:
                print(f"[lipsync] Warning: could not read voice metadata from {args.vo_manifest_ref}: {e}")

        vo_data = {
            "audio_file": Path(wav_path).with_suffix(".mp3").name,
            "total_duration_s": round(duration, 3),
            "voice_id": voice_meta["voice_id"],
            "voice_name": voice_meta["voice_name"],
            "model_id": voice_meta["model_id"],
            "speedup": 1.0,
            "words": words,
        }
        vo_path = Path(args.vo_manifest)
        vo_path.parent.mkdir(parents=True, exist_ok=True)
        with open(vo_path, "w") as vf:
            json.dump(vo_data, vf, indent=2)
        print(f"[lipsync] vo_manifest written: {vo_path} ({len(words)} words, {duration:.2f}s)")

    if args.print:
        print("\nMouth pattern:")
        for i, v in enumerate(mouth):
            bar = "█" * int(v * 20)
            print(f"  {i:3d} {i/args.fps:.2f}s {bar} {v:.2f}")

    return out_path


if __name__ == "__main__":
    main()
