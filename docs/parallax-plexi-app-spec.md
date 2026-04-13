# Parallax — Plexi App Spec

**Version:** 0.1.0
**Date:** 2026-04-11
**Status:** Draft

Parallax is a Plexi app that provides a chat-driven interface to the Parallax video agent network. Phase 1 ships a standalone chat + image generation tool using app-managed LLM calls. Phase 2 integrates the Head of Production (HoP) for full multi-agent video production.

> **Naming clarification:** "Parallax" refers to both the agent network and this Plexi app. "Narrator" is a separate web platform (Firebase/Next.js) that wraps Facebook Business SDK integrations — a different product entirely.

The value prop: video production with built-in cost tracking, conversation logging, and directory-scoped project management — all inside the Plexi terminal multiplexer, no browser or external UI required.

---

## 1. Overview

Parallax turns Plexi into a video production control surface. Users chat with an LLM to develop concepts, generate images, and (in Phase 2) execute full video pipelines — scripts, storyboards, stills, assembly, voiceover, and captions.

Key properties:

- **Directory-scoped.** Each project directory gets its own conversation history, cost ledger, and manifest.
- **Cost-aware.** Every LLM call and image generation emits a cost event. The app reports costs back to Plexi via `cost_report` events. Session and project totals are always visible.
- **Non-blocking.** LLM calls run on a background thread. The render loop never stalls.
- **App-managed intelligence.** The app manages its own LLM calls via `core/llm.py`. API keys come from Plexi secrets (via `SecretGet`), with Claude CLI as a fallback. Plexi does not proxy LLM calls. The intelligence tier concept (low/medium/high) is retained as an app-level setting that maps to model selection in `core/llm.py`.
- **App-managed external APIs.** Image generation (Gemini), voice synthesis (ElevenLabs), and other external services are called directly by the app. API keys are retrieved from Plexi secrets. Plexi provides no proxy for these services.
- **Phased.** Phase 1 has zero agent dependency. Phase 2 imports HoP; both phases use the same `core/llm.py` path for LLM calls.

---

## 2. MVP (Phase 1) — Chat + Image Generation

### 2.1 Capabilities

- Chat with an LLM via `core/llm.py` (Anthropic API key from Plexi secrets, Claude CLI fallback).
- Generate images via direct Gemini API calls (GOOGLE_API_KEY from Plexi secrets).
- Persist conversations per project directory.
- Display cumulative cost in a status bar.
- Report API costs back to Plexi via `cost_report` events.

### 2.2 Intelligence Tiers (App-Managed)

The app uses an intelligence tier concept to select models. This is entirely app-managed — `core/llm.py` maps tiers to specific models.

| Tier | Model Class | Use Case |
|------|------------|----------|
| `low` | Haiku-class | Quick lookups, formatting, simple edits |
| `medium` | Sonnet-class | Script writing, planning, most production tasks |
| `high` | Opus-class | Complex reasoning, evaluation, quality judgment |

The default tier is set in `manifest.toml` under `[app.settings]`. Users can override per-message with `/tier` or cycle with `Ctrl+T`.

### 2.3 Conversation Persistence

Conversations are stored as JSON files in the project's `.plexi/parallax/conversations/` directory.

```json
{
  "id": "conv_2026-04-11_143022",
  "created": "2026-04-11T14:30:22Z",
  "project_dir": "/Users/ian/projects/client",
  "messages": [
    {
      "role": "user",
      "content": "Write a script for a coffee ad.",
      "timestamp": "2026-04-11T14:30:22Z"
    },
    {
      "role": "assistant",
      "content": "FADE IN: ...",
      "timestamp": "2026-04-11T14:30:25Z",
      "cost_usd": 0.012,
      "model": "claude-sonnet-4-6"
    }
  ],
  "total_cost_usd": 0.052
}
```

A new conversation file is created per session (app launch within a directory). The filename is `YYYY-MM-DD_HHMMSS.json`.

---

## 3. Full Vision (Phase 2) — HoP Integration

### 3.1 HoP via Chat

Users submit job briefs as chat messages. The app detects brief-like messages (or the user prefixes with `/brief`) and routes them to `HeadOfProduction.receive_job()`.

Flow:

