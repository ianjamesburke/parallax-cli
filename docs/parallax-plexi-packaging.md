# Parallax Plexi Packaging Spec

How to decompose Parallax from a monolithic Python project into three Plexi-compatible layers: an app, a set of agents, and a tool suite.

## 1. Overview

Parallax is currently a standalone Python CLI that orchestrates multiple agents for video production. To ship as a Plexi app, it needs to split into three layers:

1. **The App** — a chat UI that lives in `~/.plexi-alpha/apps/parallax/`. Owns the conversation loop, manifest management, and Plexi SDK integration.
2. **The Agents** — system prompts, configs, memory, and versioning that live in `.plexi/agents/parallax/` per project directory. Portable, project-specific, improvable over time.
3. **The Tools** — pipeline scripts (still generation, assembly, captions, TTS, Ken Burns, etc.) that live in `~/.plexi-alpha/tools/parallax/`. Stateless, invoked by agents via argparse CLI.

## 2. Current Architecture

| Current Location | What It Is | Target |
|---|---|---|
| `core/head_of_production.py` | HoP orchestrator (routing, planning, confirmation gates) | Agent config: `.plexi/agents/parallax/orchestrator/system.md` + app logic in `parallax_app.py` |
| `core/llm.py` | LLM abstraction (API + CLI fallback) | Keep as-is, but get API key from Plexi `SecretGet` instead of env var |
| `core/agent_loop.py` | Tool-calling loop (`run_with_tools`) | Keep as-is — this is the execution engine |
| `core/budget.py` | Budget gate (per-job, per-concept caps) | Integrate with Plexi `cost_report` events |
| `core/cost_tracker.py` | Cost logging | Replace with `cost_report` events to Plexi |
| `packs/video/script_writer.py` | ScriptWriter agent | Extract system prompt to `.plexi/agents/parallax/script-writer/system.md` |
| `packs/video/storyboard_planner.py` | StoryboardPlanner | Extract system prompt to `.plexi/agents/parallax/storyboard-planner/system.md` |
| `packs/video/junior_editor.py` | JuniorEditor (tool-calling) | Extract system prompt + tool list |
| `packs/video/senior_editor.py` | SeniorEditor (escalated) | Extract system prompt + tool list |
| `packs/video/asset_generator.py` | Image/audio/video generation | Extract system prompt + service configs |
| `packs/video/evaluator.py` | Quality evaluation | Extract system prompt + criteria to `.plexi/agents/parallax/evaluator/criteria.yaml` |
| `packs/video/tools.py` | Tool registry (30+ subprocess wrappers) | Move scripts to `~/.plexi-alpha/tools/parallax/` |
| `config/agents.yaml` | Agent model assignments + thresholds | Move to `.plexi/agents/parallax/config.yaml` |
| `brands/*.yaml` | Brand compliance configs | Stay in project directory (user data) |
| `main.py` | CLI entry point | Replace with `parallax_app.py` (Plexi app) |

## 3. The Manifest-First Refactor

This is the highest-priority change. Editors currently plan individual ffmpeg tool calls, which fails ~25% of the time for footage edits. The fix: editors write structured YAML to the manifest, and a single assembly script reads and executes it.

### Current flow (broken for footage_edit)

```
User brief -> HoP -> JuniorEditor -> [tool_call: ffmpeg --trim] -> [tool_call: ffmpeg --concat] -> output
```

### Target flow

```
User brief -> HoP -> JuniorEditor -> writes scenes/clips to manifest.yaml -> assemble.py reads manifest -> output
```

### What changes

- JuniorEditor and SeniorEditor system prompts are rewritten to output YAML scene blocks instead of ffmpeg commands.
- The `ffmpeg` tool is removed from editor tool sets. Editors keep `inspect_media` and `suggest_clips`.
- A new tool `write_manifest_scenes` takes structured scene data and writes it to `manifest.yaml`.
- `assemble.py` becomes the only thing that calls ffmpeg for assembly.
- The manifest is always the source of truth — editors propose, the manifest records, the pipeline executes.

