"""
WhisperX wrapper for voice memo transcription.
Caches results — never re-transcribes the same file.
Falls back to faster-whisper if whisperx is unavailable.
"""

import json
import hashlib
from pathlib import Path
CACHE_DIR = Path("logs/transcripts")


def _file_hash(audio_path: str) -> str:
    """SHA256 hash of file contents for cache keying."""
    h = hashlib.sha256()
    try:
        with open(audio_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        raise RuntimeError(f"[transcription] Could not hash {audio_path}: {e}") from e


def _cache_path(audio_path: str) -> Path:
    """Return the cache file path for a given audio file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    file_hash = _file_hash(audio_path)
    return CACHE_DIR / f"{file_hash}.json"


def transcribe(audio_path: str) -> dict:
    """
    Transcribe audio to text using WhisperX (or faster-whisper fallback).
    Returns: {text: str, words: [{word, start, end}], segments: list}

    Caches result by file hash — calling again with the same file returns cached result instantly.
    """
    cache = _cache_path(audio_path)

    # Return cached result if available
    if cache.exists():
        try:
            with open(cache) as f:
                result = json.load(f)
            print(f"[transcription] Cache hit for {audio_path}")
            return result
        except Exception as e:
            print(f"[transcription] WARNING: Could not read cache {cache}: {e} — re-transcribing")

    # Try whisperx first, fall back to faster-whisper
    result = None
    try:
        result = _transcribe_whisperx(audio_path)
    except ImportError:
        print("[transcription] whisperx not available — falling back to faster-whisper")
        try:
            result = _transcribe_faster_whisper(audio_path)
        except ImportError as e:
            raise RuntimeError(
                "[transcription] Neither whisperx nor faster-whisper is installed. "
                "Run: pip install whisperx  OR  pip install faster-whisper"
            ) from e
        except Exception as e:
            raise RuntimeError(f"[transcription] faster-whisper failed for {audio_path}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"[transcription] whisperx failed for {audio_path}: {e}") from e

    # Cache the result
    save_transcript(result, str(cache))
    return result


def _transcribe_whisperx(audio_path: str) -> dict:
    """Transcribe using whisperx with word-level alignment."""
    import whisperx  # type: ignore

    model = whisperx.load_model("base", device="cpu", compute_type="int8")
    audio = whisperx.load_audio(audio_path)
    raw = model.transcribe(audio, batch_size=16)

    # Word-level alignment
    try:
        align_model, metadata = whisperx.load_align_model(
            language_code=raw["language"], device="cpu"
        )
        aligned = whisperx.align(raw["segments"], align_model, metadata, audio, device="cpu")
        segments = aligned.get("segments", raw["segments"])
    except Exception as e:
        print(f"[transcription] whisperx alignment failed ({e}), using unaligned segments")
        segments = raw["segments"]

    words = []
    for seg in segments:
        for w in seg.get("words", []):
            words.append({"word": w.get("word", ""), "start": w.get("start"), "end": w.get("end")})

    text = " ".join(seg.get("text", "").strip() for seg in segments)
    return {"text": text, "words": words, "segments": segments}


def _transcribe_faster_whisper(audio_path: str) -> dict:
    """Transcribe using faster-whisper (no word alignment)."""
    from faster_whisper import WhisperModel  # type: ignore

    model = WhisperModel("base", device="cpu", compute_type="int8")
    fw_segments, _ = model.transcribe(audio_path, word_timestamps=True)

    segments = []
    words = []
    text_parts = []

    for seg in fw_segments:
        seg_dict = {"start": seg.start, "end": seg.end, "text": seg.text}
        segments.append(seg_dict)
        text_parts.append(seg.text.strip())
        for w in (seg.words or []):
            words.append({"word": w.word, "start": w.start, "end": w.end})

    return {"text": " ".join(text_parts), "words": words, "segments": segments}


def save_transcript(result: dict, output_path: str) -> str:
    """
    Save transcription result as JSON to output_path.
    Returns the output path.
    """
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
        return output_path
    except Exception as e:
        raise RuntimeError(f"[transcription] Failed to save transcript to {output_path}: {e}") from e
