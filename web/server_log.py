"""
server_log.py — thin append-only lifecycle log for parallax-web.

Scope: daemon-level events only (startup, shutdown, registry failures,
uncaught request exceptions). Conversation turns, tool calls, and tool
results stay in the per-project `chat.jsonl` — do not write them here.

All writes swallow exceptions: a logger failure must never take down the
server. Stderr remains the primary channel for interactive feedback; this
log exists so post-mortem debugging of boot-time failures is possible.
"""
from __future__ import annotations

import json
import time
import traceback as _traceback
from pathlib import Path
from typing import Any

LOG_PATH = Path.home() / ".parallax" / "server.log"


def _write(record: dict[str, Any]) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def log(event: str, **fields: Any) -> None:
    _write({"ts": time.time(), "event": event, **fields})


def log_exception(event: str, exc: BaseException, **fields: Any) -> None:
    tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
    tb_lines = tb.strip().splitlines()
    short_tb = "\n".join(tb_lines[-20:])
    _write({
        "ts": time.time(),
        "event": event,
        "error_class": type(exc).__name__,
        "error": str(exc),
        "traceback": short_tb,
        **fields,
    })