1. User sends brief text.
2. App calls HoP, which generates a production plan via LLM.
3. Plan is rendered in the chat as a structured message (bulleted steps, estimated cost, agents involved).
4. User confirms or rejects in-chat (not via terminal stdin).
5. On confirm, HoP executes. Progress updates stream into chat as system messages.
6. On completion, output path and evaluation are shown.

### 3.2 Cost Tracking UI

A persistent status bar at the bottom of the app displays:

| Element | Source | Format |
|---------|--------|--------|
| Project name | `launch_dir` basename | `client-name` |
| Session cost | Sum of all `cost_usd` this session | `$0.42` |
| Project cost | Sum from `costs.jsonl` | `$3.18 / $10.00` |
| Intelligence tier | From config or manifest | `medium` |

Cost cap enforcement: if session cost exceeds `max_session_usd` or project cost exceeds the concept cap ($10), all LLM requests are blocked with a user-visible error. The user can raise the cap via manifest edit.

### 3.3 Intelligence Tier Selection

The intelligence tier defaults to the value in `manifest.toml` under `[app.settings]`. Users can override per-message with `/tier high` or cycle tiers with a keyboard shortcut (e.g., `Ctrl+T`). The tier is resolved in `core/llm.py` to a specific model name.

### 3.4 Brand Files

If the project directory contains a `brand.yaml` or `brand.json`, the app reads it on init and injects brand context into the system prompt for all LLM calls. No special UI — just automatic context injection.

---

## 4. manifest.toml

```toml
[app]
id = "parallax"
name = "Parallax"
entry = "parallax_app.py"
version = "0.1.0"
description = "Chat-driven video production assistant powered by the Parallax agent network."

[app.capabilities]
filesystem = "read_write"
terminal_write = true
network = true

[app.secrets]
required = ["ANTHROPIC_API_KEY"]
optional = ["ELEVENLABS_API_KEY", "GOOGLE_API_KEY", "FAL_API_KEY"]

[app.settings]
intelligence_tier = "medium"     # default tier: "low", "medium", or "high"
max_daily_usd = 5.00             # app-enforced daily spend cap
max_session_usd = 2.00           # app-enforced session spend cap
brand_file = "brand.yaml"        # brand context file, relative to project dir
```

### Capability Reference

| Capability | Type | Description |
|------------|------|-------------|
| `filesystem` | `"read"` \| `"read_write"` | File system access via `list_dir`, `read_file`, `write_file` |
| `terminal_write` | bool | Permission to run commands in Plexi terminal |
| `network` | bool | Outbound network access (for Anthropic, Gemini, ElevenLabs, fal.ai, etc.) |

### Secrets Reference

| Field | Description |
|-------|-------------|
| `ANTHROPIC_API_KEY` | Required. Used by `core/llm.py` for Claude API calls. |
| `GOOGLE_API_KEY` | Optional. Used for Gemini image generation. |
| `ELEVENLABS_API_KEY` | Optional. Used for TTS / voiceover generation. |
| `FAL_API_KEY` | Optional. Used for fal.ai image/video generation. |

The app retrieves secrets from Plexi via the `SecretGet` protocol event. If a required secret is missing, the app fails fast on init with a clear error.

### Settings Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `intelligence_tier` | string | `"medium"` | Default model tier: `"low"`, `"medium"`, or `"high"` |
| `max_daily_usd` | float | required | App-enforced daily spend cap |
| `max_session_usd` | float | required | App-enforced session spend cap |
| `brand_file` | string | `"brand.yaml"` | Brand context file path, relative to project dir |

---

## 5. App Entry Point Architecture

### 5.1 Structure

```python
# parallax_app.py — top-level entry point

import threading
import queue
from plexi_sdk import App, RenderContext, Emitter

app = App()

# --- Global State ---
conversation = []          # list of message dicts
input_buffer = ""          # current text input
scroll_offset = 0          # chat scroll position
response_queue = queue.Queue()  # LLM responses from bg thread
pending_request = None     # request_id of in-flight LLM call
state = "IDLE"             # state machine
session_cost = 0.0         # running cost total
project_dir = ""           # set on init
tier = "medium"            # current intelligence tier
```

### 5.2 State Machine

