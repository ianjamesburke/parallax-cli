# DEV_LOG — NRTV Video Agent Network
# This log tracks non-obvious decisions, bugs, and deferred work for the agent network.
# Entries are newest-first. Tags: [FIX] [CHANGED] [DECISION] [GOTCHA] [FUTURE]

## 2026-04-16 — [FIX] Unsafe workspace filenames break LLM path round-tripping

Session `005933acd2…` looped on `read_image` returning "file does not exist" for `Screenshot 2026-03-12 at 1.00.01 PM.png`. Root cause: macOS screenshot filenames contain U+202F (narrow no-break space) between the time and AM/PM. `list_dir` returns the raw unicode name; the model normalizes U+202F to ASCII space when echoing it back in the next `read_image` call; path lookup misses. The agent retried 3× then gave up and guessed content blind.

**Systemic fix:** extracted `_safe_filename()` as the canonical sanitizer (ASCII alnum + `._-`; everything else → `_`) and added `_ensure_safe_name()` which renames unsafe files in place during `tool_list_dir` enumeration. Two choke points now enforce the invariant: HTTP upload (already sanitized — refactored to reuse the helper) and any directory listing the agent performs. Files with unsafe names literally cannot survive first contact with the agent. Rejected: fuzzy-matching fallback in `_resolve_project_path` — hides the problem instead of eliminating it, and two-way magic is worse than one-way renames.

Documented as an invariant in `CLAUDE.md` so future file-producing paths route through `_safe_filename()` rather than reinventing.

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
