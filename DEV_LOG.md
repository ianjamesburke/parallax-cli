# DEV_LOG — NRTV Video Agent Network
# This log tracks non-obvious decisions, bugs, and deferred work for the agent network.
# Entries are newest-first. Tags: [FIX] [CHANGED] [DECISION] [GOTCHA] [FUTURE]

## 2026-04-16 — [CHANGED] fal video i2v: image-to-video, start/end-frame anchoring, audio flag

Extended the fal.ai video generation surface with image-to-video (i2v) support across all three tiers. Passing `--image PATH` to `parallax fal video` now routes to the i2v endpoint; without it the existing t2v path is unchanged.

**Capability matrix:**

| Tier   | t2v model                                      | i2v model                                            | start-frame | end-frame        | audio |
|--------|------------------------------------------------|------------------------------------------------------|-------------|------------------|-------|
| low    | fal-ai/ltx-2.3/text-to-video                   | fal-ai/ltx-2.3/image-to-video                        | yes         | yes (end_image_url) | no |
| medium | fal-ai/wan-t2v                                 | fal-ai/wan-i2v                                       | yes         | no               | no    |
| high   | fal-ai/kling-video/v1.6/standard/text-to-video | fal-ai/kling-video/v1.6/standard/image-to-video      | yes         | yes (tail_image_url) | no |

Veo 3 (`fal-ai/veo3`) confirmed as a future `high-audio` t2v tier candidate ($0.20–$0.75/sec, native dialogue + sfx + ambient). Not added — separate task when the use case is concrete.

**New CLI flags on `parallax fal video`:** `--image PATH`, `--end-frame PATH` (requires `--image`), `--audio`/`--no-audio` (fails fast on any model without `supports_audio=True`).

**ModelSpec** gained four capability flags: `supports_image_to_video`, `supports_start_frame`, `supports_end_frame`, `supports_audio`. `VIDEO_MODELS` preserved as alias for `VIDEO_T2V_MODELS` (backward compat). `get_video_model()` gained `mode` param (default `"t2v"`).

**config.py** updated to support `[fal.video.t2v]` / `[fal.video.i2v]` subsections. Legacy flat `[fal.video]` with tier keys accepted as t2v + stderr deprecation warning.

**API naming gotchas:** LTX-2.3 i2v uses `end_image_url` (not `tail_image_url`). Kling i2v uses `tail_image_url`. Wan i2v has no end-frame param at all. All three differ from each other — `_build_i2v_args` branches per model ID.

**Live test:** LTX-2.3 i2v (`fal-ai/ltx-2.3/image-to-video`, low tier): `ffprobe` → h264, 1080×1920, 6.08s. t=0 frame visually matches input (b&w silhouette, same hand pose, digital-rain head). t=3 frame shows clear morphing/wave motion. Image upload via `fal_client.upload_file` worked without issues.

**Breaks if:**
- `parallax fal video low --image /path/img.jpg --prompt X --aspect 9:16 --output /tmp/out.mp4` produces a landscape (width > height) video, exits non-zero, or produces no file.
- `parallax fal video low --audio` exits 0 instead of exit 2 with "does not support audio generation" error.
- `parallax fal video low --end-frame /tmp/x.jpg` (without `--image`) exits 0 instead of exit 2 with "--end-frame requires --image" error.
- `parallax fal models --json` returns rows missing the `mode`, `supports_start_frame`, `supports_end_frame`, `supports_audio` keys.
- `parallax fal video medium --end-frame /tmp/x.jpg --image /tmp/y.jpg --prompt X` exits 0 instead of exit 2 (Wan i2v lacks end-frame support).

## 2026-04-16 — [CHANGED] Four-task pass: drawtext audit, config.toml, voice CLI, manifest voice_id

**Task 1 — drawtext audit:** No dead code found. `burn()` in `burn-captions.py` is a live fallback when `text_render` is unavailable or the requested style isn't a PIL style (confirmed by DEV_LOG 2026-04-17 "falls back to drawtext if style is not a PIL style"). `burn-overlay.py` is entirely drawtext-based and still the correct tool for persistent lower-thirds/brand-tags (called from `head_of_production.py`). `drawtext` references in `senior_editor.py`, `junior_editor.py`, and `head_of_production.py` are in LLM prompt strings, not executable code — removing them would regress the agent's instructions. Nothing deleted; nothing flagged as dead.

**Task 2 — `.parallax/config.toml` model config:** New `packs/video/config.py` loader walks cwd upward for `.parallax/config.toml`. Precedence: `--model` CLI flag > env (`PARALLAX_FAL_VIDEO_LOW` etc.) > config.toml > built-in defaults. Invalid model IDs (missing `/`) fail fast with a clear error. `fal/models.py` updated with `model_id_override` param on `get_video_model`/`get_image_model`. `fal/cli.py` reads config on every invocation. `parallax fal models` now shows source column (default/config/env). New `parallax config show` command dumps effective config with source attribution. Verified: config.toml `fal.video.low` override shows `source: config` in `fal models --json` and `config show`.

**Task 3 — `parallax voice` CLI:** New `parallax voice list [--json]` and `parallax voice clone --name ... --sample ...` commands. `voice list` hits `GET /v1/voices` and prints voice_id, name, category, gender, accent. `voice clone` posts multipart to `/v1/voices/add`, returns new voice_id. Both require `AI_VIDEO_ELEVENLABS_KEY` or `ELEVENLABS_API_KEY` (same lookup as `cmd_voiceover`). Clear error on 402/403 (requires paid plan). Added `requests>=2.31` to `pyproject.toml`. Verified: `parallax voice list --json | head -3` returns well-formed NDJSON with real voice_ids.

**Task 4 — Persist voice_id into manifest:** `cmd_voiceover` now writes `voice_id` and `voice_name` to `manifest.voiceover` in all three manifest-write sites: pre-transcription stub, post-WhisperX update, and TEST_MODE path. `manifest.voice.voice_id` was already written; `manifest.voiceover.voice_id` was missing. Verified via TEST_MODE run: both keys present in the written YAML.

**Breaks if:**
- `parallax fal models --json` from a dir with `.parallax/config.toml` shows `"source": "default"` for an overridden tier (Task 2 config not loading).
- `parallax config show` exits non-zero or omits any tier row (Task 2 regression).
- `parallax voice list --json` with a valid API key returns no output or exits non-zero (Task 3 regression).
- After `parallax voiceover` succeeds, `manifest.yaml` has `voiceover:` section but no `voice_id` key inside it (Task 4 regression).

## 2026-04-17 — [FIX] Three caption/text_render bugs — static caption burn, block_background wrap, stroke width

Three bugs fixed in one pass:

1. `captions.text` silently ignored when no `vo_manifest.json` exists. Root cause: compose only entered the caption branch when `vo_manifest_path_full` was present. Added an `elif` static-caption path: renders one PNG via `render_caption()`, overlays for full video duration with `between(t,0,<dur>)`. No new CLI flags — manifest drives it.

2. `_render_block_background` laid all words on a single row, clipping at 4+ words. Added a greedy row packer: words accumulate on a row until the next would exceed `w - 2*safe_margin`, then a new row starts. Each row is independently centered. `FORWARD` drops to row 2 on a 5-word headline.

3. Stroke width on `outline_white_on_black` and `outline_black_on_white` was `max(3, int(4*w/1080))` — visibly thin at 1080px. Bumped to `max(3, int(6*w/1080))` for both styles.

**Breaks if:** `parallax compose` on a manifest with `captions.enabled: true` and `captions.text` set but no VO produces a video with no visible caption text; or a 5-word `block_background` headline overflows the frame edge; or outline-style text appears with a thin/barely-visible stroke.

## 2026-04-17 — [FIX] fal video `--aspect` now wired correctly for all tiers

Root cause was two-fold: (1) the low tier used model ID `fal-ai/ltx-video` which accepts no dimension params at all — silently produced 768x512 (3:2 landscape) regardless of `--aspect`. Fixed by upgrading to `fal-ai/ltx-2.3/text-to-video` which supports `aspect_ratio` + `resolution` params. (2) The `_build_video_args` builder was passing `width`/`height` for LTX instead of `aspect_ratio`/`resolution`. Wan and Kling were already passing `aspect_ratio` correctly.

Per-model aspect param table:
- `fal-ai/ltx-2.3/text-to-video` (low): `aspect_ratio` enum, `"9:16"` or `"16:9"` only. Duration is int 6/8/10 (snapped from requested seconds).
- `fal-ai/wan-t2v` (medium): `aspect_ratio` enum, `"9:16"` or `"16:9"` only.
- `fal-ai/kling-video/v1.6/standard/text-to-video` (high): `aspect_ratio` enum, `"9:16"`, `"16:9"`, or `"1:1"`.
- All image tiers (FLUX): `image_size` enum via `_ASPECT_TO_IMAGE_SIZE`, all 4 values work.

Passing `1:1` to low/medium now fails fast with a clear error (exit 2) instead of silently dropping the param.

**Breaks if:** `parallax fal video low --prompt X --aspect 9:16 --output /tmp/x.mp4` produces a file where `ffprobe width > height` (i.e. landscape instead of vertical).

## 2026-04-17 — [CHANGED] Replace ffmpeg drawtext with PIL transparent-PNG overlay system

