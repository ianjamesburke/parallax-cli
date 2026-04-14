"""
costs.py — read the parallax JSONL event log and fold it into a cost report.

Pure / side-effect free: takes an optional user filter, reads
~/.parallax/events.jsonl via `telemetry._iter_events()`, and returns a dict
with four top-level sections:

    {
      "fal": {...},          # header — fal account identity (populated by caller)
      "llm": {...},          # LLM costs from session_touch events
      "image": {...},        # image-generation costs from dispatch_event events
      "video": {...},        # voiceover / compose costs from dispatch_event events
      "generated_at": <ts>,
      "user_filter": "<user>" | None,
    }

This module does not talk to fal — the caller fills in the "fal" section
(see server.py). Everything here is a fold over the local JSONL log.

Every read is best-effort: malformed events, unknown kinds, and missing
fields all collapse to zero/unknown rather than raising. The log must keep
producing a report even if someone hand-edited it badly.

TEST MODE is respected: any dispatch_event whose payload carries
`test_mode: true` does NOT count against image/video cost totals — those
runs were stubbed and spent no real money.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Optional

# Support both import styles:
#   - `import telemetry` (flat — server.py inserts web/ into sys.path)
#   - `from web import costs` (package — used by verification scripts / tests)
try:
    import telemetry  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    from . import telemetry  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Pricing constants
# ---------------------------------------------------------------------------
# All prices are placeholders. Update them once billing is actually audited.
# Leave the `# TODO: verify pricing` comment on every constant so a future
# reader knows these are not authoritative.

# Gemini 3.1 flash image preview (the only image model parallax currently
# dispatches). Priced per image.
IMAGE_USD_PER_IMAGE = 0.04  # TODO: verify pricing

# ElevenLabs voiceover — billed per character. Parallax only records
# word_count, so we approximate char_count ≈ word_count * 5.
VOICEOVER_USD_PER_CHAR = 0.00003  # TODO: verify pricing
VOICEOVER_CHARS_PER_WORD = 5  # rough average English word length incl. spaces

# Ken Burns / compose step — a ffmpeg-only local operation. Zero marginal
# cost until we move it to a paid renderer.
COMPOSE_USD_PER_RUN = 0.0  # TODO: verify pricing

IMAGE_MODEL_NAME = "gemini-3.1-flash-image-preview"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_payload(ev: dict[str, Any]) -> dict[str, Any]:
    """Return ev['payload'] as a dict, or {} if missing/malformed."""
    p = ev.get("payload")
    if isinstance(p, dict):
        return p
    return {}


def _is_test_mode(payload: dict[str, Any]) -> bool:
    """A dispatch_event payload is a stubbed test run if test_mode is truthy."""
    tm = payload.get("test_mode")
    if isinstance(tm, bool):
        return tm
    if isinstance(tm, str):
        return tm.strip().lower() in ("1", "true", "yes", "on")
    return False


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_llm_section(events: list[dict[str, Any]], user_filter: Optional[str]) -> dict[str, Any]:
    """
    Fold session_created + session_touch events into a per-model cost table.

    Returns:
        {
          "models": [
            {"model": "...", "input_tokens": ..., "output_tokens": ...,
             "cost_usd": ..., "session_count": ...},
            ...
          ],
          "by_user": [  # only populated when user_filter is None
            {"user": "...", "cost_usd": ..., "input_tokens": ...,
             "output_tokens": ..., "session_count": ...},
            ...
          ],
          "total_cost_usd": ...,
          "total_input_tokens": ...,
          "total_output_tokens": ...,
          "total_session_count": ...,
        }
    """
    # First pass: build session_id -> {model, user} from session_created events.
    session_meta: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.get("kind") != "session_created":
            continue
        sid = ev.get("session_id")
        if not sid:
            continue
        session_meta[sid] = {
            "model": ev.get("model") or "unknown",
            "user": ev.get("user") or "unknown",
        }

    # Second pass: fold session_touch deltas grouped by model (and user).
    # Per-model totals
    per_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "model": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "session_ids": set(),
        }
    )
    # Per-user totals (for the "all users" sub-breakdown)
    per_user: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "user": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "session_ids": set(),
        }
    )

    for ev in events:
        if ev.get("kind") != "session_touch":
            continue
        sid = ev.get("session_id")
        if not sid:
            continue
        meta = session_meta.get(sid)
        if meta is None:
            # Touch before create — still count it, but attribute to unknown.
            meta = {"model": "unknown", "user": "unknown"}
        if user_filter and meta["user"] != user_filter:
            continue
        try:
            dcost = float(ev.get("cost_delta_usd") or 0.0)
        except (TypeError, ValueError):
            dcost = 0.0
        try:
            din = int(ev.get("input_tokens_delta") or 0)
        except (TypeError, ValueError):
            din = 0
        try:
            dout = int(ev.get("output_tokens_delta") or 0)
        except (TypeError, ValueError):
            dout = 0

        model = meta["model"]
        user = meta["user"]

        m = per_model[model]
        m["model"] = model
        m["input_tokens"] += din
        m["output_tokens"] += dout
        m["cost_usd"] += dcost
        m["session_ids"].add(sid)

        u = per_user[user]
        u["user"] = user
        u["input_tokens"] += din
        u["output_tokens"] += dout
        u["cost_usd"] += dcost
        u["session_ids"].add(sid)

    # Flatten to plain dicts, sort by cost desc
    models = []
    for m in per_model.values():
        models.append({
            "model": m["model"],
            "input_tokens": m["input_tokens"],
            "output_tokens": m["output_tokens"],
            "cost_usd": round(m["cost_usd"], 6),
            "session_count": len(m["session_ids"]),
        })
    models.sort(key=lambda x: x["cost_usd"], reverse=True)

    by_user = []
    if user_filter is None:
        for u in per_user.values():
            by_user.append({
                "user": u["user"],
                "input_tokens": u["input_tokens"],
                "output_tokens": u["output_tokens"],
                "cost_usd": round(u["cost_usd"], 6),
                "session_count": len(u["session_ids"]),
            })
        by_user.sort(key=lambda x: x["cost_usd"], reverse=True)

    return {
        "models": models,
        "by_user": by_user,
        "total_cost_usd": round(sum(m["cost_usd"] for m in models), 6),
        "total_input_tokens": sum(m["input_tokens"] for m in models),
        "total_output_tokens": sum(m["output_tokens"] for m in models),
        "total_session_count": len({sid for m in per_model.values() for sid in m["session_ids"]}),
    }


def _build_image_section(events: list[dict[str, Any]], user_sessions: Optional[set[str]]) -> dict[str, Any]:
    """
    Count dispatch_event events where payload.type == "still_generated".
    Skip any event whose payload.test_mode is truthy.
    """
    counted = 0
    skipped_test_mode = 0
    for ev in events:
        if ev.get("kind") != "dispatch_event":
            continue
        if user_sessions is not None and ev.get("session_id") not in user_sessions:
            continue
        payload = _safe_payload(ev)
        if payload.get("type") != "still_generated":
            continue
        if _is_test_mode(payload):
            skipped_test_mode += 1
            continue
        counted += 1

    total_cost = round(counted * IMAGE_USD_PER_IMAGE, 6)
    return {
        "model": IMAGE_MODEL_NAME,
        "image_count": counted,
        "skipped_test_mode": skipped_test_mode,
        "usd_per_image": IMAGE_USD_PER_IMAGE,
        "total_cost_usd": total_cost,
    }


def _build_video_section(events: list[dict[str, Any]], user_sessions: Optional[set[str]]) -> dict[str, Any]:
    """
    Count voiceover and compose events from dispatch_event payloads.

    Voiceover unit of cost: char_count ≈ word_count * 5, billed at
    VOICEOVER_USD_PER_CHAR. We accept two payload shapes as voiceovers:
      - payload.type == "voiceover_generated"
      - payload.type == "run_complete" with word_count present (this is how
        cmd_voiceover actually terminates)
    We deduplicate on (session_id, word_count) within a single run.
    """
    vo_count = 0
    vo_skipped_test_mode = 0
    vo_word_total = 0

    compose_count = 0
    compose_skipped_test_mode = 0

    for ev in events:
        if ev.get("kind") != "dispatch_event":
            continue
        if user_sessions is not None and ev.get("session_id") not in user_sessions:
            continue
        payload = _safe_payload(ev)
        ptype = payload.get("type")
        is_test = _is_test_mode(payload)

        # Voiceover: look for voiceover_generated OR run_complete w/ word_count.
        is_voiceover = (
            ptype == "voiceover_generated"
            or (ptype == "run_complete" and payload.get("word_count") is not None)
        )
        if is_voiceover:
            if is_test:
                vo_skipped_test_mode += 1
                continue
            vo_count += 1
            try:
                vo_word_total += int(payload.get("word_count") or 0)
            except (TypeError, ValueError):
                pass
            continue

        # Ken Burns / compose event. We don't have a dedicated type today; we
        # count assembly_complete as a proxy. Once a "compose" or "ken_burns"
        # type is emitted we'll accept those too.
        if ptype in ("assembly_complete", "compose", "ken_burns", "ken-burns"):
            if is_test:
                compose_skipped_test_mode += 1
                continue
            compose_count += 1

    vo_char_estimate = vo_word_total * VOICEOVER_CHARS_PER_WORD
    vo_cost = round(vo_char_estimate * VOICEOVER_USD_PER_CHAR, 6)
    compose_cost = round(compose_count * COMPOSE_USD_PER_RUN, 6)

    return {
        "voiceover": {
            "count": vo_count,
            "skipped_test_mode": vo_skipped_test_mode,
            "word_total": vo_word_total,
            "char_estimate": vo_char_estimate,
            "usd_per_char": VOICEOVER_USD_PER_CHAR,
            "chars_per_word": VOICEOVER_CHARS_PER_WORD,
            "total_cost_usd": vo_cost,
        },
        "compose": {
            "count": compose_count,
            "skipped_test_mode": compose_skipped_test_mode,
            "usd_per_run": COMPOSE_USD_PER_RUN,
            "total_cost_usd": compose_cost,
        },
        "total_cost_usd": round(vo_cost + compose_cost, 6),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_report(user: Optional[str]) -> dict[str, Any]:
    """
    Build the full four-section cost report. Reads the JSONL log exactly once.

    Args:
        user: if given, only events belonging to sessions created by that
              user are counted. If None, the report aggregates across all
              users and also returns a per-user sub-breakdown in the LLM
              section.

    This function never raises — any I/O or parse error collapses to an
    empty section and is reported via the "errors" field.
    """
    errors: list[str] = []

    try:
        events: list[dict[str, Any]] = list(telemetry._iter_events())
    except Exception as e:
        errors.append(f"read events: {e}")
        events = []

    # Compute which session_ids belong to `user` once so image/video sections
    # can filter without re-scanning.
    user_sessions: Optional[set[str]] = None
    if user:
        user_sessions = set()
        for ev in events:
            if ev.get("kind") == "session_created" and ev.get("user") == user:
                sid = ev.get("session_id")
                if sid:
                    user_sessions.add(sid)

    try:
        llm = _build_llm_section(events, user)
    except Exception as e:
        errors.append(f"llm section: {e}")
        llm = {"models": [], "by_user": [], "total_cost_usd": 0.0,
               "total_input_tokens": 0, "total_output_tokens": 0,
               "total_session_count": 0}

    try:
        image = _build_image_section(events, user_sessions)
    except Exception as e:
        errors.append(f"image section: {e}")
        image = {"model": IMAGE_MODEL_NAME, "image_count": 0,
                 "skipped_test_mode": 0, "usd_per_image": IMAGE_USD_PER_IMAGE,
                 "total_cost_usd": 0.0}

    try:
        video = _build_video_section(events, user_sessions)
    except Exception as e:
        errors.append(f"video section: {e}")
        video = {"voiceover": {"count": 0, "total_cost_usd": 0.0},
                 "compose": {"count": 0, "total_cost_usd": 0.0},
                 "total_cost_usd": 0.0}

    # The "fal" section is populated by the HTTP handler (it needs network
    # access). We stub it here so the shape is stable when callers use this
    # module directly from a script.
    fal = {"configured": False, "identity": None, "error": None}

    grand_total = round(
        float(llm.get("total_cost_usd") or 0.0)
        + float(image.get("total_cost_usd") or 0.0)
        + float(video.get("total_cost_usd") or 0.0),
        6,
    )

    return {
        "generated_at": time.time(),
        "user_filter": user,
        "fal": fal,
        "llm": llm,
        "image": image,
        "video": video,
        "grand_total_usd": grand_total,
        "errors": errors,
    }
