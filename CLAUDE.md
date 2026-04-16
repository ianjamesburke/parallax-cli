# Parallax CLI — Agent Rules

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
