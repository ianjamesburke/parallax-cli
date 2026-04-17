"""
server.py — parallax-web: localhost HTTP + SSE chat server backed by
the Anthropic SDK and a custom tool-use agent loop.

Design:
    - FastAPI + uvicorn for routing, SSE, and static files
    - `anthropic` SDK for the model, streaming via client.messages.stream(...)
    - One agent thread per session, fed from POST /api/message
    - SSE fan-out via sse-starlette EventSourceResponse + per-session queue.Queue
    - SQLite telemetry via telemetry.py
    - Gallery: stills/ and output/ + drafts/, mtime-sorted

Run:
    ANTHROPIC_API_KEY=sk-... python3 -m parallax_web
    (or equivalently from this file's directory: python3 server.py)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# Local package imports
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import telemetry  # noqa: E402
import costs  # noqa: E402
import registry  # noqa: E402
import server_log  # noqa: E402

# ---------------------------------------------------------------------------
# TEST_MODE — set TEST_MODE=1 to run without paid API calls
# ---------------------------------------------------------------------------
TEST_MODE: bool = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes", "on")


def _fal_whoami() -> dict:
    """
    Best-effort fetch of the fal account identity if FAL_KEY / FAL_API_KEY is
    set on the server process. Returns a dict shaped like:
        {"configured": bool, "identity": Optional[str], "error": Optional[str]}
    Never raises — network errors, missing key, and bad JSON all collapse
    to `configured=False` with an error string (when relevant).
    """
    key = os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY")
    if not key:
        return {"configured": False, "identity": None, "error": None}

    import urllib.request
    import urllib.error

    url = "https://rest.alpha.fal.ai/"  # TODO: verify fal identity route
    req = urllib.request.Request(url, headers={"Authorization": f"Key {key}"})
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            body = resp.read(4096)
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                data = None
            identity = None
            if isinstance(data, dict):
                identity = (
                    data.get("name")
                    or data.get("email")
                    or data.get("user")
                    or data.get("id")
                )
            if not identity:
                masked = key[:6] + "…" + key[-4:] if len(key) > 12 else "****"
                identity = f"key {masked}"
            return {"configured": True, "identity": identity, "error": None}
    except urllib.error.HTTPError as e:
        return {
            "configured": True,
            "identity": None,
            "error": f"fal http {e.code}",
        }
    except Exception as e:
        return {
            "configured": True,
            "identity": None,
            "error": f"fal fetch failed: {e}",
        }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL = os.environ.get("PARALLAX_WEB_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 16384

MODEL_PRICES = {
    "claude-sonnet-4-6":   {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5":   {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":     {"input": 15.00, "output": 75.00},
    "claude-opus-4-5":     {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5":    {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
}


def _model_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price = MODEL_PRICES.get(model) or MODEL_PRICES.get("claude-sonnet-4-6")
    if price is None:
        return 0.0
    return (
        (input_tokens / 1_000_000.0) * price["input"]
        + (output_tokens / 1_000_000.0) * price["output"]
    )


MAX_TEXT_FILE_BYTES = 256 * 1024
ALLOWED_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}

PROJECT_DIR = Path(os.environ.get("PARALLAX_PROJECT_DIR", os.getcwd())).resolve()
STATIC_DIR = _HERE / "static"
HOP_PROMPT_PATH = _HERE / "head_of_production_prompt.md"

_network_accessible = bool(os.environ.get("PARALLAX_WEB_HOST"))
PER_USER_WORKSPACES = (
    not bool(os.environ.get("PARALLAX_SINGLE_USER"))
    and (
        bool(os.environ.get("PARALLAX_PER_USER_WORKSPACES"))
        or bool(os.environ.get("PARALLAX_WEB_PASSWORD"))
        or _network_accessible
    )
)

WORKSPACE_ROOT_NAME = "parallax"


def _sanitize_name(name: str) -> str:
    """Filesystem-safe identifier — alphanumeric + hyphen/underscore, max 32 chars."""
    safe = "".join(c for c in (name or "") if c.isalnum() or c in "-_")[:32]
    return safe or "anon"


# Backwards-compat alias
_sanitize_user = _sanitize_name


def _ensure_workspace(workspace: Path) -> None:
    """Scaffold the canonical workspace layout if any piece is missing."""
    workspace.mkdir(parents=True, exist_ok=True)
    for sub in ("stills", "input", "output", "drafts", "audio", "logs"):
        (workspace / sub).mkdir(exist_ok=True)


def _workspace_root() -> Path:
    return PROJECT_DIR / WORKSPACE_ROOT_NAME


def _users_root() -> Path:
    return _workspace_root() / "users"


def _workspace_for(
    user: Optional[str],
    project: Optional[str] = None,
    scaffold: bool = False,
) -> Path:
    """
    Resolve the working directory for a user + project pair.

    New layout (beta):
        PROJECT_DIR/parallax/<project>/                  (single-user default)
        PROJECT_DIR/parallax/users/<user>/<project>/     (per-user mode)
    """
    project_safe = _sanitize_name(project or "main")
    if not PER_USER_WORKSPACES or not user:
        workspace = (_workspace_root() / project_safe).resolve()
    else:
        user_safe = _sanitize_name(user)
        workspace = (_users_root() / user_safe / project_safe).resolve()
    try:
        workspace.relative_to(_workspace_root())
    except ValueError:
        workspace = _users_root() / "anon" / "main"
    if scaffold:
        _ensure_workspace(workspace)
    return workspace


PARALLAX_CANDIDATES = (
    os.path.expanduser("~/.local/bin/parallax"),
    "/usr/local/bin/parallax",
    "/opt/homebrew/bin/parallax",
    str(_HERE.parent / "bin" / "parallax"),
)


def _find_parallax_bin() -> Optional[str]:
    override = os.environ.get("PARALLAX_BIN")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    which = shutil.which("parallax")
    if which:
        return which
    for p in PARALLAX_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _load_head_of_production_prompt() -> str:
    try:
        base = HOP_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        print(
            f"server: failed to load head_of_production_prompt.md: {e}", file=sys.stderr, flush=True
        )
        base = "You are the Head of Production for Parallax."
    launch_ctx = _build_launch_context()
    return base + launch_ctx if launch_ctx else base


# ---------------------------------------------------------------------------
# Path scoping — reject anything outside PROJECT_DIR
# ---------------------------------------------------------------------------


class PathError(ValueError):
    pass


def _resolve_project_path(
    user_path: str,
    workspace: Optional[Path] = None,
    read_fallback: bool = False,
) -> Path:
    """
    Resolve `user_path` relative to a workspace and verify it stays inside
    the master PROJECT_DIR tree.
    """
    if not isinstance(user_path, str) or not user_path:
        raise PathError("path must be a non-empty string")
    workspace = workspace or PROJECT_DIR
    # `project_root` sentinel — resolves relative to PROJECT_DIR, not the workspace.
    if user_path == "project_root" or user_path.startswith("project_root/"):
        remainder = user_path[len("project_root"):].lstrip("/")
        candidate = PROJECT_DIR / remainder if remainder else PROJECT_DIR
        try:
            resolved = candidate.resolve()
        except Exception as e:
            raise PathError(f"could not resolve path: {e}") from e
        try:
            resolved.relative_to(PROJECT_DIR.resolve())
        except ValueError as e:
            raise PathError(
                f"path {user_path!r} escapes project {PROJECT_DIR}"
            ) from e
        return resolved
    p = Path(user_path)
    escaped = False
    if not p.is_absolute():
        candidate = workspace / p
        try:
            resolved = candidate.resolve()
        except Exception as e:
            raise PathError(f"could not resolve path: {e}") from e
        try:
            resolved.relative_to(workspace)
            if not read_fallback or resolved.exists():
                return resolved
            escaped = True
        except ValueError:
            escaped = True
        if escaped:
            candidate = PROJECT_DIR / p
    else:
        candidate = p
    try:
        resolved = candidate.resolve()
    except Exception as e:
        raise PathError(f"could not resolve path: {e}") from e
    try:
        resolved.relative_to(PROJECT_DIR)
    except ValueError as e:
        raise PathError(
            f"path {user_path!r} escapes project {PROJECT_DIR}"
        ) from e
    return resolved


def _expand_project_root_token(value: str) -> str:
    """Rewrite the leading `project_root/...` sentinel in a CLI arg to an absolute path.

    Manifest scene values look like `project_root/image.png:5:zoom_in` — only the
    path segment (up to the first `:`) should be rewritten; trailing duration/motion
    fields must be preserved verbatim.
    """
    if not value.startswith("project_root"):
        return value
    head, sep, tail = value.partition(":")
    if head == "project_root":
        head = str(PROJECT_DIR)
    elif head.startswith("project_root/"):
        head = str(PROJECT_DIR / head[len("project_root/"):])
    return head + sep + tail


def _display_path(resolved: Path, base: Path) -> str:
    """Path string for tool output: relative to `base` when inside it, else absolute.

    Use this anywhere a resolved path is surfaced back to the agent/UI — the
    `project_root/` sentinel lets paths live outside the workspace, so a naive
    `relative_to(base)` raises. Centralizing the fallback prevents the bug from
    recurring as new tools are added.
    """
    try:
        return str(resolved.relative_to(base))
    except ValueError:
        return str(resolved)


# ---------------------------------------------------------------------------
# Event formatting for dispatch NDJSON
# ---------------------------------------------------------------------------


def format_dispatch_event(evt: dict) -> str:
    etype = evt.get("type", "")
    if etype == "run_started":
        rid = evt.get("run_id", "?")
        short = rid[:8] if isinstance(rid, str) else "?"
        return f"run {short} started"
    if etype == "agent_call":
        phase = evt.get("phase", "")
        model = evt.get("model", "?")
        if phase == "start":
            agent = evt.get("agent", "") or ""
            if len(agent) > 30:
                agent = agent[:27] + "..."
            return f"-> {model} ({agent})" if agent else f"-> {model}"
        if phase == "end":
            tin = evt.get("tokens_in", "?")
            tout = evt.get("tokens_out", "?")
            return f"ok {model} {tin}->{tout}"
        return ""
    if etype == "still_generated":
        path = evt.get("output_path") or evt.get("path") or ""
        return f"still: {os.path.basename(path)}" if path else "still generated"
    if etype == "video_generated":
        path = evt.get("output_path") or evt.get("path") or ""
        return f"video: {os.path.basename(path)}" if path else "video generated"
    if etype == "voiceover_generated":
        path = evt.get("output_path") or evt.get("path") or ""
        return f"voiceover: {os.path.basename(path)}" if path else "voiceover generated"
    if etype == "assembly_started":
        return "assembling..."
    if etype == "assembly_complete":
        path = evt.get("output_path") or evt.get("path") or ""
        return f"assembled -> {os.path.basename(path)}" if path else "assembled"
    if etype == "run_complete":
        dur = evt.get("duration_s") or evt.get("duration") or 0
        try:
            return f"done in {float(dur):.1f}s"
        except (TypeError, ValueError):
            return "done"
    if etype == "error":
        err = evt.get("message") or evt.get("error") or "unknown error"
        return f"ERROR: {err}"
    return etype or ""


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


class Session:
    def __init__(self, session_id: str, user: Optional[str] = None, project: Optional[str] = None) -> None:
        self.id = session_id
        self.user = user or os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        self.project = project or "main"
        self.workspace = _workspace_for(self.user, self.project, scaffold=True)
        self.messages: list[dict[str, Any]] = []
        self.display_log: list[dict[str, Any]] = []
        self.subscribers: list[queue.Queue[dict[str, Any]]] = []
        self.cancel_event = threading.Event()
        self.agent_thread: Optional[threading.Thread] = None
        self.dispatch_proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.created_at = time.time()
        self.test_mode = False
        self.selected_refs: list[str] = []
        try:
            self._hydrate_from_chat_log()
        except Exception as e:
            print(f"session hydrate failed: {e}", file=sys.stderr, flush=True)
        for turn in (_load_chat_history(self.workspace) or []):
            self.display_log.append({
                "kind": f"{turn.get('role', 'user')}_message" if turn.get("role") == "user" else "assistant_text",
                "data": {"text": turn.get("text") or ""},
                "ts": turn.get("ts") or time.time(),
            })
        telemetry.create_session(session_id, str(self.workspace), MODEL, user=self.user)

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=512)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast(self, kind: str, data: Any) -> None:
        evt = {"kind": kind, "data": data, "ts": time.time()}
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                pass

    def push_display(self, kind: str, data: Any) -> None:
        self.display_log.append({"kind": kind, "data": data, "ts": time.time()})

    def _hydrate_from_chat_log(self) -> None:
        turns = _load_chat_history(self.workspace)
        if not turns:
            return
        for turn in turns:
            role = turn.get("role")
            text = turn.get("text") or ""
            if not text or role not in ("user", "assistant"):
                continue
            self.messages.append({
                "role": role,
                "content": [{"type": "text", "text": text}],
            })


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def get_or_create_session(session_id: Optional[str], user: Optional[str] = None,
                           project: Optional[str] = None) -> Session:
    with SESSIONS_LOCK:
        if session_id and session_id in SESSIONS:
            existing = SESSIONS[session_id]
            if PER_USER_WORKSPACES and user and existing.user != user:
                print(
                    f"session {session_id}: user mismatch (owner={existing.user!r}, "
                    f"requester={user!r}) — creating new session",
                    file=sys.stderr, flush=True,
                )
            else:
                return existing
        sid = session_id or uuid.uuid4().hex
        s = Session(sid, user=user, project=project)
        SESSIONS[sid] = s
        return s


# ---------------------------------------------------------------------------
# Tool definitions — sent to the model
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "list_dir",
        "description": (
            "List the entries of a directory inside the current Parallax "
            "project. Paths are resolved relative to the project root. "
            "Rejects any path that escapes the project directory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Directory path. Use '.' for the workspace root, 'input' / 'stills' / etc. "
                        "for workspace subdirs, or 'project_root' (or 'project_root/sub/dir') for "
                        "anything at the launch directory. The launch directory is first-class — "
                        "files there are already enumerated in the 'Launch directory contents' block "
                        "of the system prompt, so you rarely need to list it just to discover files."
                    ),
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a text file from the project. Max 256 KB. Returns a helpful "
            "error for binary files or files that are too large."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, relative to project root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_image",
        "description": (
            "Load an image (PNG/JPEG/WEBP/GIF) from the project and return it "
            "as an image block the model can actually see. Do NOT use this on "
            "video files — ask the user to describe the video instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Image path, relative to project root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "make_storyboard",
        "description": (
            "Create a contact-sheet image showing up to 8 images from a directory, "
            "each labeled with its filename. Use this INSTEAD of multiple read_image "
            "calls whenever you need to survey what images are in a directory. "
            "Returns a single composite image you can see directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to project root (default: 'stills').",
                },
                "max_images": {
                    "type": "integer",
                    "description": "Max images to include, up to 8 (default: 8).",
                },
            },
        },
    },
    {
        "name": "parallax_create",
        "description": (
            "Generate new still images from a text brief using the Parallax CLI. "
            "Use this when the user needs new visuals that don't exist yet. "
            "Stills land in stills/ and a manifest is auto-written.\n\n"
            "REFERENCE IMAGES: pass uploaded images via the `ref` array (relative "
            "paths inside the project, e.g. ['input/foo.png']). The CLI uses them "
            "as image-to-image references — DO NOT just mention filenames in the "
            "brief text, the brief is text-only. If the user uploaded a reference "
            "and wants 'same image but X', pass it as a ref.\n\n"
            "After this call, use edit_manifest to choose which stills to compose, "
            "then parallax_compose."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief": {
                    "type": "string",
                    "description": "Creative brief — what to generate.",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of variations to generate. Default is 1. Only increase if the user explicitly asks for multiple.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["1:1","2:3","3:2","3:4","4:3","4:5","5:4","9:16","16:9"],
                    "description": "Output aspect ratio (default: 3:4).",
                },
                "ref": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reference image paths (relative to project root) for image-to-image generation.",
                },
            },
            "required": ["brief"],
        },
    },
    {
        "name": "edit_manifest",
        "description": (
            "Edit cwd/manifest.yaml — the source of truth for what gets rendered. "
            "The manifest has a `scenes` list; each scene is either a still scene "
            "(with `still`, `duration`, optional `motion`) or a video scene (with "
            "`type: video`, `source`, optional `start_s`/`end_s`). Both scene types "
            "support `vo_text`.\n\n"
            "Operations:\n"
            "  set-scenes — replace the still scenes list.\n"
            "  add-scene — append one still scene.\n"
            "  add-video-scene — append a video scene.\n"
            "  remove-scene — `values` is ['<number>'].\n"
            "  reorder — `values` is ['1,3,2'] (comma-separated scene numbers).\n"
            "  set-vo — set the voiceover text for a scene.\n"
            "  set-voice — pick the voiceover voice.\n"
            "  set-headline — set a static headline overlay for the whole video.\n"
            "  clear-headline — remove the headline overlay.\n"
            "  enable-captions / disable-captions — toggle word-by-word caption burn.\n"
            "  set — set an arbitrary top-level key.\n"
            "  show — print the current manifest.\n\n"
            "ALWAYS use this tool to change the manifest. NEVER ask the user to edit YAML manually."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "set-scenes", "add-scene", "add-video-scene", "remove-scene", "reorder",
                        "set", "set-vo", "set-voice",
                        "set-headline", "clear-headline",
                        "enable-captions", "disable-captions",
                        "show",
                    ],
                    "description": "The edit operation to perform.",
                },
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Op-specific values (see op descriptions).",
                },
                "duration": {
                    "type": "number",
                    "description": "Default duration in seconds for add-scene (default: 3).",
                },
                "motion": {
                    "type": "string",
                    "enum": [
                        "zoom_in", "zoom_out", "zoom_drift_right", "zoom_drift_left",
                        "zoom_drift_down", "zoom_drift_up", "pan_right", "pan_left",
                        "pan_down", "pan_up",
                    ],
                    "description": "Motion preset for add-scene.",
                },
                "start_s": {
                    "type": "number",
                    "description": "Video scene start time in seconds (add-video-scene).",
                },
                "end_s": {
                    "type": "number",
                    "description": "Video scene end time in seconds (add-video-scene).",
                },
            },
            "required": ["op"],
        },
    },
    {
        "name": "parallax_voiceover",
        "description": (
            "Generate an ElevenLabs voiceover AND auto-transcribe with WhisperX. "
            "Reads `vo_text` from each scene in the manifest, concatenates, and "
            "synthesizes the audio. Then automatically runs WhisperX phoneme-level "
            "forced alignment to produce precise word-level timestamps.\n\n"
            "Voice shortcuts: george, rachel, domi, bella, antoni, arnold.\n\n"
            "Output: audio/voiceover.mp3 + audio/vo_manifest.json (whisperx)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "voice": {
                    "type": "string",
                    "description": "Voice name shortcut or raw voice_id. Overrides manifest voice.",
                },
                "model_id": {
                    "type": "string",
                    "description": "ElevenLabs model id (default: eleven_v3).",
                },
                "script": {
                    "type": "string",
                    "description": "Optional explicit script text. If omitted, concatenates scene vo_text.",
                },
            },
        },
    },
    {
        "name": "parallax_transcribe",
        "description": (
            "Run WhisperX phoneme-level forced alignment on an audio file. "
            "THE SINGULAR transcription path in parallax.\n\n"
            "Use this when you need to re-transcribe an existing audio file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "audio": {
                    "type": "string",
                    "description": "Audio file path relative to project root. Default: audio/voiceover.mp3",
                },
                "model": {
                    "type": "string",
                    "description": "WhisperX model size (default: base.en).",
                },
                "language": {
                    "type": "string",
                    "description": "Language code (default: en).",
                },
            },
        },
    },
    {
        "name": "parallax_trim_silence",
        "description": (
            "Remove silent gaps from audio/voiceover.mp3 and rewrite "
            "vo_manifest.json word timestamps proportionally. Run AFTER "
            "parallax_voiceover and BEFORE parallax_align."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "parallax_align",
        "description": (
            "Align each scene's duration to its vo_text word timing. Reads "
            "audio/vo_manifest.json, matches scene vo_text first words to spoken "
            "words, and rewrites scene durations. Run AFTER voiceover and BEFORE compose."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "parallax_compose",
        "description": (
            "Render the project EXACTLY as specified in cwd/manifest.yaml. "
            "Always run edit_manifest first to set the scenes. "
            "Output: an mp4 in output/, plus output/latest.mp4 symlink."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "parallax_ingest",
        "description": (
            "Index footage files so they can be searched and used in the pipeline. "
            "Pass a file path or directory path (relative to project root). "
            "Use 'project_root' to ingest everything in the master project directory. "
            "ALWAYS offer to run this when you see unindexed video files — don't ask the user to run it from the terminal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File or directory to ingest, relative to project root. Use 'project_root' for the launch directory.",
                },
                "no_vision": {
                    "type": "boolean",
                    "description": "Skip Gemini visual analysis — transcription only. Faster and cheaper.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_footage",
        "description": (
            "Search indexed footage by transcript content, scene descriptions, or keywords. "
            "Returns matching clips with timestamps and excerpts. "
            "Always run this before asking the user to locate footage manually."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term or phrase to find in transcripts, descriptions, or keywords",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_footage",
        "description": "List all indexed footage clips with duration and scene count.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "analyze_footage_segment",
        "description": (
            "Get a detailed analysis of a specific time segment of a footage clip. "
            "Use this for high-resolution reads on any part of any clip — e.g. "
            "'what exactly is written on the whiteboard at 6:30'. "
            "Merges result into the clip's metadata file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the video file (relative to project dir)",
                },
                "start_time": {
                    "type": "string",
                    "description": "Start time in HH:MM:SS format",
                },
                "end_time": {
                    "type": "string",
                    "description": "End time in HH:MM:SS format",
                },
                "question": {
                    "type": "string",
                    "description": "Specific question to answer about this segment (optional)",
                },
            },
            "required": ["path", "start_time", "end_time"],
        },
    },
    {
        "name": "relink_footage",
        "description": (
            "Update the index when a footage file has been moved or renamed. "
            "Use when auto-relink failed and the user tells you where the file now lives."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "old_path": {
                    "type": "string",
                    "description": "Original path as stored in the index",
                },
                "new_path": {
                    "type": "string",
                    "description": "New path relative to project dir",
                },
            },
            "required": ["old_path", "new_path"],
        },
    },
    {
        "name": "move_to_shared",
        "description": (
            "Move files from the current project into the shared/ directory, "
            "making them accessible to all projects under this root."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths within the current project to move to shared/",
                }
            },
            "required": ["paths"],
        },
    },
    {
        "name": "list_shared",
        "description": "List files in the shared/ directory accessible to all projects.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "parallax_fal_video",
        "description": (
            "Animate a reference image into a short video clip using AI video generation. "
            "Use when the user has an image and wants the subject to actually move "
            "(not pan/zoom on a static frame). "
            "Costs ~$0.02 at the low tier — ALWAYS confirm with the user before calling. "
            "Always offer Ken Burns (free) as the alternative first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "Reference image path (relative to project root).",
                },
                "prompt": {
                    "type": "string",
                    "description": "Motion description — what the subject should do.",
                },
                "tier": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Cost tier. low=~$0.02 (LTX-2.3), medium=~$0.20 (Wan), high=~$0.50 (Kling). Default: low.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["9:16", "16:9", "1:1"],
                    "description": "Output aspect ratio (default: 9:16).",
                },
                "output": {
                    "type": "string",
                    "description": "Output path relative to project root (default: output/fal_<ts>.mp4).",
                },
            },
            "required": ["image", "prompt"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_list_dir(path: str, workspace: Optional[Path] = None) -> dict:
    base = workspace or PROJECT_DIR
    if path == "project_root":
        resolved = PROJECT_DIR
    else:
        try:
            resolved = _resolve_project_path(path, workspace=base)
        except PathError as e:
            return {"error": str(e)}
    if not resolved.exists():
        return {"error": f"path does not exist: {path}"}
    if not resolved.is_dir():
        return {"error": f"not a directory: {path}"}
    entries = []
    try:
        for entry in sorted(resolved.iterdir(), key=lambda p: p.name.lower()):
            try:
                st = entry.stat()
                entries.append(
                    {
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size": st.st_size if entry.is_file() else None,
                        "mtime": st.st_mtime,
                    }
                )
            except Exception as e:
                entries.append({"name": entry.name, "error": str(e)})
    except Exception as e:
        return {"error": f"could not list directory: {e}"}
    return {
        "path": _display_path(resolved, base) or ".",
        "entries": entries,
    }


def tool_read_file(path: str, workspace: Optional[Path] = None) -> dict:
    base = workspace or PROJECT_DIR
    try:
        resolved = _resolve_project_path(path, workspace=base)
    except PathError as e:
        return {"error": str(e)}
    if not resolved.exists():
        return {"error": f"file does not exist: {path}"}
    if not resolved.is_file():
        return {"error": f"not a regular file: {path}"}
    try:
        size = resolved.stat().st_size
    except Exception as e:
        return {"error": f"stat failed: {e}"}
    if size > MAX_TEXT_FILE_BYTES:
        return {
            "error": (
                f"file is too large ({size} bytes). Max is "
                f"{MAX_TEXT_FILE_BYTES} bytes."
            )
        }
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"error": "file is not valid UTF-8 text"}
    except Exception as e:
        return {"error": f"read failed: {e}"}
    return {
        "path": _display_path(resolved, base),
        "size": size,
        "content": text,
    }


def _sniff_image_mime(head: bytes) -> Optional[str]:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def tool_read_image(path: str, workspace: Optional[Path] = None) -> dict:
    base = workspace or PROJECT_DIR
    try:
        resolved = _resolve_project_path(path, workspace=base)
    except PathError as e:
        return {"error": str(e)}
    if not resolved.exists():
        return {"error": f"image does not exist: {path}"}
    if not resolved.is_file():
        return {"error": f"not a regular file: {path}"}
    try:
        raw = resolved.read_bytes()
    except Exception as e:
        return {"error": f"read failed: {e}"}
    mime = _sniff_image_mime(raw[:16])
    if mime is None:
        ext_mime, _ = mimetypes.guess_type(str(resolved))
        mime = ext_mime
    if mime is None:
        return {"error": f"could not determine mime type for {path}"}
    if mime.startswith("video/"):
        return {
            "error": (
                "this is a video file — read_image cannot load videos. "
                "Ask the user to describe the video or pull out key stills."
            )
        }
    if mime not in ALLOWED_IMAGE_MIMES:
        return {
            "error": (
                f"unsupported image mime {mime}. "
                f"Allowed: {sorted(ALLOWED_IMAGE_MIMES)}"
            )
        }
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return {
        "path": _display_path(resolved, base),
        "mime": mime,
        "image_block": {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": b64,
            },
        },
    }


def _clean_subprocess_env() -> dict:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    path_parts = env.get("PATH", "").split(":")
    cleaned = [p for p in path_parts if "/venv/" not in p and "/.venv/" not in p and "/virtualenvs/" not in p]
    if cleaned:
        env["PATH"] = ":".join(cleaned)
    env["PARALLAX_QUIET"] = "1"
    if TEST_MODE:
        env["TEST_MODE"] = "true"
    return env


def _stream_parallax_subprocess(
    session: Session,
    cmd: list[str],
    stdin_payload: Optional[str],
    label: str,
    _collected: Optional[dict] = None,
) -> str:
    telemetry.record_event(session.id, "dispatch_start", {"cmd": " ".join(cmd[:3]), "label": label[:200]})
    session.broadcast("dispatch_event", {"phase": "starting", "text": label})

    try:
        subproc_env = _clean_subprocess_env()
        if getattr(session, "test_mode", False):
            subproc_env["TEST_MODE"] = "true"
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(session.workspace),
            env=subproc_env,
        )
    except Exception as e:
        msg = f"failed to spawn parallax: {e}"
        print(f"dispatch: {msg}", file=sys.stderr, flush=True)
        session.broadcast("dispatch_event", {"phase": "error", "text": msg})
        telemetry.record_event(session.id, "dispatch_error", {"error": msg})
        return msg

    session.dispatch_proc = proc
    if stdin_payload is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_payload)
            proc.stdin.close()
        except Exception as e:
            print(f"dispatch: stdin write failed: {e}", file=sys.stderr, flush=True)

    last_output_path = ""
    last_error = ""
    try:
        if proc.stdout is not None:
            for raw in proc.stdout:
                if session.cancel_event.is_set():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                evt_label = format_dispatch_event(evt)
                session.broadcast(
                    "dispatch_event",
                    {"phase": evt.get("type", ""), "text": evt_label, "raw": evt},
                )
                telemetry.record_event(session.id, "dispatch_event", evt)
                path = evt.get("output_path") or evt.get("path")
                if isinstance(path, str) and path:
                    last_output_path = path
                if evt.get("type") == "still_generated" and isinstance(path, str) and path:
                    if _collected is not None:
                        _collected.setdefault("still_paths", []).append(path)
                if evt.get("type") == "error":
                    last_error = evt.get("message") or evt.get("error") or ""
    except Exception as e:
        print(f"dispatch: stream error: {e}", file=sys.stderr, flush=True)
        last_error = str(e)

    try:
        rc = proc.wait(timeout=5.0)
    except Exception:
        rc = proc.poll()

    err_tail = ""
    if proc.stderr is not None:
        try:
            err_tail = (proc.stderr.read() or "").strip()
        except Exception:
            err_tail = ""

    session.dispatch_proc = None

    if session.cancel_event.is_set():
        telemetry.record_event(session.id, "dispatch_complete", {"cancelled": True})
        return "dispatch cancelled by user"

    if rc == 0:
        telemetry.record_event(session.id, "dispatch_complete", {"rc": 0, "output_path": last_output_path})
        summary = "render complete"
        if last_output_path:
            summary += f": {last_output_path}"
        session.broadcast("dispatch_event", {"phase": "done", "text": summary})
        return summary

    msg = f"parallax exited rc={rc}"
    if last_error:
        msg += f"; {last_error}"
    elif err_tail:
        msg += f"; stderr: {err_tail[-300:]}"
    telemetry.record_event(
        session.id, "dispatch_complete",
        {"rc": rc, "error": last_error or err_tail[-300:]},
    )
    session.broadcast("dispatch_event", {"phase": "error", "text": msg})
    return msg


def tool_parallax_create(
    session: Session,
    brief: str,
    count: int = 1,
    aspect_ratio: str = "3:4",
    ref: Optional[list] = None,
) -> dict:
    """Returns {"summary": str, "still_paths": [absolute_path, ...]}"""
    bin_path = _find_parallax_bin()
    if bin_path is None:
        return {"summary": "parallax CLI binary not found on PATH", "still_paths": []}
    if not ref and session.selected_refs:
        ref = list(session.selected_refs)
    resolved_refs = []
    if ref:
        for r in ref:
            try:
                p = _resolve_project_path(r, workspace=session.workspace, read_fallback=True)
            except PathError as e:
                return {"summary": f"invalid ref path {r!r}: {e}", "still_paths": []}
            if not p.exists():
                return {"summary": f"ref image not found: {r}", "still_paths": []}
            resolved_refs.append(str(p))
    spec = {"brief": brief, "count": count, "aspect_ratio": aspect_ratio}
    if resolved_refs:
        spec["ref"] = resolved_refs
    cmd = [bin_path, "create", "--stdin", "--json"]
    ref_label = f", refs={len(resolved_refs)}" if resolved_refs else ""
    label = f"create: {brief[:100]} (count={count}, aspect={aspect_ratio}{ref_label})"
    collected: dict = {}
    summary = _stream_parallax_subprocess(session, cmd, json.dumps(spec), label, _collected=collected)
    return {"summary": summary, "still_paths": collected.get("still_paths", [])}


def tool_parallax_compose(session: Session) -> str:
    bin_path = _find_parallax_bin()
    if bin_path is None:
        return "parallax CLI binary not found on PATH"
    cmd = [bin_path, "compose", "--json"]
    return _stream_parallax_subprocess(session, cmd, None, "compose: rendering manifest")


def tool_parallax_voiceover(
    session: Session,
    voice: Optional[str] = None,
    model_id: Optional[str] = None,
    script: Optional[str] = None,
) -> str:
    bin_path = _find_parallax_bin()
    if bin_path is None:
        return "parallax CLI binary not found on PATH"
    spec = {}
    if voice:
        spec["voice"] = voice
    if model_id:
        spec["model_id"] = model_id
    if script:
        spec["script"] = script
    cmd = [bin_path, "voiceover", "--stdin", "--json"]
    label = f"voiceover: {voice or 'manifest voice'}"
    return _stream_parallax_subprocess(session, cmd, json.dumps(spec), label)


def tool_parallax_trim_silence(session: Session) -> str:
    bin_path = _find_parallax_bin()
    if bin_path is None:
        return "parallax CLI binary not found on PATH"
    cmd = [bin_path, "trim-silence", "--json"]
    return _stream_parallax_subprocess(session, cmd, None, "trim-silence: removing gaps")


def tool_parallax_align(session: Session) -> str:
    bin_path = _find_parallax_bin()
    if bin_path is None:
        return "parallax CLI binary not found on PATH"
    cmd = [bin_path, "align", "--json"]
    return _stream_parallax_subprocess(session, cmd, None, "align: syncing scenes to vo")


def tool_parallax_ingest(
    session: Session,
    path: str,
    no_vision: bool = False,
) -> str:
    bin_path = _find_parallax_bin()
    if bin_path is None:
        return "parallax CLI binary not found on PATH"
    # Resolve "project_root" sentinel to PROJECT_DIR
    if path == "project_root":
        ingest_path = str(PROJECT_DIR)
    else:
        try:
            resolved = _resolve_project_path(path, workspace=session.workspace, read_fallback=True)
            ingest_path = str(resolved)
        except PathError as e:
            return f"invalid path: {e}"
    cmd = [bin_path, "ingest", ingest_path]
    if no_vision:
        cmd.append("--no-vision")
    label = f"ingest: indexing {path}"
    return _stream_parallax_subprocess(session, cmd, None, label)


def tool_parallax_transcribe(
    session: Session,
    audio: Optional[str] = None,
    model: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    bin_path = _find_parallax_bin()
    if bin_path is None:
        return "parallax CLI binary not found on PATH"
    cmd = [bin_path, "transcribe", "--json"]
    if audio:
        cmd += ["--audio", audio]
    if model:
        cmd += ["--model", model]
    if language:
        cmd += ["--language", language]
    label = f"transcribe: whisperx ({audio or 'audio/voiceover.mp3'})"
    return _stream_parallax_subprocess(session, cmd, None, label)


def tool_edit_manifest(
    workspace: Path,
    op: str,
    values: Optional[list] = None,
    duration: Optional[float] = None,
    motion: Optional[str] = None,
    start_s: Optional[float] = None,
    end_s: Optional[float] = None,
) -> dict:
    bin_path = _find_parallax_bin()
    if bin_path is None:
        return {"error": "parallax CLI binary not found on PATH"}

    cmd = [bin_path, "manifest", op]
    if duration is not None:
        cmd += ["--duration", str(duration)]
    if motion:
        cmd += ["--motion", motion]
    if start_s is not None:
        cmd += ["--start-s", str(start_s)]
    if end_s is not None:
        cmd += ["--end-s", str(end_s)]
    if values:
        cmd += [_expand_project_root_token(str(v)) for v in values]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
            cwd=str(workspace), env=_clean_subprocess_env(),
        )
    except Exception as e:
        return {"error": f"manifest edit failed: {e}"}

    if result.returncode != 0:
        return {"error": (result.stderr or result.stdout).strip()}
    return {"output": result.stdout.strip()}


# ---------------------------------------------------------------------------
# Footage indexing tool implementations
# ---------------------------------------------------------------------------


def _load_footage_index() -> list[dict]:
    """Read footage.jsonl at PROJECT_DIR root. Returns list of entry dicts."""
    footage_jsonl = PROJECT_DIR / "footage.jsonl"
    entries = []
    if not footage_jsonl.exists():
        return entries
    try:
        with open(footage_jsonl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"footage.jsonl: bad line skipped: {e}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"footage.jsonl: read failed: {e}", file=sys.stderr, flush=True)
    return entries


_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".mxf", ".webm"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif", ".bmp", ".tiff"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus"}

# --- Invariant ---
# Every user-facing content file at PROJECT_DIR root (videos, images, audio)
# MUST be surfaced in the system prompt via _build_launch_context(). The agent
# should never have to call list_dir to discover that a user-provided file
# exists. When you add support for a new content class, extend the extension
# set AND extend _build_launch_context() to list it. See the CLAUDE.md note
# on "user content discovery" for the reasoning.
_CONTENT_EXTENSIONS = _VIDEO_EXTENSIONS | _IMAGE_EXTENSIONS | _AUDIO_EXTENSIONS


def _discover_project_content(max_depth: int = 3) -> dict[str, list[Path]]:
    """
    Walk PROJECT_DIR for user content up to max_depth levels deep, grouped by
    class. Skips the parallax/ workspace subtree and hidden directories.
    Returns {"videos": [...], "images": [...], "audio": [...]}.
    """
    workspace_root = (PROJECT_DIR / WORKSPACE_ROOT_NAME).resolve()
    buckets: dict[str, list[Path]] = {"videos": [], "images": [], "audio": []}

    def _walk(dirpath: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(dirpath.iterdir())
        except PermissionError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                if entry.resolve() == workspace_root:
                    continue
                _walk(entry, depth + 1)
                continue
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext in _VIDEO_EXTENSIONS:
                buckets["videos"].append(entry)
            elif ext in _IMAGE_EXTENSIONS:
                buckets["images"].append(entry)
            elif ext in _AUDIO_EXTENSIONS:
                buckets["audio"].append(entry)

    _walk(PROJECT_DIR, 0)
    return buckets


# Back-compat shim — some callers may still reference the video-only discovery.
def _discover_project_media(max_depth: int = 3) -> list[Path]:
    return _discover_project_content(max_depth=max_depth)["videos"]


def _build_launch_context() -> str:
    """
    Return a markdown block describing all user-provided content at PROJECT_DIR
    root — videos, images, and audio — so the agent knows what the user has
    dropped in without having to call list_dir. Returns empty string when
    nothing is found.

    Invariant: this function is the single source of truth for "what content
    exists at project_root." If a file class is missing here, the agent will
    fail to find files of that class. Do not bypass.
    """
    try:
        buckets = _discover_project_content()
        videos = buckets["videos"]
        images = buckets["images"]
        audio = buckets["audio"]
        if not (videos or images or audio):
            return ""

        lines: list[str] = ["", "## Launch directory contents"]
        lines.append(
            f"PROJECT_DIR (`{PROJECT_DIR}`) contains user-provided files at the "
            f"project root. Address any of these by prefixing with "
            f"`project_root/` (e.g. `project_root/image.png`). Do not ask the "
            f"user to copy files into `input/` — use them where they are."
        )

        if videos:
            indexed_paths: set[str] = set()
            for entry in _load_footage_index():
                raw = entry.get("path", "")
                if raw:
                    p = Path(raw) if Path(raw).is_absolute() else PROJECT_DIR / raw
                    indexed_paths.add(str(p.resolve()))
            unindexed = [p for p in videos if str(p.resolve()) not in indexed_paths]
            indexed_count = len(videos) - len(unindexed)
            lines.append("")
            lines.append(
                f"### Videos ({len(videos)}) — {indexed_count} indexed, "
                f"{len(unindexed)} not yet indexed"
            )
            for p in videos[:20]:
                marker = "" if str(p.resolve()) in indexed_paths else "  [unindexed]"
                lines.append(f"  - {p.relative_to(PROJECT_DIR)}{marker}")
            if len(videos) > 20:
                lines.append(f"  - …and {len(videos) - 20} more")
            if unindexed:
                lines.append(
                    "**Proactively offer** to run "
                    "`parallax_ingest(path='project_root')` for unindexed videos."
                )

        if images:
            lines.append("")
            lines.append(f"### Images ({len(images)})")
            for p in images[:20]:
                lines.append(f"  - {p.relative_to(PROJECT_DIR)}")
            if len(images) > 20:
                lines.append(f"  - …and {len(images) - 20} more")

        if audio:
            lines.append("")
            lines.append(f"### Audio ({len(audio)})")
            for p in audio[:20]:
                lines.append(f"  - {p.relative_to(PROJECT_DIR)}")
            if len(audio) > 20:
                lines.append(f"  - …and {len(audio) - 20} more")

        return "\n".join(lines)
    except Exception as e:
        print(f"[parallax] launch context scan failed: {e}", file=sys.stderr, flush=True)
        return ""


def _fingerprint_file_server(path: Path) -> str:
    """SHA256 of the first 4 MB of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            h.update(f.read(4 * 1024 * 1024))
    except Exception as e:
        print(f"[parallax] fingerprint failed ({path}): {e}", file=sys.stderr, flush=True)
        return ""
    return h.hexdigest()