```
IDLE
  |-- user submits message -> call core/llm.py on bg thread -> WAITING_FOR_RESPONSE
  |
WAITING_FOR_RESPONSE
  |-- LLM response received (via queue) -> append to conversation -> IDLE
  |-- LLM error received -> show error in chat -> IDLE
  |
DISPLAYING_PLAN  (Phase 2 only)
  |-- user confirms -> start HoP execution -> EXECUTING
  |-- user rejects -> IDLE
  |
EXECUTING  (Phase 2 only)
  |-- HoP completes -> show results -> COMPLETE
  |
COMPLETE
  |-- user sends new message -> IDLE
```

### 5.3 Background Threading

LLM calls run on a background thread via `core/llm.py` (Anthropic API). Responses are posted to a queue and drained during `on_render`. HoP execution in Phase 2 also runs on a background thread because `HeadOfProduction.receive_job()` is blocking.

Pattern (same as the Wikipedia app):

```python
def run_hop_job(job_dict, result_queue):
    """Runs on background thread. Posts result to queue."""
    try:
        result = hop.receive_job(job_dict)
        result_queue.put(("success", result))
    except Exception as e:
        result_queue.put(("error", str(e)))

# In on_render, drain the queue:
while not response_queue.empty():
    status, data = response_queue.get_nowait()
    # update conversation + state
```

### 5.4 Event Handlers

| Handler | Responsibility |
|---------|---------------|
| `on_init` | Set `project_dir`, load conversation history, read brand file, set tier from manifest |
| `on_render` | Drain response queue, draw status bar + chat messages + input field |
| `on_key` | Accumulate `input_buffer`, handle Enter (submit), arrow keys (scroll), Ctrl+T (cycle tier) |
| `on_command` | Handle `/brief`, `/tier`, `/clear`, `/history` |
| `on_click` | Plan confirm/reject buttons (Phase 2) |
| `on_shutdown` | Persist current conversation to disk |

---

## 6. Chat UI Protocol

### 6.1 Layout

```
+------------------------------------------+
|  Parallax -- client-name                 |  <- header bar
+------------------------------------------+
|                                          |
|  [assistant] Here's a script concept...  |  <- messages area
|                                          |  (scrollable)
|              [user] Make it shorter ---- |
|                                          |
|  [assistant] Revised version: ...        |
|                                          |
|  -- image generated: hero_shot.png --    |  <- system message
|                                          |
+------------------------------------------+
| > Write a 15-second version_            |  <- input field
+------------------------------------------+
| client-name | $0.42 session | medium     |  <- status bar
+------------------------------------------+
```

### 6.2 Message Rendering

All rendering uses the draw protocol (`rect`, `text`). No `list` command — messages are manually laid out.

| Message Type | Alignment | Background | Text Color |
|-------------|-----------|------------|------------|
| User | Right-aligned, 20px right margin | `#2a2a3a` | `#e0e0e0` |
| Assistant | Left-aligned, 20px left margin | `#1a1a2a` | `#d0d0e0` |
| System | Centered | none | `#808090` |
| Error | Left-aligned | `#3a1a1a` | `#ff6666` |

Message bubbles: draw a `rect` with `radius: 8` behind each message's text block. Word-wrap text to `width - 80px` (40px margin each side).

### 6.3 Scrolling

- Track `scroll_offset` (in pixels).
- Arrow Up / Arrow Down (when input is empty): scroll by `line_height`.
- Page Up / Page Down: scroll by `height - 120` (visible area minus header/input/status).
- Total content height = sum of all message heights. Clamp scroll so newest message is visible by default.

### 6.4 Input Field

- A `rect` spanning full width, 40px tall, at `y = height - 80`.
- Text rendered inside with 10px padding.
- Keypress events append to `input_buffer`. Backspace removes last character.
- Enter submits: appends user message to `conversation`, clears buffer, dispatches LLM call on background thread.
- Shift+Enter inserts a newline (multi-line input).

### 6.5 Status Bar

- A `rect` spanning full width, 30px tall, at `y = height - 30`.
- Three text segments: project name (left), session cost (center), tier (right).
- Background: `#1a1a1a`. Text: `#909090`.

---

## 7. File Layout

### 7.1 App Installation

