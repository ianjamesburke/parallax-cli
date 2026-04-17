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

**Repo state:** one branch (`main`), one worktree. Beta branch + worktree deleted, all dev work consolidated. Origin not pushed.

**Last shipped this session:**
- Blocks A/B/C: upload cap 50→500 MB, web-layer TEST_MODE mock stream, `parallax chat --test` flag.
- Consolidated beta's FastAPI migration, V2 CLI surface, `--engine say`, uv build, and uncommitted UI work (three-column tabs layout: `app.html` + `timeline.js`) onto main.
- Fixed `python-multipart` missing dep (uploads returning 400).

**Next up (new context window):** scope video generation tools — real fal.ai integration for `parallax veo` (documented in DEV_LOG 2026-04-13 as HIGH PRIORITY).

**Deferred threads:**
- End-to-end test harness from live `chat.jsonl` logs (scoped but not built this session — user turns only as input, free-form markdown rubric, Haiku LLM judge, `test/cases/<slug>/` layout).
- Project sidebar scoping (what "project" means, delete semantics).
- Pre-existing pyright noise around Anthropic SDK types — not functional.

**Known open GitHub issue (pre-existing, not from this session):** `feat: footage indexing system — ingest, master index, segment reads, shared assets` (enhancement, 2026-04-16).



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