def _rewrite_footage_jsonl_entry(old_path: str, new_rel_path: str) -> None:
    """Update the path field of an entry in footage.jsonl matched by old_path.
    Also updates the YAML source field if present.
    """
    footage_jsonl = PROJECT_DIR / "footage.jsonl"
    if not footage_jsonl.exists():
        return
    try:
        with open(footage_jsonl, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                new_lines.append(line)
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            if entry.get("path") == old_path:
                entry["path"] = new_rel_path
                new_lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
                # Also update YAML source field
                meta_rel = entry.get("meta", "")
                if meta_rel:
                    meta = _load_clip_meta(meta_rel)
                    if meta is not None:
                        meta["source"] = new_rel_path
                        _save_clip_meta(meta_rel, meta)
            else:
                new_lines.append(line)
        with open(footage_jsonl, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"[parallax] footage.jsonl rewrite failed: {e}", file=sys.stderr, flush=True)


def _resolve_footage_path(entry: dict) -> tuple:
    """Resolve the physical path for a footage entry.

    Returns (Path | None, bool) — (resolved_path, was_relocated).
    - If path exists at recorded location: (path, False)
    - If missing but fingerprint present and file found elsewhere: auto-relink
      then return (new_path, True)
    - If not found: (None, False)
    """
    recorded_rel = entry.get("path", "")
    candidate = PROJECT_DIR / recorded_rel
    if candidate.exists():
        return (candidate, False)

    # Can't auto-locate without fingerprint data
    size_bytes = entry.get("size_bytes")
    fingerprint = entry.get("fingerprint", "")
    if not size_bytes or not fingerprint:
        return (None, False)

    # Scan PROJECT_DIR for a matching file
    try:
        for root, _dirs, files in os.walk(PROJECT_DIR):
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix.lower() not in _VIDEO_EXTENSIONS:
                    continue
                try:
                    fsize = os.path.getsize(fpath)
                except Exception:
                    continue
                if fsize != size_bytes:
                    continue
                # Size matches — verify fingerprint
                fp = _fingerprint_file_server(fpath)
                if fp != fingerprint:
                    continue
                # Found it — auto-relink
                new_rel = str(fpath.relative_to(PROJECT_DIR))
                old_rel = recorded_rel
                _rewrite_footage_jsonl_entry(old_rel, new_rel)
                print(
                    f"[parallax] auto-relinked: {old_rel} → {new_rel}",
                    file=sys.stderr,
                    flush=True,
                )
                return (fpath, True)
    except Exception as e:
        print(f"[parallax] auto-relink scan failed: {e}", file=sys.stderr, flush=True)

    return (None, False)


def _load_clip_meta(meta_rel: str) -> Optional[dict]:
    """Load _meta YAML for a clip given its relative meta path."""
    try:
        import yaml
    except ImportError:
        return None
    meta_path = PROJECT_DIR / meta_rel
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"meta yaml read failed ({meta_rel}): {e}", file=sys.stderr, flush=True)
        return None


