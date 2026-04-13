# Manifest-First Refactor — Handoff for Execution Agent

This document is a self-contained handoff. Read only this document and execute the refactor. No other conversation context is needed.

**Goal:** Fix the ~25% failure rate on footage edits by forcing editors to write manifest YAML instead of planning ffmpeg commands, then validate the change with an automated test harness that uses placeholder assets instead of real API calls.

---

## Section 1: Current State

### 1.1 Repository Layout

```
~/Documents/GitHub/parallax/
  main.py                           # CLI entry point — invokes HeadOfProduction
  core/
    head_of_production.py           # HoP orchestrator (62k, ~1100 lines) — routes jobs, plan gate, manifest writes
    agent_loop.py                   # Generic tool-calling loop (run_with_tools)
    llm.py                          # LLM abstraction (Anthropic API + Claude CLI fallback)
    evaluator.py                    # Quality evaluation agent
    budget.py                       # Cost-gated autonomy (BudgetGate)
    trust.py                        # Prediction accuracy tracker (TrustScore)
    cost_tracker.py                 # Cost logging to JSONL
    paths.py                        # project_dir(), run_dir() helpers
    concerns.py                     # Concern/ConcernBus for agent-raised issues
  packs/video/
    tools.py                        # 30+ tool wrappers calling ~/.agents/skills/video-production/scripts/
    script_writer.py                # Generates scripts from briefs
    storyboard_planner.py           # Plans scene-by-scene storyboards with image gen prompts
    junior_editor.py                # Executes edits (Haiku), escalates if confidence < 0.70
    senior_editor.py                # Handles escalated edits (Sonnet)
    asset_generator.py              # Image/audio generation (Gemini, ElevenLabs, macOS say)
    evaluator.py                    # Thin wrapper delegating to core/evaluator.py
  config/
    agents.yaml                     # Agent model assignments + escalation thresholds
    parallax.yaml                   # Global config (output paths, ffmpeg timeout)
  test/
    test_regression.py              # 5-scenario regression suite (plan gate, brief block, pipeline)
    test_scenarios.py               # 2 real-file scenarios (YouTube footage, strawberry Ken Burns)
    drill_scenarios.py              # Original drill tests
    drill_scenarios_r2.py           # Round 2 drills
    harness.py                      # Test utilities
  specs/
    manifest-first-refactor.md      # Existing spec for the plan gate + brief block (ALREADY IMPLEMENTED)
  docs/
    parallax-plexi-packaging.md     # Plexi packaging spec (Phase 1 = this refactor)
    parallax-plexi-app-spec.md      # Full Plexi app spec
```

### 1.2 Video Production Skill (External)

```
~/.agents/skills/video-production/
  scripts/
    generate-still.py               # Gemini image generation
    generate-voiceover.py           # ElevenLabs TTS
    assemble.py                     # ffmpeg assembly from manifest (Ken Burns stills)
    assemble-clips.py               # ffmpeg assembly from clip-index manifests (footage)
    burn-captions.py                # Word-by-word subtitle overlay
    index-clip.py                   # Transcribe + segment raw footage
    plan-scenes.py                  # Break script into scenes (Gemini)
    align-scenes.py                 # Align scene timing to VO timestamps
    trim-silence.py                 # Remove silence gaps from VO
    generate-caption.py             # PNG text overlays
    inspect-media.py                # ffprobe wrapper
    suggest-clips.py                # Clip selection helper
    init-project.py                 # Project folder scaffolding
    render-animation.py             # HTML -> MP4 via Playwright
    manifest_schema.py              # Manifest validation
    # ... 20+ more utility scripts (effects, grade, grain, etc.)
```

### 1.3 Where the 25% Failure Rate Comes From

The failure happens in `footage_edit` jobs. The current flow is:

1. HoP indexes clips via `index-clip.py` (transcription + silence segmentation)
2. HoP sends clip data to JuniorEditor or SeniorEditor
3. **The editor has two output modes:**
   - `clip_index_data` path: editor returns `selected_clips` string (e.g., "0,2,4-6") — this works fine
   - Non-indexed path (or when `output_mode != "manifest"`): editor returns `tool_calls` with raw ffmpeg commands — **this fails ~25% of the time** because editors hallucinate wrong ffmpeg flags, incorrect filter syntax, or bad timecodes

