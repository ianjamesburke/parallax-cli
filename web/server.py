"""
server.py — parallax-web: localhost HTTP + SSE chat server backed by
the Anthropic SDK and a custom tool-use agent loop.

Design:
    - stdlib http.server for routing + SSE (no Flask/FastAPI)
    - `anthropic` SDK for the model, streaming via client.messages.stream(...)
    - One agent thread per session, fed from POST /api/message
    - SSE fan-out via a per-session queue.Queue read by GET /api/stream/<id>
    - SQLite telemetry via telemetry.py
    - Gallery: stills/ and output/ + drafts/, mtime-sorted

Run:
    ANTHROPIC_API_KEY=sk-... python3 -m parallax_web
    (or equivalently from this file's directory: python3 server.py)
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs, unquote

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

    # TODO: verify — fal does not publish a single "whoami" endpoint. The
    # queue API accepts `Authorization: Key <key>`; hitting the root returns
    # 200 when the key is valid. Once fal documents a real identity route
    # (or we find it in their dashboard's network tab) wire it up here.
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
            # Fallback: mask the key itself so the page shows *something*
            # when fal doesn't return identifying fields.
            if not identity:
                masked = key[:6] + "…" + key[-4:] if len(key) > 12 else "****"
                identity = f"key {masked}"
            return {"configured": True, "identity": identity, "error": None}
    except urllib.error.HTTPError as e:
        # A 401/403 means the key is present but invalid — still "configured".
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

# USD per million tokens. Used to compute per-session cost from usage deltas.
# Numbers track Anthropic's public pricing. Prompt-cache pricing would be
# different but we don't emit cache_control blocks yet.
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

# Per-user workspaces are off by default — a local dev run collapses to
# PROJECT_DIR/parallax/<project>/ with no users/<name>/ nesting, which is
# the simple case and matches the single-operator office-computer
# deployment. Flip on explicitly for multi-user scenarios via
# PARALLAX_PER_USER_WORKSPACES=1, or implicitly when the server is
# network-accessible (PARALLAX_WEB_HOST) or password-protected.
_network_accessible = bool(os.environ.get("PARALLAX_WEB_HOST"))
PER_USER_WORKSPACES = (
    not bool(os.environ.get("PARALLAX_SINGLE_USER"))
    and (
        bool(os.environ.get("PARALLAX_PER_USER_WORKSPACES"))
        or bool(os.environ.get("PARALLAX_WEB_PASSWORD"))
        or _network_accessible
    )
)

# Top-level folder that scopes every parallax-managed directory under
# PROJECT_DIR. Previously this was the hidden `.parallax/` sibling; in beta
# it's a visible `parallax/` sibling that holds <project>/ subdirs (or
# users/<user>/<project>/ when PER_USER_WORKSPACES is on).
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
    """PROJECT_DIR/parallax/ — scoped parallax-managed folder at master dir."""
    return PROJECT_DIR / WORKSPACE_ROOT_NAME


def _users_root() -> Path:
    """PROJECT_DIR/parallax/users/ — where every user workspace lives."""
    return _workspace_root() / "users"


def _workspace_for(
    user: Optional[str],
    project: Optional[str] = None,
    scaffold: bool = False,
) -> Path:
    """
    Resolve the working directory for a user + project pair. Pure path
    computation by default — pass `scaffold=True` only when the caller
    is about to WRITE to the workspace. Without that flag a GET handler
    can safely compute the workspace path without accidentally creating
    a ghost folder on disk.

    New layout (beta):
        PROJECT_DIR/parallax/<project>/                  (single-user default)
        PROJECT_DIR/parallax/users/<user>/<project>/     (per-user mode)

    Raw media at PROJECT_DIR itself is shared read-only across projects
    via _resolve_project_path's read_fallback.

    `project` defaults to "main". Two browser tabs with different
    `?project=` values are isolated and can run jobs in parallel without
    manifest collisions.
    """
    project_safe = _sanitize_name(project or "main")
    if not PER_USER_WORKSPACES or not user:
        workspace = (_workspace_root() / project_safe).resolve()
    else:
        user_safe = _sanitize_name(user)
        workspace = (_users_root() / user_safe / project_safe).resolve()
    # Safety: must be inside PROJECT_DIR/parallax/
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
    # Explicit override wins — lets tests / worktrees point at a specific
    # binary without relying on PATH order (e.g. when ~/.local/bin/parallax
    # symlinks to main while you're developing in a worktree).
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

    Two concentric sandboxes:
      - primary:  the per-user workspace (where writes should land)
      - fallback: PROJECT_DIR itself (the master launch dir, for cross-
                  project shared raw media)

    Relative paths are joined against the workspace first. If the resolved
    path escapes the workspace (`..`), we fall back to joining against
    PROJECT_DIR. When `read_fallback=True` the caller is doing a read, so
    we also fall through to PROJECT_DIR when the workspace-side path is
    valid but the file does not exist — that's how raw media at the master
    dir becomes visible from any per-user project. For writes, leave
    `read_fallback=False` so new files always land inside the workspace.

    Absolute paths and `..` traversal are allowed only if the final
    resolved path stays inside PROJECT_DIR.
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
            escaped = True  # fall through to PROJECT_DIR for a read
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
# Event formatting for dispatch NDJSON (ported from parallax-app/chat.py)
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
    """
    Per-session state: chat history, SSE subscribers, cancel flag, dispatch
    bookkeeping. One instance per session_id.
    """

    def __init__(self, session_id: str, user: Optional[str] = None, project: Optional[str] = None) -> None:
        self.id = session_id
        self.user = user or os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        self.project = project or "main"
        # Session creation is the write boundary — the very next thing
        # that happens is an append to chat.jsonl + a dispatch subprocess
        # that writes into the workspace. So scaffold eagerly here.
        self.workspace = _workspace_for(self.user, self.project, scaffold=True)
        self.messages: list[dict[str, Any]] = []  # Anthropic messages format
        self.display_log: list[dict[str, Any]] = []  # normalized for /api/session
        self.subscribers: list[queue.Queue[dict[str, Any]]] = []
        self.cancel_event = threading.Event()
        self.agent_thread: Optional[threading.Thread] = None
        self.dispatch_proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.created_at = time.time()
        # Session-sticky test mode — tripped by the user typing "TEST MODE"
        # (case-insensitive) in their message. Once on, stays on for the
        # lifetime of the session and every subprocess runs with TEST_MODE=true.
        self.test_mode = False
        # Session-sticky reference images — the frontend sends these with
        # every /api/message POST. Populated per message so `parallax_create`
        # tool calls can auto-inject them even if Claude forgets to set the
        # `ref` arg. Cleared when the user explicitly deselects refs.
        self.selected_refs: list[str] = []
        # Hydrate this session's Anthropic `messages` list from the
        # project's on-disk chat.jsonl so a page reload keeps conversational
        # context. Without this, every new tab would talk to Claude with a
        # blank history even though the chat transcript is right there on
        # disk.
        try:
            self._hydrate_from_chat_log()
        except Exception as e:
            print(f"session hydrate failed: {e}", file=sys.stderr, flush=True)
        # Build the display_log too so /api/session history endpoints stay
        # useful as a fallback, but the frontend now reads /api/chat directly
        # for replay — this is just backfill.
        for turn in (_load_chat_history(self.workspace) or []):
            self.display_log.append({
                "kind": f"{turn.get('role', 'user')}_message" if turn.get("role") == "user" else "assistant_text",
                "data": {"text": turn.get("text") or ""},
                "ts": turn.get("ts") or time.time(),
            })
        telemetry.create_session(session_id, str(self.workspace), MODEL, user=self.user)

    # ---- SSE fan-out ------------------------------------------------------

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
                # drop — client is too slow, they'll re-sync on refresh
                pass

    # ---- display log ------------------------------------------------------

    def push_display(self, kind: str, data: Any) -> None:
        self.display_log.append({"kind": kind, "data": data, "ts": time.time()})

    # ---- chat.jsonl hydration --------------------------------------------

    def _hydrate_from_chat_log(self) -> None:
        """
        Load the project's on-disk chat.jsonl into `self.messages` so the
        very first /api/message POST in a freshly-opened session already
        has the full project history as context. Only text turns are
        loaded; tool calls aren't replayed since they're stored in the
        per-session telemetry, not chat.jsonl.
        """
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
            # Reject if the session belongs to a different user — don't let one
            # user resume another's session, even with a valid session_id.
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
                    "description": "Reference image paths (relative to project root) for image-to-image generation. Use this when the user wants a new image based on one they uploaded.",
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
            "  set-scenes — replace the still scenes list. `values` is a list of specs in the form "
            "'still_path:duration:motion'. Example: ['stills/a.png:3:zoom_in']\n"
            "  add-scene — append one still scene. `values` is ['still_path']. Use `duration`/`motion` fields.\n"
            "  add-video-scene — append a video scene. `values` is ['video_path']. Use `start_s`/`end_s`/`duration` fields.\n"
            "  remove-scene — `values` is ['<number>'].\n"
            "  reorder — `values` is ['1,3,2'] (comma-separated scene numbers).\n"
            "  set-vo — set the voiceover text for a scene. `values` is ['<scene_number>', '<vo text>']\n"
            "  set-voice — pick the voiceover voice. `values` is ['<voice_name_or_id>']. "
            "Shortcuts: george, rachel, domi, bella, antoni, arnold.\n"
            "  set-headline — set a static headline overlay for the whole video. `values` is ['<headline text>']\n"
            "  clear-headline — remove the headline overlay.\n"
            "  enable-captions / disable-captions — toggle word-by-word caption burn in compose.\n"
            "  set — set an arbitrary top-level key. `values` is ['<key>', '<value>'].\n"
            "  show — print the current manifest. No values.\n\n"
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
            "forced alignment to produce precise word-level timestamps, saved to "
            "audio/vo_manifest.json. The WhisperX pass is mandatory and not "
            "configurable — there is no other transcription path in parallax.\n\n"
            "BEFORE calling this: each scene that should have spoken audio must "
            "have a `vo_text` field set via edit_manifest set-vo. Optionally set "
            "the voice via edit_manifest set-voice or pass it directly here.\n\n"
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
            "THE SINGULAR transcription path in parallax — there is no fallback "
            "and no other backend. Every audio that needs word-level timestamps "
            "MUST go through this command.\n\n"
            "Use this when you need to re-transcribe an existing audio file (e.g. "
            "after manual edits or trim-silence). For new voiceovers, parallax_voiceover "
            "already auto-runs WhisperX so you don't need to call this separately."
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
            "parallax_voiceover and BEFORE parallax_align. Optional — only use "
            "if the voiceover has noticeable dead air."
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
            "audio/vo_manifest.json (WhisperX-derived), matches scene vo_text "
            "first words to spoken words, and rewrites scene durations so the "
            "visual timing exactly matches when the words are spoken. Run AFTER "
            "voiceover (and trim-silence if used) and BEFORE compose.\n\n"
            "This is the 'retie the manifest' step that makes scene cuts land "
            "on sentence boundaries automatically."
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
            "This is the canonical render path — deterministic, no globbing, no "
            "fallbacks. Compose is one command that does ALL post-processing: "
            "scene clip rendering (Ken Burns for stills, trim+scale for video "
            "scenes), concat, audio mux (if voiceover exists), headline overlay "
            "(if manifest.headline is set), word-by-word caption burn (if "
            "manifest.captions.enabled is true).\n\n"
            "Always run edit_manifest first to set the scenes. For polished "
            "videos with audio, run voiceover → align → compose in that order. "
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
    """
    Detect the actual image format from the file header. Extensions lie —
    Gemini sometimes returns JPEG bytes saved as .png, and Anthropic's API
    rejects mime/content mismatches with a hard 400.
    """
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
    """
    Returns a dict with either {"error": ...} or
    {"image_block": {...}, "path": ..., "mime": ...}.
    The image_block is a valid Anthropic image content block (source.type=base64).
    Mime type is sniffed from the file header, not the extension.
    """
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

    # Sniff the actual format from magic bytes; fall back to extension.
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
    """
    Build an env dict that strips the parent process's venv so the parallax
    CLI runs in its own installed Python (where its dependencies live).
    Without this, a venv-launched server poisons every subprocess with a
    Python interpreter that doesn't have parallax's deps.
    """
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    # Rebuild PATH: strip any path that looks like a venv bin
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
    """
    Common subprocess runner: spawn parallax, stream NDJSON events back to
    the session SSE channel, return a short summary string.
    """
    # Run CLI subprocesses with the current (venv) interpreter so all installed
    # deps (pyyaml, anthropic, etc.) are available — not the system python3 from
    # the shebang line, which may be missing packages.
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
    # Auto-inject session.selected_refs when the model forgets to. The
    # user's intent is "use what I selected in the gallery", so if the tool
    # call ships without a ref list we fill it in from the session. Claude
    # can still override by passing an explicit empty list [] plus text
    # like "ignore the selected references" — but nothing in the prompt
    # contract encourages that, so in practice this closes the gap.
    if not ref and session.selected_refs:
        ref = list(session.selected_refs)
    # Validate any reference paths — reads allowed to fall through to the
    # master dir (read_fallback=True) so a project can point at raw media
    # at the launch root.
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
    """
    Edit the manifest by shelling out to `parallax manifest <op> ...` from
    the user's workspace. Returns {"output": str, "error": Optional[str]}.
    """
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
    """
    Run each tool_use block and return a list of tool_result content blocks
    suitable for the next user turn.
    """
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
                            # Use the venv's Python (sys.executable). Runtime
                            # deps like Pillow must be installed in the venv
                            # via web/requirements.txt — do NOT side-step the
                            # venv by reaching out to system python.
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
    """`<workspace>/chat.jsonl` — one append-only chat transcript per project."""
    return workspace / "chat.jsonl"


def _append_chat_turn(workspace: Path, role: str, text: str) -> None:
    """
    Append one chat turn to the project's chat.jsonl. Per-project, append
    only, best-effort. Each line is `{"role", "text", "ts"}`. Used for
    reload persistence — tool calls and dispatch events are deliberately
    NOT logged here, only the human-readable user/assistant turns.
    """
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
    """Read `<workspace>/chat.jsonl` into a list of turn dicts. [] if missing."""
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

    # Append user turn.
    user_content: list[dict] = [{"type": "text", "text": user_text}]
    session.messages.append({"role": "user", "content": user_content})
    session.push_display("user_message", {"text": user_text})
    telemetry.record_event(session.id, "user_message", {"text": user_text})
    # Persist to project-scoped chat.jsonl so a reload keeps the convo.
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
                tools=TOOL_SCHEMAS,
                messages=session.messages,
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

        # Unpack final message content into serializable blocks for history.
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
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
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

        # Update tokens + cost in telemetry.
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

        # Run the tools and feed the results back.
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
# HTTP handler
# ---------------------------------------------------------------------------


class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Suppress connection resets — these are normal when the browser
        # closes a tab or refreshes while an SSE stream is open.
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "parallax-web/0.1"

    # Silence the default request-logger noise.
    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("PARALLAX_WEB_VERBOSE"):
            super().log_message(format, *args)

    # ---- auth + user identity --------------------------------------------

    _PASSWORD = os.environ.get("PARALLAX_WEB_PASSWORD", "")

    def _check_auth(self) -> bool:
        """Return True if auth passes (or no password is configured)."""
        if not self._PASSWORD:
            return True
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            _, password = decoded.split(":", 1)
            return password == self._PASSWORD
        except Exception:
            return False

    def _require_auth(self) -> bool:
        """Send 401 and return False if auth fails."""
        if self._check_auth():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Parallax"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _get_request_user(self) -> str:
        """
        Resolve the requesting user in priority order:
          1. 'user' query param in the current request URL  (live override)
          2. 'parallax_user' cookie (set on first visit)
          3. $USER env var (server process owner)
        """
        # Live query param wins so users can switch identities mid-session
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        user_param = qs.get("user", [""])[0].strip()
        if user_param:
            return user_param
        # Then cookie
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("parallax_user="):
                val = part[len("parallax_user="):].strip()
                if val:
                    return val
        return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))

    def _get_request_project(self) -> str:
        """
        Resolve the requesting project (workspace subdirectory) in priority order:
          1. 'project' query param  (live override)
          2. 'parallax_project' cookie
          3. 'main' (default)
        """
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        proj_param = qs.get("project", [""])[0].strip()
        if proj_param:
            return proj_param
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("parallax_project="):
                val = part[len("parallax_project="):].strip()
                if val:
                    return val
        return "main"

    def _maybe_set_user_cookie(self) -> list:
        """
        If the request has ?user= or ?project= params, return Set-Cookie
        header values to persist them. Returns an empty list if no params present.
        """
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        cookies = []
        user_param = qs.get("user", [""])[0].strip()
        if user_param:
            safe = "".join(c for c in user_param if c.isalnum() or c in "-_")[:32]
            if safe:
                cookies.append(f"parallax_user={safe}; Path=/; SameSite=Strict; HttpOnly")
        project_param = qs.get("project", [""])[0].strip()
        if project_param:
            safe = "".join(c for c in project_param if c.isalnum() or c in "-_")[:32]
            if safe:
                cookies.append(f"parallax_project={safe}; Path=/; SameSite=Strict; HttpOnly")
        return cookies

    # ---- helpers ---------------------------------------------------------

    def _write_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _write_error(self, status: int, message: str) -> None:
        self._write_json(status, {"error": message})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"http: bad json body: {e}", file=sys.stderr, flush=True)
            return {}

    # ---- routing ---------------------------------------------------------

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "":
            # Serve index and set user/project cookies if either was passed
            cookies = self._maybe_set_user_cookie()
            if cookies:
                static_path = STATIC_DIR / "index.html"
                try:
                    body = static_path.read_bytes()
                except Exception as e:
                    return self._write_error(500, str(e))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for c in cookies:
                    self.send_header("Set-Cookie", c)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass
                return
            return self._serve_static("index.html")

        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            return self._serve_static(rel)

        if path.startswith("/media/"):
            rel = path[len("/media/"):]
            return self._serve_project_file(rel)

        if path == "/api/gallery":
            workspace = _workspace_for(self._get_request_user(), self._get_request_project())
            return self._write_json(200, list_gallery(workspace=workspace))

        if path == "/api/sessions":
            return self._handle_list_sessions()

        if path == "/api/projects":
            return self._handle_list_projects()

        if path == "/api/usage":
            return self._handle_usage()

        if path == "/api/costs":
            return self._handle_costs_report()

        if path == "/costs":
            return self._serve_static("costs.html")

        if path == "/api/manifest":
            return self._handle_manifest_report()

        if path == "/manifest":
            return self._serve_static("manifest.html")

        if path == "/api/servers":
            return self._write_json(200, {"servers": registry.list_servers()})

        if path == "/api/chat":
            workspace = _workspace_for(self._get_request_user(), self._get_request_project())
            return self._write_json(200, {
                "workspace": str(workspace),
                "project": workspace.name,
                "turns": _load_chat_history(workspace),
            })

        if path.startswith("/api/session/") and path.endswith("/history"):
            sid = path[len("/api/session/"):-len("/history")]
            return self._handle_session_history(sid)

        if path.startswith("/api/session/"):
            sid = path[len("/api/session/"):]
            s = SESSIONS.get(sid)
            if s is None:
                return self._write_json(
                    200, {"session_id": sid, "log": telemetry.load_session_events(sid)}
                )
            return self._write_json(
                200, {"session_id": sid, "log": s.display_log}
            )

        if path.startswith("/api/stream/"):
            sid = path[len("/api/stream/"):]
            return self._serve_sse(sid)

        self._write_error(404, f"not found: {path}")

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/message":
            body = self._read_json_body()
            text = (body.get("text") or "").strip()
            if not text:
                return self._write_error(400, "text is required")
            sid = body.get("session_id")
            user = self._get_request_user()
            project = self._get_request_project()
            session = get_or_create_session(
                sid if isinstance(sid, str) else None,
                user=user, project=project,
            )
            # Capture selected reference images on the session so
            # tool_parallax_create can auto-inject them even if Claude
            # doesn't set `ref` explicitly. Also prepend a visible hint to
            # the user turn so the model knows refs are in play.
            ref_images = body.get("reference_images")
            if isinstance(ref_images, list):
                session.selected_refs = [str(p) for p in ref_images if p]
            if session.selected_refs:
                paths_str = ", ".join(session.selected_refs)
                text = f"[Reference images selected: {paths_str}]\n\n{text}"
            # Session-sticky TEST MODE trigger: if the user's message contains
            # "TEST MODE" (case-insensitive, whole phrase), flip the session
            # into test mode. Every subprocess dispatch then runs with
            # TEST_MODE=true, so Gemini/ElevenLabs are never called.
            if re.search(r"\bTEST\s+MODE\b", text, re.IGNORECASE):
                session.test_mode = True
                print(f"session {session.id}: TEST MODE enabled", file=sys.stderr, flush=True)
            start_agent_thread(session, text)
            return self._write_json(200, {"session_id": session.id, "project": session.project})

        if path == "/api/projects":
            return self._handle_create_project()

        if path == "/api/cancel":
            body = self._read_json_body()
            sid = body.get("session_id")
            if not isinstance(sid, str):
                return self._write_error(400, "session_id required")
            s = SESSIONS.get(sid)
            if s is None:
                return self._write_error(404, "no such session")
            s.cancel_event.set()
            proc = s.dispatch_proc
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return self._write_json(200, {"ok": True})

        if path == "/api/upload":
            return self._handle_upload()

        if path == "/api/open_in_finder":
            body = self._read_json_body()
            user_path = body.get("path") or ""
            workspace = _workspace_for(self._get_request_user(), self._get_request_project())
            if user_path:
                try:
                    target = _resolve_project_path(user_path, workspace=workspace)
                except PathError as e:
                    return self._write_error(400, str(e))
            else:
                target = workspace
            try:
                subprocess.run(["open", str(target)], check=False)
            except Exception as e:
                return self._write_error(500, f"open failed: {e}")
            return self._write_json(200, {"ok": True, "path": str(target)})

        self._write_error(404, f"not found: {path}")

    def do_DELETE(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/image":
            body = self._read_json_body()
            rel = (body.get("path") or "").strip()
            if not rel:
                return self._write_error(400, "path is required")
            workspace = _workspace_for(self._get_request_user(), self._get_request_project())
            try:
                target = _resolve_project_path(rel, workspace=workspace)
            except PathError as e:
                return self._write_error(400, str(e))
            if not target.is_file():
                return self._write_error(404, "file not found")
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f'tell application "Finder" to delete POSIX file "{target}"'],
                    check=True, capture_output=True,
                )
            except Exception as e:
                return self._write_error(500, f"trash failed: {e}")
            return self._write_json(200, {"ok": True, "path": rel})

        if path.startswith("/api/session/"):
            sid = path[len("/api/session/"):]
            if not sid:
                return self._write_error(400, "session_id required")
            # Cancel any live session first
            with SESSIONS_LOCK:
                live = SESSIONS.get(sid)
            if live:
                live.cancel_event.set()
                if live.dispatch_proc:
                    try:
                        live.dispatch_proc.kill()
                    except Exception:
                        pass
            removed = telemetry.delete_session(sid)
            with SESSIONS_LOCK:
                SESSIONS.pop(sid, None)
            return self._write_json(200, {"ok": True, "removed": removed})

        if path.startswith("/api/projects/"):
            name = unquote(path[len("/api/projects/"):])
            return self._handle_delete_project(name)

        self._write_error(404, f"not found: {path}")

    # ---- projects --------------------------------------------------------

    def _handle_delete_project(self, name: str) -> None:
        """
        DELETE /api/projects/<name> — remove a per-user project workspace.

        Refuses to delete `main` (the default, always required) and refuses
        to delete the caller's currently-active project (which would pull
        the rug out from under their running session). Raw media at the
        master dir is never touched — this only nukes the nested workspace.
        """
        name = _sanitize_name(name or "")
        if not name:
            return self._write_error(400, "project name is required")
        if name == "main":
            return self._write_error(400, "cannot delete the default 'main' project")
        current = self._get_request_project() or "main"
        if name == current:
            return self._write_error(
                400,
                f"cannot delete the active project '{name}'. Switch to another project first.",
            )
        user = self._get_request_user()
        if PER_USER_WORKSPACES and user:
            workspace = (_users_root() / _sanitize_name(user) / name).resolve()
            scope = _users_root() / _sanitize_name(user)
        else:
            workspace = (_workspace_root() / name).resolve()
            scope = _workspace_root()
        # Belt-and-suspenders: the target must live inside the expected scope.
        try:
            workspace.relative_to(scope.resolve())
        except ValueError:
            return self._write_error(400, "invalid project path")
        if not workspace.is_dir():
            return self._write_error(404, f"project '{name}' not found")
        try:
            shutil.rmtree(workspace)
        except Exception as e:
            return self._write_error(500, f"failed to delete: {e}")
        return self._write_json(200, {"ok": True, "deleted": name})

    # ---- upload ----------------------------------------------------------

    def _handle_upload(self) -> None:
        """
        POST /api/upload — multipart/form-data with a single 'file' field.
        Writes to <user_workspace>/input/<filename>. Sanitizes filename.
        """
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self._write_error(400, "Content-Type must be multipart/form-data")

        # Extract boundary
        try:
            _, params = ctype.split(";", 1)
            boundary = None
            for part in params.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[len("boundary="):].strip('"')
                    break
            if not boundary:
                return self._write_error(400, "missing multipart boundary")
        except Exception:
            return self._write_error(400, "could not parse Content-Type")

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._write_error(400, "invalid Content-Length")
        if length <= 0 or length > 50 * 1024 * 1024:  # 50 MB cap
            return self._write_error(400, "file too large or empty (max 50 MB)")

        try:
            body = self.rfile.read(length)
        except Exception as e:
            return self._write_error(500, f"read failed: {e}")

        # Parse multipart manually — we only need the first file field
        boundary_bytes = ("--" + boundary).encode("ascii")
        parts = body.split(boundary_bytes)
        filename = None
        file_data = None
        for part in parts:
            if not part or part == b"--\r\n" or part == b"--":
                continue
            # Each part: \r\nheader\r\nheader\r\n\r\nbody\r\n
            try:
                header_blob, _, payload = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
                headers_text = header_blob.decode("latin-1", errors="ignore")
                if "filename=" not in headers_text:
                    continue
                # Pull filename
                for h in headers_text.split("\r\n"):
                    if h.lower().startswith("content-disposition"):
                        for token in h.split(";"):
                            token = token.strip()
                            if token.startswith("filename="):
                                filename = token[len("filename="):].strip('"')
                                break
                        break
                if filename:
                    file_data = payload.rstrip(b"\r\n")
                    break
            except Exception:
                continue

        if not filename or file_data is None:
            return self._write_error(400, "no file field in upload")

        # Sanitize filename: basename only, ASCII alnum + . _ - and collapse
        # runs of unsafe chars to a single underscore. Preserves spaces as
        # underscores so screenshot names stay readable.
        filename = os.path.basename(filename)
        safe_chars = []
        prev_underscore = False
        for c in filename:
            # Only ASCII letters/digits — c.isalnum() would let through Unicode.
            if (c.isascii() and c.isalnum()) or c in "._-":
                safe_chars.append(c)
                prev_underscore = False
            else:
                if not prev_underscore:
                    safe_chars.append("_")
                    prev_underscore = True
        safe_name = "".join(safe_chars).strip("_")[:200]
        if not safe_name or safe_name in (".", ".."):
            return self._write_error(400, "invalid filename")

        # Upload is a write boundary — scaffold so all canonical subdirs
        # exist, not just input/. The inline mkdir below is belt-and-suspenders.
        workspace = _workspace_for(
            self._get_request_user(), self._get_request_project(), scaffold=True,
        )
        input_dir = workspace / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        target = input_dir / safe_name

        try:
            target.write_bytes(file_data)
        except Exception as e:
            return self._write_error(500, f"write failed: {e}")

        rel_path = str(target.relative_to(workspace))
        return self._write_json(200, {
            "ok": True,
            "path": rel_path,
            "size": len(file_data),
        })

    # ---- usage / cost ----------------------------------------------------

    def _handle_usage(self) -> None:
        """
        GET /api/usage — aggregate cost/tokens for the requesting user.
        Returns today's (rolling 24h) and lifetime spend by scanning the
        JSONL log once per query.
        """
        user = self._get_request_user()
        now = time.time()
        day_start = now - 86400

        try:
            lifetime = telemetry.usage_for_user(user)
            today = telemetry.usage_for_user(user, since_ts=day_start)
        except Exception as e:
            return self._write_error(500, f"usage query failed: {e}")

        return self._write_json(200, {
            "user": user,
            "today": today,
            "lifetime": lifetime,
        })

    def _handle_manifest_report(self) -> None:
        """
        GET /api/manifest — parsed workspace manifest.yaml + a scene list
        with resolved thumbnail URLs. Honors ?user= and ?project= via the
        same rules as the rest of the app, so the manifest view can be
        bookmarked for a specific workspace.
        """
        workspace = _workspace_for(self._get_request_user(), self._get_request_project())
        manifest_path = workspace / "manifest.yaml"
        if not manifest_path.is_file():
            return self._write_json(200, {
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
            return self._write_error(500, f"manifest read failed: {e}")

        scenes_out: list[dict] = []
        for s in (raw.get("scenes") or []):
            if not isinstance(s, dict):
                continue
            still_rel = s.get("still") or ""
            still_url = None
            if still_rel:
                # /media/... resolves via _resolve_project_path with read
                # fallback, so this works for master-dir and workspace stills.
                still_url = f"/media/{still_rel}"
            scenes_out.append({
                "number": s.get("number"),
                "title": s.get("title"),
                "duration": s.get("duration"),
                "still": still_rel,
                "still_url": still_url,
                "motion": s.get("motion"),
                "vo_text": s.get("vo_text") or "",
            })

        # Voiceover block gets its own shape so the UI can link to the
        # audio file + display word count / duration if present.
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

        return self._write_json(200, {
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

    def _handle_costs_report(self) -> None:
        """
        GET /api/costs — full cost breakdown (LLM + image + video) folded
        from the JSONL event log. Honors ?user= for per-user filtering.
        Also fetches the fal account identity if FAL_KEY or FAL_API_KEY is
        set on the server process — that is the only live network call in
        the handler and it is wrapped in a short timeout.
        """
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        user_param = qs.get("user", [""])[0].strip() or None

        try:
            report = costs.build_report(user_param)
        except Exception as e:
            return self._write_error(500, f"costs build failed: {e}")

        # Fal whoami — best-effort, stdlib urllib only.
        report["fal"] = _fal_whoami()
        return self._write_json(200, report)

    # ---- projects (filesystem-backed) -----------------------------------

    def _handle_list_projects(self) -> None:
        """GET /api/projects — list every project folder owned by the user."""
        user = self._get_request_user()
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
            return self._write_error(500, f"failed to list projects: {e}")

        return self._write_json(200, {"projects": projects, "user": user or ""})

    def _handle_create_project(self) -> None:
        """POST /api/projects — create a new project folder."""
        body = self._read_json_body()
        name = _sanitize_name(body.get("name") or "")
        if not name:
            return self._write_error(400, "name is required")
        user = self._get_request_user()
        try:
            workspace = _workspace_for(user, name, scaffold=True)
        except Exception as e:
            return self._write_error(500, f"failed to create project: {e}")
        return self._write_json(200, {"name": name, "path": str(workspace)})

    # ---- session history -------------------------------------------------

    def _handle_list_sessions(self) -> None:
        """GET /api/sessions — recent sessions with preview text and live status."""
        request_user = self._get_request_user() if PER_USER_WORKSPACES else None
        try:
            sessions = telemetry.list_sessions(limit=50, user=request_user)
        except Exception as e:
            return self._write_error(500, f"sessions query failed: {e}")

        # Snapshot which sessions are currently live (agent thread alive)
        with SESSIONS_LOCK:
            live_ids = {
                sid for sid, s in SESSIONS.items()
                if s.agent_thread and s.agent_thread.is_alive()
            }

        sessions_out = []
        for s in sessions:
            preview = (s.get("first_user_message") or "")[:80]
            sid = s.get("id")
            # Extract just the project name from the full project_dir path
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

        return self._write_json(200, {"sessions": sessions_out})

    def _handle_session_history(self, session_id: str) -> None:
        """GET /api/session/<id>/history — reconstructed message list."""
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
                # skip other event kinds (dispatch_event raw lines, etc.)
            except Exception as e:
                print(
                    f"history: failed to convert event kind={kind}: {e}",
                    file=sys.stderr,
                    flush=True,
                )

        return self._write_json(200, {"session_id": session_id, "messages": messages})

    # ---- static + media --------------------------------------------------

    def _serve_static(self, rel: str) -> None:
        safe = (STATIC_DIR / rel).resolve()
        try:
            safe.relative_to(STATIC_DIR)
        except ValueError:
            return self._write_error(400, "bad path")
        if not safe.is_file():
            return self._write_error(404, f"static not found: {rel}")
        mime, _ = mimetypes.guess_type(str(safe))
        data = safe.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _serve_project_file(self, rel: str) -> None:
        workspace = _workspace_for(self._get_request_user(), self._get_request_project())
        # URL-decode the path — browsers percent-escape spaces and non-ASCII
        # chars, but Path resolution needs the raw filename.
        rel = unquote(rel)
        try:
            # `read_fallback=True` lets any project read raw media dropped
            # at the master launch dir — core to the beta cross-project
            # layout. Writes still target the workspace.
            target = _resolve_project_path(rel, workspace=workspace, read_fallback=True)
        except PathError as e:
            return self._write_error(400, str(e))
        if not target.is_file():
            return self._write_error(404, f"not found: {rel}")
        mime, _ = mimetypes.guess_type(str(target))
        try:
            data = target.read_bytes()
        except Exception as e:
            return self._write_error(500, f"read failed: {e}")
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    # ---- SSE -------------------------------------------------------------

    def _serve_sse(self, session_id: str) -> None:
        session = get_or_create_session(session_id)
        q = session.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # initial hello
            self._sse_write("hello", {"session_id": session_id})
            while True:
                try:
                    evt = q.get(timeout=15.0)
                except queue.Empty:
                    # keep-alive
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except Exception:
                        return
                    continue
                kind = evt.get("kind", "message")
                data = evt.get("data", {})
                self._sse_write(kind, data)
        except Exception as e:
            print(f"sse: stream ended: {e}", file=sys.stderr, flush=True)
        finally:
            session.unsubscribe(q)

    def _sse_write(self, kind: str, data: Any) -> None:
        try:
            payload = json.dumps(data, default=str)
            msg = f"event: {kind}\ndata: {payload}\n\n".encode("utf-8")
            self.wfile.write(msg)
            self.wfile.flush()
        except Exception:
            raise


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
    telemetry.init_db()

    # Eagerly scaffold the default workspace in single-user mode so the
    # sidebar has something to list on first load. In per-user mode we
    # don't know who the default user is at startup, so each user's
    # workspace is scaffolded lazily by the first request that touches it.
    if not PER_USER_WORKSPACES:
        try:
            _ensure_workspace(_workspace_for(None, "main"))
        except Exception as e:
            print(f"parallax-web: default workspace scaffold failed: {e}", file=sys.stderr)

    port = find_free_port()
    host = os.environ.get("PARALLAX_WEB_HOST", "127.0.0.1")
    bind_host = "0.0.0.0" if host != "127.0.0.1" else "127.0.0.1"
    url = f"http://{host}:{port}/"
    print(f"parallax-web: project  = {PROJECT_DIR}")
    print(f"parallax-web: model    = {MODEL}")
    print(f"parallax-web: url      = {url}")
    if os.environ.get("PARALLAX_WEB_PASSWORD"):
        print(f"parallax-web: auth     = enabled (Basic Auth)")
    else:
        print(f"parallax-web: auth     = DISABLED — set PARALLAX_WEB_PASSWORD to protect")

    server = ThreadedHTTPServer((bind_host, port), Handler)

    # Announce ourselves in the cross-machine server registry so other
    # parallax-web processes (and the CLI) can find us by cwd/port/pid.
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

    if os.environ.get("PARALLAX_WEB_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"parallax-web: webbrowser.open failed: {e}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nparallax-web: shutting down")
    finally:
        try:
            registry.deregister()
        except Exception:
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
