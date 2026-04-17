"""
cli_errors.py — single helper for every CLI error-path in bin/parallax.

Before this module, bin/parallax had ~40 copies of the same four-step
error ritual scattered across every command:

    try:
        ...
    except Exception as e:
        log.exception("something")
        emitter.emit("error", message=str(e), where="cmd_x.step")
        print(f"[parallax] ERROR: {e}", file=sys.stderr)
        return 1

Some sites printed, some didn't. Some logged, some didn't. Exit codes
drifted between 1 and 2 with no rule. Maintaining any kind of
consistency across the CLI required touching forty places every time.

`fail()` collapses all four side effects into a single call. The
caller's single return statement is the signal that the command is
exiting with an error — everything else happens in one place, and
future changes (a new error format, a telemetry field, a different
logger call) only touch this file.

Exit code convention:
    1  pipeline failure / operation error — the work itself failed
    2  missing config / missing dependency — fix the environment
    3  bad user input — fix the invocation

Usage:

    from core.cli_errors import fail

    def cmd_thing(args):
        log = logging.getLogger("parallax.thing")
        try:
            do_the_work()
        except MissingKey as e:
            return fail(str(e), where="cmd_thing.auth", exit_code=2, log=log)
        except Exception as e:
            return fail(str(e), where="cmd_thing.work", log=log)
        return 0
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

from core.events import emitter


def fail(
    message: str,
    where: str,
    exit_code: int = 1,
    log: Optional[logging.Logger] = None,
    prefix: str = "[parallax]",
) -> int:
    """
    One-line error reporter for CLI commands.

    - Writes a structured `error` event via `emitter.emit(...)` so the
      NDJSON stream + telemetry log both capture it with a stable shape.
    - Logs with traceback via `log.exception(...)` when a logger is
      supplied. Callers inside an `except` block should always pass one.
    - Prints a human-readable line to stderr so interactive users see
      it without having to read the NDJSON stream.
    - Returns `exit_code` so the caller's `return fail(...)` compiles
      cleanly.

    Design notes:

    - The `where` field is mandatory. It has to be a dotted path that
      pinpoints the failing step (e.g. `cmd_create.still_3`). Without
      it, error events in the log are impossible to triage.
    - The helper never raises. If emitter / logging / stderr fail for
      any reason, we still return the exit code — the caller's error
      path must never be blocked by the error reporter itself.
    - `prefix` is a presentation knob. Keep `[parallax]` everywhere so
      scraper tools can grep for it.
    """
    try:
        if log is not None:
            # `.exception()` picks up the current exception's traceback
            # when called from inside an `except` block. If we're not in
            # one (e.g. a guard-clause fail()), it degrades to an error
            # line without a traceback, which is still useful.
            log.exception(message) if sys.exc_info()[0] else log.error(message)
    except Exception:
        pass

    try:
        emitter.emit("error", message=str(message), where=where)
    except Exception:
        pass

    try:
        print(f"{prefix} ERROR: {message}", file=sys.stderr)
    except Exception:
        pass

    return int(exit_code)