The `_manifest_prompt()` method already exists on both JuniorEditor (line 181) and SeniorEditor (line 185), but it is only activated when `job.get("output_mode") == "manifest"`. HoP does NOT currently set `output_mode = "manifest"` for footage_edit jobs — editors get the general tool_calls prompt instead.

For storyboard jobs, the pipeline already works manifest-first: StoryboardPlanner writes scenes, HoP writes them to manifest.yaml, assemble.py reads from manifest. The failure is exclusively in the footage_edit path.

### 1.4 What Has Already Been Implemented

The `specs/manifest-first-refactor.md` spec was partially implemented:
- Plan confirmation gate: DONE (in `receive_job`, `_generate_plan`, `_confirm_plan`)
- Brief block in manifest: DONE (`_write_brief_to_manifest`)
- Evaluator reading `manifest_brief`: DONE
- `footage` block in manifest: PARTIALLY DONE (written after assembly, but editor still uses tool_calls path)

What is NOT done:
- Editors do NOT default to manifest output mode for footage edits
- The `ffmpeg` tool is NOT removed from editor tool sets
- A `write_manifest_scenes` tool does NOT exist
- `assemble.py` does NOT read a `footage` section from manifest (it reads scene data, not footage clips)
- No automated test verifying the manifest-first flow end-to-end

### 1.5 TEST_MODE (Already Exists)

TEST_MODE is already well-established. Every agent and tool checks it:

- **Agents** (ScriptWriter, StoryboardPlanner, JuniorEditor, SeniorEditor, AssetGenerator): return stubs immediately, zero LLM calls
- **Tools** (in `packs/video/tools.py`): each tool has a `if TEST_MODE:` block that returns drill stubs
- **Image gen** (`generate_still`): creates real 1080x1920 placeholder PNGs with scene labels using PIL
- **Assembly** (`assemble`): creates a real black video with silent audio via ffmpeg
- **Clip assembly** (`assemble_clips`): creates a real black video
- **VO gen** (`generate_voiceover`): returns stub
- **Clip indexing** (`index_clip`): creates real `_meta/*.yaml` manifest files with synthetic clip data

TEST_MODE is set via environment variable: `TEST_MODE=true`.

---

## Section 2: The Refactor

### 2.1 What "Manifest-First" Means Concretely

After this refactor:
1. Editors (JuniorEditor, SeniorEditor) ALWAYS output a manifest structure for footage_edit jobs, never raw ffmpeg commands
2. The `ffmpeg` tool is removed from editor tool sets (they keep `inspect_media` and `suggest_clips`)
3. A new tool `write_manifest_scenes` writes structured scene data into `manifest.yaml`
4. Assembly reads from the manifest's `footage` section instead of ad-hoc variables

### 2.2 Changes Required

#### File 1: `packs/video/junior_editor.py`

**Change A — Force manifest mode for footage edits (non-indexed path)**

In `_build_prompt()` (line 93), the current logic is:
```python
if clip_index_data:
    # ... clip selection prompt (this path is fine, keep it)
elif job.get("output_mode") == "manifest":
    parts.append(self._manifest_prompt(job))
else:
    # ... tool_calls prompt (THIS IS THE PROBLEM)
```

Change the `else` branch: when `job.get("type") == "footage_edit"`, always use the manifest prompt. Only fall through to the tool_calls prompt for non-footage-edit jobs.

**Change B — Remove `ffmpeg` from tool set**

In `_get_tools()` (line 174):
```python
def _get_tools(self, job: dict) -> list:
    if job.get("clip_index_data"):
        return ["inspect_media", "suggest_clips", "ffmpeg"]  # REMOVE "ffmpeg"
    return []
```

Remove `"ffmpeg"` from the return list. Keep `inspect_media` and `suggest_clips`.