### Manifest schema for footage edits (new `footage` section)

```yaml
footage:
  source_clips:
    - path: input/clip_01.mp4
      start_s: 0.0
      end_s: 12.5
      label: "intro"
    - path: input/clip_02.mp4
      start_s: 3.2
      end_s: 8.7
      label: "product shot"
  assembly_order: [0, 1]
  transitions:
    - type: crossfade
      duration_s: 0.5
      between: [0, 1]
```

`assembly_order` is an array of indices into `source_clips`. `transitions` reference clip indices. All timing values are in seconds. No ffmpeg flags leak into the manifest — `assemble.py` owns the translation from this schema to ffmpeg commands.

## 4. Agent Extraction

Each agent becomes a directory with standalone files instead of Python classes with embedded prompts.

### Target structure

```
.plexi/agents/parallax/
  config.yaml                    # global agent config (model assignments, thresholds)
  orchestrator/
    system.md                    # HoP system prompt (extracted from hop.py)
    memory/                      # accumulated learnings (starts empty)
    references/
      video-production-patterns.md
      cost-estimation-table.md
  script-writer/
    system.md
    memory/
    references/
      script-structure-guide.md
  storyboard-planner/
    system.md
    memory/
    references/
      scene-composition-guide.md
  junior-editor/
    system.md
    tools.yaml                   # available tools for this agent
    memory/
  senior-editor/
    system.md
    tools.yaml
    memory/
  evaluator/
    system.md
    criteria.yaml                # scoring rubrics
    memory/
  improvement-officer/
    system.md
    memory/
```

### config.yaml (global agent config)

```yaml
agents:
  orchestrator:
    model: claude-sonnet-4-6
    trust: 0.5
    auto_approve_threshold: 0.88
  script-writer:
    model: claude-sonnet-4-6
    trust: 0.5
    escalation_threshold: 0.60
  junior-editor:
    model: claude-haiku-4-5
    trust: 0.5
    escalation_threshold: 0.70
  senior-editor:
    model: claude-sonnet-4-6
    trust: 0.5
    escalation_threshold: 0.85
  evaluator:
    model: claude-sonnet-4-6
    trust: 0.5
  improvement-officer:
    model: claude-opus-4-6
    trust: 0.5
    # runs offline only -- reviews logs, proposes agent improvements

cost_alerts:
  per_job_usd: 2.00
  per_concept_usd: 10.00

defaults:
  resolution: 1080x1920
  fps: 30
```

Trust scores are continuous floats (0.0-1.0), starting at 0.5. They adjust over time based on evaluator feedback from approved runs. An agent whose trust exceeds its `auto_approve_threshold` can proceed without confirmation gates.

### System prompt extraction

For each agent, the system prompt is extracted verbatim from the Python class into a `system.md` file. The Python class then reads the file at init:

```python
# Before: prompt embedded in Python
class ScriptWriter:
    SYSTEM_PROMPT = """You are a script writer for short-form video..."""

# After: prompt loaded from file
class ScriptWriter:
    def __init__(self, agent_dir):
        self.system_prompt = Path(agent_dir / "script-writer/system.md").read_text()
```

No prompt content changes in this step — extraction only. Prompt improvements happen in Phase 4 under versioning.

### tools.yaml (per-agent tool availability)

```yaml
# junior-editor/tools.yaml
tools:
  - inspect_media
  - suggest_clips
  - write_manifest_scenes
  - generate_still
  - generate_tts
  # ffmpeg is NOT listed -- editors write manifests, not ffmpeg commands
```

The agent runtime only injects tools listed in this file. This is the enforcement mechanism for the manifest-first pattern.

## 5. Tool Packaging

Pipeline scripts become standalone tools installed at the system level.

### Target location

`~/.plexi-alpha/tools/parallax/`

### Tool descriptor format

Each tool is a Python script with an argparse CLI (most already have this) plus a `tool.yaml` descriptor:

```yaml
name: generate-still
description: Generate a still image from a scene description using Gemini
requires_secrets: ["GOOGLE_API_KEY"]
input_schema:
  manifest_path: {type: string, required: true}
  scene: {type: integer, required: false}
  variants: {type: integer, required: false, default: 1}
```

