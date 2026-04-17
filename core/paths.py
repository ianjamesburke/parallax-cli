"""
Path resolution for Parallax logs, state, and project output.

Logs live in the project directory where Parallax is invoked:
  logs/runs/{run_id}/
  logs/budgets/{concept_id}.json
  logs/trust.json

In beta the legacy `.parallax/` hidden folder was flattened: per-project
state lives at the workspace root directly and run logs live under
`logs/`. See core/project_layout.py for the canonical layout.

Project output goes to the configured output directory:
  ~/Movies/Parallax/{concept_id}/  (default)
"""

import os
from pathlib import Path

import yaml

# ── Config loading ──────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "parallax.yaml"
_config = {}

def _load_config():
    global _config
    if _config:
        return _config
    try:
        with open(_CONFIG_PATH) as f:
            _config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        _config = {}
    return _config


def get_config(key: str, default=None):
    """Dot-notation config lookup: get_config('paths.output')"""
    cfg = _load_config()
    for part in key.split("."):
        if isinstance(cfg, dict):
            cfg = cfg.get(part)
        else:
            return default
    return cfg if cfg is not None else default


# ── Log paths ───────────────────────────────────────────────────────────────

_env_root = os.environ.get("PARALLAX_LOG_DIR")
LOG_ROOT = Path(_env_root) if _env_root else Path.cwd() / "logs"

RUNS_DIR = LOG_ROOT / "runs"
BUDGETS_DIR = LOG_ROOT / "budgets"
TRUST_FILE = LOG_ROOT / "trust.json"


def ensure_dirs():
    """Create log directories if they don't exist."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    BUDGETS_DIR.mkdir(parents=True, exist_ok=True)


def run_dir(run_id: str) -> Path:
    """Return the directory for a specific run's logs."""
    d = RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Project output paths ────────────────────────────────────────────────────

def output_root() -> Path:
    """
    Configured output directory, expanded. Creates if needed.

    Resolution order:
      1. PARALLAX_OUTPUT_ROOT env var (used by the `parallax` CLI to pin output
         to cwd/logs/ for project-local runs)
      2. paths.output from config/parallax.yaml
      3. ~/Movies/Parallax default
    """
    env_root = os.environ.get("PARALLAX_OUTPUT_ROOT")
    raw = env_root if env_root else get_config("paths.output", "~/Movies/Parallax")
    p = Path(raw).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def project_dir(concept_id: str) -> Path:
    """
    Return the project working directory for a concept.
    Creates standard folder structure: input/, assets/, output/
    """
    d = output_root() / concept_id
    (d / "input").mkdir(parents=True, exist_ok=True)
    (d / "assets").mkdir(parents=True, exist_ok=True)
    (d / "output").mkdir(parents=True, exist_ok=True)
    return d
