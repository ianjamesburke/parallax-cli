#!/usr/bin/env python3
"""
sanitize-path.py — Return a shell-safe path for a source file.

If the filename contains non-ASCII whitespace (e.g. narrow no-break space
\u202f, common in macOS screen recordings), creates a symlink with a clean
ASCII name and prints that path instead. Otherwise prints the original path.

Usage:
    python3 sanitize-path.py /path/to/file.mov
    # prints a safe path — use it in place of the original

Exit codes: 0 on success, 1 on error.
"""

import os
import re
import sys
import unicodedata


def has_non_ascii_whitespace(s: str) -> bool:
    return any(unicodedata.category(c) == "Zs" and c != " " for c in s)


def sanitize(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)

    filename = os.path.basename(path)
    if not has_non_ascii_whitespace(filename):
        print(path)
        return path

    safe_name = re.sub(r"\s+", "_", filename)
    safe_name = re.sub(r"[^\x20-\x7E]", "_", safe_name).strip("_")
    safe_path = os.path.join(os.path.dirname(path), safe_name)

    if not os.path.exists(safe_path):
        try:
            os.symlink(path, safe_path)
        except OSError as e:
            print(f"ERROR: Could not create symlink {safe_path}: {e}", file=sys.stderr)
            sys.exit(1)

    print(safe_path)
    return safe_path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <file-path>", file=sys.stderr)
        sys.exit(1)
    sanitize(sys.argv[1])