Agents reference tools by name. The agent runtime resolves the tool name to the script path via a registry built from all `tool.yaml` files at startup. It builds the subprocess call from the `input_schema`.

### Secret injection

Tools that need API keys declare them in `requires_secrets`. The app resolves these via `SecretGet` at startup and passes them as environment variables to the subprocess. Tools never read secrets directly — they receive them from the runtime.

### Tool list (current scripts to package)

Each script in `tools/` and the wrapper functions in `packs/video/tools.py` becomes a standalone tool. Key ones:

- `generate-still` — Gemini image generation
- `generate-tts` — ElevenLabs TTS
- `assemble` — ffmpeg assembly from manifest
- `burn-captions` — subtitle overlay
- `generate-caption` — whisper transcription + SRT
- `ken-burns` — zoom/pan animation
- `inspect-media` — ffprobe wrapper
- `suggest-clips` — clip selection helper
- `write-manifest-scenes` — structured scene data to manifest.yaml (new)
- `trim-silence` — silence detection and trimming
- `generate-headline` — headline overlay

## 6. LLM Integration with Plexi Secrets

### Current

`core/llm.py` reads `ANTHROPIC_API_KEY` from environment variable.

### Target

`core/llm.py` gets the API key from Plexi's secrets manager on startup:

```python
# At app init, resolve secrets
api_key = emit.secret_get("ANTHROPIC_API_KEY")
os.environ["ANTHROPIC_API_KEY"] = api_key
```

The fallback chain stays: API key -> Claude CLI. The key source changes from `.env` file to Plexi secrets manager with directory walk-up resolution.

### manifest.toml secrets declaration

```toml
[app]
name = "parallax"
version = "0.1.0"

[secrets]
ANTHROPIC_API_KEY = { required = true, description = "Claude API access" }
GOOGLE_API_KEY = { required = true, description = "Gemini image generation" }
ELEVENLABS_API_KEY = { required = false, description = "TTS voice synthesis" }
```

## 7. Cost Integration

### Current

`core/cost_tracker.py` logs to `/logs/{concept_id}_{run_id}.jsonl`

### Target

In addition to local logging, emit `cost_report` events to Plexi via stdout draw commands:

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
  "directory": "/Users/ian/projects/brand",
  "timestamp": "2026-04-11T14:30:25Z"
}
```

Plexi logs these to `~/.plexi-alpha/costs.jsonl`. The app's budget gate reads from both the local log and Plexi's cost ledger to enforce caps.

### Integration point

Wrap the existing `log_cost()` call in `core/cost_tracker.py` to also emit the Plexi event:

```python
def log_cost(self, cost_data):
    # Existing: write to local JSONL
    self._write_local(cost_data)
    # New: emit to Plexi
    emit.cost_report(
        service=cost_data["service"],
        cost_usd=cost_data["cost_usd"],
        agent=cost_data["agent"],
        # ... rest of fields
    )
```

## 8. Agent Versioning

Each agent directory supports versioning via a `versions/` subdirectory.

### Structure

```
.plexi/agents/parallax/script-writer/
  versions/
    v1/
      system.md
      memory/
    v2/
      system.md               # refined prompt
      memory/                  # compressed memory
  current -> v2/               # symlink to active version
  test-cases/
    case-001/
      input/
        brief.md
      expected/
        output_script.md
        cost_usd: 0.03
        tool_calls: 2
      runs/
        v1_run_001.json
        v2_run_001.json
