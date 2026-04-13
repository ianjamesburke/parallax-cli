"""
File logging for a single Parallax run.

Configures a file handler on the root logger that writes to:
    <PARALLAX_LOG_DIR>/logs/runs/<run_id>/parallax.log

Called once per run from cmd_run / cmd_create after the run_id is known.
Idempotent: repeated calls for the same run_id are a no-op.
"""

import logging
from pathlib import Path

from core.paths import run_dir

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_installed: set = set()


def setup_run_logging(run_id: str) -> Path:
    """
    Install a file handler for this run and return the log file path.
    Safe to call multiple times — will only install once per run_id.
    """
    log_dir = run_dir(run_id)
    log_path = log_dir / "parallax.log"

    if run_id in _installed:
        return log_path

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.setLevel(logging.INFO)

    root = logging.getLogger()
    # If the root has no level set, pin it to INFO so our handler actually gets records.
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)

    _installed.add(run_id)
    logging.getLogger("parallax").info("run logging started run_id=%s log=%s", run_id, log_path)
    return log_path