#### File 2: `packs/video/senior_editor.py`

Mirror the same two changes:

**Change A — Force manifest mode** in `_build_prompt()` (line 96): same logic as JuniorEditor.

**Change B — Remove `ffmpeg` from tool set** in `_get_tools()` (line 179): remove `"ffmpeg"`.

#### File 3: `packs/video/tools.py`

**Add `write_manifest_scenes` tool** — a new function that takes a manifest path and a list of scene dicts, validates them, and writes/merges them into the manifest YAML.

```
def write_manifest_scenes(manifest_path: str, scenes: list[dict]) -> dict:
    """
    Write structured scene data into the manifest's footage section.
    Each scene: {index, type, source, start_s, end_s, rotate?, description?}
    Merges with existing manifest (preserves brief block).
    """
```

In TEST_MODE, still write the real YAML (so tests can verify manifest structure).

Add `"write_manifest_scenes": write_manifest_scenes` to `TOOL_REGISTRY`.

Add the signature to `tool_signatures()`.

#### File 4: `core/head_of_production.py`

**Change A — Set `output_mode = "manifest"` for footage_edit jobs**

In `_route()`, the `footage_edit` branch (starts around line 919), before calling JuniorEditor, add:
```python
job["output_mode"] = "manifest"
```

**Change B — After editor returns manifest, write it and assemble from it**

Currently (around line 990), after the editor returns, HoP extracts `selected_clips` and passes them directly to `assemble_clips()`. Change this:

1. If the editor result contains `output.manifest`, call `write_manifest_scenes()` to persist it
2. Read the manifest back and use it to drive assembly
3. If the editor result contains `output.selected_clips` (the indexed-clip path), keep the existing behavior

This is a fallback chain: manifest output > selected_clips output > fail.

**Change C — Write `footage` block to manifest after assembly**

This is partially done already. Ensure the manifest gets the footage block with clip paths, selections, and output path written back after assembly completes.

### 2.3 Manifest YAML Schema — `footage` Section

This is the new section editors write for footage_edit jobs:

```yaml
footage:
  config:
    resolution: "1920x1080"    # or "1080x1920" for vertical
    fps: 30

  source_clips:
    - path: /absolute/path/to/clip_01.MOV
      start_s: 0.0
      end_s: 12.5
      rotate: 0                # optional: 0, 90, 180, 270
      label: "intro"           # optional description

    - path: /absolute/path/to/clip_02.MOV
      start_s: 3.2
      end_s: 8.7
      label: "product shot"

  assembly_order: [0, 1]       # indices into source_clips
  transitions:                  # optional
    - type: crossfade
      duration_s: 0.5
      between: [0, 1]

  text_overlays:                # optional
    - text: "Title Card"
      position: center
      duration_s: 3.0

  output_path: null             # filled after assembly
```

No ffmpeg flags leak into the manifest. `assemble.py` (or a new `assemble-from-manifest.py`) owns the translation.

### 2.4 What Stays the Same

- Storyboard pipeline: untouched (already manifest-first)
- `script_brief` jobs: untouched (no assembly)
- `generate_stills` jobs: untouched
- Agent models, escalation thresholds: untouched
- `config/agents.yaml`: untouched
- TEST_MODE drill gates in all agents: untouched (they fire first and return stubs)
- `trust.py`, `budget.py`: untouched

---

## Section 3: TEST_MODE Design

### 3.1 Enabling TEST_MODE

Already exists. Set the environment variable:
```bash
export TEST_MODE=true
```

Or inline:
```bash
TEST_MODE=true python main.py --type footage_edit --content "test" --clips clip1.mp4 clip2.mp4
```

### 3.2 Mock Responses by Service (Already Implemented)

