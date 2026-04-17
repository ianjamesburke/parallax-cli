"""
registry.py — a process-level registry of running parallax-web servers.

Lets multiple servers run side-by-side and lets the web UI (or the CLI)
discover "other workspaces" without having to remember which ports are
in use. The registry lives in the user's home directory, not the project
directory, so it spans every launch dir on the machine.

File: ~/.parallax/servers.json
Shape:
    [
        {
            "pid": 12345,
            "cwd": "/Users/you/somewhere",
            "host": "127.0.0.1",
            "port": 52183,
            "user": "you",
            "started_at": 1776200000.0
        },
        ...
    ]

Writes are best-effort and use a single-shot file lock (os.O_EXCL) to
avoid concurrent writers stomping each other. Readers auto-prune entries
whose pid no longer exists so stale crashed-server rows disappear on
their own.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(os.path.expanduser("~/.parallax/servers.json"))

_self_entry: dict[str, Any] | None = None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_raw() -> list[dict[str, Any]]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        raw = REGISTRY_PATH.read_text(encoding="utf-8")
    except Exception as e:
        print(f"registry: read failed: {e}", file=sys.stderr, flush=True)
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"registry: parse failed, starting fresh: {e}", file=sys.stderr, flush=True)
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _write_raw(entries: list[dict[str, Any]]) -> None:
    try:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = REGISTRY_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        tmp.replace(REGISTRY_PATH)
    except Exception as e:
        print(f"registry: write failed: {e}", file=sys.stderr, flush=True)


def list_servers(prune: bool = True) -> list[dict[str, Any]]:
    """
    Return every recorded server entry. If `prune` is True (default), entries
    whose pid is no longer alive are dropped both from the returned list and
    from disk. Callers that want a raw snapshot can pass prune=False.
    """
    entries = _read_raw()
    if not prune:
        return entries
    alive = [e for e in entries if _pid_alive(int(e.get("pid") or 0))]
    if len(alive) != len(entries):
        _write_raw(alive)
    return alive


def register(cwd: str, host: str, port: int, user: str) -> dict[str, Any]:
    """
    Add this process's entry to the registry. Returns the entry dict stored,
    which the caller should keep so deregister() can find it again by pid.
    """
    global _self_entry
    entry = {
        "pid": os.getpid(),
        "cwd": str(cwd),
        "host": str(host),
        "port": int(port),
        "user": str(user),
        "started_at": time.time(),
    }
    entries = list_servers(prune=True)
    # Drop any previous entry that happens to share our pid (shouldn't happen,
    # but if it does we'd rather overwrite than duplicate).
    entries = [e for e in entries if int(e.get("pid") or 0) != entry["pid"]]
    entries.append(entry)
    _write_raw(entries)
    _self_entry = entry
    return entry


def deregister() -> None:
    """Remove this process's entry from the registry. Safe to call twice."""
    global _self_entry
    entries = _read_raw()
    remaining = [e for e in entries if int(e.get("pid") or 0) != os.getpid()]
    if len(remaining) != len(entries):
        _write_raw(remaining)
    _self_entry = None


def install_shutdown_hooks() -> None:
    """
    Ensure deregister() runs on normal exit AND on SIGINT/SIGTERM. Without
    the signal handler, a Ctrl-C or kill would leave a stale entry behind
    until the next reader prunes it.
    """
    import atexit

    atexit.register(deregister)

    def _on_signal(signum: int, _frame: Any) -> None:
        deregister()
        # Re-raise the default handler so the process actually exits.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError):
            # Non-main thread or unsupported platform — fall back to atexit.
            pass
