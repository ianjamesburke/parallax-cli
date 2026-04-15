"""
instrumented.py — structured request + cost logging around external calls.

The goal is that every dispatch — real mode or TEST_MODE — emits the SAME
shape of NDJSON events, so:

  1. The costs page can fold projected spend (from cost_estimated events)
     side-by-side with actual spend (from session_touch / anthropic_usage).
  2. TEST_MODE is a usable dry-run cost estimator: the events land in
     events.jsonl even though the provider API was never called.
  3. A future provider-abstraction refactor has a clean seam to bolt
     into — move these two functions into the provider wrapper and
     every CLI call site stays unchanged.

Event kinds:
  request_intended   {provider, model, mode, params}
  cost_estimated     {provider, model, usd, quantity, unit, mode, test_mode}

`mode` is the high-level operation ("image", "voiceover", "compose"...).
`test_mode` is True when the event came from a TEST_MODE branch.
"""
from __future__ import annotations

from typing import Any, Optional

from core.events import emitter
from core import pricing


def emit_request_intended(
    provider: str,
    model: str,
    mode: str,
    params: dict[str, Any],
    test_mode: bool = False,
) -> None:
    """
    Emit a request_intended event describing what we're about to ask the
    provider for. Call this BEFORE the TEST_MODE / real branch so both
    paths capture identical structured data.
    """
    emitter.emit(
        "request_intended",
        provider=provider,
        model=model,
        mode=mode,
        params=params,
        test_mode=bool(test_mode),
    )


def emit_cost_estimated(
    provider: str,
    model: str,
    usd: float,
    quantity: int,
    unit: str,
    mode: str,
    test_mode: bool = False,
) -> None:
    """
    Emit a cost_estimated event for a single provider call. `quantity` is
    the billing unit count (images, characters, tokens), `unit` names it.
    Real and test modes both emit — the costs page folds them into
    projected spend.
    """
    emitter.emit(
        "cost_estimated",
        provider=provider,
        model=model,
        usd=round(float(usd or 0.0), 6),
        quantity=int(quantity or 0),
        unit=unit,
        mode=mode,
        test_mode=bool(test_mode),
    )


# ── high-level convenience: one call to emit both events per dispatch ──


def log_image_generation(
    model: str,
    count: int,
    brief: str,
    aspect_ratio: Optional[str] = None,
    ref_images: Optional[list] = None,
    test_mode: bool = False,
) -> float:
    """
    Emit request_intended + cost_estimated for an image-gen call.
    Returns the estimated USD so callers can print it too.
    """
    params = {
        "count": int(count or 0),
        "aspect_ratio": aspect_ratio,
        "brief": brief,
        "brief_chars": len(brief or ""),
        "ref_images": list(ref_images or []),
    }
    emit_request_intended(
        provider="gemini", model=model, mode="image",
        params=params, test_mode=test_mode,
    )
    usd = pricing.estimate_image_cost(model, int(count or 0))
    emit_cost_estimated(
        provider="gemini", model=model, usd=usd,
        quantity=int(count or 0), unit="image",
        mode="image", test_mode=test_mode,
    )
    return usd


def log_voiceover(
    model: str,
    voice_id: str,
    voice_name: str,
    script_text: str,
    test_mode: bool = False,
) -> float:
    """
    Emit request_intended + cost_estimated for an ElevenLabs call.
    Character count drives the cost — we use the script length verbatim.
    """
    char_count = len(script_text or "")
    params = {
        "voice_id": voice_id,
        "voice_name": voice_name,
        "char_count": char_count,
        "word_count": len((script_text or "").split()),
        "script_preview": (script_text or "")[:120],
    }
    emit_request_intended(
        provider="elevenlabs", model=model, mode="voiceover",
        params=params, test_mode=test_mode,
    )
    usd = pricing.estimate_voiceover_cost(model, char_count)
    emit_cost_estimated(
        provider="elevenlabs", model=model, usd=usd,
        quantity=char_count, unit="char",
        mode="voiceover", test_mode=test_mode,
    )
    return usd
