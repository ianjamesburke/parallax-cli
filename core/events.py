"""
Event emitter for the Parallax CLI.

Default mode: silent no-op (text output from existing prints is unchanged).

JSON mode: emits one JSON object per line to stdout (NDJSON), flushing after
each event, so a parent process can stream the run line-by-line.

All events carry `type`, `ts` (ISO 8601 UTC), and `run_id`. Instrumentation
points call a small number of well-placed `emit()` calls at phase boundaries
(run start/end, agent calls, asset generations, errors). Default behaviour
when json mode is NOT enabled is a total no-op — no prints, no side effects.

Usage:
    from core.events import emitter
    emitter.enable_json("run_id")
    emitter.emit("run_started", brief="...", mode="run")
    emitter.emit("run_complete", output_path="...", duration_s=12.3)
"""

import json
import sys
from datetime import datetime, timezone


class Emitter:
    def __init__(self) -> None:
        self._json = False
        self._run_id: str = ""
        # Stdout stream to use for NDJSON emission. When enable_json() is called
        # we capture the real stdout here, then rebind sys.stdout to sys.stderr
        # so every existing print() call in the pipeline lands on stderr and
        # only our emit() calls can write to the real stdout.
        self._out = sys.stdout

    def enable_json(self, run_id: str) -> None:
        """Enable NDJSON streaming mode for this run. Redirects sys.stdout → sys.stderr
        so pre-existing prints do not corrupt the NDJSON stream; NDJSON is written
        to the original stdout captured here."""
        self._json = True
        self._run_id = run_id
        self._out = sys.stdout
        sys.stdout = sys.stderr

    def set_run_id(self, run_id: str) -> None:
        self._run_id = run_id

    @property
    def json_mode(self) -> bool:
        return self._json

    def emit(self, event_type: str, **fields) -> None:
        """Emit a single NDJSON event. No-op in text mode."""
        if not self._json:
            return
        payload = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
        }
        # Drop None values so consumers don't have to filter them
        for k, v in fields.items():
            if v is not None:
                payload[k] = v
        try:
            self._out.write(json.dumps(payload, default=str) + "\n")
            self._out.flush()
        except Exception:
            # Never let telemetry take down the pipeline
            pass


# Module-level singleton
emitter = Emitter()