def _save_clip_meta(meta_rel: str, data: dict) -> None:
    """Save updated _meta YAML."""
    try:
        import yaml
    except ImportError:
        print("pyyaml not installed — cannot save meta", file=sys.stderr, flush=True)
        return
    meta_path = PROJECT_DIR / meta_rel
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    except Exception as e:
        print(f"meta yaml write failed ({meta_rel}): {e}", file=sys.stderr, flush=True)


def _hms_to_seconds(hms: str) -> float:
    """Convert HH:MM:SS or MM:SS string to float seconds."""
    parts = hms.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def tool_list_footage() -> dict:
    """List all indexed footage clips."""
    entries = _load_footage_index()
    if not entries:
        return {"clips": [], "message": "No footage indexed yet. Run `parallax ingest <path>` to index footage."}
    clips = []
    for e in entries:
        clip: dict = {
            "path": e.get("path", ""),
            "duration_s": e.get("duration_s", 0),
            "scene_count": e.get("scene_count", 0),
            "indexed_at": e.get("indexed_at", ""),
        }
        resolved, relocated = _resolve_footage_path(e)
        if resolved is None:
            clip["missing"] = True
        elif relocated:
            clip["relocated"] = True
            clip["new_path"] = str(resolved.relative_to(PROJECT_DIR))
        clips.append(clip)
    return {"clips": clips, "total": len(clips)}