| Service | Real Mode | TEST_MODE |
|---------|-----------|-----------|
| LLM (Anthropic) | claude-sonnet-4-6 / haiku-4-5 via API | Agents return stubs immediately, zero API calls |
| Image Gen (Gemini) | `generate-still.py` calls Gemini API | PIL creates 1080x1920 gray/blue PNGs with scene labels |
| Voice Gen (ElevenLabs) | `generate-voiceover.py` calls ElevenLabs | Returns stub dict (`[DRILL] Generated voiceover`) |
| Assembly (ffmpeg) | `assemble.py` / `assemble-clips.py` | Creates real black MP4 via ffmpeg (so ffprobe works on it) |
| Clip Indexing (Whisper) | `index-clip.py` → Whisper transcription | Creates real `_meta/*.yaml` with synthetic clip data |
| Evaluation | LLM-based quality check | Returns stub evaluation with test-appropriate scoring |
| Plan Gate | LLM generates plan, user confirms | Returns stub plan, auto-confirms |

### 3.3 What's New for This Refactor

The existing TEST_MODE infrastructure is sufficient. No new mocks are needed. The key addition is **test assertions** that verify the pipeline produces correct manifest structures. See Section 4.

### 3.4 What the Tests Must Verify at Each Stage

1. **Job intake**: HoP receives a `footage_edit` job and generates a plan
2. **Plan confirmation**: plan is auto-confirmed in TEST_MODE
3. **Brief written to manifest**: `manifest.yaml` exists with a `brief` block
4. **Editor output**: editor returns `output.manifest` (not `output.tool_calls`)
5. **Manifest written**: `manifest.yaml` contains a `footage.source_clips` section
6. **Assembly**: output MP4 exists at the expected path with non-zero size
7. **Evaluation**: evaluator produces a result dict

---

## Section 4: Test Harness

### 4.1 Test File Location

Create: `~/Documents/GitHub/parallax/test/test_manifest_first.py`

### 4.2 Test Structure

Use the same pattern as `test/test_regression.py`:
- Force `TEST_MODE=true` via `os.environ`
- Patch `builtins.input` to auto-proceed
- Import `HeadOfProduction` directly
- Each test is a function that returns `(result, failures)` where `failures` is a list of strings
- A `main()` function runs all tests and reports pass/fail

### 4.3 Test Cases

```python
#!/usr/bin/env python3
"""
Manifest-first refactor tests.

Verifies that footage_edit jobs produce manifest YAML instead of ffmpeg commands,
and that the pipeline assembles from the manifest.

Usage:
    TEST_MODE=true python test/test_manifest_first.py
    TEST_MODE=true python test/test_manifest_first.py --repeat 3
"""

import os
import sys
import yaml
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["TEST_MODE"] = "true"
os.environ.setdefault("PARALLAX_LOG_DIR", str(Path(__file__).parent.parent / ".parallax"))

import builtins
builtins.input = lambda prompt="": ""
```

#### Test 1: `test_footage_edit_produces_manifest`

Submit a `footage_edit` job with synthetic clips. Verify:
- `result["status"] == "complete"`
- The manifest YAML file exists at `project_dir / manifest.yaml`
- The manifest contains a `brief` block
- The manifest contains a `footage` block with `source_clips` (or the editor returned `selected_clips` for the indexed path)
- The output MP4 exists and has size > 0

This test needs source clips. Since TEST_MODE creates synthetic `_meta/*.yaml` manifests, it needs real video files that ffprobe can read. Options:
- Use the existing YouTube test clips (if available — `test_scenarios.py` uses them)
- Create a tiny synthetic MP4 via ffmpeg in the test setup

Recommended: create a 2-second black video in `/tmp/parallax_test/` during test setup:
```bash
ffmpeg -y -f lavfi -i "color=black:size=1920x1080:duration=2:rate=30" -c:v libx264 /tmp/parallax_test/clip_01.mp4
```

#### Test 2: `test_editor_no_ffmpeg_tool`

Verify that JuniorEditor and SeniorEditor do NOT include `ffmpeg` in their tool set for footage_edit jobs:
- Instantiate JuniorEditor
- Call `_get_tools({"clip_index_data": [...]})` 
- Assert `"ffmpeg"` is not in the returned list

#### Test 3: `test_editor_manifest_output_mode`

