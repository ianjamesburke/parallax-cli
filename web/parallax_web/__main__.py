"""Allow `python3 -m parallax_web` to start the server."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The server module lives in the parent web/ directory alongside this package.
_WEB_DIR = Path(__file__).resolve().parent.parent
if str(_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_DIR))

import server  # noqa: E402


if __name__ == "__main__":
    sys.exit(server.main())