def tool_search_footage(query: str) -> dict:
    """Search footage by transcript, scene descriptions, and keywords."""
    if not query or not query.strip():
        return {"error": "query must be a non-empty string"}
    q = query.strip().lower()
    entries = _load_footage_index()
    if not entries:
        return {"matches": [], "message": "No footage indexed yet."}

    matches = []
    missing_count = 0
    for entry in entries:
        resolved, _relocated = _resolve_footage_path(entry)
        if resolved is None:
            missing_count += 1
            continue

        path = entry.get("path", "")
        meta_rel = entry.get("meta", "")
        clip_matches = []

        # Search transcript
        transcript = entry.get("transcript", "") or ""
        if q in transcript.lower():
            # Find an excerpt around the match
            idx = transcript.lower().find(q)
            start = max(0, idx - 80)
            end = min(len(transcript), idx + len(q) + 80)
            excerpt = ("..." if start > 0 else "") + transcript[start:end] + ("..." if end < len(transcript) else "")
            clip_matches.append({"type": "transcript", "excerpt": excerpt})

        # Search scenes from meta
        if meta_rel:
            meta = _load_clip_meta(meta_rel)
            if meta:
                for scene in (meta.get("scenes") or []):
                    desc = (scene.get("description") or "").lower()
                    kws = " ".join(scene.get("keywords") or []).lower()
                    if q in desc or q in kws:
                        clip_matches.append({
                            "type": "scene",
                            "start_time": scene.get("start_time", ""),
                            "end_time": scene.get("end_time", ""),
                            "description": scene.get("description", ""),
                            "keywords": scene.get("keywords", []),
                        })

        if clip_matches:
            matches.append({
                "path": path,
                "duration_s": entry.get("duration_s", 0),
                "matches": clip_matches,
            })
            if len(matches) >= 10:
                break

    result: dict = {
        "query": query,
        "matches": matches,
        "total_found": len(matches),
    }
    if missing_count:
        result["missing_clips_skipped"] = missing_count
    return result