`drawtext` was causing two recurring failures: (1) font path lookup brittle on macOS (Homebrew ffmpeg-full required, stock ffmpeg missing libfreetype), and (2) shell-escape hell for quotes/punctuation in caption text causing silently wrong renders or hard crashes.

New path: render RGBA transparent PNGs via `PIL.ImageDraw.text(stroke_width=N)` in `packs/video/text_render.py`, then overlay via `-filter_complex "[0:v][1:v]overlay=0:0:enable='between(t,s,e)'"`. Fonts bundled in `assets/fonts/` (Inter SemiBold, Anton Regular, DM Sans Medium — all OFL).

Three styles registered: `outline_white_on_black` (white text/black stroke/Inter, bottom-center), `outline_black_on_white` (inverted), `block_background` (Anton, per-word solid black rectangles, top-center, 84pt). All verified by frame-sample against a test video.

Wired into:
- `burn-captions.py`: `--style <name>` triggers `burn_pil()` which chains N overlay inputs (one PNG per caption chunk) with time-windowed `enable=` expressions. Falls back to drawtext if style is not a PIL style.
- `bin/parallax` headline path: uses `text_render.render_headline()` directly for PIL styles; falls back to `generate-caption.py` subprocess for legacy style names (`title`, `banner`).
- `bin/parallax` caption path: reads `manifest.captions.style` (default `outline_white_on_black`) and passes `--style` to burn script.

**Breaks if:** `burn-captions.py --style outline_white_on_black` exits non-zero or produces video with no captions visible; or `parallax compose` on a manifest with `captions.enabled: true, captions.style: outline_white_on_black` burns no caption text; or any caption text with apostrophes/commas crashes the render (the whole point of this change — PIL has no escape requirements).

## 2026-04-17 — [FIX] `parallax fal image --output` now honors caller-specified extension

`packs/video/fal/client.py` previously overrode the caller's output suffix with the URL's extension — so `--output foo.png` silently wrote `foo.jpg` (fal serves Flux results as jpeg). This broke any automated flow whose manifest referenced the expected path. Now: if `output_path` has a suffix, save to that exact path; only infer from the URL when the caller omitted the suffix. Found during the e2e script-first test run.

**Breaks if:** `parallax fal image low --prompt X --output /tmp/x.png` produces `/tmp/x.jpg` instead of `/tmp/x.png`.

## 2026-04-17 — [DECISION] E2E verification pass — three parallel agent-driven flows

Ran three parallel Sonnet sub-agents each simulating a real-user flow end-to-end with low-tier paid APIs (~$0.05 total spend). All three produced valid 9:16 mp4 output.

- **Product-first** (image → variants → video → VO + headline): surfaced bugs 1–3 below, all fixed in this session. Headline overlay "STAY HYDRATED." burned correctly over hero frame.
- **Footage-edit** (mixed 1080×1920 + 1920×1080 clips → 9:16 test edit): horizontal clips correctly pillarboxed with black bars, not stretched. `compose`'s `scale + pad` filter handles orientation mixing cleanly.
- **Script-first** (script → VO → stills → Ken Burns → captions): caption word "one day," fires at 9.033s matching the VO manifest. Ken Burns motion confirmed via pixel diff. Surfaced bug 4 (above).

All three agents fell back to Option B (direct CLI) rather than driving the HoP agent via `parallax web`'s HTTP API. The tool layer is validated; **the HoP agent's ability to orchestrate these tools is NOT verified by this pass** — that's a separate test pass worth running.

**Deferred issues from this run** (not fixed, candidates for .plexi/backlog or issues):
- LTX-Video image-to-video ignores aspect flag (outputs 3:2 regardless).
- No top-level `parallax captions` command — captions require hand-editing `captions.enabled: true` into the manifest.
- `parallax ingest` uses a stub for Gemini Vision in TEST_MODE — real visual-description path unverified here.
- `drawtext` missing from stock homebrew ffmpeg — caption/headline burn will fail on a clean machine.
- WhisperX emits noisy `torchcodec` dylib warnings on every run.

**Breaks if:** the next e2e pass reintroduces any of bugs 1–4 listed in adjacent entries.

## 2026-04-17 — [FIX] Three bugs found in e2e product-ad run (image aspect, voice shutil, manifest mux key)

**Bug 1 — Flux schnell image aspect ratio rejected by API.** `_ASPECT_TO_IMAGE_SIZE` in `packs/video/fal/models.py` mapped `"9:16"` → `"portrait_9_16"`, but fal.ai's Flux models only accept `"portrait_16_9"` for tall/portrait orientation. The enum names the aspect ratio of the short-edge/long-edge pair (landscape_16_9 = wide, portrait_16_9 = tall) — not WxH order. Fixed by changing the 9:16 key to `"portrait_16_9"`.

**Bug 2 — `generate voice --engine say` crashes with `AttributeError: module 'subprocess' has no attribute 'which'`.** `cmd_generate_voice` imports `subprocess as _sp` then calls `_sp.which("say")` — but `subprocess` has no `which`; that's `shutil.which`. Added `import shutil as _shutil` and replaced both `_sp.which` calls with `_shutil.which`.

**Bug 3 — `manifest set-voice <file>` doesn't wire to compose mux.** `set-voice` writes `manifest["voice"]["voice_name"]`; compose's audio-mux step reads `manifest["voiceover"]["audio_file"]` — different keys. The voiceover file is set but silently not muxed (compose runs without audio). Workaround: manually add `voiceover: {audio_file: ...}` to manifest.yaml. `set-voice` should either write to `voiceover.audio_file` or compose should read both key shapes.

**Breaks if:** `parallax fal image low --aspect 9:16` returns a 422 aspect-ratio error (bug 1 regressed); `parallax generate voice --engine say "text"` exits with AttributeError (bug 2 regressed); compose run on a manifest with `voice.voice_name` set produces a video with no audio stream (bug 3 unfixed).

## 2026-04-16 — [CHANGED] parallax fal — video/image generation subcommand group

New `parallax fal video <tier>` and `parallax fal image <tier>` commands backed by fal.ai. Three-tier cost ladder for both media kinds:

**Video:** low = `fal-ai/ltx-video` ($0.02/clip), medium = `fal-ai/wan-t2v` ($0.20/clip 480p), high = `fal-ai/kling-video/v1.6/standard/text-to-video` (~$0.056/sec).
**Image (scaffold):** low = `fal-ai/flux/schnell` (~$0.003), medium = `fal-ai/flux/dev` (~$0.025), high = `fal-ai/flux-pro/v1.1` (~$0.05).

Implementation lives in `packs/video/fal/` (models.py tier registry, client.py submit/poll/download, cli.py handlers). Wired into `bin/parallax` as `fal` subcommand group. `fal-client==0.13.2` added to pyproject.toml. TEST_MODE writes a 1s black mp4 (video) or blank PNG (image) via ffmpeg and emits fake NDJSON events — no API key required.

**Live test (low tier):** `parallax fal video low --prompt "..." --duration 3 --aspect 9:16 --output /tmp/parallax-fal-live-low.mp4` → h264, 5.0s, 768×512, ~9 MB. Note: LTX-Video ignores `--duration` below its minimum (~5s); the param is passed through but the model clips it. Duration is respected by Kling (medium/high tiers).

**Gotcha:** `fal_client.subscribe()` emits `Queued` status objects during polling — these don't carry `.logs`; the `_on_update` handler catches `AttributeError` and emits a generic "processing…" log instead. Don't add `.logs` attribute access outside the `InProgress` branch.

**Breaks if:** `parallax fal models` exits non-zero or prints no rows; `TEST_MODE=1 parallax fal video low --prompt x` exits non-zero or fails to write an mp4 to `./parallax-out/`; `parallax fal video low --prompt x` (live) exits non-zero when FAL_KEY is set.

## 2026-04-16 — [CHANGED] Branch consolidation: beta → main, single-worktree state

Wiped the multi-branch/multi-worktree setup. End state: one branch (`main`), one worktree, origin untouched.

**What was consolidated onto main:**
- Beta's FastAPI migration (replaced stdlib `http.server`, added sse-starlette, Pydantic request bodies, OpenAPI at `/docs`).
- Beta's V2 CLI surface (`generate still|voice|video`, `script write|rewrite`, `ingest`, `web`).
- Beta's `--engine say` macOS voice engine + Mode 2 e2e test.
- Beta's uv migration (replacing `web/.venv` + pip).
- **Beta worktree's uncommitted UI work** (never made it to a commit on beta branch): three-column tabs layout (`web/static/app.html` + `timeline.js`), reworked `app.css`/`app.js`, `web/server_log.py`, `web/head_of_production_prompt.md` (renamed from `hop_prompt.md`), ~1000 lines of server.py additions (project_root/ sentinel, `_display_path` helper, launch-context builder).
- Today's Blocks A/B/C re-applied on top (see below).

**Why this was messy:** the merge went through 3 iterations of "restore UI" before finding the right source. Main's pre-merge UI ≠ beta HEAD's UI ≠ beta worktree's uncommitted UI. The one the user remembered ("left chat / middle media bin / right video preview with tabs") lived only in beta worktree's uncommitted files — `app.html` and `timeline.js` were untracked. Lesson: on a branch consolidation, always diff against each worktree's working tree, not just HEADs.