```

### Test case capture

Test cases are captured automatically from approved production runs. When a user approves an agent's output, the system saves the input brief, the output, the cost, and the tool call count as a test case.

### Regression testing

The improvement officer runs regressions when proposing version changes:

1. It proposes a new `system.md` (version N+1).
2. It runs all existing test cases against the new version.
3. It compares output quality (via evaluator), cost, and tool call count.
4. If regressions are detected, the version is rejected with a report.
5. If all tests pass or improve, the version is proposed for human approval.

No version goes live without passing regressions. The `current` symlink only updates after approval.

## 9. Packaging for Release

### What ships with Plexi (system-level)

1. **The Parallax app** -> `~/.plexi-alpha/apps/parallax/` (installed via app update system)
2. **The pipeline tools** -> `~/.plexi-alpha/tools/parallax/` (installed with the app)
3. **Agent templates** -> bundled in the app, copied to `.plexi/agents/parallax/` on first use in a project directory

### What lives in the project directory (user data)

1. **Agent configs + memory** -> `.plexi/agents/parallax/` (copied from template, accumulates project-specific learning)
2. **Brand files** -> `brand.yaml` in project root
3. **Project manifest** -> `manifest.yaml`
4. **Assets** -> `input/`, `stills/`, `audio/`, `output/`
5. **Cost logs** -> `.plexi/parallax/costs.jsonl`
6. **Conversation history** -> `.plexi/parallax/conversations/`

### Update model

- **App code + tools:** updated via Plexi's app update system (GitHub release or registry).
- **Agent templates:** updated with the app, but existing project agents are NOT overwritten (they have project-specific memory).
- **Template merge:** users can opt-in to merge template improvements into existing agents. The merge applies to `system.md` only — `memory/` is never touched. This works like a git merge: if the user has not modified the system prompt, the new template replaces it cleanly. If they have, the diff is shown for manual resolution.

### agents.md (programmatic interaction)

The app includes an `agents.md` file that describes how other Plexi apps can interact with Parallax:

```markdown
# Parallax Agents

## produce-video
Takes a brief and produces a short-form video.
Input: A text brief describing the video concept, target audience, and brand.
Output: A rendered video file path and cost summary.

## edit-footage
Takes raw footage and an edit brief.
Input: Paths to source clips + edit instructions.
Output: Assembled video file path.

## evaluate-video
Takes a video and evaluates quality.
Input: Video file path + evaluation criteria.
Output: Score (0-100) + detailed feedback.
```

## 10. Migration Path

### Phase 1: Manifest-first refactor

**Goal:** Fix the 25% footage edit failure rate.

- Rewrite JuniorEditor and SeniorEditor system prompts to output YAML scene blocks.
- Implement `write_manifest_scenes` tool.
- Remove `ffmpeg` from editor tool sets.
- Add `footage` section to manifest schema.
- Update `assemble.py` to read the `footage` section.
- Run existing test suite + new footage_edit regression tests.

**Done when:** editors never emit raw ffmpeg commands; all assembly goes through the manifest.

### Phase 2: Agent extraction

**Goal:** Make agents portable and file-based.

- Extract each agent's system prompt from Python code to `system.md`.
- Create `config.yaml` from `agents.yaml`.
- Create `tools.yaml` for each agent that uses tools.
- Create `criteria.yaml` for the evaluator.
- Modify `core/agent_loop.py` to read system prompts from files instead of Python strings.
- Create `tool.yaml` descriptors for each pipeline script.

**Done when:** deleting embedded prompt strings from Python code causes no behavior change.

### Phase 3: Plexi integration

**Goal:** Make Parallax a Plexi app.

- Write `parallax_app.py` (chat UI using `plexi_sdk.py`).
- Write `manifest.toml` with secrets declarations.
- Add `SecretGet` calls for API keys.
- Add `cost_report` emission to cost tracker.
- Write `agents.md` for programmatic interaction.
- Create the project template (copied to new project directories on init).
- Package tools into `~/.plexi-alpha/tools/parallax/`.

**Done when:** `parallax_app.py` can be installed into `~/.plexi-alpha/apps/` and run a full video production job using Plexi secrets and cost reporting.

### Phase 4: Polish and release

**Goal:** Production-ready agent management.

- Agent versioning support (versions/, symlinks, test-cases/).
- Test case auto-capture from approved runs.
- Improvement officer integration (offline regression testing, version proposals).
- App update system integration.
- Template merge tooling for existing project agents.

**Done when:** the improvement officer can propose, test, and deploy a prompt version change end-to-end.