def tool_analyze_footage_segment(
    path: str,
    start_time: str,
    end_time: str,
    question: Optional[str] = None,
) -> dict:
    """Deep-read a time window of a footage clip via Gemini."""
    gemini_key = os.environ.get("AI_VIDEO_GEMINI_KEY") or os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        return {"error": "AI_VIDEO_GEMINI_KEY or GEMINI_API_KEY not set"}

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return {"error": "google-genai not installed — run: pip install google-genai"}

    try:
        import yaml
    except ImportError:
        return {"error": "pyyaml not installed — run: pip install pyyaml"}

    # Find matching entry in footage index
    entries = _load_footage_index()
    entry = next((e for e in entries if e.get("path", "") == path), None)
    if entry is None:
        return {"error": f"clip not found in footage index: {path}. Run `parallax ingest` first."}

    meta_rel = entry.get("meta", "")
    if not meta_rel:
        return {"error": f"no meta path for clip: {path}"}

    meta = _load_clip_meta(meta_rel)
    if meta is None:
        return {"error": f"could not load meta for clip: {path}"}

    start_s = _hms_to_seconds(start_time)
    end_s = _hms_to_seconds(end_time)

    client = genai.Client(api_key=gemini_key)

    # Check if gemini_file_uri is still valid; re-upload if not
    gemini_file_uri = meta.get("gemini_file_uri")
    if gemini_file_uri:
        try:
            f_info = client.files.get(name=gemini_file_uri.split("/")[-1])
            state_str = str(getattr(f_info, "state", ""))
            if "ACTIVE" not in state_str:
                gemini_file_uri = None
        except Exception:
            gemini_file_uri = None

    if not gemini_file_uri:
        # Re-upload — resolve physical path first
        video_path, _relocated = _resolve_footage_path(entry)
        if video_path is None:
            return {
                "error": (
                    f"clip file not found on disk and could not be auto-located. "
                    f"Use relink_footage to point to its new location."
                )
            }
        try:
            uploaded = client.files.upload(
                file=str(video_path),
                config=genai_types.UploadFileConfig(
                    mime_type="video/mp4",
                    display_name=video_path.name,
                ),
            )
            import time as _time
            upload_name = uploaded.name or ""
            for _ in range(60):
                f_info = client.files.get(name=upload_name)
                state_str = str(getattr(f_info, "state", ""))
                if "ACTIVE" in state_str:
                    break
                if "FAILED" in state_str:
                    return {"error": f"Gemini file upload FAILED: {f_info}"}
                _time.sleep(5)
            else:
                return {"error": "Gemini file never became ACTIVE after 5 minutes"}
            gemini_file_uri = uploaded.uri or ""
            meta["gemini_file_uri"] = gemini_file_uri
            _save_clip_meta(meta_rel, meta)
        except Exception as e:
            return {"error": f"failed to re-upload video to Gemini: {e}"}

    # Build prompt with optional question
    prompt_text = (
        f"Analyze the segment of this video from {start_time} to {end_time} "
        f"(seconds {start_s:.1f}–{end_s:.1f}). "
        "Give a detailed description of everything visible and audible. "
    )
    if question:
        prompt_text += f"\n\nSpecifically answer this question: {question}"

    try:
        video_part = genai_types.Part(
            file_data=genai_types.FileData(
                file_uri=gemini_file_uri,
                mime_type="video/mp4",
            ),
            video_metadata=genai_types.VideoMetadata(
                start_offset=f"{int(start_s)}s",
                end_offset=f"{int(end_s)}s",
            ),
        )
        resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[video_part, prompt_text],
            config=genai_types.GenerateContentConfig(
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        description = (resp.text or "").strip()
    except Exception as e:
        return {"error": f"Gemini segment analysis failed: {e}"}

    # Find matching scene by time overlap and append to reads
    read_entry = {
        "start_time": start_time,
        "end_time": end_time,
        "description": description,
        "question": question,
    }
    scenes = meta.get("scenes") or []
    matched_scene_idx = None
    for i, sc in enumerate(scenes):
        sc_start = _hms_to_seconds(sc.get("start_time", ""))
        sc_end = _hms_to_seconds(sc.get("end_time", ""))
        # Overlap: query segment overlaps with scene
        if sc_start < end_s and sc_end > start_s:
            matched_scene_idx = i
            break

    if matched_scene_idx is not None:
        reads = scenes[matched_scene_idx].setdefault("reads", [])
        reads.append(read_entry)
        meta["scenes"] = scenes
    else:
        # No matching scene — store in a top-level reads list
        top_reads = meta.setdefault("reads", [])
        top_reads.append(read_entry)

    _save_clip_meta(meta_rel, meta)

    return {
        "path": path,
        "start_time": start_time,
        "end_time": end_time,
        "description": description,
        "question": question,
        "matched_scene": matched_scene_idx,
    }


def tool_relink_footage(old_path: str, new_path: str) -> dict:
    """Update footage.jsonl and YAML meta when a clip has been moved/renamed."""
    if not old_path or not new_path:
        return {"error": "old_path and new_path are required"}

    # Verify new path exists
    new_abs = PROJECT_DIR / new_path
    try:
        if not new_abs.exists():
            return {"error": f"new_path does not exist on disk: {new_path}"}
    except Exception as e:
        return {"error": f"could not check new_path: {e}"}

    # Find the entry in the index
    entries = _load_footage_index()
    entry = next((e for e in entries if e.get("path", "") == old_path), None)
    if entry is None:
        return {"error": f"no footage entry found for old_path: {old_path}"}

    # Optionally verify fingerprint if available
    fingerprint = entry.get("fingerprint", "")
    fingerprint_warning = None
    if fingerprint:
        try:
            new_fp = _fingerprint_file_server(new_abs)
            if new_fp and new_fp != fingerprint:
                fingerprint_warning = (
                    "Fingerprint mismatch — this may not be the same file. "
                    "Proceeding because you explicitly specified the path."
                )
        except Exception as e:
            fingerprint_warning = f"Could not verify fingerprint: {e}"

    # Rewrite footage.jsonl
    try:
        _rewrite_footage_jsonl_entry(old_path, new_path)
    except Exception as e:
        return {"error": f"failed to update footage.jsonl: {e}"}

    result: dict = {
        "relinked": True,
        "old_path": old_path,
        "new_path": new_path,
    }
    if fingerprint_warning:
        result["warning"] = fingerprint_warning
    return result


def tool_move_to_shared(paths: list, workspace: Optional[Path] = None) -> dict:
    """Move files from current project into PROJECT_DIR/shared/."""
    if not paths:
        return {"error": "paths list is empty"}
    base = workspace or PROJECT_DIR
    shared_dir = PROJECT_DIR / "shared"
    try:
        shared_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"error": f"could not create shared/ directory: {e}"}

    moved = []
    errors = []
    for p in paths:
        try:
            resolved = _resolve_project_path(p, workspace=base)
        except PathError as e:
            errors.append({"path": p, "error": str(e)})
            continue
        if not resolved.exists():
            errors.append({"path": p, "error": "file not found"})
            continue
        if not resolved.is_file():
            errors.append({"path": p, "error": "not a regular file"})
            continue
        dest = shared_dir / resolved.name
        # Avoid collisions: suffix the name if dest exists
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 1
            while dest.exists():
                dest = shared_dir / f"{stem}_{counter}{suffix}"
                counter += 1
        try:
            shutil.move(str(resolved), str(dest))
            moved.append({"from": p, "to": str(dest.relative_to(PROJECT_DIR))})
        except Exception as e:
            errors.append({"path": p, "error": f"move failed: {e}"})

    result: dict = {"moved": moved}
    if errors:
        result["errors"] = errors
    return result


def tool_list_shared() -> dict:
    """List files in PROJECT_DIR/shared/."""
    shared_dir = PROJECT_DIR / "shared"
    if not shared_dir.exists():
        return {"files": [], "message": "shared/ directory does not exist yet"}
    entries = []
    try:
        for entry in sorted(shared_dir.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file():
                try:
                    st = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })
                except Exception as e:
                    entries.append({"name": entry.name, "error": str(e)})
    except Exception as e:
        return {"error": f"could not list shared/: {e}"}
    return {"files": entries, "total": len(entries)}


# ---------------------------------------------------------------------------
# Agent loop (Anthropic SDK)
# ---------------------------------------------------------------------------


def _lazy_client():
    try:
        import anthropic  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "the `anthropic` Python package is not installed. "
            "Run: pip install -r web/requirements.txt"
        ) from e
    from anthropic import Anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
    return Anthropic()


def _execute_tool_calls(
    session: Session, tool_uses: list[dict]
) -> list[dict]:
    results: list[dict] = []
    for tu in tool_uses:
        name = tu.get("name")
        tool_id = tu.get("id")
        args = tu.get("input") or {}
        session.broadcast(
            "tool_use",
            {"id": tool_id, "name": name, "input": args},
        )
        session.push_display("tool_use", {"id": tool_id, "name": name, "input": args})
        telemetry.record_event(
            session.id, "tool_use", {"id": tool_id, "name": name, "input": args}
        )
        _append_tool_log(
            session.workspace,
            {"role": "tool_call", "name": name, "input": args},
        )

        # Per-tool state populated by branches below; used when building tool_result broadcast.
        images_rel: list[str] = []
        _storyboard_rel_path: Optional[str] = None

        try:
            if name == "list_dir":
                out = tool_list_dir(args.get("path", "."), workspace=session.workspace)
                content_block = [
                    {"type": "text", "text": json.dumps(out, indent=2)}
                ]
                summary = out.get("error") or f"{len(out.get('entries', []))} entries"
            elif name == "read_file":
                out = tool_read_file(args.get("path", ""), workspace=session.workspace)
                if "error" in out:
                    content_block = [{"type": "text", "text": out["error"]}]
                    summary = out["error"]
                else:
                    content_block = [
                        {
                            "type": "text",
                            "text": f"# {out['path']} ({out['size']} bytes)\n\n{out['content']}",
                        }
                    ]
                    summary = f"{out['size']} bytes"
            elif name == "read_image":
                out = tool_read_image(args.get("path", ""), workspace=session.workspace)
                if "error" in out:
                    content_block = [{"type": "text", "text": out["error"]}]
                    summary = out["error"]
                else:
                    content_block = [
                        out["image_block"],
                        {"type": "text", "text": f"(image: {out['path']})"},
                    ]
                    summary = f"loaded {out['path']}"
            elif name == "make_storyboard":
                raw_path = (args.get("path") or "stills").strip()
                max_img = min(int(args.get("max_images") or 8), 8)
                try:
                    dir_path = _resolve_project_path(raw_path, workspace=session.workspace)
                except PathError as pe:
                    content_block = [{"type": "text", "text": f"error: {pe}"}]
                    summary = str(pe)
                else:
                    if not dir_path.exists() or not dir_path.is_dir():
                        msg = f"directory not found: {raw_path}"
                        content_block = [{"type": "text", "text": msg}]
                        summary = msg
                    else:
                        script = _HERE.parent / "tools" / "storyboard.py"
                        try:
                            result = subprocess.run(
                                [sys.executable, str(script), str(dir_path), "--max", str(max_img)],
                                capture_output=True, text=True, timeout=30,
                                stdin=subprocess.DEVNULL,
                            )
                        except Exception as e:
                            msg = f"storyboard failed: {e}"
                            content_block = [{"type": "text", "text": msg}]
                            summary = msg
                        else:
                            if result.returncode != 0:
                                msg = (result.stderr or "storyboard error").strip()
                                content_block = [{"type": "text", "text": msg}]
                                summary = msg
                            else:
                                out_path = Path(result.stdout.strip())
                                try:
                                    raw = out_path.read_bytes()
                                    b64 = base64.standard_b64encode(raw).decode("ascii")
                                    content_block = [
                                        {
                                            "type": "image",
                                            "source": {"type": "base64", "media_type": "image/png", "data": b64},
                                        },
                                        {"type": "text", "text": f"storyboard of {raw_path} ({max_img} images max)"},
                                    ]
                                    summary = f"storyboard: {out_path.name}"
                                    # Expose workspace-relative path so UI can render inline.
                                    try:
                                        _storyboard_rel_path = str(out_path.relative_to(session.workspace))
                                    except ValueError:
                                        pass  # storyboard outside workspace — skip preview
                                except Exception as e:
                                    msg = f"could not read storyboard output: {e}"
                                    content_block = [{"type": "text", "text": msg}]
                                    summary = msg
            elif name == "parallax_create":
                brief = args.get("brief", "")
                count = int(args.get("count") or 1)
                aspect = args.get("aspect_ratio") or "3:4"
                ref = args.get("ref")
                if not isinstance(brief, str) or not brief.strip():
                    result_text = "error: brief is required and must be a non-empty string"
                    result_paths: list[str] = []
                else:
                    create_result = tool_parallax_create(
                        session, brief=brief, count=count, aspect_ratio=aspect, ref=ref,
                    )
                    result_text = create_result["summary"]
                    result_paths = create_result["still_paths"]
                # Build the model-facing content block: text summary + each still as an image.
                content_block = [{"type": "text", "text": result_text}]
                for still_abs in result_paths:
                    try:
                        raw = Path(still_abs).read_bytes()
                        b64 = base64.standard_b64encode(raw).decode("ascii")
                        content_block.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": b64},
                        })
                    except Exception as img_err:
                        print(f"parallax_create: could not embed still {still_abs}: {img_err}",
                              file=sys.stderr, flush=True)
                summary = result_text[:2000]
                # Convert absolute paths → workspace-relative for the UI media endpoint.
                images_rel: list[str] = []
                for still_abs in result_paths:
                    try:
                        rel = str(Path(still_abs).relative_to(session.workspace))
                        images_rel.append(rel)
                    except ValueError:
                        pass
            elif name == "parallax_compose":
                text = tool_parallax_compose(session)
                content_block = [{"type": "text", "text": text}]
                summary = text[:2000]
            elif name == "parallax_voiceover":
                text = tool_parallax_voiceover(
                    session,
                    voice=args.get("voice"),
                    model_id=args.get("model_id"),
                    script=args.get("script"),
                )
                content_block = [{"type": "text", "text": text}]
                summary = text[:2000]
            elif name == "parallax_trim_silence":
                text = tool_parallax_trim_silence(session)
                content_block = [{"type": "text", "text": text}]
                summary = text[:2000]
            elif name == "parallax_align":
                text = tool_parallax_align(session)
                content_block = [{"type": "text", "text": text}]
                summary = text[:2000]
            elif name == "parallax_ingest":
                text = tool_parallax_ingest(
                    session,
                    path=args.get("path", "project_root"),
                    no_vision=bool(args.get("no_vision", False)),
                )
                content_block = [{"type": "text", "text": text}]
                summary = text[:2000]
            elif name == "parallax_transcribe":
                text = tool_parallax_transcribe(
                    session,
                    audio=args.get("audio"),
                    model=args.get("model"),
                    language=args.get("language"),
                )
                content_block = [{"type": "text", "text": text}]
                summary = text[:2000]
            elif name == "edit_manifest":
                op = args.get("op")
                if not op:
                    text = "error: op is required"
                else:
                    result = tool_edit_manifest(
                        workspace=session.workspace,
                        op=op,
                        values=args.get("values"),
                        duration=args.get("duration"),
                        motion=args.get("motion"),
                        start_s=args.get("start_s"),
                        end_s=args.get("end_s"),
                    )
                    if "error" in result:
                        text = f"manifest edit failed: {result['error']}"
                    else:
                        text = result.get("output") or f"manifest {op} ok"
                content_block = [{"type": "text", "text": text}]
                summary = text[:2000]
            elif name == "list_footage":
                out = tool_list_footage()
                content_block = [{"type": "text", "text": json.dumps(out, indent=2)}]
                summary = out.get("error") or f"{out.get('total', 0)} clip(s) indexed"
            elif name == "search_footage":
                query = args.get("query", "")
                if not query:
                    out = {"error": "query is required"}
                else:
                    out = tool_search_footage(query)
                content_block = [{"type": "text", "text": json.dumps(out, indent=2)}]
                summary = out.get("error") or f"{out.get('total_found', 0)} match(es) for {query!r}"
            elif name == "analyze_footage_segment":
                clip_path = args.get("path", "")
                start_t = args.get("start_time", "")
                end_t = args.get("end_time", "")
                if not clip_path or not start_t or not end_t:
                    out = {"error": "path, start_time, and end_time are required"}
                else:
                    out = tool_analyze_footage_segment(
                        path=clip_path,
                        start_time=start_t,
                        end_time=end_t,
                        question=args.get("question"),
                    )
                content_block = [{"type": "text", "text": json.dumps(out, indent=2)}]
                summary = out.get("error") or f"analyzed {clip_path} [{start_t}–{end_t}]"
            elif name == "relink_footage":
                old_p = args.get("old_path", "")
                new_p = args.get("new_path", "")
                if not old_p or not new_p:
                    out = {"error": "old_path and new_path are required"}
                else:
                    out = tool_relink_footage(old_path=old_p, new_path=new_p)
                content_block = [{"type": "text", "text": json.dumps(out, indent=2)}]
                summary = out.get("error") or f"relinked {old_p} → {new_p}"
            elif name == "parallax_fal_video":
                bin_path = _find_parallax_bin()
                if bin_path is None:
                    text = "parallax CLI binary not found on PATH"
                else:
                    fal_image = args.get("image", "")
                    fal_prompt = args.get("prompt", "")
                    fal_tier = args.get("tier") or "low"
                    fal_aspect = args.get("aspect_ratio") or "9:16"
                    fal_output = args.get("output") or ""
                    if not fal_image or not fal_prompt:
                        text = "error: image and prompt are required"
                    else:
                        try:
                            img_p = _resolve_project_path(fal_image, workspace=session.workspace, read_fallback=True)
                        except PathError as pe:
                            text = f"invalid image path {fal_image!r}: {pe}"
                            img_p = None
                        if img_p is not None:
                            if not fal_output:
                                ts = int(time.time())
                                fal_output = f"output/fal_{ts}.mp4"
                            out_p = session.workspace / fal_output
                            out_p.parent.mkdir(parents=True, exist_ok=True)
                            fal_cmd = [
                                bin_path, "fal", "video", fal_tier,
                                "--image", str(img_p),
                                "--prompt", fal_prompt,
                                "--aspect", fal_aspect,
                                "--output", str(out_p),
                            ]
                            text = _stream_parallax_subprocess(
                                session, fal_cmd, None,
                                f"fal video {fal_tier}: {fal_prompt[:60]}",
                            )
                content_block = [{"type": "text", "text": text}]
                summary = text[:2000]
            elif name == "move_to_shared":
                paths_arg = args.get("paths", [])
                if not isinstance(paths_arg, list):
                    paths_arg = [paths_arg] if paths_arg else []
                out = tool_move_to_shared(paths_arg, workspace=session.workspace)
                content_block = [{"type": "text", "text": json.dumps(out, indent=2)}]
                n_moved = len(out.get("moved", []))
                n_err = len(out.get("errors", []))
                summary = f"moved {n_moved} file(s)" + (f", {n_err} error(s)" if n_err else "")
            elif name == "list_shared":
                out = tool_list_shared()
                content_block = [{"type": "text", "text": json.dumps(out, indent=2)}]
                summary = out.get("error") or f"{out.get('total', 0)} file(s) in shared/"
            else:
                text = f"unknown tool: {name}"
                content_block = [{"type": "text", "text": text}]
                summary = text
        except Exception as e:
            tb = traceback.format_exc()
            print(
                f"tool {name} crashed: {e}\n{tb}", file=sys.stderr, flush=True
            )
            text = f"tool {name} crashed: {e}"
            content_block = [{"type": "text", "text": text}]
            summary = text

        # Attach inline image paths for tools that produce visuals.
        _tool_images: list[str] = []
        if name == "parallax_create":
            _tool_images = images_rel
        elif name == "make_storyboard" and _storyboard_rel_path:
            _tool_images = [_storyboard_rel_path]

        # Detect tool failures so the model can't misread a failed call as success.
        # Heuristics: _stream_parallax_subprocess returns "parallax exited rc=N" on failure;
        # tool_edit_manifest returns text starting with "manifest edit failed:";
        # subprocess-not-found returns "parallax CLI binary not found".
        _first_text = (
            content_block[0]["text"] if content_block and content_block[0].get("type") == "text" else ""
        )
        _is_error = (
            "parallax exited rc=" in _first_text
            or _first_text.startswith("manifest edit failed:")
            or _first_text.startswith("parallax CLI binary not found")
            or _first_text.startswith("failed to spawn parallax")
            or _first_text.startswith("invalid path:")
            or _first_text.startswith("tool " + name + " crashed")
        )
        if _is_error and not summary.startswith("ERROR"):
            summary = f"ERROR: {summary}"

        _tr_payload: dict = {"id": tool_id, "name": name, "summary": summary}
        if _tool_images:
            _tr_payload["images"] = _tool_images

        session.broadcast("tool_result", _tr_payload)
        session.push_display("tool_result", _tr_payload)
        telemetry.record_event(
            session.id,
            "tool_result",
            {"id": tool_id, "name": name, "summary": summary},
        )
        _append_tool_log(
            session.workspace,
            {"role": "tool_result", "name": name, "summary": summary},
        )

        _tool_result: dict = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": content_block,
        }
        if _is_error:
            _tool_result["is_error"] = True
        results.append(_tool_result)
    return results