**Breaks if:** `git branch` shows anything other than `main`; `git worktree list` shows anything other than the main checkout; `parallax chat --test` opens anything other than the three-column tabs UI.

## 2026-04-16 — [FIX] python-multipart missing — all uploads returning 400

Every `POST /api/upload` returned `{"detail":"form parse failed: The python-multipart library must be installed to use form parsing."}`. Starlette's form() parser requires `python-multipart`; the beta FastAPI migration added `fastapi` + `uvicorn` + `sse-starlette` to `pyproject.toml` but missed this transitive. Added `python-multipart>=0.0.9` to `[project] dependencies`.

**Breaks if:** file uploads to `/api/upload` return 400 with a form-parse error; or `uv sync` stops installing `python-multipart==0.0.26+`.

## 2026-04-16 — [CHANGED] parallax chat --test flag

New `--test` flag on `parallax chat` (bin/parallax). Skips the `ANTHROPIC_API_KEY` check, skips the `import anthropic` probe, exports `TEST_MODE=true` to the spawned server. Also rewrote the interpreter selection logic: prefer repo-root `.venv/bin/python3` (uv-managed), fall back to legacy `web/.venv`, finally system `python3`.

**Breaks if:** `parallax chat --test` asks for ANTHROPIC_API_KEY; or the spawned server doesn't have `TEST_MODE=true` in its env; or the shim at `~/.local/bin/parallax` doesn't find main's `.venv`.

## 2026-04-16 — [CHANGED] Video uploads in media bin + web-layer TEST_MODE

Two master-agent blocks shipped back-to-back.

**Block A — video uploads in media bin.** `_handle_upload()` size cap in `web/server.py` raised from 50 MB to 500 MB so real video files fit. No gallery changes needed: `list_gallery` was already scanning `input/` for both image (png/jpg/jpeg/webp/gif) and video (mp4/mov/webm/m4v) extensions, and `/media/` already serves project-relative paths. The only gap was the cap.

**Block B — web-layer TEST_MODE.** Set `TEST_MODE=1` on `parallax-web` to run the entire stack without paid APIs. Implementation in `web/server.py` only:
1. Module-level `TEST_MODE` constant with canonical truthy parse (matches `main.py`).
2. `preflight()` early-returns when TEST_MODE — no `ANTHROPIC_API_KEY` required.
3. `_clean_subprocess_env()` sets `env["TEST_MODE"] = "true"` — one choke point propagates to every CLI subprocess so existing stubs in `packs/video/tools.py` fire (stills, voiceover, compose, evaluator all skip paid calls).
4. Inline `_MockStream` + `_mock_anthropic_stream()` mimic the Anthropic SDK stream interface (context manager, `text_stream`, `get_final_message()`, usage). First user message containing "still"/"compose"/"voiceover" triggers one tool_use; everything else is plain text. Zero-token usage events emitted so telemetry doesn't crash.
5. `run_agent_turn()` branches on TEST_MODE at the `client.messages.stream(...)` site.

**Why inline mock over a fixture dir:** deterministic, no file I/O, one-file diff. Rejected adding a `--test-mode` CLI flag — env var is consistent with the existing CLI contract.

**Breaks if:** uploading a 100+ MB video returns "file too large" (cap regressed); OR `TEST_MODE=1 just web` fails at startup asking for `ANTHROPIC_API_KEY` (preflight gate regressed); OR sending "hello" in TEST_MODE hits the real Anthropic API (mock branch regressed); OR a subprocess spawned from the server with TEST_MODE on still tries to call paid APIs (env propagation regressed).

## 2026-04-16 — [FIX] Orphan tool_use 400s + mid-flight interrupt

A session hit `anthropic.BadRequestError: messages.38: tool_use ids were found without tool_result blocks`. Root cause in `web/server.py:run_agent_turn`: the assistant turn (with `tool_use` blocks) was appended to `session.messages` **before** `_execute_tool_calls` ran. Any exception, cancel, or abort between those two statements left an orphan `tool_use` committed to history — every subsequent API call on the session then 400'd forever. Test replaying the failing transcript (`test/test_transcript_integrity.py`) reproduces it.

**Fix:** inverted the commit order. Tools now execute first, then assistant+tool_result are appended atomically as a pair. If `_execute_tool_calls` crashes, we synthesize an `is_error: True` tool_result for every pending `tool_use` id so the transcript stays valid. Added `_finalize_pending_tool_uses(session, reason)` as belt-and-suspenders, called in a `finally` around the whole turn and again at the top of every new turn (repairs any history corrupted before this fix). Regression test covers all three failure modes (tool crash, cancel mid-exec, stream error) plus a replay of the real failing transcript.

**Also landed in the same pass:**
- **Mid-flight interrupt**: new `POST /api/interrupt` lets the user type while the agent is running. The server sets `cancel_event` + stores `pending_interrupt_text`; the top-of-loop cancel check injects the text as a user message and continues the same turn instead of ending it. Frontend (`app.js:sendMessage`) detects `state.thinking` and routes to `/api/interrupt` automatically — no separate UI control. Transcript integrity guaranteed by (A) so the interrupted turn is always valid.
- **Scene-creation checklist in `web/hop_prompt.md`**: explicit 5-step ordering (survey → show manifest → single `set-scenes` call → create only if needed → confirm) to stop the agent from dribbling `add-scene` calls one-at-a-time and skipping ahead to compose.

**Breaks if:** agent sends a prompt that triggers tool use and the subsequent send 400s with "tool_use ids were found without tool_result" — the atomic-commit or finalize path regressed. Or: user types during an agent turn and the message starts a separate new turn instead of landing as an inline interrupt.



Session `005933acd2…` looped on `read_image` returning "file does not exist" for `Screenshot 2026-03-12 at 1.00.01 PM.png`. Root cause: macOS screenshot filenames contain U+202F (narrow no-break space) between the time and AM/PM. `list_dir` returns the raw unicode name; the model normalizes U+202F to ASCII space when echoing it back in the next `read_image` call; path lookup misses. The agent retried 3× then gave up and guessed content blind.

**Systemic fix:** extracted `_safe_filename()` as the canonical sanitizer (ASCII alnum + `._-`; everything else → `_`) and added `_ensure_safe_name()` which renames unsafe files in place during `tool_list_dir` enumeration. Two choke points now enforce the invariant: HTTP upload (already sanitized — refactored to reuse the helper) and any directory listing the agent performs. Files with unsafe names literally cannot survive first contact with the agent. Rejected: fuzzy-matching fallback in `_resolve_project_path` — hides the problem instead of eliminating it, and two-way magic is worse than one-way renames.

Documented as an invariant in `CLAUDE.md` so future file-producing paths route through `_safe_filename()` rather than reinventing.
## 2026-04-15 — [CHANGED] web/server.py: Flask → FastAPI

Replaced stdlib http.server with FastAPI + uvicorn. SSE streaming now uses sse-starlette
EventSourceResponse (proper async, no manual write/flush). All request bodies are Pydantic
models. Auto-generated OpenAPI docs available at /docs.

**Breaks if:** `parallax web` fails to start, SSE stream from /api/stream/<id> stops
delivering incremental tokens, or any existing frontend fetch call gets a 422
instead of the expected response shape.

## 2026-04-15 — [CHANGED] generate voice --engine say + Mode 2 e2e test

Added macOS `say` as a first-class voice engine option. `parallax generate voice --engine say`
calls `say -v <voice>` (free, offline, no API key) and converts AIFF→MP3 via ffmpeg.
TEST_MODE on macOS now defaults to `say` when available — output is real speech, not silence.
Falls back to silent stub on non-macOS or if `say` is missing.

Mode 2 Ken Burns Draft e2e test added: `generate still` → `compose` → verify real MP4.
This is the first test that exercises the full ffmpeg render path against TEST_MODE stills.

**Breaks if:** `parallax generate voice --engine say "hello"` exits non-zero on macOS with ffmpeg
installed, or if Mode 2 test fails because `compose` can't read the stub PNG from `generate still`.

## 2026-04-15 — [CHANGED] V2 command surface added to beta (generate, script, ingest, web)

Added all V2 command groups alongside existing V1 commands — no breakage, addition only.
V1 commands (`run`, `create`, `animate`, `voiceover`, `transcribe`, `align`, `chat`) remain
for backward compat; retirement in V3 when V2 is proven stable.

New commands:
- `generate still|voice|video` — V2 image/audio/video generation group. `still` delegates
  to existing Gemini path (real mode) or creates a real solid-color PNG via ffmpeg (TEST_MODE).
  `voice` adapts `cmd_voiceover`. `video` is a stub pointing to fal.ai (not yet built).
- `script write|rewrite` — write/rewrite scripts via Claude. TEST_MODE returns a deterministic
  seed-based placeholder so tests are fully offline.
- `ingest <path>` — transcription + index for video files. `--estimate` dry-runs cost projection
  (zero API calls). Full WhisperX bulk ingest deferred — single-file path still via `transcribe`.
- `web` — alias for `chat`. V2 name per spec.
- `project list` — alias for `projects`. V2 subcommand name.
- `manifest validate` — new op added to existing manifest command. Validates sequential
  scene numbers, positive durations, resolution format, fps whitelist, missing stills.