Verify that JuniorEditor's `_build_prompt()` includes the manifest prompt (not tool_calls prompt) when `job["type"] == "footage_edit"`:
- Build a job dict with `type: "footage_edit"` and no `clip_index_data`
- Call `_build_prompt(job)`
- Assert the prompt contains "MANIFEST" and does NOT contain "tool_calls"

#### Test 4: `test_write_manifest_scenes_tool`

Verify the new `write_manifest_scenes` tool:
- Create a temp manifest with just a `brief` block
- Call `write_manifest_scenes(manifest_path, scenes=[...])`
- Read the manifest back
- Assert `brief` is preserved
- Assert `footage.source_clips` matches what was written

#### Test 5: `test_storyboard_pipeline_unchanged`

Verify the storyboard pipeline still works after refactor:
- Submit a `storyboard` job with `deliverable: "draft"`
- Assert `result["status"] == "complete"`
- Assert manifest has `scenes` (not `footage`)

This is a regression guard — the refactor must not break the storyboard path.

### 4.4 Running Tests

```bash
# Run all manifest-first tests
TEST_MODE=true python test/test_manifest_first.py

# Run with repeat for flakiness check
TEST_MODE=true python test/test_manifest_first.py --repeat 3

# Run existing regression suite to verify nothing broke
TEST_MODE=true python test/test_regression.py
```

---

## Section 5: Agent Execution Plan

### 5.1 Prerequisites

- Python 3.11+ with `anthropic`, `pyyaml`, `Pillow` installed
- ffmpeg available on PATH
- `~/.agents/skills/video-production/scripts/` exists with the pipeline scripts
- `ANTHROPIC_API_KEY` exported (only needed for non-TEST_MODE runs)

### 5.2 Step-by-Step Execution

Execute these steps in order. After each step, run the specified verification.

#### Step 1: Read and understand the current code

Read these files to build a mental model before changing anything:

1. `~/Documents/GitHub/parallax/packs/video/junior_editor.py` — focus on `_build_prompt()`, `_get_tools()`, `_manifest_prompt()`
2. `~/Documents/GitHub/parallax/packs/video/senior_editor.py` — same methods
3. `~/Documents/GitHub/parallax/packs/video/tools.py` — focus on `TOOL_REGISTRY`, `tool_signatures()`, `write_manifest_scenes` (does not exist yet)
4. `~/Documents/GitHub/parallax/core/head_of_production.py` — focus on `_route()` method, specifically the `footage_edit` branch (starts ~line 919)

#### Step 2: Modify JuniorEditor

File: `~/Documents/GitHub/parallax/packs/video/junior_editor.py`

**2a.** In `_build_prompt()`, change the else branch (around line 151) so that footage_edit jobs always get the manifest prompt:

Before:
```python
elif job.get("output_mode") == "manifest":
    parts.append(self._manifest_prompt(job))
else:
    # ... tool_calls prompt
```

After:
```python
elif job.get("output_mode") == "manifest" or job.get("type") == "footage_edit":
    parts.append(self._manifest_prompt(job))
else:
    # ... tool_calls prompt
```

**2b.** In `_get_tools()`, remove `"ffmpeg"` from the footage-edit tool set:

Before:
```python
return ["inspect_media", "suggest_clips", "ffmpeg"]
```

After:
```python
return ["inspect_media", "suggest_clips"]
```

**Verify:** `TEST_MODE=true python test/test_regression.py` — all existing tests should still pass (TEST_MODE agents return stubs before reaching the prompt logic).

#### Step 3: Modify SeniorEditor

File: `~/Documents/GitHub/parallax/packs/video/senior_editor.py`

Apply the exact same two changes as Step 2.

**Verify:** `TEST_MODE=true python test/test_regression.py` — still passes.

#### Step 4: Add `write_manifest_scenes` tool

File: `~/Documents/GitHub/parallax/packs/video/tools.py`

**4a.** Add the function after the existing `ffmpeg()` function (around line 677):