def _chat_log_path(workspace: Path) -> Path:
    return workspace / "chat.jsonl"


def _append_chat_turn(workspace: Path, role: str, text: str) -> None:
    if not text:
        return
    try:
        path = _chat_log_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"role": role, "text": text, "ts": time.time()},
            ensure_ascii=False,
        ) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"chat.jsonl write failed: {e}", file=sys.stderr, flush=True)


def _append_tool_log(workspace: Path, entry: dict) -> None:
    try:
        path = _chat_log_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({**entry, "ts": time.time()}, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"chat.jsonl tool write failed: {e}", file=sys.stderr, flush=True)


def _load_chat_history(workspace: Path) -> list[dict[str, Any]]:
    path = _chat_log_path(workspace)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"chat.jsonl read failed: {e}", file=sys.stderr, flush=True)
    return out


# ---------------------------------------------------------------------------
# Mock Anthropic stream for TEST_MODE
# ---------------------------------------------------------------------------

class _MockUsage:
    input_tokens = 0
    output_tokens = 0


class _MockTextBlock:
    type = "text"
    def __init__(self, text: str):
        self.text = text


class _MockToolUseBlock:
    type = "tool_use"
    def __init__(self, tool_id: str, name: str, input_args: dict):
        self.id = tool_id
        self.name = name
        self.input = input_args


class _MockFinalMessage:
    stop_reason: str
    usage: _MockUsage
    content: list

    def __init__(self, text_blocks: list, tool_blocks: list):
        self.content = text_blocks + tool_blocks
        self.stop_reason = "tool_use" if tool_blocks else "end_turn"
        self.usage = _MockUsage()


class _MockDeltaEvent:
    type = "content_block_delta"
    def __init__(self, text: str):
        self.delta = type("delta", (), {"type": "text_delta", "text": text})()


class _MockStreamCtx:
    """Context manager that mimics the Anthropic streaming interface."""

    def __init__(self, text: str, tool_block: Optional[dict]):
        self._text = text
        self._tool_block = tool_block

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        yield _MockDeltaEvent(self._text)

    def get_final_message(self) -> _MockFinalMessage:
        text_blocks = [_MockTextBlock(self._text)] if self._text else []
        tool_blocks = []
        if self._tool_block:
            tool_blocks.append(
                _MockToolUseBlock(
                    self._tool_block["id"],
                    self._tool_block["name"],
                    self._tool_block["input"],
                )
            )
        return _MockFinalMessage(text_blocks, tool_blocks)


def _mock_anthropic_stream(messages: list, tools: list) -> _MockStreamCtx:
    """Return a mock stream based on the latest user message content."""
    user_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                user_text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        user_text += block.get("text", "")
            break
    lower = user_text.lower()
    tool_block: Optional[dict] = None
    reply_text = "TEST_MODE: mock reply. No API call made."
    if "still" in lower:
        reply_text = "TEST_MODE: invoking parallax_create for a still."
        tool_block = {
            "id": f"mock_tu_{uuid.uuid4().hex[:8]}",
            "name": "parallax_create",
            "input": {"description": user_text[:200], "output": "stills/mock_still.png"},
        }
    elif "compose" in lower:
        reply_text = "TEST_MODE: invoking parallax_compose."
        tool_block = {
            "id": f"mock_tu_{uuid.uuid4().hex[:8]}",
            "name": "parallax_compose",
            "input": {"manifest": "manifest.json", "output": "output/mock_video.mp4"},
        }
    elif "voiceover" in lower:
        reply_text = "TEST_MODE: invoking parallax_voiceover."
        tool_block = {
            "id": f"mock_tu_{uuid.uuid4().hex[:8]}",
            "name": "parallax_voiceover",
            "input": {"script": user_text[:200], "output": "audio/mock_vo.mp3"},
        }
    return _MockStreamCtx(reply_text, tool_block)


def run_agent_turn(session: Session, user_text: str) -> None:
    """Agent loop body — runs in a background thread per POST /api/message."""
    use_mock = TEST_MODE or getattr(session, "test_mode", False)
    if not use_mock:
        try:
            client = _lazy_client()
        except Exception as e:
            session.broadcast("error", {"message": str(e)})
            telemetry.record_event(session.id, "error", {"where": "client_init", "message": str(e)})
            session.broadcast("agent_done", {"ok": False})
            return

    user_content: list[dict] = [{"type": "text", "text": user_text}]
    session.messages.append({"role": "user", "content": user_content})
    session.push_display("user_message", {"text": user_text})
    telemetry.record_event(session.id, "user_message", {"text": user_text})
    _append_chat_turn(session.workspace, "user", user_text)

    head_of_production_prompt = _load_head_of_production_prompt()

    MAX_ITER = 12
    for _iter in range(MAX_ITER):
        if session.cancel_event.is_set():
            session.broadcast("agent_done", {"ok": False, "cancelled": True})
            return

        try:
            _stream_ctx = (
                _mock_anthropic_stream(session.messages, TOOL_SCHEMAS)
                if use_mock
                else client.messages.stream(  # type: ignore[union-attr]
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=head_of_production_prompt,
                    tools=TOOL_SCHEMAS,  # type: ignore[arg-type]
                    messages=session.messages,  # type: ignore[arg-type]
                )
            )
            with _stream_ctx as stream:
                for event in stream:
                    if session.cancel_event.is_set():
                        break
                    etype = getattr(event, "type", "")
                    if etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is not None and getattr(delta, "type", "") == "text_delta":
                            session.broadcast(
                                "assistant_delta", {"text": delta.text}
                            )
                final = stream.get_final_message()
        except Exception as e:
            tb = traceback.format_exc()
            print(f"agent: stream error: {e}\n{tb}", file=sys.stderr, flush=True)
            session.broadcast("error", {"message": f"stream error: {e}"})
            telemetry.record_event(
                session.id, "error", {"where": "stream", "message": str(e)}
            )
            session.broadcast("agent_done", {"ok": False})
            return

        blocks: list[dict] = []
        text_chunks: list[str] = []
        tool_uses: list[dict] = []
        for block in final.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                text = getattr(block, "text", "") or ""
                blocks.append({"type": "text", "text": text})
                text_chunks.append(text)
            elif btype == "tool_use":
                tu = {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}),
                }
                blocks.append(tu)
                tool_uses.append(tu)

        if text_chunks:
            joined = "".join(text_chunks)
            session.push_display("assistant_text", {"text": joined})
            telemetry.record_event(
                session.id, "assistant_text", {"text": joined}
            )
            _append_chat_turn(session.workspace, "assistant", joined)

        try:
            usage = getattr(final, "usage", None)
            if usage is not None:
                in_tok = getattr(usage, "input_tokens", 0) or 0
                out_tok = getattr(usage, "output_tokens", 0) or 0
                cost_delta = _model_cost_usd(MODEL, in_tok, out_tok)
                telemetry.touch_session(
                    session.id,
                    input_tokens_delta=in_tok,
                    output_tokens_delta=out_tok,
                    cost_delta_usd=cost_delta,
                )
                telemetry.record_event(
                    session.id,
                    "anthropic_usage",
                    {
                        "model": MODEL,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "cost_usd": round(cost_delta, 6),
                    },
                )
        except Exception as e:
            print(f"agent: usage update failed: {e}", file=sys.stderr, flush=True)

        session.messages.append({"role": "assistant", "content": blocks})

        stop_reason = getattr(final, "stop_reason", None)
        if stop_reason != "tool_use" or not tool_uses:
            session.broadcast("agent_done", {"ok": True})
            return

        tool_results = _execute_tool_calls(session, tool_uses)
        session.messages.append({"role": "user", "content": tool_results})

    session.broadcast(
        "agent_done", {"ok": False, "error": "max iterations exceeded"}
    )