```
~/.plexi-alpha/apps/parallax/
  manifest.toml
  parallax_app.py
  plexi_sdk.py           # copied from Plexi SDK
  agents.md              # describes how to interact with the app programmatically
  core/
    llm.py               # LLM calls: API key (from secrets) → Claude CLI fallback
    hop.py               # Phase 2: Head of Production
    agent_loop.py        # Phase 2: agent execution loop
    budget.py            # Phase 2: budget management
    cost_tracker.py      # cost tracking + cost_report emission
  packs/                 # Phase 2: agent packs
    video/
  config/                # Phase 2: agent config
    agents.yaml
  versions/              # agent versioning
    v1/                  # frozen agent version
    v2/                  # next version under development
    current -> v1        # symlink to active version
    test-cases/          # regression tests for version upgrades
```

`agents.md` describes the app's available commands, expected inputs/outputs, and interaction patterns. This enables the Plexi terminal agent to use the app as a tool — e.g., sending a `/brief` command and reading the structured response.

Phase 1 ships with `core/llm.py` and `core/cost_tracker.py`. Other `core/` modules, `packs/`, `config/`, and `versions/` are Phase 2.

### 7.2 Project Directory Structure

When the Parallax app initializes in a directory, it creates the `.plexi/parallax/` subtree if missing.

```
~/projects/client-name/
  .plexi/
    parallax/
      conversations/
        2026-04-11_143022.json
        2026-04-11_160500.json
      manifest.yaml          # Phase 2: Parallax project manifest
      costs.jsonl             # Append-only cost log
    agents/                  # Project-level agent configs
      system.md              # Project-scoped system prompt / instructions
      memory/                # Agent memory files for this project
      logs/                  # Agent execution logs
  brand.yaml                  # Optional: brand context for LLM calls
  input/                      # Raw footage / assets
  stills/                     # Generated stills
  audio/                      # TTS / voiceover output
  output/                     # Final renders
```

### 7.3 costs.jsonl Format

One JSON object per line, appended after every billable operation.

```jsonl
{"timestamp":"2026-04-11T14:30:25Z","type":"llm","model":"claude-sonnet-4-6","input_tokens":1200,"output_tokens":450,"cost_usd":0.012,"tier":"medium","request_id":"req_001"}
{"timestamp":"2026-04-11T14:31:02Z","type":"image_gen","prompt":"neon coffee cup","dimensions":"1920x1080","cost_usd":0.04,"request_id":"img_001"}
```

---

## 8. Parallax Integration Roadmap

### Phase 1: Chat + Image Gen (no agent dependency)

- Chat via `core/llm.py` (Anthropic API key from Plexi secrets, Claude CLI fallback).
- Image generation via direct Gemini API calls (GOOGLE_API_KEY from Plexi secrets).
- Conversation persistence and cost tracking.
- Cost reporting back to Plexi via `cost_report` events.
- Ship target: standalone app, useful immediately for brainstorming and concept development.

### Phase 2: HoP Integration