```python
def write_manifest_scenes(manifest_path: str, scenes: list[dict]) -> dict:
    """
    Write structured scene/footage data into the manifest.
    Merges with existing manifest (preserves brief, project, config blocks).

    Args:
        manifest_path: path to manifest.yaml
        scenes: list of scene dicts, each with:
            index (int), type (str: video|text_overlay|still|effect_overlay),
            source (str: absolute path), start_s (float), end_s (float),
            rotate (int, optional), description (str, optional),
            overlay_text (str, optional), estimated_duration_s (float, optional)
    """
    import yaml

    try:
        existing = {}
        if Path(manifest_path).exists():
            with open(manifest_path) as f:
                existing = yaml.safe_load(f) or {}

        # Build footage section from scenes
        source_clips = []
        for s in scenes:
            clip = {
                "path": s.get("source", ""),
                "start_s": s.get("start_s", 0.0),
                "end_s": s.get("end_s", 0.0),
            }
            if s.get("rotate"):
                clip["rotate"] = s["rotate"]
            if s.get("description"):
                clip["label"] = s["description"]
            if s.get("type") != "video":
                clip["type"] = s["type"]
            if s.get("overlay_text"):
                clip["overlay_text"] = s["overlay_text"]
            if s.get("estimated_duration_s"):
                clip["estimated_duration_s"] = s["estimated_duration_s"]
            source_clips.append(clip)

        existing["footage"] = {
            "source_clips": source_clips,
            "assembly_order": list(range(len(source_clips))),
        }

        # Preserve config if not set
        if "config" not in existing:
            existing["config"] = {"resolution": "1920x1080", "fps": 30}

        Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "w") as f:
            yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

        return {
            "success": True,
            "stdout": f"Wrote {len(source_clips)} scenes to {manifest_path}",
            "stderr": "",
            "tool": "write_manifest_scenes",
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"write_manifest_scenes failed: {e}",
            "tool": "write_manifest_scenes",
        }
```

**4b.** Add to `TOOL_REGISTRY` (around line 702):

```python
"write_manifest_scenes": write_manifest_scenes,
```

**4c.** Add to `tool_signatures()` return string:

```
write_manifest_scenes(manifest_path: str, scenes: list[dict]) — Write scene data to manifest. Each scene: {index, type, source, start_s, end_s, rotate?, description?}
```

**Verify:** `TEST_MODE=true python test/test_regression.py` — still passes (no existing code calls the new tool yet).

#### Step 5: Modify HoP's footage_edit routing

File: `~/Documents/GitHub/parallax/core/head_of_production.py`

**5a.** In `_route()`, footage_edit branch (around line 919), before calling JuniorEditor (around line 983), add:

```python
job["output_mode"] = "manifest"
```

**5b.** After the editor returns (around line 988), add manifest handling:

If `result.get("output", {}).get("manifest")` exists (editor wrote a manifest structure), call `write_manifest_scenes()` to persist it before assembly.

If the editor returned `selected_clips` instead (the indexed-clip path), keep the existing `assemble_clips()` flow unchanged.

**5c.** After assembly, write the `footage` block to the manifest (this is partially done — ensure it includes `output_path`).

**Verify:** `TEST_MODE=true python test/test_regression.py` — still passes.

#### Step 6: Write the test harness

File: `~/Documents/GitHub/parallax/test/test_manifest_first.py`

Write the test file as described in Section 4. Follow the pattern from `test/test_regression.py` exactly:
- Same import structure
- Same `os.environ` setup
- Same `builtins.input` patch
- Same `failures` list pattern
- Same `main()` runner with `--repeat` support

**Verify:** `TEST_MODE=true python test/test_manifest_first.py` — all tests pass.

#### Step 7: Run full regression

```bash
# New tests
TEST_MODE=true python test/test_manifest_first.py --repeat 3

# Existing regression suite
TEST_MODE=true python test/test_regression.py

# Existing scenario tests (requires reference files on disk)
TEST_MODE=true python test/test_scenarios.py
```

All three must pass.

#### Step 8: Real-mode smoke test (optional but recommended)

With `ANTHROPIC_API_KEY` exported (NOT in TEST_MODE):

