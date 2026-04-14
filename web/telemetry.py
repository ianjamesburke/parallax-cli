"""
telemetry.py — JSONL append log for parallax-web.

One file: ~/.parallax/events.jsonl

Every line is a single JSON object with at minimum `ts`, `session_id`, and
`kind`. All session state (user, project_dir, model, token totals, cost) is
derived at read time by folding events in order. This is a pure append-only
event log — no updates, no deletes, no schema migrations.

Why JSONL:
  - `tail -f ~/.parallax/events.jsonl` streams live activity
  - `cat events.jsonl | jq` for ad-hoc queries
  - No sqlite dependency
  - Trivial to rsync between machines for centralized logging
  - At parallax scale (<30 MB/month per user) linear scans are ~100ms — fine

Event kinds used by the app (not a closed set — any string is allowed):
  session_created       {user, project_dir, model}
  session_touch         {cost_delta_usd, input_tokens_delta, output_tokens_delta}
  user_message          {text}
  assistant_text        {text}
  tool_use              {id, name, input}
  tool_result           {id, name, summary}
  dispatch_start        {mode, brief_preview, ...}
  dispatch_event        {type, ...}
  dispatch_complete     {rc, output_path}
  dispatch_error        {error}
  anthropic_usage       {model, input_tokens, output_tokens, cost_usd}
  error                 {where, message}

Every function here is best-effort — it logs write failures to stderr but
never crashes the caller.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional


LOG_PATH = Path(os.path.expanduser("~/.parallax/events.jsonl"))

# A single process-wide lock around append writes. POSIX guarantees that
# writes smaller than PIPE_BUF (4KB on macOS) are atomic even without locks,
# but agent threads can emit events bigger than that (image summaries,
# serialized tool payloads) so we lock explicitly.
_lock = threading.Lock()


def _get_user() -> str:
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USER", "unknown")


def init_db() -> None:
    """
    Ensure the log directory + file exist. Returns nothing — the name is kept
    for compatibility with the old SQLite API call sites.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not LOG_PATH.exists():
            LOG_PATH.touch()
    except Exception as e:
        print(
            f"telemetry: failed to init log at {LOG_PATH}: {e}",
            file=sys.stderr,
            flush=True,
        )
        raise


def _append(event: dict[str, Any]) -> None:
    """Append one JSON line to the log. Best-effort; logs failures to stderr."""
    try:
        init_db()
    except Exception:
        return
    try:
        line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
    except Exception as e:
        print(
            f"telemetry: serialize failed for kind={event.get('kind')!r}: {e}",
            file=sys.stderr,
            flush=True,
        )
        line = json.dumps({"kind": "_serialize_error", "error": str(e)}) + "\n"
    with _lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            print(
                f"telemetry: write failed ({event.get('kind')!r}): {e}",
                file=sys.stderr,
                flush=True,
            )


