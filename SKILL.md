---
name: parallax
description: >
  The definitive CLI for AI-assisted video production. Drive the full pipeline —
  ingest, stills generation, scripting, voiceover, composition, and publishing —
  through a clean V2 command namespace. Fully agent-operable via --json NDJSON,
  machine-readable exit codes, and TEST_MODE for offline pipeline verification.
last_synced: 2026-04-15
metadata:
  openclaw:
    requires:
      env:
        - AI_VIDEO_GEMINI_KEY
    optional_env:
      - ANTHROPIC_API_KEY
      - AI_VIDEO_ELEVENLABS_KEY
      - FAL_KEY
    primaryEnv: AI_VIDEO_GEMINI_KEY
    emoji: "🎞️"
---

# Parallax CLI — Agent Operating Manual

Parallax is a CLI-first video production powerhouse. Every capability is a
`parallax` command. The manifest is the project. Agents drive it the same way
humans do.

**`last_synced` discipline:** when the command surface changes, update this
date. If today's date is more than 30 days past `last_synced`, treat the
command references below as potentially stale and verify with `parallax --help`
before running.

---

## Command Namespace (V2)

```
parallax ingest   <path|dir>           footage intake — transcription + scene index
parallax generate still|voice|video   explicit generative calls
parallax script   write|rewrite        script and VO copy via Claude
parallax edit     <instruction>        natural language manifest editing
parallax compose                       manifest → final video (pure ffmpeg, no AI)
parallax trim     <file>               clip trimming primitive
parallax manifest show|set|add-scene|remove-scene|validate
parallax status                        project health and pre-flight check
parallax web                           local web UI (replaces `parallax chat`)
parallax project  new|list             project scaffolding and listing
parallax setup                         dep check, API key config
parallax update                        pull latest from GitHub
parallax publish                       [stub — YouTube + social, V3]
```

Every command supports `--json` for NDJSON output. Help text includes:
```
Reads: <inputs>
Writes: <outputs>
Exit: 0=success  1=<condition>  2=<condition>
```

---

## Capability Modes

Route user intent to the right mode before issuing any commands.

| Mode | Goal | Command sequence | API spend |
|------|------|-----------------|-----------|
| 1 — Stills Only | Images, no video | `generate still` | Gemini only |
| 2 — Ken Burns Draft | Stills → draft video, no footage | `generate still` → `compose` | Gemini + ffmpeg (local) |
| 3 — Storyboard | Scene plan + script, zero generation | `script write` → `manifest` → `status` | LLM only |
| 4 — Full Production | Brief → finished video | `ingest` → `generate still` → `generate voice` → `edit` → `compose` | Whisper + Gemini + ElevenLabs + ffmpeg |
| 5 — Footage Edit | Cut existing footage | `ingest` → `edit` → `compose` | Whisper (optional) + ffmpeg |
| 6 — Script First | Copy → video | `script write` → `generate voice` → `generate still` → `compose` | LLM + ElevenLabs + Gemini + ffmpeg |

**Default for uncertain users:** Mode 3 (storyboard). Zero generation cost, surfaces a structured plan, agent explains: "I can map out a complete scene plan right now at no cost."

---

## TEST_MODE

Set `TEST_MODE=true` to run the full pipeline without generative API calls.

- `generate still` — writes a real solid-color PNG via ffmpeg (deterministic color from brief hash). Valid PNG, usable by `compose`.
- `generate voice --engine say` — macOS `say` TTS (free, offline). Falls back to silent AIFF stub if not on macOS.
- `generate voice` (no engine flag) — silent AIFF stub, duration estimated from word count at 150 WPM.
- `generate video` — stub message, no file written.
- `script write` — deterministic seed-based placeholder (3-scene structure, offline).
- `ingest` — writes stub `transcript.json` + `index.json` per file. No Whisper call.
- `compose` — runs real ffmpeg against stub inputs. Tests the actual render path.

All `--json` events still fire in TEST_MODE with `"test_mode": true` added.

---

## Key Commands

### `parallax generate still "<brief>"`

Writes solid-color PNG stub in TEST_MODE; calls Gemini in real mode.

```
Reads:  optional --ref <image> files
Writes: stills/<slug>.png
Exit:   0=success  1=ffmpeg/API error  2=invalid params
```

Flags: `--count N` (default 1), `--ref IMAGE` (repeatable), `--aspect-ratio` (default 3:4).

### `parallax generate voice "<script>"`

```
Reads:  inline script or --from-manifest reads vo_text from scenes
Writes: audio/voiceover.mp3 (or .aiff on macOS say path)
Exit:   0=success  1=API/TTS error  2=manifest missing
```

Flags: `--engine [elevenlabs|say]`, `--voice <name|id>`, `--from-manifest`.

