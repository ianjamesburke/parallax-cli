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
HOP_PROMPT_PATH = _HERE / "hop_prompt.md"

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


def _load_hop_prompt() -> str:
    try:
        return HOP_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        print(
            f"server: failed to load hop_prompt.md: {e}", file=sys.stderr, flush=True
        )
        return "You are the Head of Production for Parallax."


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
                    "description": "Directory path, relative to project root. Use '.' for the root.",
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
                    "description": "Number of variations to generate (default: 3).",
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
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_list_dir(path: str, workspace: Optional[Path] = None) -> dict:
    base = workspace or PROJECT_DIR
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
        "path": str(resolved.relative_to(base)) or ".",
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
        "path": str(resolved.relative_to(base)),
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
        "path": str(resolved.relative_to(base)),
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
    return env


def _stream_parallax_subprocess(
    session: Session,
    cmd: list[str],
    stdin_payload: Optional[str],
    label: str,
) -> str:
    if cmd and not cmd[0].endswith("python3") and not cmd[0].endswith("python"):
        cmd = [sys.executable] + cmd
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
    count: int = 3,
    aspect_ratio: str = "3:4",
    ref: Optional[list] = None,
) -> str:
    bin_path = _find_parallax_bin()
    if bin_path is None:
        return "parallax CLI binary not found on PATH"
    if not ref and session.selected_refs:
        ref = list(session.selected_refs)
    resolved_refs = []
    if ref:
        for r in ref:
            try:
                p = _resolve_project_path(r, workspace=session.workspace, read_fallback=True)
            except PathError as e:
                return f"invalid ref path {r!r}: {e}"
            if not p.exists():
                return f"ref image not found: {r}"
            resolved_refs.append(str(p))
    spec = {"brief": brief, "count": count, "aspect_ratio": aspect_ratio}
    if resolved_refs:
        spec["ref"] = resolved_refs
    cmd = [bin_path, "create", "--stdin", "--json"]
    ref_label = f", refs={len(resolved_refs)}" if resolved_refs else ""
    label = f"create: {brief[:100]} (count={count}, aspect={aspect_ratio}{ref_label})"
    return _stream_parallax_subprocess(session, cmd, json.dumps(spec), label)


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
        cmd += [str(v) for v in values]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            cwd=str(workspace), env=_clean_subprocess_env(),
        )
    except Exception as e:
        return {"error": f"manifest edit failed: {e}"}

    if result.returncode != 0:
        return {"error": (result.stderr or result.stdout).strip()}
    return {"output": result.stdout.strip()}


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
                                except Exception as e:
                                    msg = f"could not read storyboard output: {e}"
                                    content_block = [{"type": "text", "text": msg}]
                                    summary = msg
            elif name == "parallax_create":
                brief = args.get("brief", "")
                count = int(args.get("count") or 3)
                aspect = args.get("aspect_ratio") or "3:4"
                ref = args.get("ref")
                if not isinstance(brief, str) or not brief.strip():
                    text = "error: brief is required and must be a non-empty string"
                else:
                    text = tool_parallax_create(
                        session, brief=brief, count=count, aspect_ratio=aspect, ref=ref,
                    )
                content_block = [{"type": "text", "text": text}]
                summary = text[:200]
            elif name == "parallax_compose":
                text = tool_parallax_compose(session)
                content_block = [{"type": "text", "text": text}]
                summary = text[:200]
            elif name == "parallax_voiceover":
                text = tool_parallax_voiceover(
                    session,
                    voice=args.get("voice"),
                    model_id=args.get("model_id"),
                    script=args.get("script"),
                )
                content_block = [{"type": "text", "text": text}]
                summary = text[:200]
            elif name == "parallax_trim_silence":
                text = tool_parallax_trim_silence(session)
                content_block = [{"type": "text", "text": text}]
                summary = text[:200]
            elif name == "parallax_align":
                text = tool_parallax_align(session)
                content_block = [{"type": "text", "text": text}]
                summary = text[:200]
            elif name == "parallax_transcribe":
                text = tool_parallax_transcribe(
                    session,
                    audio=args.get("audio"),
                    model=args.get("model"),
                    language=args.get("language"),
                )
                content_block = [{"type": "text", "text": text}]
                summary = text[:200]
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
                summary = text[:200]
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

        session.broadcast(
            "tool_result", {"id": tool_id, "name": name, "summary": summary}
        )
        session.push_display(
            "tool_result", {"id": tool_id, "name": name, "summary": summary}
        )
        telemetry.record_event(
            session.id,
            "tool_result",
            {"id": tool_id, "name": name, "summary": summary},
        )

        results.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": content_block,
            }
        )
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


def run_agent_turn(session: Session, user_text: str) -> None:
    """Agent loop body — runs in a background thread per POST /api/message."""
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

    hop_prompt = _load_hop_prompt()

    MAX_ITER = 12
    for _iter in range(MAX_ITER):
        if session.cancel_event.is_set():
            session.broadcast("agent_done", {"ok": False, "cancelled": True})
            return

        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=hop_prompt,
                tools=TOOL_SCHEMAS,  # type: ignore[arg-type]
                messages=session.messages,  # type: ignore[arg-type]
            ) as stream:
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
    try:
        registry.install_shutdown_hooks()
        port = int(os.environ.get("_PARALLAX_PORT", "0"))
        host = os.environ.get("PARALLAX_WEB_HOST", "127.0.0.1")
        registry.register(
            cwd=str(PROJECT_DIR),
            host=host,
            port=port,
            user=os.environ.get("USER", "unknown"),
        )
    except Exception as e:
        print(f"parallax-web: registry register failed: {e}", file=sys.stderr)
    yield
    # Shutdown
    try:
        registry.deregister()
    except Exception:
        pass


app = FastAPI(title="parallax-web", lifespan=lifespan)

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


@app.get("/")
async def serve_index(request: Request):
    _require_auth(request)
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(str(index), media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/costs")
async def serve_costs_page(request: Request):
    _require_auth(request)
    page = STATIC_DIR / "costs.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="costs.html not found")
    return FileResponse(str(page), media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/manifest")
async def serve_manifest_page(request: Request):
    _require_auth(request)
    page = STATIC_DIR / "manifest.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="manifest.html not found")
    return FileResponse(str(page), media_type="text/html", headers={"Cache-Control": "no-store"})


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

    upload_file = form.get("file")
    if upload_file is None:
        raise HTTPException(status_code=400, detail="no file field in upload")

    try:
        file_data = await upload_file.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")

    if not file_data:
        raise HTTPException(status_code=400, detail="file too large or empty (max 50 MB)")
    if len(file_data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="file too large (max 50 MB)")

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
    print(f"parallax-web: project  = {PROJECT_DIR}")
    print(f"parallax-web: model    = {MODEL}")
    print(f"parallax-web: url      = {url}")
    if os.environ.get("PARALLAX_WEB_PASSWORD"):
        print("parallax-web: auth     = enabled (Basic Auth)")
    else:
        print("parallax-web: auth     = DISABLED — set PARALLAX_WEB_PASSWORD to protect")

    # Pass port to lifespan via env so the registry gets the real port
    os.environ["_PARALLAX_PORT"] = str(port)

    if os.environ.get("PARALLAX_WEB_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"parallax-web: webbrowser.open failed: {e}", file=sys.stderr)

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