TEST_MODE improvement: `generate still` now writes real PNG files (ffmpeg solid-color) instead
of the V1 `b"TEST_PLACEHOLDER"` bytes. Downstream `compose` can now actually run on TEST_MODE
stills without ffmpeg choking on invalid image data.

Two V2 e2e tests added to `test/test_cli.py`:
- Mode 1: `generate still` — verifies real PNG header + manifest creation.
- Mode 3: `script write --out` — verifies structured script file written.

**Breaks if:** `parallax generate still "brief"` exits non-zero in TEST_MODE, or `parallax script
write "brief" --out f.txt` exits non-zero in TEST_MODE — these are now covered by CI.

## 2026-04-14 — [DECISION] Instrumented logging + pricing table (beta)

Every external provider call (Gemini image, ElevenLabs voiceover) now
emits `request_intended` + `cost_estimated` events via `core/instrumented.py`
before dispatching, reading rates from `core/pricing.py`. Real-mode and
TEST_MODE emit the same shape, so TEST MODE doubles as a dry-run cost
estimator and the costs page can fold projected spend alongside actual.

**Why this shape:** parallax had ~100 scattered `TEST_MODE` branches and
zero structured capture of what was being requested — neither real nor
test runs were auditable. A provider-interface refactor is the right
long-term move, but I scoped down to a two-function helper that the
existing CLI can call from inline. Same observable events; no big-bang
rewrite. The two helpers are deliberately tiny so they can be lifted
into a `plexi_harness` package later without dragging parallax internals
with them.

**Rejected:** full `ImageGenerator`/`TTS` provider abstraction (too much
for one session, regression-heavy), runtime pricing fetches (no provider
publishes a stable identity/pricing endpoint, and a network call per
dispatch is a latency bomb). `core/pricing.py` carries a `LAST_VERIFIED`
date instead — update the file quarterly.

## 2026-04-14 — [DECISION] beta layout: flatten `.parallax/` → `parallax/users/<user>/<project>/`

Breaking path change on the `beta` branch. Hidden `.parallax/` at cwd
root is gone; everything parallax-managed now lives at a visible
`parallax/users/<user>/<project>/` nested workspace. Raw media at the
launch dir root is shared across every user/project via a two-tier
sandbox in `_resolve_project_path(read_fallback=True)`: writes target
the per-user workspace, but relative reads fall through to PROJECT_DIR
when the workspace-side path doesn't exist.

**Why:** the old layout had three problems — the hidden dotdir was
invisible to users browsing their projects, the per-user mode was
gated on network-access/password flags (so local dev never triggered
it), and raw uploaded media lived inside a specific project with no
way to share across variations. The new layout makes the filesystem
structure legible, always nests per-user, and lets the user drop raw
files at the master dir once and reference them from any project.

**Rejected:** writing a migrator from old to new layout (beta is a new
branch, users still on main keep the old layout; a migrator would
double the code paths we'd have to maintain), keeping `.parallax/` but
adding a `users/` subdir (still hides the nested work from casual
inspection). `~/.parallax/` paths in the user's HOME (events log,
update cache, server registry) are unchanged — those are global,
cross-project state and don't need the visibility fix.

## 2026-04-14 — [FIX] Reference image prepend regression in /api/message

`POST /api/message` used to prepend `[Reference images selected: ...]`
to the user turn so Claude could see selected refs and pass them into
`parallax_create(ref=[...])`. An earlier TEST MODE trigger edit in the
same handler silently removed the block — the Edit call replaced the
whole `session = get_or_create_session(...)` + prepend block with just
the trigger check, and the prepend was lost. Result: refs went
straight to the floor; Gemini got a text-only brief.

**Root cause:** I wrote the Edit `old_string` / `new_string` too wide
and didn't re-read the removed lines to verify what I was discarding.
Restored the prepend AND went further — `Session.selected_refs` now
persists per message, and `tool_parallax_create` auto-injects them
into the `ref` arg whenever Claude forgets to set it explicitly.

**What NOT to do next time:** on Edit calls that delete surrounding
code as a side effect, grep for any feature tag in the removed region
before running the tool. The regression would have been caught by a
pre-edit `grep -n reference_images web/server.py`.

## 2026-04-14 — [GOTCHA] Python pipe buffering + `PARALLAX_BIN` override for tests

The baseline Playwright test hung for ~15 minutes on its first run
because `parallax chat` spawns `python -m parallax_web` without `-u`
and its `print("parallax-web: url = ...")` line sits in a 4KB stdout
buffer forever when the parent pipes stdout. Fix: set
`PYTHONUNBUFFERED=1` in the child env — `start_server()` in
`test/playwright/_helpers.py` now does this by default.

**Separately:** `shutil.which("parallax")` resolves to
`~/.local/bin/parallax` which symlinks to the *main* worktree's
`bin/parallax`. A test that launches a worktree's server ends up
invoking main's CLI binary, so any CLI changes on a feature branch are
silently bypassed. Added `PARALLAX_BIN` env var override to
`_find_parallax_bin()` so tests can pin the binary, and the shared
test helper sets it automatically.

**What NOT to do next time:** trust `print()` to line-buffer on a
piped subprocess, or trust PATH resolution inside a worktree.

## 2026-04-14 — [CHANGED] Server registry at `~/.parallax/servers.json`

`web/registry.py` records `{pid, cwd, host, port, user, started_at}`
for every running parallax-web process at home-dir scope. Each server
registers on startup and deregisters on normal exit / SIGINT / SIGTERM
via `install_shutdown_hooks()`; readers auto-prune entries whose pid
is dead. `GET /api/servers` exposes the live list.

**Why home-dir, not project-dir:** the user wanted multi-server +
cross-workspace discovery. A per-project file would only see one
server at a time; a home-dir list spans every launch dir. Aligns with
the existing `~/.parallax/events.jsonl` telemetry log.

## 2026-04-13 — [CHANGED] Web UI sidebar, update command, install bootstrap

Session focused on parallax-web frontend and CLI tooling. Built a persistent left-sidebar session switcher replacing the history drawer, added video delete (× on hover), replaced trash emoji with minimal × on stills, removed Open in Finder button, and added a Google Drive link stored in localStorage per user. Added `parallax update` CLI command and a daily background update check (git ls-remote, cached in `~/.parallax/.update_check`, prints one-liner to stderr if behind). Added `scripts/install.sh` for curl-pipe bootstrap (brew + python@3.11 + just + ffmpeg + just install). Fixed exit-code-127 bug in `just install-web`: switched from `web/.venv/bin/pip` to `web/.venv/bin/python3 -m pip`; same fix applied in `cmd_update` which now does install steps directly in Python without calling `just`.
**Progress:** All shipped and pushed — sidebar, delete, Drive link, update cmd, install.sh, 127 fix.
**Open:** Project sidebar scoping deferred (what "project" means, delete-vs-delete-workspace distinction, live status for backgrounded sessions).

## 2026-04-13 — [FUTURE] HIGH PRIORITY: real fal.ai video generation

**Status:** Stub only. The CLI has zero working code that calls fal.ai. The
audit of the video-production skill confirmed it was never implemented there
either — only documented in `references/video-duplication.md`.

**What needs to be built:**
- New `parallax veo` subcommand that takes a brief + optional reference image
  and generates a real AI video clip via `fal_client.subscribe(...)`.
- Recommended model: `fal-ai/kling-video/v3/pro/image-to-video` (per the skill's
  reference doc). Cost: ~$0.336/sec with audio, ~$0.392/sec with voice control.
- Call signature: `arguments={image_url, prompt, duration: 3-15, generate_audio: True, aspect_ratio: "9:16"}`
- Add `type: ai_video` scene support in compose so a generated clip can be
  spliced into the manifest like any other video scene.
- `parallax_veo` web tool + HoP prompt update so the agent can dispatch it.
- `FAL_KEY` env var resolution already exists in `packs/video/scripts/api_config.py`,
  no auth work needed.

**Why deferred:** Both end-to-end tests passed without it (TST-A: video + still
+ vo + headline + captions; TST-B: stills only Ken Burns). The canonical pipeline
is production-grade with the existing primitives. fal.ai unlocks a meaningfully
new capability (true AI video gen with talking characters) but it's a clean
add-on, not a blocker. Worth a focused session with a single committed model
choice rather than rushed at 6 AM.

**Reference:** `~/.claude/skills/video-production/references/video-duplication.md`
has the exact endpoint contract.

## 2026-04-13 — [DECISION] Manifest-first compose is the canonical pipeline architecture

The Parallax pipeline must be manifest-driven. The flow is:

1. **HoP agent** (creative collaborator) talks to the user, makes creative decisions,
   confirms the brief. HoP does NOT know the manifest schema.
2. **Editor agent** (manifest specialist) takes a creative brief or edit instruction
   and produces or modifies `.parallax/manifest.yaml`. The schema lives in this
   agent's prompt and tools, nowhere else.
3. **Compose** is a deterministic Python step. `parallax compose` reads the manifest
   and renders exactly what's specified — scene list, motion presets per scene,
   voiceover text, captions, headline, transitions. No globbing. No "just grab
   everything in stills/."

Each stage is independently approvable: still gen → manifest draft → Ken Burns
preview → voiceover → caption burn → final assembly. Approval gates between
stages let the user catch problems before paying for the next step.