def _iter_events() -> Iterator[dict[str, Any]]:
    """
    Yield every event in the log, in append order. Malformed lines are
    skipped with a stderr warning — the log must keep being readable even if
    one line gets corrupted.
    """
    if not LOG_PATH.exists():
        return
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError as e:
                    print(
                        f"telemetry: skipping malformed line {lineno}: {e}",
                        file=sys.stderr,
                        flush=True,
                    )
    except Exception as e:
        print(f"telemetry: read failed: {e}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Writers — match the old SQLite API so server.py doesn't need big edits
# ---------------------------------------------------------------------------


def create_session(
    session_id: str,
    project_dir: str,
    model: str,
    user: Optional[str] = None,
) -> None:
    _append({
        "ts": time.time(),
        "session_id": session_id,
        "kind": "session_created",
        "user": user or _get_user(),
        "project_dir": project_dir,
        "model": model,
    })


def touch_session(
    session_id: str,
    *,
    cost_delta_usd: float = 0.0,
    input_tokens_delta: int = 0,
    output_tokens_delta: int = 0,
) -> None:
    _append({
        "ts": time.time(),
        "session_id": session_id,
        "kind": "session_touch",
        "cost_delta_usd": float(cost_delta_usd or 0.0),
        "input_tokens_delta": int(input_tokens_delta or 0),
        "output_tokens_delta": int(output_tokens_delta or 0),
    })


def record_event(session_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Append an arbitrary event. `payload` is flattened into the top-level JSON."""
    event = {
        "ts": time.time(),
        "session_id": session_id,
        "kind": kind,
        "payload": payload,
    }
    _append(event)


# ---------------------------------------------------------------------------
# Readers — fold events to compute state at query time
# ---------------------------------------------------------------------------


def _fold_session_totals(session_id: str) -> dict[str, Any]:
    """
    Scan the log and fold every event for a single session into its current
    state (user, project_dir, model, started_at, last_activity_at, totals,
    event_count, first user_message text for preview).
    """
    state: dict[str, Any] = {
        "id": session_id,
        "user": None,
        "project_dir": None,
        "model": None,
        "started_at": None,
        "last_activity_at": None,
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "event_count": 0,
        "first_user_message": None,
    }
    for ev in _iter_events():
        if ev.get("session_id") != session_id:
            continue
        ts = ev.get("ts") or 0.0
        kind = ev.get("kind") or ""
        state["event_count"] += 1
        if state["started_at"] is None:
            state["started_at"] = ts
        state["last_activity_at"] = ts

        if kind == "session_created":
            state["user"] = ev.get("user") or state["user"]
            state["project_dir"] = ev.get("project_dir") or state["project_dir"]
            state["model"] = ev.get("model") or state["model"]
        elif kind == "session_touch":
            state["total_cost_usd"] += float(ev.get("cost_delta_usd") or 0.0)
            state["total_input_tokens"] += int(ev.get("input_tokens_delta") or 0)
            state["total_output_tokens"] += int(ev.get("output_tokens_delta") or 0)
        elif kind == "user_message" and state["first_user_message"] is None:
            payload = ev.get("payload") or {}
            text = payload.get("text") if isinstance(payload, dict) else None
            if text:
                state["first_user_message"] = text
    return state


def list_sessions(limit: int = 50, user: Optional[str] = None) -> list[dict[str, Any]]:
    """
    Return up to `limit` sessions, most recent last_activity_at first.
    If `user` is provided, only sessions belonging to that user are returned.
    Scans the log once and buckets events by session_id.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for ev in _iter_events():
        sid = ev.get("session_id")
        if not sid:
            continue
        s = buckets.get(sid)
        if s is None:
            s = {
                "id": sid,
                "user": None,
                "project_dir": None,
                "model": None,
                "started_at": ev.get("ts"),
                "last_activity_at": ev.get("ts"),
                "total_cost_usd": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "event_count": 0,
                "first_user_message": None,
            }
            buckets[sid] = s
        s["event_count"] += 1
        s["last_activity_at"] = ev.get("ts") or s["last_activity_at"]

        kind = ev.get("kind") or ""
        if kind == "session_created":
            s["user"] = ev.get("user") or s["user"]
            s["project_dir"] = ev.get("project_dir") or s["project_dir"]
            s["model"] = ev.get("model") or s["model"]
        elif kind == "session_touch":
            s["total_cost_usd"] += float(ev.get("cost_delta_usd") or 0.0)
            s["total_input_tokens"] += int(ev.get("input_tokens_delta") or 0)
            s["total_output_tokens"] += int(ev.get("output_tokens_delta") or 0)
        elif kind == "user_message" and s["first_user_message"] is None:
            payload = ev.get("payload") or {}
            text = payload.get("text") if isinstance(payload, dict) else None
            if text:
                s["first_user_message"] = text

    all_sessions = list(buckets.values())
    if user:
        all_sessions = [s for s in all_sessions if s.get("user") == user]
    sessions = sorted(
        all_sessions,
        key=lambda s: s.get("last_activity_at") or 0,
        reverse=True,
    )
    return sessions[:limit]


def delete_session(session_id: str) -> int:
    """
    Remove all events for `session_id` from the log. Rewrites the file in-place.
    Returns the number of lines removed.
    """
    with _lock:
        try:
            lines = LOG_PATH.read_bytes().splitlines(keepends=True)
        except FileNotFoundError:
            return 0
        kept = []
        removed = 0
        for line in lines:
            try:
                ev = json.loads(line)
                if ev.get("session_id") == session_id:
                    removed += 1
                    continue
            except Exception:
                pass
            kept.append(line)
        try:
            LOG_PATH.write_bytes(b"".join(kept))
        except Exception as e:
            print(f"telemetry: delete_session write failed: {e}", file=sys.stderr, flush=True)
        return removed


def load_session_events(session_id: str) -> list[dict[str, Any]]:
    """
    Return every event for a single session, in append order, as
    `[{"ts", "kind", "payload"}, ...]`. Matches the shape the old SQLite
    reader used so server.py's history handler needs no changes.
    """
    out: list[dict[str, Any]] = []
    for ev in _iter_events():
        if ev.get("session_id") != session_id:
            continue
        # Pass through the payload field if it exists, otherwise build one
        # from the top-level fields (for session_created / session_touch
        # where the interesting bits are hoisted to the top level).
        payload = ev.get("payload")
        if payload is None:
            payload = {
                k: v
                for k, v in ev.items()
                if k not in ("ts", "session_id", "kind", "payload")
            }
        out.append({
            "ts": ev.get("ts"),
            "kind": ev.get("kind"),
            "payload": payload,
        })
    return out


def usage_for_user(user: str, since_ts: Optional[float] = None) -> dict[str, Any]:
    """
    Aggregate tokens + cost for every session belonging to `user`. If
    `since_ts` is set, only count events at or after that timestamp.
    """
    total_in = 0
    total_out = 0
    total_cost = 0.0
    session_ids: set[str] = set()

    # First pass: find which session_ids belong to this user.
    user_sessions: set[str] = set()
    for ev in _iter_events():
        if ev.get("kind") == "session_created" and ev.get("user") == user:
            sid = ev.get("session_id")
            if sid:
                user_sessions.add(sid)

    # Second pass: sum session_touch deltas for those session ids, filtered by ts.
    for ev in _iter_events():
        if ev.get("kind") != "session_touch":
            continue
        sid = ev.get("session_id")
        if sid not in user_sessions:
            continue
        ts = ev.get("ts") or 0.0
        if since_ts is not None and ts < since_ts:
            continue
        total_in += int(ev.get("input_tokens_delta") or 0)
        total_out += int(ev.get("output_tokens_delta") or 0)
        total_cost += float(ev.get("cost_delta_usd") or 0.0)
        session_ids.add(sid)

    return {
        "cost_usd": total_cost,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "session_count": len(session_ids),
    }
