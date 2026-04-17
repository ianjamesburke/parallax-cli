# Parallax CLI — Agent Rules

## VISION

Parallax is the definitive CLI + web UI for AI-assisted short-form video production. A creative Head-of-Production (HoP) agent talks to the user in plain English, dispatches specialized tools (stills, voiceover, Ken Burns, captions, AI video gen), and drives a manifest-first pipeline that renders deterministic, production-grade video. Every stage is independently approvable (stills → manifest → preview → voiceover → captions → assembly). The CLI is fully agent-operable (`--json` NDJSON, `--stdin` job specs, predictable exit codes, TEST_MODE for offline runs) so other agents / Plexi apps can wrap it. The web UI is the human surface: a single chat pane with a persistent sidebar of sessions, a media bin of uploaded + generated assets (images AND video), a gallery of rendered outputs, and mid-flight interrupt so the user can steer the agent without waiting for its turn to end.

**North star:** from a vague creative brief, the user should be one conversation away from a finished 9:16 clip — with the ability to test the full pipeline offline for free.

## CURRENT STATUS

**Last shipped (2026-04-16):**
- Fixed orphan `tool_use` 400s (atomic commit of assistant+tool_result; `_finalize_pending_tool_uses` safety net).
- Mid-flight interrupt: `POST /api/interrupt` injects user text into the running turn instead of starting a new one.
- Scene-creation checklist in `web/hop_prompt.md` (survey → manifest → `set-scenes` → create → confirm).
- Safe-filename invariant enforced at upload + `tool_list_dir`.

**Active thread:** master-agent loop running. Shipped: Block A (video uploads in media bin — upload cap 50 MB → 500 MB; gallery already scanned `input/`), Block B (web-layer `TEST_MODE=1` — mock Anthropic stream + env propagation to all CLI subprocesses), Block C (`parallax chat --test` flag — skips key check + anthropic probe, exports `TEST_MODE=true` to the spawned server). Next scoped block TBD.

**Known gaps / near-term:**
- `parallax veo` (real fal.ai video gen) is still a stub — deferred, documented in DEV_LOG 2026-04-13.
- Project sidebar scoping (what "project" means, delete semantics) deferred.
- Pre-existing pyright noise in `web/server.py` around Anthropic SDK types (lines 74/75, 1612/1613, 1651) — not functional bugs.



## Self-verify end-to-end on FFmpeg/CLI fixes

When fixing bugs in the CLI, compose pipeline, caption/overlay/Ken Burns logic, or any ffmpeg-driven path — anything that does NOT cost API credits (no LLM calls, no ElevenLabs, no image gen) — run the full end-to-end reproduction yourself before handing back to the user:

1. Apply the fix.
2. Run the actual CLI command on real inputs (the user's `/Users/ianburke/parallax-test-dir` is the canonical test bed).
3. Sample frames with `ffmpeg -ss <t> -vframes 1` and Read the PNG to visually confirm the fix.
4. Only then report the fix as done.

**Why:** the user explicitly asked for this loop. Previously we shipped "fixes" without verifying output and the same bug resurfaced. Visual self-check on free operations is the bar.

**Skip the self-verify only when:** the path requires paid API calls (voiceover, still generation, transcription) — in that case describe the expected outcome and ask the user to run it.

## Workspace filename invariant

Every file under a workspace MUST have an ASCII-safe name: alnum + `._-` only, with any other character (spaces, unicode whitespace like U+202F in macOS screenshots, punctuation) collapsed to `_`. Enforced by `_safe_filename()` in `web/server.py` at two choke points: HTTP upload (`/api/upload`) and `tool_list_dir` (auto-renames unsafe siblings on enumeration).

**Why:** filenames with spaces or non-ASCII chars break LLM path round-tripping — the model normalizes U+202F → ASCII space, spaces → %20, etc., so `read_image` loops on "file does not exist". Sanitizing at ingest AND enumeration means the agent literally cannot see an unsafe name.

**When adding a new file-producing path** (new upload endpoint, new tool that writes to the workspace, new ingest mechanism), route the name through `_safe_filename()` before hitting disk. Do not add fuzzy-matching fallbacks in read paths — keep the invariant one-way: unsafe in, safe on disk, safe out.

## Lessons

- **Tool registry location:** The agent tool schema list in `web/server.py` is named `TOOL_SCHEMAS` (not `TOOLS`). When adding or referencing a tool, grep for `TOOL_SCHEMAS` — plural `TOOLS` does not exist.
- **Video scripts live in `packs/video/scripts/`:** ffmpeg-driven scripts (preview-sheet.py, assemble.py, burn-captions.py, etc.) live at `packs/video/scripts/*.py`. `tools/` only holds image-side helpers like `storyboard.py`.