def start_agent_thread(session: Session, user_text: str) -> None:
    session.cancel_event.clear()

    def _run():
        try:
            run_agent_turn(session, user_text)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"agent thread crashed: {e}\n{tb}", file=sys.stderr, flush=True)
            session.broadcast("error", {"message": f"agent crashed: {e}"})

    t = threading.Thread(target=_run, name=f"agent-{session.id[:8]}", daemon=True)
    session.agent_thread = t
    t.start()


# ---------------------------------------------------------------------------
# Gallery
# ---------------------------------------------------------------------------

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".webm")


def _scan(dir_path: Path, exts: tuple[str, ...], base: Path) -> list[dict]:
    out: list[dict] = []
    if not dir_path.is_dir():
        return out
    try:
        for entry in dir_path.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in exts:
                continue
            try:
                st = entry.stat()
            except Exception:
                continue
            out.append(
                {
                    "name": entry.name,
                    "path": str(entry.relative_to(base)),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
    except Exception as e:
        print(f"gallery: scan failed in {dir_path}: {e}", file=sys.stderr, flush=True)
    return out


def list_gallery(workspace: Optional[Path] = None) -> dict:
    base = workspace or PROJECT_DIR
    stills = _scan(base / "stills", IMAGE_EXTS, base)
    stills += _scan(base / "input", IMAGE_EXTS, base)
    stills.sort(key=lambda d: d["mtime"], reverse=True)

    videos: list[dict] = []
    videos += _scan(base / "output", VIDEO_EXTS, base)
    videos += _scan(base / "drafts", VIDEO_EXTS, base)
    videos += _scan(base / "input", VIDEO_EXTS, base)
    videos.sort(key=lambda d: d["mtime"], reverse=True)

    return {
        "project_dir": str(base),
        "stills": stills,
        "videos": videos,
    }


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

_PASSWORD = os.environ.get("PARALLAX_WEB_PASSWORD", "")


def _check_auth(request: Request) -> bool:
    if not _PASSWORD:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        _, password = decoded.split(":", 1)
        return password == _PASSWORD
    except Exception:
        return False


def _require_auth(request: Request) -> None:
    if not _check_auth(request):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Parallax"'},
        )


def _get_user(request: Request) -> str:
    user_param = request.query_params.get("user", "").strip()
    if user_param:
        return user_param
    return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))


def _get_project(request: Request) -> str:
    proj_param = request.query_params.get("project", "").strip()
    if proj_param:
        return proj_param
    return "main"


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------


class MessageRequest(BaseModel):
    text: str
    session_id: Optional[str] = None
    reference_images: Optional[List[str]] = None


class CancelRequest(BaseModel):
    session_id: str


class OpenInFinderRequest(BaseModel):
    path: Optional[str] = None


class DeleteImageRequest(BaseModel):
    path: str


class CreateProjectRequest(BaseModel):
    name: str


class RenameProjectRequest(BaseModel):
    new_name: str


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    telemetry.init_db()
    if not PER_USER_WORKSPACES:
        try:
            _ensure_workspace(_workspace_for(None, "main"))
        except Exception as e:
            print(f"parallax-web: default workspace scaffold failed: {e}", file=sys.stderr)
            server_log.log_exception("workspace_scaffold_failed", e)
    port = int(os.environ.get("_PARALLAX_PORT", "0"))
    host = os.environ.get("PARALLAX_WEB_HOST", "127.0.0.1")
    try:
        registry.install_shutdown_hooks()
        registry.register(
            cwd=str(PROJECT_DIR),
            host=host,
            port=port,
            user=os.environ.get("USER", "unknown"),
        )
    except Exception as e:
        print(f"parallax-web: registry register failed: {e}", file=sys.stderr)
        server_log.log_exception("registry_register_failed", e)
    if os.environ.get("PARALLAX_WEB_NO_BROWSER") != "1" and port:
        url = f"http://{host}:{port}/"
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"parallax-web: webbrowser.open failed: {e}", file=sys.stderr)
            server_log.log_exception("webbrowser_open_failed", e, url=url)
    yield
    # Shutdown
    try:
        registry.deregister()
    except Exception as e:
        server_log.log_exception("registry_deregister_failed", e)
    server_log.log("shutdown")


app = FastAPI(title="parallax-web", lifespan=lifespan)


@app.exception_handler(Exception)
async def _log_uncaught_exception(request: Request, exc: Exception):
    server_log.log_exception(
        "request_error",
        exc,
        path=str(request.url.path),
        method=request.method,
    )
    if isinstance(exc, HTTPException):
        raise exc
    return JSONResponse(
        status_code=500,
        content={"detail": f"internal server error: {type(exc).__name__}"},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files if the directory exists
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Routes — GET
# ---------------------------------------------------------------------------


def _serve_shell(request: Request) -> FileResponse:
    _require_auth(request)
    shell = STATIC_DIR / "app.html"
    if not shell.is_file():
        raise HTTPException(status_code=404, detail="app.html not found")
    return FileResponse(str(shell), media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/")
async def serve_index(request: Request):
    return _serve_shell(request)


@app.get("/timeline")
async def serve_timeline_page(request: Request):
    return _serve_shell(request)


@app.get("/costs")
async def serve_costs_page(request: Request):
    return _serve_shell(request)


@app.get("/manifest")
async def serve_manifest_page(request: Request):
    return _serve_shell(request)


@app.get("/media")
async def serve_media_page(request: Request):
    return _serve_shell(request)


@app.get("/media/{rel_path:path}")
async def serve_project_file(rel_path: str, request: Request):
    _require_auth(request)
    workspace = _workspace_for(_get_user(request), _get_project(request))
    try:
        target = _resolve_project_path(rel_path, workspace=workspace, read_fallback=True)
    except PathError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"not found: {rel_path}")
    mime, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        str(target),
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/gallery")
async def api_gallery(request: Request):
    _require_auth(request)
    workspace = _workspace_for(_get_user(request), _get_project(request))
    return JSONResponse(list_gallery(workspace=workspace))


@app.get("/api/sessions")
async def api_list_sessions(request: Request):
    _require_auth(request)
    request_user = _get_user(request) if PER_USER_WORKSPACES else None
    try:
        sessions = telemetry.list_sessions(limit=50, user=request_user)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"sessions query failed: {e}")

    with SESSIONS_LOCK:
        live_ids = {
            sid for sid, s in SESSIONS.items()
            if s.agent_thread and s.agent_thread.is_alive()
        }

    sessions_out = []
    for s in sessions:
        preview = (s.get("first_user_message") or "")[:80]
        sid = s.get("id")
        project_dir = s.get("project_dir") or ""
        try:
            project_name = Path(project_dir).name if project_dir else "main"
        except Exception:
            project_name = "main"
        sessions_out.append({
            "id": sid,
            "started_at": s.get("started_at"),
            "last_activity_at": s.get("last_activity_at"),
            "event_count": s.get("event_count", 0),
            "preview": preview,
            "project": project_name,
            "user": s.get("user") or "",
            "live": sid in live_ids,
        })

    return JSONResponse({"sessions": sessions_out})


@app.get("/api/projects")
async def api_list_projects(request: Request):
    _require_auth(request)
    user = _get_user(request)
    if PER_USER_WORKSPACES and user:
        user_root = (_users_root() / _sanitize_name(user)).resolve()
    else:
        user_root = _workspace_root()

    projects = []
    try:
        if user_root.exists():
            for entry in sorted(user_root.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    projects.append({"name": entry.name, "path": str(entry)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to list projects: {e}")

    return JSONResponse({"projects": projects, "user": user or ""})


@app.get("/api/usage")
async def api_usage(request: Request):
    _require_auth(request)
    user = _get_user(request)
    now = time.time()
    day_start = now - 86400
    try:
        lifetime = telemetry.usage_for_user(user)
        today = telemetry.usage_for_user(user, since_ts=day_start)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"usage query failed: {e}")
    return JSONResponse({"user": user, "today": today, "lifetime": lifetime})


@app.get("/api/costs")
async def api_costs(request: Request):
    _require_auth(request)
    user_param = request.query_params.get("user", "").strip() or None
    try:
        report = costs.build_report(user_param)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"costs build failed: {e}")
    report["fal"] = _fal_whoami()
    return JSONResponse(report)


@app.get("/api/manifest")
async def api_manifest(request: Request):
    _require_auth(request)
    workspace = _workspace_for(_get_user(request), _get_project(request))
    manifest_path = workspace / "manifest.yaml"
    if not manifest_path.is_file():
        return JSONResponse({
            "exists": False,
            "workspace": str(workspace),
            "manifest": None,
            "voiceover": None,
            "scenes": [],
        })
    try:
        import yaml as _yaml
        with open(manifest_path, "r", encoding="utf-8") as f:
            raw = _yaml.safe_load(f) or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"manifest read failed: {e}")

    scenes_out: list[dict] = []
    for s in (raw.get("scenes") or []):
        if not isinstance(s, dict):
            continue
        still_rel = s.get("still") or ""
        still_url = f"/media/{still_rel}" if still_rel else None
        scenes_out.append({
            "number": s.get("number"),
            "title": s.get("title"),
            "duration": s.get("duration"),
            "still": still_rel,
            "still_url": still_url,
            "motion": s.get("motion"),
            "vo_text": s.get("vo_text") or "",
        })

    vo = raw.get("voiceover") or {}
    vo_out = None
    if isinstance(vo, dict) and vo:
        audio_rel = vo.get("audio_file") or ""
        vo_out = {
            "audio_file": audio_rel,
            "audio_url": f"/media/{audio_rel}" if audio_rel else None,
            "vo_manifest": vo.get("vo_manifest"),
            "duration_s": vo.get("duration_s"),
            "script": vo.get("script"),
            "transcribed_by": vo.get("transcribed_by"),
        }

    return JSONResponse({
        "exists": True,
        "workspace": str(workspace),
        "project": workspace.name,
        "brief": raw.get("brief"),
        "concept_id": raw.get("concept_id"),
        "voice": raw.get("voice") or {},
        "voiceover": vo_out,
        "headline": raw.get("headline"),
        "captions": raw.get("captions") or {},
        "scenes": scenes_out,
        "raw": raw,
    })


@app.get("/api/servers")
async def api_servers(request: Request):
    _require_auth(request)
    return JSONResponse({"servers": registry.list_servers()})


@app.get("/api/chat")
async def api_chat(request: Request):
    _require_auth(request)
    workspace = _workspace_for(_get_user(request), _get_project(request))
    return JSONResponse({
        "workspace": str(workspace),
        "project": workspace.name,
        "turns": _load_chat_history(workspace),
    })


@app.get("/api/session/{session_id}/history")
async def api_session_history(session_id: str, request: Request):
    _require_auth(request)
    events = telemetry.load_session_events(session_id)
    messages = []
    for ev in events:
        kind = ev.get("kind", "")
        p = ev.get("payload", {})
        try:
            if kind == "user_message":
                messages.append({"role": "user", "text": p.get("text", "")})
            elif kind == "assistant_text":
                messages.append({"role": "assistant", "text": p.get("text", "")})
            elif kind == "tool_use":
                messages.append({
                    "role": "tool_use",
                    "name": p.get("name", ""),
                    "args": p.get("input", {}),
                    "tool_id": p.get("id", ""),
                })
            elif kind == "tool_result":
                messages.append({
                    "role": "tool_result",
                    "tool_id": p.get("id", ""),
                    "summary": p.get("summary", ""),
                })
            elif kind == "dispatch_start":
                mode = p.get("mode", "")
                preview = p.get("brief_preview", "")
                messages.append({
                    "role": "dispatch",
                    "phase": "starting",
                    "text": f"dispatching ({mode}): {preview}",
                })
            elif kind == "dispatch_complete":
                cancelled = p.get("cancelled")
                if cancelled:
                    text = "dispatch cancelled"
                else:
                    rc = p.get("rc")
                    out = p.get("output_path", "")
                    err = p.get("error", "")
                    if rc == 0:
                        text = f"render complete: {out}" if out else "render complete"
                    else:
                        text = f"dispatch failed rc={rc}: {err}" if err else f"dispatch failed rc={rc}"
                messages.append({"role": "dispatch", "phase": "done", "text": text})
            elif kind == "dispatch_error":
                messages.append({
                    "role": "dispatch",
                    "phase": "error",
                    "text": p.get("error", "dispatch error"),
                })
            elif kind == "error":
                messages.append({"role": "error", "text": p.get("message", str(p))})
        except Exception as e:
            print(
                f"history: failed to convert event kind={kind}: {e}",
                file=sys.stderr,
                flush=True,
            )
    return JSONResponse({"session_id": session_id, "messages": messages})