```bash
python main.py --type footage_edit --content "Cut together a highlight reel from these clips" --clips /path/to/clip1.mp4 /path/to/clip2.mp4
```

Verify:
- HoP shows a plan and waits for confirmation
- Editor produces a manifest (check stdout for "manifest" references)
- Assembly runs and produces output
- No raw ffmpeg commands in editor output

### 5.3 Success Criteria

All of these must be true:

1. `TEST_MODE=true python test/test_manifest_first.py` passes with 0 failures
2. `TEST_MODE=true python test/test_regression.py` passes with 0 failures (regression guard)
3. JuniorEditor and SeniorEditor `_get_tools()` do NOT return `"ffmpeg"` for footage_edit jobs
4. JuniorEditor and SeniorEditor `_build_prompt()` use the manifest prompt (not tool_calls prompt) for footage_edit jobs
5. The `write_manifest_scenes` tool exists in `TOOL_REGISTRY` and works
6. A footage_edit job produces a `manifest.yaml` with a `footage.source_clips` section
7. The storyboard pipeline is unaffected (regression test passes)

### 5.4 Files Modified (Summary)

| File | What Changes |
|------|-------------|
| `packs/video/junior_editor.py` | `_build_prompt()` routes footage_edit to manifest prompt; `_get_tools()` removes ffmpeg |
| `packs/video/senior_editor.py` | Same two changes as JuniorEditor |
| `packs/video/tools.py` | New `write_manifest_scenes()` function + registry entry + signature |
| `core/head_of_production.py` | `_route()` sets `output_mode="manifest"` for footage_edit; handles manifest output from editor |
| `test/test_manifest_first.py` | NEW FILE — test harness for the refactor |

### 5.5 What NOT to Change

- Do not restructure `_route()` — it is 550+ lines but this refactor is not the time to break it apart
- Do not modify agent model assignments or escalation thresholds
- Do not change the storyboard pipeline path
- Do not modify the plan confirmation gate (already working)
- Do not modify `trust.py` or `budget.py`
- Do not create new scripts in `~/.agents/skills/video-production/scripts/` — the existing `assemble-clips.py` handles assembly; the manifest is consumed by Parallax, not by skill scripts directly
- Do not introduce new dependencies

### 5.6 Gotchas

1. **TEST_MODE agents return stubs before reaching prompt logic.** The drill gate fires at the very top of `execute()` / `plan()`. This means editor prompt changes (Steps 2-3) are invisible in TEST_MODE. The tests verify the tool set and prompt structure directly, not through the drill path.

2. **`assemble_clips()` takes `clip_manifests` (list of `_meta/*.yaml` paths), not a project manifest.** The footage_edit path uses `assemble_clips()`, not `assemble()`. Don't confuse them. `assemble()` is for storyboard/Ken Burns. `assemble_clips()` is for footage.

3. **The `_merge_selected_clips()` helper in HoP** handles per-file clip selections (e.g., `9657:3,9657:4`). Don't break this — it is needed for the indexed-clip path which coexists with the manifest path.

4. **`builtins.input` must be patched in tests.** Without the patch, the plan confirmation gate blocks on stdin.

5. **`PARALLAX_LOG_DIR` must be set** or HoP crashes trying to write run logs.

6. **The `clip_index_data` path (indexed clips) is separate from the manifest path.** When `clip_index_data` exists, the editor gets a different prompt (just select clip indices). The manifest-first change applies to the non-indexed path, OR you can decide to make ALL footage_edit paths go through manifest. The simpler approach: set `output_mode = "manifest"` only when there is NO `clip_index_data`. But the recommended approach (from the packaging spec) is to make ALL paths manifest-first eventually. Start with the non-indexed path to prove the pattern, then migrate the indexed path later.

   **Decision for this refactor:** Set `output_mode = "manifest"` unconditionally for footage_edit. The indexed-clip path already works fine (it returns `selected_clips`, which HoP handles). The `_build_prompt()` change preserves the indexed-clip prompt when `clip_index_data` exists (that `if` branch fires first). So setting `output_mode` is harmless for indexed clips — it only affects the fallback path.