- Import `HeadOfProduction` from the Parallax agent core.
- HoP LLM calls go through the same `core/llm.py` path — no change to the LLM calling mechanism.
- Plan confirmation happens in-chat (HoP's trust gate uses the chat UI, not terminal stdin).
- Cost events from HoP agents flow into the same `costs.jsonl` ledger and emit `cost_report` events to Plexi.

**Integration seam:** The app gets `ANTHROPIC_API_KEY` from Plexi secrets via `SecretGet` and passes it to `core/llm.py`. The fallback chain stays: API key (from secrets) → Claude CLI. Plexi doesn't touch the LLM calls. This is a configuration seam, not a code swap — `core/llm.py` is unchanged, it just gets its API key from a different source.

**Fallback mode:** If Plexi secrets aren't available (e.g., running outside Plexi), `core/llm.py` falls back to environment variables or Claude CLI. The app works in both contexts.

### Phase 3: Full Pipeline

- Storyboard generation and visualization in chat.
- Still generation with inline preview (render path shown as system message, user can `open` via terminal).
- Assembly pipeline: Ken Burns, captions, voiceover, final encode.
- Evaluation agent results displayed as a report card in chat.

### Critical Prerequisite

Parallax editors (Junior, Senior) currently plan ffmpeg calls directly. Before Phase 2 can ship, editors must be refactored to write to `manifest.yaml` and let the pipeline scripts execute from the manifest. This is the manifest-first refactor — without it, the HoP can't be cleanly wrapped because intermediate state is trapped in agent tool calls instead of a readable file.

---

## 9. State Protocol

### 9.1 State Buckets

App state is divided into four buckets with different persistence and sync semantics.

| Bucket | Undo-able | Synced (Multiplayer) | Survives Hot Reload | Survives Restart | Examples |
|--------|-----------|---------------------|---------------------|-----------------|----------|
| `user_state` | yes | yes | yes | yes | Conversation messages, tier selection, brand overrides |
| `derived` | no | yes (display only) | yes | no | Playhead position, elapsed time, computed cost totals |
| `session` | no | no | yes | no | Scroll offset, input buffer, pending request state |
| `persistent` | no | no | yes | yes | Conversation history files, cost ledger, agent memory |

### 9.2 get_state / set_state Protocol Events

Apps read and write state via `get_state` and `set_state` protocol events. Plexi manages the state storage and handles undo/redo for `user_state`.

**get_state (App to Plexi):**

```json
{
  "type": "get_state",
  "bucket": "user_state",
  "key": "conversation"
}
```

**set_state (App to Plexi):**

```json
{
  "type": "set_state",
  "bucket": "user_state",
  "key": "conversation",
  "value": [{"role": "user", "content": "Write a script."}]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"get_state"` \| `"set_state"` | yes | Event discriminator |
| `bucket` | `"user_state"` \| `"derived"` \| `"session"` \| `"persistent"` | yes | Which state bucket |
| `key` | string | yes | Dot-delimited key path |
| `value` | any | yes (set only) | JSON-serializable value |

**Response (Plexi to App):**

```json
{
  "type": "state_value",
  "bucket": "user_state",
  "key": "conversation",
  "value": [{"role": "user", "content": "Write a script."}]
}
```

---

## 10. Cost Reporting Protocol

The app manages its own API calls and reports costs back to Plexi for centralized logging.

### 10.1 cost_report Event (App to Plexi)

After every billable API call, the app emits a `cost_report` event. Plexi appends it to `costs.jsonl`.

```json
{
  "type": "cost_report",
  "app_id": "parallax",
  "service": "anthropic",
  "endpoint": "messages",
  "cost_usd": 0.012,
  "input_tokens": 1200,
  "output_tokens": 450,
  "operation_id": "op_abc",
  "chain_id": "NTV-001_run_04",
  "agent": "script_writer",
  "directory": "/Users/ian/projects/client",
  "timestamp": "2026-04-11T14:30:25Z"
}
```

### 10.2 Field Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"cost_report"` | yes | Event discriminator |
| `app_id` | string | yes | Always `"parallax"` |
| `service` | string | yes | API provider: `"anthropic"`, `"google"`, `"elevenlabs"`, `"fal"` |
| `endpoint` | string | yes | API endpoint: `"messages"`, `"imagen"`, `"tts"`, etc. |
| `cost_usd` | float | yes | USD cost for this call |
| `input_tokens` | int | no | Prompt tokens (LLM calls only) |
| `output_tokens` | int | no | Completion tokens (LLM calls only) |
| `operation_id` | string | yes | Unique ID for this operation |
| `chain_id` | string | no | Groups related operations (e.g., a full production run) |
| `agent` | string | no | Which agent made the call (Phase 2) |
| `directory` | string | yes | Project directory path |
| `timestamp` | ISO 8601 | yes | When the call completed |

Plexi logs each `cost_report` to `costs.jsonl`. This enables per-directory, per-app cost attribution across all Plexi apps.

### 10.3 App-Side Cost Aggregation

The app also maintains its own running totals for UI display and cap enforcement:

| Scope | Source | Cap |
|-------|--------|-----|
| Session | In-memory sum | `max_session_usd` from settings |
| Daily | Filter `costs.jsonl` by today's date | `max_daily_usd` from settings |
| Project | Full `costs.jsonl` sum | `$10.00` (Parallax concept cap) |

When any cap is exceeded, the app refuses new LLM and image generation calls and displays a warning in the status bar. The user must edit `manifest.toml` to raise the cap or start a new session/day.

---

## 11. SDK configure()

Apps declare standard behaviors via `app.configure()`. Plexi handles the mechanics (save, undo, quit confirmation). Apps only override when they need custom behavior.

```python
app.configure(
    auto_save=True,
    auto_save_interval_s=300,
    save_on_quit=True,
    confirm_unsaved=True,
    undo=True,
    max_undo_history=100,
    standard_keys={
        "save": True, "save_as": True, "undo": True, "redo": True,
        "quit": True, "find": False, "copy": False, "paste": False,
    }
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `auto_save` | bool | `False` | Periodically save `user_state` to disk |
| `auto_save_interval_s` | int | `300` | Auto-save interval in seconds |
| `save_on_quit` | bool | `True` | Save `user_state` on app quit |
| `confirm_unsaved` | bool | `True` | Prompt before quitting with unsaved changes |
| `undo` | bool | `True` | Enable undo/redo for `user_state` mutations |
| `max_undo_history` | int | `100` | Max undo steps to retain |
| `standard_keys` | dict | all `True` | Which standard key bindings Plexi should handle |

When `standard_keys["save"]` is `True`, Plexi intercepts `Cmd+S` / `Ctrl+S` and persists `user_state`. When `standard_keys["undo"]` is `True`, Plexi intercepts `Cmd+Z` / `Ctrl+Z` and rolls back the last `user_state` mutation. Apps set a key to `False` when they need to handle it themselves (e.g., `copy`/`paste` with custom clipboard behavior).

---

## 12. Commands

Commands are prefixed with `/` in the chat input.

| Command | Phase | Description |
|---------|-------|-------------|
| `/clear` | 1 | Clear current conversation (starts new file) |
| `/history` | 1 | List past conversations for this project |
| `/tier low\|medium\|high` | 1 | Change intelligence tier for this session |
| `/cost` | 1 | Show detailed cost breakdown (session, daily, project) |
| `/brief <text>` | 2 | Submit a job brief to HoP |
| `/confirm` | 2 | Confirm the displayed production plan |
| `/reject` | 2 | Reject the displayed plan, return to chat |
| `/status` | 2 | Show HoP execution progress |
| `/brand` | 2 | Show loaded brand context |

---

## 13. Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Enter | Submit message |
| Shift+Enter | Newline in input |
| Arrow Up/Down | Scroll chat (when input is empty) |
| Page Up/Down | Scroll chat by page |
| Ctrl+T | Cycle intelligence tier |
| Ctrl+L | Clear chat (`/clear`) |
| Escape | Cancel in-flight request (if `WAITING_FOR_RESPONSE`) |
| Cmd+S / Ctrl+S | Save (handled by Plexi if `standard_keys["save"]` is `True`) |
| Cmd+Z / Ctrl+Z | Undo (handled by Plexi if `standard_keys["undo"]` is `True`) |
| Cmd+Shift+Z / Ctrl+Y | Redo (handled by Plexi if `standard_keys["redo"]` is `True`) |

---

## 14. Future Primitives

Things discussed but not yet specced. Each item here is a candidate for a future spec revision when the need becomes concrete.

- **Retained-mode rendering.** Draw commands with stable IDs for diffing. Plexi diffs the draw tree and only updates changed regions. Eliminates full-frame redraws for apps with mostly-static UI.
- **Audio playback/streaming.** `audio_play` (play a file), `audio_stream_start` (stream audio chunks for real-time TTS playback). Plexi manages the audio device.
- **Video playback/streaming.** `video_play` (play a file in a region), `video_stream_start` (stream video frames). For preview during assembly.
- **Image display primitive.** An `image` draw command that renders an image file at a given rect. Currently the app can only show image paths as text.
- **Toolbar / settings panel.** App name + gear icon in the header bar. Clicking the gear opens a Plexi-rendered settings panel populated from `[app.settings]` in the manifest. Standardized UI, zero app code.
- **Named layouts.** App declares layout presets (e.g., `"chat"`, `"split"`, `"fullscreen_preview"`). Plexi handles transitions between them. Apps switch layouts by name, not by manual rect math.
- **state_mutation events.** For multiplayer sync — Plexi broadcasts `user_state` mutations to connected peers. Apps don't manage sync directly.
- **Job queue with dependency graphs.** Apps submit background jobs with declared dependencies (e.g., "generate stills" must complete before "assemble video"). Plexi manages execution order, parallelism, and failure propagation.