**Why this matters:** the current `parallax animate` does a dumb glob of
`stills/*.png` and ignores `.parallax/manifest.yaml` entirely. We've been
patching around this in the parallax-web server (temp dirs with symlinks for
specific stills) — those are workarounds for a missing primitive. The right
fix is `parallax compose --from-manifest`, not more server-side hacks.

**What was rejected:**
- HoP editing the manifest directly. Leaks schema knowledge into the wrong
  agent. Breaks every time the schema changes. HoP should never see YAML.
- Natural-language dispatch driving rendering. The agent is good at creative
  decisions, bad at deterministic file selection. Manifest is the contract.
- `parallax animate` taking file path arguments. Same shape problem — the
  manifest already exists for this purpose, don't bypass it.

**Next steps (deferred until we commit to scoping this):**
- Add `parallax compose` subcommand that reads `.parallax/manifest.yaml` and
  drives ken-burns + voiceover + caption + assembly stages.
- Define the editor agent prompt + tools. Probably a single `edit_manifest`
  tool that accepts a JSON patch or a natural-language instruction.
- Strip the temp-dir symlink hack from `parallax-web/server.py` once compose
  exists — the editor agent will write the manifest, HoP will dispatch compose,
  no specific-stills parameter needed.

## 2026-04-12 — [CHANGED] CLI observability: --json NDJSON, --stdin, file logging

Instrumented the CLI so other agents (Plexi app wrappers, orchestrators)
can drive it over a JSON border and inspect runs via real log files —
unblocks the Parallax Plexi app that wants to wrap the CLI conversationally.

Three additions, all opt-in, all zero-impact when unused:

1. **`--json` NDJSON output mode** on `run`, `create`, `animate`. New
   `core/events.py` module-level `Emitter` singleton is a no-op by default.
   `enable_json(run_id)` captures the real stdout, rebinds `sys.stdout` to
   `sys.stderr` so every pre-existing `print()` in the pipeline harmlessly
   routes to stderr, and writes newline-delimited JSON events to the
   captured stdout with `flush()` after every emit. Event types:
   `run_started`, `agent_call` (start/end pair — wrapped around
   `core/llm.complete`), `still_generated`, `voiceover_generated`,
   `assembly_started`, `assembly_complete`, `error`, `run_complete`.
   Every event carries `type`, `ts` (ISO 8601 UTC), `run_id`.

2. **`--stdin` JSON job spec** on `run` and `create`. When set, the CLI
   reads one JSON object from stdin — `{brief, mode?, style?, count?, ref?}`
   — instead of taking positional argv. Fails fast with a clear message on
   invalid JSON or missing `brief`. Composes with `--json` so an
   orchestrator can pipe JSON in and get NDJSON out. Also implicitly sets
   `skip_clarifications=True` because there's no human to answer.

3. **Per-run file logging** via new `core/logging_setup.py`. Installs a
   file handler writing
   `<PARALLAX_LOG_DIR>/logs/runs/<run_id>/parallax.log` with format
   `%(asctime)s %(levelname)s %(name)s: %(message)s`. Called once from
   `cmd_run`/`cmd_create`/`cmd_animate` after the run_id is minted.
   Instrumented the hot spots: `cmd_run` boundary, HoP assembly (replaced
   the noisiest prints with `logger.info`/`logger.exception`), and a
   `logger.exception` on the top-level HoP `_route` exception so
   tracebacks land in the file instead of being swallowed into the
   RuntimeError chain.

Critical ordering detail: `emitter.enable_json()` must run BEFORE
`_ensure_project_layout()` and any clip-scan prints, or the project-layout
init message escapes on the real stdout and corrupts the NDJSON stream.
Moved all three command entry points to call `enable_json` immediately
after argv is parsed.

Also normalized `TEST_MODE` truthy parsing at the CLI level — the old
`== "true"` check rejected `TEST_MODE=1`, which the acceptance smoke test
uses. Now accepts `1|true|yes|on` and re-exports `TEST_MODE=true` to the
environment so the deeper `HoP`/`asset_generator` checks that still read
the literal string keep working.

Pre-existing per-run JSON state files under `.parallax/logs/runs/<run_id>/`
(`run.json`, `job.json`, `result.json`, `review.json`) are untouched —
these are the offline audit trail; NDJSON is the online live stream.

Rejected: (a) a full `print → logger` sweep across HoP and the packs —
would be a 400-line refactor for marginal gain over instrumenting the
hot spots; (b) a per-agent `agent_call` registry — derive the agent name
from the first 40 chars of the system prompt, good enough for now; (c) a
`.env.example` — out of scope, user explicitly said no new config
scaffolding in this task.

## 2026-04-12 — [CHANGED] bin/parallax shebang → python3, explicit version check in setup

Cross-machine install compatibility. The shebang was pinned to
`#!/usr/bin/env python3.11`, which fails cryptically on a fresh Mac where
Homebrew's `python3` is typically 3.12/3.13 and `python3.11` is absent
unless explicitly installed. Changed the shebang to `#!/usr/bin/env python3`
so the CLI launches under any 3.x on PATH, and added a runtime check at the
top of `cmd_setup` that fails fast with a clear message if
`sys.version_info < (3, 11)` (styled to match the existing `✓ / ✗` setup
output). The 3.11 floor was kept to match the prior shebang intent —
repo-wide grep found no syntax above PEP 585 generics (3.9+): no `match`,
no `except*`, no `Self`, and no `pyproject.toml` / `requirements.txt`
declaring a minimum, so the old shebang was the only version signal.
Follow-up: `test/test_cli.py` shebang and `Makefile` still hardcode
`python3.11` — left untouched per scope, should be migrated next.

## 2026-04-12 — [CHANGED] Single-command ecosystem bootstrap for fresh Mac

Added `scripts/bootstrap-ecosystem.sh` — one script that takes a blank Mac to
a working Parallax ecosystem (Plexi desktop app + Parallax CLI + Parallax
Plexi app) in a single invocation. Motivation: someone other than Ian needs
to be able to sit down at a fresh Mac tonight and install the whole stack
without reading three READMEs and cross-referencing install paths.

Eight stages: preflight (macOS check, Xcode CLT, Homebrew, brew deps,
rustup), clone-or-update for all three repos (real remotes hardcoded — no
guessing), PLEXI `install.sh`, `make install-cli` + PATH guarded-append to
`~/.zshenv`, `rsync -a --delete --exclude=.git` of parallax-app into
`~/.plexi/apps/parallax/`, interactive secret prompts for the three API keys
(`-s` silent read, upsert via Python for BSD-sed-safe in-place rewrite),
`parallax setup` smoke test, done banner with next-step instructions.

Idempotency: every stage checks state before mutating. `brew list` gates
`brew install`. `git pull --ff-only` on existing clones. `rsync --delete`
mirrors the app dir. `guarded_append` uses `grep -Fqx` to avoid duplicate
PATH lines. Secret stage is the one interactive-by-design exception: if a
key is already present it asks "overwrite? [y/N]" rather than silently
clobbering.

Rejected alternatives: (1) publishing a Homebrew tap — overkill for three
repos one person uses; (2) embedding keys in a template dotenv — violates
the no-echoing-secrets rule; (3) symlinking parallax-app into
`~/.plexi/apps/parallax/` — would make source edits live instantly but
makes updates implicit; copy-with-rsync forces explicit re-bootstrap and
matches how Plexi's own `install-alpha` recipe works.

## 2026-04-12 — [CHANGED] Parallax CLI project layout — any cwd is a project

Two motivations, one change: (a) the final artifact was buried four levels
deep in `.parallax/<concept>/output/` and the user had to dig to find it;
(b) you had to run `parallax project new <name>` before any real work, which
fought the intuition that cd-ing into a folder full of footage should Just
Work.

New canonical layout, auto-scaffolded on first command in any cwd:

```
<cwd>/
├── input/      # source footage + reference images (user-provided)
├── output/     # finals only — <concept>_v<version>.mp4 + latest.mp4 symlink
├── drafts/     # version history + pre-overlay assembly cuts
├── stills/     # generated stills from `parallax create`
└── .parallax/  # internal state, manifests, run logs
```

`core/project_layout.py` owns `ensure_project_layout(cwd)` (idempotent, moves
loose videos/images into `input/` on first run, silent no-op after),
`next_version(project_root, concept_id)` (scans output/drafts for used
`v0.0.N` patches), `update_latest_symlink` (relative symlink with copy
fallback), and `extract_abs_video_path` (regex extractor for absolute paths
mentioned in briefs). Wired into `cmd_run`, `cmd_animate`, `cmd_create`.

HoP's footage_edit branch now publishes its true final (post-overlay if
overlays fired, else assembly output) to `project_root/output/<concept>_v<ver>.mp4`
via `job["project_root"]`, updates `output/latest.mp4`, and — if overlays
fired — archives the pre-overlay cut to `drafts/<concept>_v<ver>_assembly.mp4`.
If the brief contains an absolute `.mp4`/`.mov`/etc path the final is
additionally copied there and the path is printed alongside the canonical one.

Also added: final spend line at the end of `parallax run` using
`get_run_cost(run_id)` against the default $20 budget from BudgetGate.