@app.get("/api/session/{session_id}")
async def api_session(session_id: str, request: Request):
    _require_auth(request)
    s = SESSIONS.get(session_id)
    if s is None:
        return JSONResponse({"session_id": session_id, "log": telemetry.load_session_events(session_id)})
    return JSONResponse({"session_id": session_id, "log": s.display_log})


@app.get("/api/stream/{session_id}")
async def api_stream(session_id: str, request: Request):
    _require_auth(request)
    session = get_or_create_session(session_id)
    q = session.subscribe()

    async def event_generator():
        # Initial hello
        yield {
            "event": "hello",
            "data": json.dumps({"session_id": session_id}),
        }
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Poll with a short timeout so we can check disconnect
                    evt = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: q.get(timeout=15.0)
                    )
                except queue.Empty:
                    # Keep-alive ping
                    yield {"event": "ping", "data": "{}"}
                    continue
                kind = evt.get("kind", "message")
                data = evt.get("data", {})
                yield {
                    "event": kind,
                    "data": json.dumps(data, default=str),
                }
        finally:
            session.unsubscribe(q)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Routes — POST
# ---------------------------------------------------------------------------


@app.post("/api/message")
async def api_message(body: MessageRequest, request: Request):
    _require_auth(request)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    sid = body.session_id
    user = _get_user(request)
    project = _get_project(request)
    session = get_or_create_session(
        sid if isinstance(sid, str) else None,
        user=user, project=project,
    )
    ref_images = body.reference_images
    if isinstance(ref_images, list):
        session.selected_refs = [str(p) for p in ref_images if p]
    if session.selected_refs:
        paths_str = ", ".join(session.selected_refs)
        text = f"[Reference images selected: {paths_str}]\n\n{text}"
    if re.search(r"\bTEST\s+MODE\b", text, re.IGNORECASE):
        session.test_mode = True
        print(f"session {session.id}: TEST MODE enabled", file=sys.stderr, flush=True)
    start_agent_thread(session, text)
    return JSONResponse({"session_id": session.id, "project": session.project})


@app.post("/api/projects")
async def api_create_project(body: CreateProjectRequest, request: Request):
    _require_auth(request)
    name = _sanitize_name(body.name or "")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    user = _get_user(request)
    try:
        workspace = _workspace_for(user, name, scaffold=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to create project: {e}")
    return JSONResponse({"name": name, "path": str(workspace)})


@app.post("/api/cancel")
async def api_cancel(body: CancelRequest, request: Request):
    _require_auth(request)
    sid = body.session_id
    s = SESSIONS.get(sid)
    if s is None:
        raise HTTPException(status_code=404, detail="no such session")
    s.cancel_event.set()
    proc = s.dispatch_proc
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    return JSONResponse({"ok": True})


@app.post("/api/upload")
async def api_upload(request: Request):
    _require_auth(request)
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=400, detail="Content-Type must be multipart/form-data")

    try:
        form = await request.form()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"form parse failed: {e}")

    _upload_raw = form.get("file")
    if _upload_raw is None or isinstance(_upload_raw, str):
        raise HTTPException(status_code=400, detail="no file field in upload")
    upload_file = _upload_raw

    try:
        file_data = await upload_file.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")

    if not file_data:
        raise HTTPException(status_code=400, detail="file too large or empty (max 500 MB)")
    if len(file_data) > 500 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="file too large (max 500 MB)")

    filename = upload_file.filename or ""
    filename = os.path.basename(filename)
    safe_chars = []
    prev_underscore = False
    for c in filename:
        if (c.isascii() and c.isalnum()) or c in "._-":
            safe_chars.append(c)
            prev_underscore = False
        else:
            if not prev_underscore:
                safe_chars.append("_")
                prev_underscore = True
    safe_name = "".join(safe_chars).strip("_")[:200]
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")

    workspace = _workspace_for(
        _get_user(request), _get_project(request), scaffold=True,
    )
    input_dir = workspace / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    target = input_dir / safe_name

    try:
        target.write_bytes(file_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"write failed: {e}")

    rel_path = str(target.relative_to(workspace))
    return JSONResponse({"ok": True, "path": rel_path, "size": len(file_data)})


@app.post("/api/open_in_finder")
async def api_open_in_finder(body: OpenInFinderRequest, request: Request):
    _require_auth(request)
    user_path = (body.path or "").strip()
    workspace = _workspace_for(_get_user(request), _get_project(request))
    if user_path:
        try:
            target = _resolve_project_path(user_path, workspace=workspace)
        except PathError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        target = workspace
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"path does not exist: {target}")
    try:
        result = subprocess.run(
            ["open", str(target)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"open failed: {e}")
    if result.returncode != 0:
        err = (result.stderr or "").strip() or f"open exit {result.returncode}"
        raise HTTPException(status_code=500, detail=f"open failed: {err}")
    return JSONResponse({"ok": True, "path": str(target)})


# ---------------------------------------------------------------------------
# Routes — DELETE
# ---------------------------------------------------------------------------


@app.delete("/api/image")
async def api_delete_image(body: DeleteImageRequest, request: Request):
    _require_auth(request)
    rel = (body.path or "").strip()
    if not rel:
        raise HTTPException(status_code=400, detail="path is required")
    workspace = _workspace_for(_get_user(request), _get_project(request))
    try:
        target = _resolve_project_path(rel, workspace=workspace)
    except PathError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "Finder" to delete POSIX file "{target}"'],
            check=True, capture_output=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"trash failed: {e}")
    return JSONResponse({"ok": True, "path": rel})


@app.delete("/api/session/{session_id}")
async def api_delete_session(session_id: str, request: Request):
    _require_auth(request)
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    with SESSIONS_LOCK:
        live = SESSIONS.get(session_id)
    if live:
        live.cancel_event.set()
        if live.dispatch_proc:
            try:
                live.dispatch_proc.kill()
            except Exception:
                pass
    removed = telemetry.delete_session(session_id)
    with SESSIONS_LOCK:
        SESSIONS.pop(session_id, None)
    return JSONResponse({"ok": True, "removed": removed})


@app.delete("/api/projects/{name}")
async def api_delete_project(name: str, request: Request):
    _require_auth(request)
    name = _sanitize_name(name or "")
    if not name:
        raise HTTPException(status_code=400, detail="project name is required")
    if name == "main":
        raise HTTPException(status_code=400, detail="cannot delete the default 'main' project")
    current = _get_project(request) or "main"
    if name == current:
        raise HTTPException(
            status_code=400,
            detail=f"cannot delete the active project '{name}'. Switch to another project first.",
        )
    user = _get_user(request)
    if PER_USER_WORKSPACES and user:
        workspace = (_users_root() / _sanitize_name(user) / name).resolve()
        scope = _users_root() / _sanitize_name(user)
    else:
        workspace = (_workspace_root() / name).resolve()
        scope = _workspace_root()
    try:
        workspace.relative_to(scope.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid project path")
    if not workspace.is_dir():
        raise HTTPException(status_code=404, detail=f"project '{name}' not found")
    try:
        shutil.rmtree(workspace)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to delete: {e}")
    return JSONResponse({"ok": True, "deleted": name})


def _archive_root(user: Optional[str] = None) -> Path:
    if PER_USER_WORKSPACES and user:
        return _users_root() / _sanitize_name(user) / ".archive"
    return _workspace_root() / ".archive"


@app.get("/api/projects/archived")
async def api_list_archived_projects(request: Request):
    _require_auth(request)
    user = _get_user(request)
    root = _archive_root(user)
    projects = []
    try:
        if root.exists():
            for entry in sorted(root.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    meta_file = entry / "_meta.json"
                    archived_date = None
                    if meta_file.exists():
                        try:
                            meta = json.loads(meta_file.read_text())
                            archived_date = meta.get("archived_date")
                        except Exception:
                            pass
                    projects.append({"name": entry.name, "archived_date": archived_date})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to list archived: {e}")
    return JSONResponse({"projects": projects})


@app.post("/api/projects/{name}/archive")
async def api_archive_project(name: str, request: Request):
    _require_auth(request)
    name = _sanitize_name(name or "")
    if not name or name == "main":
        raise HTTPException(status_code=400, detail="cannot archive 'main' project")
    current = _get_project(request) or "main"
    if name == current:
        raise HTTPException(
            status_code=400,
            detail=f"cannot archive the active project '{name}'. Switch to another project first.",
        )
    user = _get_user(request)
    workspace = _workspace_for(user, name)
    if not workspace.is_dir():
        raise HTTPException(status_code=404, detail=f"project '{name}' not found")
    archive_root = _archive_root(user)
    archive_root.mkdir(parents=True, exist_ok=True)
    dest = archive_root / name
    if dest.exists():
        raise HTTPException(status_code=400, detail=f"archived project '{name}' already exists — delete it first")
    try:
        import datetime as _dt
        shutil.move(str(workspace), str(dest))
        (dest / "_meta.json").write_text(json.dumps({"archived_date": _dt.date.today().isoformat()}))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to archive: {e}")
    return JSONResponse({"ok": True, "archived": name})


@app.post("/api/projects/{name}/unarchive")
async def api_unarchive_project(name: str, request: Request):
    _require_auth(request)
    name = _sanitize_name(name or "")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    user = _get_user(request)
    src = _archive_root(user) / name
    if not src.is_dir():
        raise HTTPException(status_code=404, detail=f"archived project '{name}' not found")
    dest = _workspace_for(user, name)
    if dest.exists():
        raise HTTPException(status_code=400, detail=f"project '{name}' already exists in active workspace")
    try:
        meta_file = src / "_meta.json"
        if meta_file.exists():
            meta_file.unlink()
        shutil.move(str(src), str(dest))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to unarchive: {e}")
    return JSONResponse({"ok": True, "unarchived": name})


@app.post("/api/projects/{name}/rename")
async def api_rename_project(name: str, body: RenameProjectRequest, request: Request):
    _require_auth(request)
    name = _sanitize_name(name or "")
    new_name = _sanitize_name(body.new_name or "")
    if not name or not new_name:
        raise HTTPException(status_code=400, detail="name is required")
    if name == "main":
        raise HTTPException(status_code=400, detail="cannot rename 'main' project")
    if name == new_name:
        return JSONResponse({"ok": True, "name": name})
    user = _get_user(request)
    workspace = _workspace_for(user, name)
    if not workspace.is_dir():
        raise HTTPException(status_code=404, detail=f"project '{name}' not found")
    new_workspace = _workspace_for(user, new_name)
    if new_workspace.exists():
        raise HTTPException(status_code=400, detail=f"project '{new_name}' already exists")
    try:
        workspace.rename(new_workspace)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to rename: {e}")
    return JSONResponse({"ok": True, "name": new_name})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def find_free_port() -> int:
    forced = os.environ.get("PARALLAX_WEB_PORT")
    if forced:
        try:
            return int(forced)
        except ValueError:
            pass
    host = "0.0.0.0" if os.environ.get("PARALLAX_WEB_HOST") else "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def preflight() -> None:
    """Validate required environment before spawning the server."""
    if TEST_MODE:
        print(
            "parallax-web: TEST_MODE enabled — API key not required, mock stream active",
            flush=True,
        )
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "parallax-web: ANTHROPIC_API_KEY is not set. "
            "Export it before running.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print(
            "parallax-web: the `anthropic` package is not installed.\n"
            "Install with: pip install -r web/requirements.txt",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)


def main() -> int:
    preflight()

    port = find_free_port()
    host = os.environ.get("PARALLAX_WEB_HOST", "127.0.0.1")
    bind_host = "0.0.0.0" if host != "127.0.0.1" else "127.0.0.1"
    url = f"http://{host}:{port}/"
    auth_on = bool(os.environ.get("PARALLAX_WEB_PASSWORD"))
    print(f"parallax-web: project  = {PROJECT_DIR}")
    print(f"parallax-web: model    = {MODEL}")
    print(f"parallax-web: url      = {url}")
    try:
        _content = _discover_project_content()
        _nv, _ni, _na = len(_content["videos"]), len(_content["images"]), len(_content["audio"])
        if _nv or _ni or _na:
            print(
                f"parallax-web: content  = {_nv} video, {_ni} image, {_na} audio at project_root"
            )
        else:
            print("parallax-web: content  = (none at project_root)")
    except Exception as _e:
        print(f"parallax-web: content  = scan failed ({_e})")
    if auth_on:
        print("parallax-web: auth     = enabled (Basic Auth)")
    else:
        print("parallax-web: auth     = DISABLED — set PARALLAX_WEB_PASSWORD to protect")
    server_log.log(
        "startup",
        project_dir=str(PROJECT_DIR),
        host=host,
        port=port,
        model=MODEL,
        auth=auth_on,
    )

    # Pass port to lifespan via env so the registry gets the real port
    os.environ["_PARALLAX_PORT"] = str(port)

    import uvicorn
    uvicorn.run(
        app,
        host=bind_host,
        port=port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