**macOS `say` engine:** free, offline, no API key. Good for drafts. Available voices: `Samantha`, `Alex`, `Ava`, `Tom`, etc. (run `say -v ?` for full list).

### `parallax script write "<brief>"`

```
Reads:  brief as inline argument
Writes: stdout (or --out <file>)
Exit:   0=success  1=API error
```

TEST_MODE returns a deterministic 3-scene seed-based script. Real mode calls Claude Sonnet.

### `parallax ingest <path|dir>`

```
Reads:  video file(s)
Writes: ingest/<clip-slug>/transcript.json + index.json
Exit:   0=success  1=file not found  2=API error
```

Flags: `--estimate` (dry-run cost projection, zero API calls), `--visual` (Gemini Vision frame analysis, coming), `--force`.

### `parallax compose`

```
Reads:  cwd/manifest.yaml
Writes: output/<concept>_v<version>.mp4
Exit:   0=success  1=manifest missing/invalid  2=ffmpeg error
```

Pure ffmpeg execution — zero AI calls. Validates all scene assets exist before rendering. Flags: `--preview` (480p fast), `--scene N` (single scene), `--json`.

### `parallax manifest <op>`

Operations: `show`, `set <key> <value>`, `add-scene <still>`, `add-video-scene <source>`, `remove-scene <N>`, `reorder <N,N,...>`, `set-vo <N> "<text>"`, `set-voice <name>`, `set-headline <text>`, `enable-captions`, `disable-captions`, `validate`.

`validate` checks: sequential scene numbers, positive durations, resolution format, fps whitelist (12/24/30/60), missing still files. Exit 0 = valid, exit 1 = errors listed.

### `parallax status`

Reads manifest, prints scene count, stills present/missing, audio present, output file status. Exit 0 always (informational).

### `parallax web`

Launches parallax-web localhost server (replaces `parallax chat`). Requires `ANTHROPIC_API_KEY`.

---

## Workspace Layout

```
<project-root>/
  manifest.yaml         ← the manifest — the project
  input/                ← raw footage + generate video outputs
  output/               ← final composed videos
  stills/               ← generate still outputs
  audio/                ← generate voice outputs
  drafts/               ← intermediate/draft files
  ingest/               ← ingest outputs
    <clip-slug>/
      transcript.json
      index.json
  logs/                 ← CLI and agent run logs
```

Global: `~/.parallax/config.toml`, `~/.parallax/servers.json`.

Projects at `~/Documents/parallax-projects/<name>/` when scaffolded via `project new`.

---

## Confirmation Before API Spend

**Hard requirement:** before any command that makes a paid API call (`generate still`, `generate voice`, `generate video`, `ingest` without `--estimate`), the agent must:
1. State what it is about to call.
2. State the estimated cost (run `ingest --estimate` or use `core/pricing.py` rates).
3. Ask for explicit confirmation.

Never auto-proceed on generative API calls.

---

## Environment

| Var | Required for |
|-----|-------------|
| `AI_VIDEO_GEMINI_KEY` or `GEMINI_API_KEY` | `generate still` |
| `ANTHROPIC_API_KEY` | `script write/rewrite`, `edit`, `web` |
| `AI_VIDEO_ELEVENLABS_KEY` or `ELEVENLABS_API_KEY` | `generate voice --engine elevenlabs` |
| `FAL_KEY` | `generate video` (coming) |

`generate voice --engine say` requires no API key — macOS built-in TTS only.

---

## Quick Smoke Test (TEST_MODE, zero cost)

```sh
mkdir -p /tmp/parallax-smoke && cd /tmp/parallax-smoke
TEST_MODE=true parallax generate still "cold brew coffee brand, moody lighting"
TEST_MODE=true parallax script write "cold brew coffee 30-second ad" --out script.txt
TEST_MODE=true parallax status
```

All three should exit 0. The generate still produces a real PNG under `stills/`.

---

## Things That Will Bite You

- **`generate still` TEST_MODE writes real PNGs.** Color is deterministic from brief hash. If compose fails on test stills, it's a manifest path issue, not an image issue.
- **`manifest validate` is pre-flight.** Run it before `compose` to surface missing stills early — compose will fail at the validation step anyway, but validate gives a complete error list upfront.
- **V1 commands still exist.** `create`, `animate`, `chat` still work. They are V1 and will be retired in V3. Prefer V2 commands in new sessions.
- **`say` output is AIFF, not MP3.** ffmpeg converts before writing to `audio/`. If you bypass the CLI and call `say` directly, don't feed a raw AIFF into `compose` — it expects MP3/WAV in the voiceover path.
- **`compose` re-encodes, never stream-copies.** `-c copy` on concat causes black first frames from non-keyframe starts. The CLI enforces re-encode.