Rejected: a `parallax init` subcommand (adds a ceremonial step the user
explicitly doesn't want), migrating existing projects under
`~/Documents/parallax-projects/` (out of scope, user can opt in). Kept
`parallax project new` backwards-compatible but emitting the new layout —
some users may still want to scaffold in the projects root by name.

## 2026-04-12 — [FIX] HoP reliability: SeniorEditor fail-loud + Evaluator split-rubric

Two silent-failure bugs found during verification and fixed together as paired
reliability fixes for the footage_edit HoP pipeline.

**Bug 1 — SeniorEditor silently swallowed JSON parse failures.** `_parse_response`
caught parse errors, printed a warning, and returned a stub `{output: {}, escalate: True}`.
HoP then happily fell through to "assemble all clips" because `selected_clips` was
missing. In the verified run this looked fine only because the cached manifest was
already optimal. In a real edit where clip selection matters, a broken LLM response
would ship the wrong cut with zero visible error.

Root cause: defensive fallback hiding a failure that should halt the pipeline.

Fix: `_parse_response` now raises `RuntimeError` on any parse failure. `execute()`
catches the first failure, retries the LLM call ONCE with an explicit schema
reminder appended to the user prompt ("RESPOND WITH VALID JSON ONLY..." + a
call-specific schema block for clip-selection / manifest / generic tool-call modes),
and if the retry also fails to parse, raises with the raw response truncated to
500 chars. No silent fallback anywhere in the path. Retry count is exactly 1.

**Bug 2 — Evaluator used the stills-pipeline rubric for footage_edit jobs.**
`core.evaluator.Evaluator._inspect_output` produced a report containing
`scene_count`, `draft_success`, and `stills_success` — fields that only exist
for the stills pipeline. For a footage_edit result none of these are populated,
so the LLM saw `scene_count: 0`, `stills_success: None`, etc., and scored a
perfectly valid 55s silence-trimmed cut at 30% "Revise". Completely wrong rubric.

Fix: added `_inspect_footage_edit(job, result)` which checks only clip-assembly
correctness — output file exists, duration matches `sum(selected_clip_duration)`
within ±5% (or ±0.5s floor for short cuts), assembly_success, and
expected_clip_count > 0. Added `_build_prompt_footage_edit` and
`_inspection_only_score_footage_edit` mirrors. `Evaluator.evaluate()` now
dispatches on `job.get("type") == "footage_edit"` to pick the right rubric;
the stills pipeline path is untouched. The output envelope
(`{approved, score, issues, responsible, recommendation}`) is unchanged so
downstream consumers don't break. Also added `_parse_selected_clips` to parse
the editor's `"0,2,4-6"` format for expected-duration computation.

**Alternatives considered and rejected.**
- Two evaluator classes: rejected — adds a dispatch layer outside the module
  with no real benefit. The internal `_inspect_*` + `_build_prompt_*` +
  `_inspection_only_score_*` split is self-contained in one file.
- Shared JSON-parse helper in SeniorEditor: rejected — only one parse site
  (`_parse_response`). A helper would have been over-engineering. The retry
  logic lives inline in `execute()`.
- Fixing the HoP fallback instead of the editor: rejected — the editor is
  where the parse happens, and fail-loud at the parse site is cleaner than
  adding detection logic in HoP.

**Smoke test.** `TEST_MODE=true parallax run --yes "cut silences"` against a
symlinked OBS clip. Evaluator inspection report now contains only footage_edit
fields: `job_type, output_file, output_exists, has_video, has_audio, duration_s,
resolution, expected_clip_count, expected_duration_s, selected_clips_spec,
duration_delta_s, duration_within_tolerance, assembly_success, assembly_stderr`.
Zero references to scene_count / draft_success / stills_success. Score: 0.92
Approve (was 0.30 Revise under the stills rubric). Direct-call unit checks
also confirm `_parse_selected_clips('0,2,4-6')` → `[0,2,4,5,6]`, no-selection
defaults to all clips, and forbidden field names do not appear anywhere in
the footage_edit prompt or inspection report.

**TEST_MODE caveat.** The TEST_MODE stub assembler produces a synthetic ~60s
placeholder regardless of the selected clip durations, which breaks the
duration-tolerance check. The footage_edit prompt's TEST_MODE note now
explicitly tells the LLM to ignore `duration_within_tolerance`, size, and
visual quality in DRILL runs — only check `output_exists` and `assembly_success`.

Files: `packs/video/senior_editor.py`, `core/evaluator.py`.

## 2026-04-12 — [CHANGED] Added persistent text-overlay primitive + overlay_burn pipeline phase

Earlier verification caught that `parallax run` silently dropped brief-requested
text overlays. Parallax had two caption tools — `burn-captions.py` (word-level
from a VO manifest) and `generate-caption.py` (PNG headline for the first ~1.5s)
— but nothing for "burn this arbitrary text persistently across the whole
footage cut," which is the most common footage-edit overlay case.

**New script.** `packs/video/scripts/burn-overlay.py` — single drawtext pass
with named `--position` (lower-third/upper-third/top/bottom/center), fontcolor,
stroke color/width, fontsize, font/font-file, and optional start/end window.
Prefers `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` (libfreetype guaranteed) with
a warned PATH fallback. Re-encodes `libx264 -crf 18 -preset fast`, audio copy.
Apostrophes and the other drawtext specials (`\\ : ; [ ] , =`) are escaped
explicitly inside `text='...'`; smoke-tested with "don't stop it's a test".

**Tool wrapper.** `burn_overlay()` added to `packs/video/tools.py` following
the existing TEST_MODE stub + `_run()` pattern.

**Pipeline phase.** Added `overlay_burn` to the footage_edit phases
(`index_clips → editor_review → assembly → overlay_burn → evaluate`). The
phase runs a single `_parse_overlay_intent()` call on the brief using the same
`claude-sonnet-4-6` model as SeniorEditor, asking for strict JSON:
`{"overlays": [...]}`. Keyword pre-gate skips the LLM call entirely when the
brief mentions none of: overlay/text/caption/subtitle/title/lower-third/label/
watermark/brand — this prevents "cut a 15s teaser" from accidentally
triggering overlay processing. Multiple overlays chain through intermediate
files under `work_dir`; the final result is published back to
`result["assembly"]["output_path"]` so `evaluate` picks it up transparently
with no other plumbing changes.

**Failure policy.** LLM parse failures raise loudly (real bug, not silent
no-op). Per-overlay `burn_overlay()` failures log and pass the previous video
through unchanged — overlays are additive, they must not fail the pipeline.

**Smoke test.** 3.4-min OBS clip with brief "Cut silences longer than 1.5s
using -35dB threshold. Add a persistent lower-third text throughout the video
reading 'vibe coding a vibe editor'..." — pipeline reached `complete`, overlay
phase burned to `PBN-164_v0.0.1_overlay1.mp4`, midpoint frame pixel check
showed ~5721 near-white pixels in the lower-third strip vs ~2396 in a
same-sized control strip at mid-frame — strong drawtext signature confirmed.

**Not touched intentionally.** `bin/parallax` (WIP preserved),
`packs/video/senior_editor.py`, `core/evaluator.py`, and assembly-output
routing — all flagged as out-of-scope for this change.

## 2026-04-12 — [CHANGED] Parallax is now self-contained — video scripts bundled

Parallax was shelling out to `~/.agents/skills/video-production/scripts/` for every
footage-edit and animation step. Removed that external dependency entirely so a
single `git clone + make install-cli` is everything a user needs.

**What moved.** Copied 22 scripts actually called from parallax code plus 5
helper modules (`api_config`, `config`, `manifest_schema`, `cost_tracker`,
`registry`) from the video-production skill into `packs/video/scripts/`. Import
graph was walked until closure — no dangling imports. Also copied the skill's
`skill-config.yaml` to `packs/video/skill-config.yaml` so `config.py`'s
`SKILL_ROOT / skill-config.yaml` resolution keeps working without hunting for an
external file. Scripts were copied verbatim with exec bits preserved; no
refactors.

**Rewiring sites.** `packs/video/tools.py` (`SKILL_DIR` now resolves via
`Path(__file__).parent`), `core/video_tools.py`, `core/health.py`,
`packs/video/asset_generator.py`, and `bin/parallax` (three separate hardcoded
references in `cmd_animate`, `cmd_create`, `cmd_setup`, plus the module-level
`SKILL_SCRIPTS` constant). A grep for `.agents/skills/video-production` across
the live code paths now returns zero hits — only historical docs/handoffs and
this DEV_LOG still reference the old path.

**Proof of self-containment.** Temporarily renamed
`~/.agents/skills/video-production/scripts` to `.BAK` and re-ran both smoke
tests (`parallax animate` with fake stills, and `TEST_MODE=true parallax run
--yes "trim silences"` against a real OBS clip) — both passed with the external
dir gone. Restored the dir afterward. The video-production skill is deliberately
left intact on disk; each project now stands alone.

Rejected: symlinking to the external skill scripts (still leaves the external
dep), keeping a fallback (hides the real state), vendoring the entire skill dir
wholesale (ships dead weight). One-shot copy of only the live-callee set was
the minimum viable fix.

## 2026-04-12 — [FIX] `parallax run --yes` + symlink-safe indexing

Two unrelated bugs blocked a real user run tonight. Fixed both.

**Bug 1 — clarification gate had no bypass.** `bin/parallax` was stubbing
`builtins.input` to return `""` as a "non-interactive" hack, but HoP interprets
empty input at the human-clarification gate as "approve all questions" and then
exits with `awaiting_clarification`. Root cause: there was no actual skip path
— the stub just silenced the prompt, it didn't tell HoP to proceed. Even fully
specified briefs hit this because `_gather_clarifications` always invents
questions.

Fix: added `-y/--yes` flag to `parallax run` that sets `job["skip_clarifications"]
= True`. `HeadOfProduction.receive_job` now short-circuits the entire
clarification block (both question generation and the `ask_client` decision)
when that flag, or `PARALLAX_SKIP_CLARIFICATIONS=true`, is set. Rejected:
filtering questions to empty inside `_gather_clarifications` — still runs an
LLM call for nothing.

**Bug 2 — symlinked clips broke manifest lookup.** HoP computes
`cached = clip_p.parent / "_meta" / f"{clip_p.stem}.yaml"` but
`~/.agents/skills/video-production/scripts/index-clip.py` starts with
`input_path = Path(raw_input).resolve()` and writes the manifest to the
*resolved* target's `_meta/` dir. So if the caller passes
`assets/clip.mp4 -> /Users/.../OBS/real.mp4`, HoP looked in `assets/_meta/`
while the script wrote to `/Users/.../OBS/_meta/`. Every run with symlinked
clips failed with "No clip manifests produced — all indexing failed".

Fix: resolve once at the top of the HoP indexing loop
(`clip_p = Path(raw_clip_path).resolve()`) and use the resolved path for
meta_dir, cached lookup, and the `index_clip` call itself. This guarantees HoP,
index-clip.py, and downstream assembly all see the same path. Rejected: passing
an explicit `--output` to `index_clip` — more plumbing for the same outcome,
and the resolved-path approach also fixes any other tool in the pipeline that
assumes real paths.

Smoke-tested both fixes together: `TEST_MODE=true parallax run --yes "trim
silences to 200ms"` against a symlinked clip completed with `status: complete`.

## 2026-04-12 — [DECISION] `parallax` CLI + PARALLAX_OUTPUT_ROOT env override

Added a thin `parallax` CLI at `bin/parallax` so users can kick off runs from
any project directory without writing Python wrappers. Four subcommands: `run`,
`status`, `project new`, `projects`. Installed via `make install-cli` which
symlinks into `~/.local/bin/`. Tests at `test/test_cli.py` — 3 smoke cases, all
passing under TEST_MODE.

Non-obvious decisions:

1. **`PARALLAX_OUTPUT_ROOT` env var added to `core/paths.py`.** The existing
   `output_root()` only respected `config/parallax.yaml` (`~/Movies/Parallax`
   default), so there was no way to pin project output to the invocation
   directory. The CLI sets both `PARALLAX_LOG_DIR` and `PARALLAX_OUTPUT_ROOT`
   to `cwd/.parallax/` for project-local runs. Rejected: passing `work_dir`
   through the job dict — would have required plumbing through HoP and every
   branch of `_route()`. Env var is a one-line change and backwards-compatible.

2. **CLI uses `receive_job()`, not `execute()`.** The handoff spec referenced
   `HeadOfProduction.execute({...})` but the actual method is `receive_job()`
   and the brief field is `content`, not `brief`. CLI normalizes this so users
   type `parallax run "..."` and get the real API call.

3. **`footage_edit` requires a `clips` list.** The CLI globs `./`, `./assets/`,
   `./input/` for common video extensions. Under TEST_MODE with no clips it
   synthesizes a 2s black clip via ffmpeg so the pipeline has input — matches
   the pattern in `test/test_manifest_first.py`. Outside TEST_MODE it exits 2
   with a clear message rather than silently generating a placeholder.

4. **CLI is a script, not a package entry_point.** The repo has no
   `setup.py`/`pyproject.toml`, and adding one just for an entry point would
   drag in packaging decisions that aren't in scope. The symlink approach is
   simpler and survives `git pull` without reinstall.

5. **`status` reads the newest manifest under `cwd/.parallax/<concept_id>/`,
   not a fixed path.** Each run produces a new concept_id, so status picks the
   most-recently-modified manifest.



Simplified the editor flow: HoP now routes footage_edit jobs directly to
SeniorEditor instead of going Junior → Senior. SeniorEditor uses Sonnet.
JuniorEditor is left in the codebase as dead code for future cost-optimization
experiments (panel pattern, multi-persona teams) but is no longer invoked.

Why: the junior/senior split was a cost optimization with no current run
history to validate it. Single Sonnet is simpler to debug. Will revisit
multi-agent topologies once we have replay history to A/B against.

All tests still pass under TEST_MODE.

## 2026-04-12 — [CHANGED] Manifest schema validation with feedback loop

Added Pydantic v2 schema for the video manifest at packs/video/manifest_schema.py.
Validator wraps write_manifest_scenes() — invalid writes raise ManifestValidationError with
agent-friendly error messages that flow back through the agent loop, letting the editor
self-correct on the next turn. New manifest_version field ("0.1.0") stamped on every write
enables future schema migrations. Missing manifest_version on read defaults to "0.1.0"
for backwards compatibility with pre-validator manifests.

Updated junior_editor and senior_editor _manifest_prompt() methods to explain the
validation feedback loop so the agent knows to read tool_result errors and fix the field.

Key schema decision: SourceClip (footage section) skips end_s > start_s validation for
non-video clip types (text_overlay, effect_overlay, still) since those use estimated_duration_s
and get start_s/end_s defaulted to 0.0 by write_manifest_scenes. Only type=video (or None)
enforces the time bound.

Tests at test/test_manifest_validator.py — 9 cases, all passing under TEST_MODE.
test_manifest_first.py and test_regression.py still pass after SourceClip schema fix.

## 2026-04-12 — [FIX] Manifest-first refactor — editors no longer plan ffmpeg

Root cause of the 25% failure rate fixed. Both junior_editor.py and senior_editor.py
now always use the _manifest_prompt() path for footage edits, removing ffmpeg from
the editor toolset. New regression tests at test/test_manifest_first.py verify the
manifest path is taken and ffmpeg is no longer in the tool list.

Changes:
- `_build_prompt()` in both editors: `elif output_mode == "manifest"` → `elif output_mode == "manifest" or type == "footage_edit"` so footage_edit always takes the manifest path
- `_get_tools()` in both editors: removed `"ffmpeg"` from the returned list (kept `inspect_media`, `suggest_clips`)
- `packs/video/tools.py`: added `write_manifest_scenes()` function + TOOL_REGISTRY entry + tool_signatures() entry
- `core/head_of_production.py`: sets `job["output_mode"] = "manifest"` before calling editors in the footage_edit branch; after editor returns, persists any manifest output via `write_manifest_scenes()` before assembly

Tests pass with TEST_MODE=1. All 5 existing regression scenarios also pass.

## 2026-04-11 — [FUTURE] Manifest-first refactor handoff doc ready for execution

Self-contained handoff written at `docs/handoffs/manifest-first-refactor.md` for a Sonnet agent to execute end-to-end. Investigation found the 25% failure rate root cause: `packs/video/junior_editor.py` and `senior_editor.py` both have a `_manifest_prompt()` method that only fires when `job["output_mode"] == "manifest"`, but HoP never sets that flag. For non-indexed footage_edit jobs, editors fall through to a `tool_calls` prompt that includes `ffmpeg` in the tool set (`_get_tools()` returns `["inspect_media", "suggest_clips", "ffmpeg"]`). The literal `ffmpeg` string is the smoking gun.

**Also discovered:** TEST_MODE infrastructure already exists. Every tool in `packs/video/tools.py` has `if TEST_MODE:` blocks. `generate_still` creates real 1080x1920 placeholder PNGs via PIL. `assemble` creates real black MP4s via ffmpeg. Test harness needs writing but the stubs are already there.

**Progress:** Handoff doc has 5 sections (current state with line numbers, file-by-file before/after, TEST_MODE design, 5 specific test cases, step-by-step execution plan with verification commands). A Sonnet agent should be able to execute without further context.

**Open:**
1. **TEST_MODE refactor** — current `if TEST_MODE:` scattered pattern is hacky. Better: decorator pattern (`@test_mode_returns(stub=create_placeholder_still)`) so production code stays clean and you can grep for every function with test mode in one query. Refactor AFTER manifest-first lands, not as part of it.
2. **Run the handoff** — handoff is ready but no agent has executed it yet. Next session, spawn a Sonnet agent with the handoff doc as input.

## 2026-04-11 — [DECISION] Parallax → Plexi app packaging architecture

Designed how Parallax decomposes into three Plexi layers: one chat app (`~/.plexi-alpha/apps/parallax/`), agent configs in `.plexi/agents/parallax/` (system prompts extracted from Python to standalone .md files), and pipeline tools in `~/.plexi-alpha/tools/parallax/`. Manifest-first refactor is Phase 1 — editors must write manifest.yaml instead of planning ffmpeg calls (fixes 25% failure rate on footage edits). Agent versioning with test-case regression suites captured from approved production runs. LLM integration shifts from env var to Plexi SecretGet for API keys; cost tracking via cost_report events.
**Progress:** Full packaging spec at `docs/parallax-plexi-packaging.md`. App spec updated at `docs/parallax-plexi-app-spec.md` with state management, cost reporting, SDK configure, and future primitives.
**Open:** No code changes yet — all specs. Manifest-first refactor (rewrite editor prompts, add write_manifest_scenes tool, remove ffmpeg from editor toolset) is the first implementation task.

## 2026-04-10 — [CHANGED] Brand confirmation in plan gate + agent tool use loop

**Brand gate**: `_generate_plan` now reads brand_file (if provided) and extracts the brand
name for display. `_confirm_plan` now takes `job` as a third argument so it can mutate
`job["brand_file"]` in place. When trust < 0.75 and no brand is set, HoP prompts "Brand
file? (path to YAML, or Enter to skip)" before the Proceed prompt. Brand name shows in the
plan block and auto-proceed summary line.

**Agent tool use** (`core/agent_loop.py`): Generic tool-calling loop using Anthropic's tool
use API. `build_tool_schemas()` converts TOOL_REGISTRY Python signatures to Anthropic tool
input schemas via `inspect.signature()` + type annotation walking. `run_with_tools()` runs
the standard tool_use loop: model call → tool_use blocks → `call_tool()` → tool_result →
next turn, returning `{text, tool_calls, input_tokens, output_tokens}`.

Falls back to `llm_complete()` (which handles CLI fallback) when:
- `tool_names=[]` — no tools requested (e.g., StoryboardPlanner)
- No `ANTHROPIC_API_KEY` available

JuniorEditor and SeniorEditor now use `run_with_tools()`. For footage_edit jobs (those with
`clip_index_data`), they get tools: `inspect_media`, `suggest_clips`, `ffmpeg`. StoryboardPlanner
uses `run_with_tools` with `tool_names=[]` (falls back to single call — no behavior change,
door open for future tools like `generate_still`).

All regression and scenario tests continue to pass.

## 2026-04-10 — [CHANGED] Plan confirmation gate + manifest-first architecture

HoP now generates a production plan and presents it to the user before any expensive work
starts. The plan includes: articulated intent (HoP's one-sentence understanding of the job),
deliverable type, estimated scene count, cost range, and pipeline phases.

Trust score gates the confirmation:
- Low trust (<0.75): full plan printed, user must Enter to proceed or type 'abort'
- High trust (>=0.75): one-line auto-proceed summary
- TEST_MODE: skipped entirely

The plan is written into the manifest as a `brief` block, making it the source of truth for
all downstream agents and the evaluator. The evaluator now reads `manifest_brief` (HoP's
articulated intent) instead of raw `job["content"]`.

Storyboard branch now merges into existing manifest (preserving the brief) instead of
overwriting. Footage_edit branch writes a `footage` block after assembly for auditability.

5-scenario regression test suite added: script_brief, storyboard/draft, footage_edit,
plan_abort, storyboard/stills_only. All pass in TEST_MODE alongside existing scenario tests.

## 2026-04-10 — [FIX] End-to-end pipeline fixes for real (non-TEST_MODE) runs

Multiple issues found and fixed during real e2e testing:

1. **StoryboardPlanner max_tokens truncation**: 4096 tokens was insufficient for 18-scene
   storyboards with detailed image prompts. Increased to 16384. This was the root cause of
   every strawberry parse failure — the JSON was being cut off mid-response.

2. **StoryboardPlanner JSON parsing**: Added brace-matching extraction strategy as fallback
   when markdown fence stripping fails. Handles LLMs that embed JSON in prose.

3. **Manifest missing resources.supplied**: `assemble.py` validates that `character-ad` format
   manifests include `script` and `character_reference` in `resources.supplied`. HoP wasn't
   writing these. Added resource injection from job data.

4. **output_path missing from tool results**: `_run()` returns `{success, returncode, stdout,
   stderr}` but not `output_path`. Both `assemble()` and `assemble_clips()` results lacked it.
   HoP now injects output_path after successful assembly (finds newest .mp4 in output dir).

5. **Multi-manifest clip selection**: Editor returns per-file selections like `9657:3,9657:4`.
   Added `_merge_selected_clips()` to parse both `KEY:[indices]` and `KEY:INDEX` formats,
   merge selected clips into a single manifest, and pass to assemble-clips.py. Previously,
   selections were silently ignored with >1 manifest, causing all 54 clips to be assembled
   (30+ min ffmpeg run that timed out).

6. **assemble_clips timeout**: Increased from 600s to 900s.

## 2026-04-10 — [FIX] TEST_MODE index_clip and assemble_clips now produce real artifacts

index_clip in TEST_MODE returned a success stub but never created the _meta/*.yaml manifest
file that HoP's footage_edit path reads. All clips were silently skipped → "No clip manifests
produced" crash. Fixed: drill mode now creates a real stub YAML with synthetic clip data
(duration from ffprobe, ~8s per synthetic clip). Same pattern as generate_lipsync creating
a real vo_manifest.json.

assemble_clips similarly returned a success stub with no output file. Evaluator couldn't
ffprobe it. Fixed: now creates a real black video with silent audio track via ffmpeg, matching
what the storyboard `assemble()` drill already does.

Also fixed evaluator to check `draft` key as fallback for `assembly_success` — the storyboard
path stores assembly result under `result["draft"]`, not `result["assembly"]`.

Improved TEST_MODE evaluator prompt: now explicitly tells the LLM that scene_count=0 in
container metadata is expected (scenes are in the manifest, not embedded in black placeholder
video), and that footage_edit drill videos won't contain real source footage.

## 2026-04-10 — [DECISION] Scenario test suite for two core use cases

Created test/test_scenarios.py with two concrete scenarios tied to real reference material:
1. YouTube footage edit: two OBS clips from 5-ians-youtube-video/input/ → footage_edit pipeline
2. Sexy strawberry Ken Burns: ref video (recycled VO) + ref image → storyboard/draft pipeline

Both pass 3 consecutive runs in TEST_MODE. Brief file added to YouTube input folder so future
test runs have a self-contained description of the edit. Test verifies: concept_id, status,
assembly success, output file exists and is non-trivial, evaluation present.

## 2026-04-10 — [CHANGED] Replaced trust score with cost-gated autonomy (BudgetGate)

The abstract 0-1 trust score with streak-based increases was replaced by dollar-denominated
decision gates. Three layers: per-decision cap ($2 default), per-concept budget ($20 default),
session velocity check ($10/30min default). Gate logic: if max_loss < per_decision_cap AND
budget_remaining > cost, proceed autonomously. Otherwise escalate with cost context.

Rejected: the trust score system. It was a proxy metric — a hardcoded streak counter that bumped
a float by 0.05, with no connection to actual risk. Cost-of-mistake is concrete, context-dependent,
and naturally escalates the right things. The prediction tracker (TrustScore class) is kept as a
learning signal for HoP accuracy improvement, but it no longer gates decisions.

## 2026-04-10 — [DECISION] PreWatchBrief + ReviewSession as the human feedback loop

Human reviews are now structured: PreWatchBrief generates a hypothesis (what changed, predicted
rating, predicted feedback) before the human watches. ReviewSession collects rating + notes and
feeds the prediction outcome back into the trust tracker. This makes review faster — human watches
to confirm/override, not to discover — and generates training data for improving predictions.

## 2026-04-10 — [DECISION] Drill gate pattern — TEST_MODE short-circuits at agent entry points

Every agent (ScriptWriter, JuniorEditor, SeniorEditor, AssetGenerator) checks TEST_MODE at the
very top of its entry point and returns immediately with a stub. No LLM calls, no API calls,
no validation. AssetGenerator produces actual JPEG gray cards (PIL) with prompts stamped on them
and macOS `say` audio with voice inferred from brief keywords. This lets the full pipeline run
end-to-end at zero cost.

## 2026-04-09 — [GOTCHA] ANTHROPIC_API_KEY not available in subprocess shells when running via Claude Code

Claude Code stores the Anthropic API key internally (likely macOS Keychain or its own credential store) and does NOT expose it as ANTHROPIC_API_KEY in subprocess environments. Running `TEST_MODE=true python3 test/run_test.py` from a Bash tool call will fail at any `anthropic.Anthropic()` instantiation. Workaround: run from a terminal where ANTHROPIC_API_KEY is explicitly exported, or add it to ~/.zsh_secrets. The AssetGenerator concern test is unaffected because brief validation failure is best-effort (caught silently), and the mock generation path doesn't call the API.

## 2026-04-09 — [DECISION] v1 uses direct messages.create, not client.beta.agents

Implemented all agents as Python classes calling `client.messages.create` directly rather than
registering persistent agents via `client.beta.agents`. Reason: beta agents API requires account
access that may not be provisioned, and adds complexity (session management, agent IDs) that
isn't needed for a single-machine workflow. v2 migration path is commented in every agent class.
Rejected: using LangChain or other orchestration frameworks — too much abstraction for a codebase
we need to control tightly.

## 2026-04-09 — [DECISION] Stateless agents — state lives in log files only

All agent classes have no persistent instance variables between jobs. run_id is injected into the
job dict and passed through to cost logging and file writes. This makes the system trivially
parallelizable and debuggable — every run is self-contained in logs/runs/{run_id}/.
