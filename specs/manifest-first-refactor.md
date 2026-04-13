# Manifest-First Refactor

## Layer 1: Overview

### What

This refactor restructures the Parallax agent pipeline so the manifest is the single source
of truth from job receipt through final assembly. Today, HoP orchestrates by passing dicts
between agents and wiring results together imperatively. After this change, HoP writes a
confirmed production plan into the manifest once — and every downstream step reads from and
writes to that manifest, not to in-memory dicts.

Three structural changes drive everything:

1. A plan confirmation gate sits between job intake and execution. HoP articulates its
   understanding of the job as a structured plan (deliverables, estimated scene count, cost
   estimate), shows it to the human, and only proceeds after confirmation. Trust/budget level
   determines whether that confirmation is required or auto-approved.

2. The two assembly paths (`assemble` for stills/Ken Burns, `assemble_clips` for footage
   edits) converge conceptually on the manifest. For `storyboard` jobs the manifest already
   drives `assemble`. For `footage_edit` jobs a new `footage` section in the manifest
   captures the clip selections, making the assembler read from the manifest rather than
   from ad-hoc variables in `_route`.

3. The evaluator stops reading raw `job["content"]` and instead reads HoP's articulated
   brief from `manifest["brief"]`. Every agent, including the evaluator, works from the
   same processed understanding of the job.

### Why

The current architecture has two failure modes that are already causing real pain. First,
when a job goes wrong it is hard to audit — the manifest tells you what was rendered but not
what was intended, because HoP's understanding of the job lives only in its prompt. Second,
HoP jumps straight from receiving a job to burning API budget on it, with no checkpoint
where a human can say "wait, that is not what I meant." The plan confirmation gate closes
the second failure mode. The manifest-as-brief closes the first.

The trust/budget system already exists and already gates decisions. This refactor wires that
system into the one place it was always needed: before any expensive work starts.

### Success Criteria

- A submitted job always produces a human-readable plan before execution begins.
- At trust score < 0.4, the plan requires explicit user approval before proceeding; at
  trust score >= 0.75, it auto-proceeds with a printed summary.
- The manifest for every completed job contains a `brief` section written by HoP, and the
  evaluator reads from that section rather than `job["content"]`.
- `storyboard` and `footage_edit` jobs both assemble from their respective manifest sections
  without separate code paths for what gets passed to the assembler.
- All existing TEST_MODE drill scenarios continue to pass without modification.

---

## Layer 2: Requirements & Implementation Strategy

### Functional Requirements

- After receiving a job and before calling any specialist agent, HoP generates a production
  plan containing: job type, its own one-sentence articulation of the deliverable, estimated
  scene count or clip count, estimated API cost range, and which pipeline phases will run.
- The plan is printed to stdout in a structured format the human producer can read quickly.
- At low trust (score < 0.4): HoP calls `input()` to wait for confirmation, same pattern
  used in `_human_clarification_gate`. User types Enter to proceed, 'abort' to cancel.
- At medium trust (0.4–0.75): HoP prints the plan and a 5-second countdown, then proceeds
  unless the user types 'abort'. This is aspirational — for MVP, medium trust behaves like
  low trust (requires Enter). Flag it in code with a TODO.
- At high trust (>= 0.75): HoP auto-proceeds and prints a one-line summary.
- After confirmation, HoP writes a `brief` block into the manifest YAML before calling any
  agent. For `footage_edit` jobs (which do not currently write a manifest), HoP creates one.
- Agents do not change. They receive the same job dicts they do today. The manifest `brief`
  is additional state written by HoP, not input consumed by agents.
- The evaluator reads `manifest["brief"]["articulated_intent"]` if present, falling back to
  `job["content"]` for backward compatibility.
- TEST_MODE: plan confirmation gate is skipped entirely (same as clarification gathering).

### Codebase Integration

**Files that change:**

- `core/head_of_production.py` — primary change
  surface. Adds `_generate_plan`, `_confirm_plan`, `_write_brief_to_manifest` methods.
  Modifies `receive_job` to call the plan/confirm flow before `_route`. Modifies `_route`'s
  `footage_edit` branch to initialize and write a manifest. Modifies `_route`'s final
  evaluation call to pass the manifest path alongside the job.

- `core/evaluator.py` — `_build_prompt` reads
  `job.get("manifest_brief")` (injected by HoP before calling evaluate) in preference to
  `job.get("content")`. No structural change to Evaluator class.

- `packs/video/tools.py` — no changes needed.
  Both `assemble` and `assemble_clips` already work fine. The convergence here is logical
  (manifest drives both), not a code merge.

**Files that do not change:**
- `ScriptWriter`, `StoryboardPlanner`, `JuniorEditor`, `SeniorEditor` — untouched.
- `trust.py`, `budget.py` — consumed by the plan gate but not modified.
- `paths.py` — no changes. `project_dir()` already creates the folder structure.
- `config/agents.yaml` — no changes.
- `test/test_scenarios.py` — no changes. TEST_MODE skips the gate.

### Refactors Needed

**Before implementing the plan gate:** The `storyboard` branch of `_route` currently
writes the manifest mid-pipeline (after StoryboardPlanner runs, at Phase 3). The `brief`
block needs to be written before any agent runs, which means HoP needs to create the
manifest file earlier — at confirmation time — and then StoryboardPlanner's scene data
gets appended to it later. This requires splitting the current manifest write in `_route`
into two steps: a `brief`-only write at confirmation, and a `scenes` merge after planning.

**For `footage_edit`:** This branch has no manifest at all today. Add a minimal manifest
write after the plan is confirmed. The manifest does not need to drive `assemble_clips`
immediately — the clip selection is still passed as arguments — but HoP should write the
clip paths and editor selections into `manifest["footage"]` after assembly completes, so
the run is auditable.

### Technical Debt Considerations

- The `_route` method is already 550+ lines with deeply nested if/elif branches. This
  refactor adds more logic to it. Do not try to restructure `_route` as part of this
  change — that is a separate refactor. Add the new methods as clean helpers and call them
  at the top of `receive_job`.

- The plan cost estimate will necessarily be rough (LLM token costs vary, image generation
  costs are per-call). Display it as a range ("estimated $0.50–$2.00") rather than a
  precise figure. Do not try to implement real cost prediction — HoP generates a ballpark
  estimate based on job type and scene count.

- The `input()` pattern for confirmations (already used in `_human_clarification_gate` and
  `_make_decision`) correctly handles `EOFError` for non-interactive mode. Reuse this
  pattern exactly. Do not introduce a new confirmation mechanism.

- Trust score is read from `TrustScore().score` at job receipt time. The same `trust`
  object is already instantiated in `receive_job`. Pass it through to `_confirm_plan`
  rather than constructing a second instance.

---

## Layer 3: Technical Specification

### New Manifest Schema — `brief` Block

The manifest YAML gains a top-level `brief` key written by HoP at confirmation time.
Existing keys (`project`, `config`, `resources`, `scenes`, `voiceover`) are unchanged.

```
brief:
  raw_input: str                  # job["content"] verbatim
  articulated_intent: str         # HoP's one-sentence restatement of the deliverable
  job_type: str                   # mirrors job["type"]
  deliverable: str                # e.g. "draft", "full", "footage_edit"
  estimated_scenes: int | null    # null for footage_edit until clip indexing
  estimated_cost_low_usd: float
  estimated_cost_high_usd: float
  pipeline_phases: list[str]      # e.g. ["script", "storyboard", "stills", "draft"]
  confirmed_at: str               # ISO timestamp
  trust_score_at_confirmation: float
  auto_confirmed: bool            # true if trust >= 0.75 or TEST_MODE
```

For `footage_edit` jobs, also add a `footage` block written after assembly completes:

```
footage:
  clips: list[str]                # source file paths
  clip_manifests: list[str]       # _meta/*.yaml paths
  selected_clips: str | null      # editor selection string, e.g. "0,2,4-6"
  output_path: str | null
```

### New Methods on `HeadOfProduction`

**`_generate_plan(job: dict) -> dict`**

Called from `receive_job` after `_gather_clarifications`, before `_route`.

In TEST_MODE: returns a stub plan immediately without any LLM call.

Otherwise: makes one LLM call (same `_llm_complete` pattern used throughout HoP, max_tokens
256) asking HoP to articulate the job as: intent sentence, job type, deliverable, estimated
scenes, pipeline phases. Returns a dict matching the `brief` schema (minus `confirmed_at`,
`trust_score_at_confirmation`, `auto_confirmed` — those are added at confirmation time).

Cost estimate is computed locally, not by LLM: look up a cost table keyed by job type
(`storyboard/draft` ~ $0.50–$1.50, `storyboard/full` ~ $1.00–$3.00, `footage_edit` ~
$0.20–$0.80). This is a hardcoded dict in `_generate_plan`, not a config file.

**`_confirm_plan(plan: dict, trust: TrustScore) -> bool`**

Called after `_generate_plan`. Returns True to proceed, False to abort.

Prints the plan in a human-readable block using the same `[HoP → You]` prefix convention.

Trust routing:
- `trust.score >= 0.75`: auto-confirms, prints one-line summary, returns True.
- `trust.score < 0.75`: prints full plan, calls `input("Proceed? (Enter / abort): ")`.
  Handles `EOFError`/`KeyboardInterrupt` by returning True (non-interactive mode proceeds).
  Returns False if user types 'abort' or 'n'. Returns True otherwise.

If `_confirm_plan` returns False, `receive_job` returns early with status "aborted" and
the plan dict, no agent calls made.

**`_write_brief_to_manifest(manifest_path: str, plan: dict, job: dict)`**

Called after `_confirm_plan` returns True, before any agent or tool is invoked.

For `storyboard` jobs: the manifest file may not exist yet (currently created at Phase 3).
This method creates it with only `project`, `config`, and `brief` populated. The rest of
the manifest (`resources`, `scenes`, `voiceover`) is filled in later by the existing Phase
3 code, which should be updated to open and merge into the existing file rather than
overwriting it.

For `footage_edit` jobs: creates the manifest with `project`, `config`, `brief`, and an
empty `footage` block.

For other job types (script_brief, generate_stills, etc.): this method is a no-op. Those
paths don't use a manifest file, and forcing one would add complexity with no payoff.

Writes YAML using the same `yaml.dump(..., default_flow_style=False, sort_keys=False)`
pattern already used in `_route`.

### Changes to `receive_job`

Insert after `_gather_clarifications` and before `_route`:

1. Call `_generate_plan(job)` → `plan`
2. Call `_confirm_plan(plan, trust)` → `confirmed`
3. If not confirmed: return `{"status": "aborted", "plan": plan, "concept_id": ..., "run_id": ...}`
4. Determine manifest path using the existing `project_dir(job["concept_id"])` pattern
   (same as what `_route` does for storyboard/footage_edit jobs)
5. If job type is `storyboard` or `footage_edit`: call `_write_brief_to_manifest(manifest_path, plan, job)`
6. Store `manifest_path` on `job` as `job["manifest_path"]` so `_route` can reference it
   without recomputing

### Changes to `_route` — `storyboard` Branch

Phase 3 (manifest write) currently does `yaml.dump(manifest_data, ...)` which overwrites
any existing file. Change to: read existing manifest if present, merge `resources` and
`scenes` into it (preserving `brief` written earlier), then write back. Use a simple
`existing = yaml.safe_load(...)` + `existing.update({...})` pattern.

No other changes to the storyboard branch.

### Changes to `_route` — `footage_edit` Branch

After assembly completes (currently line ~790), write the `footage` block into the manifest:

```
footage_data = {
    "clips": clips,                     # job["clips"]
    "clip_manifests": clip_manifests,
    "selected_clips": selected_clips,
    "output_path": asm_result.get("output_path"),
}
```

Open the existing manifest (written at confirmation), add/update the `footage` key,
write back. Same merge pattern as the storyboard branch.

### Changes to `Evaluator._build_prompt`

Replace:
```
f"Brief: {job.get('content', '')}",
```

With:
```
brief_text = job.get("manifest_brief") or job.get("content", "")
f"Brief: {brief_text}",
```

In `_route`, before calling `Evaluator().evaluate(job, result)`, inject:
```python
job["manifest_brief"] = plan.get("articulated_intent", job.get("content", ""))
```

This requires `plan` to be in scope at the bottom of `_route`. Pass it as a parameter to
`_route`, or store it on `self` as `self._current_plan` during the job lifecycle.

Recommendation: store as `self._current_plan` since `receive_job` and `_route` are on the
same instance and `_route` is always called synchronously within a single job.

### Error Handling

- `_generate_plan` LLM failure: catch exception, log warning, return a stub plan with
  `articulated_intent = job.get("content", "")[:120]` and default cost estimates. The gate
  still fires — a failed plan generation does not skip confirmation.

- `_write_brief_to_manifest` failure: log warning, do not raise. The run continues without
  a brief block in the manifest. Evaluator falls back to `job["content"]`.

- `_confirm_plan` `input()` raising: handled by `EOFError`/`KeyboardInterrupt` catch,
  returns True (proceeds). Same policy as `_human_clarification_gate`.

### TEST_MODE Behavior

`_generate_plan` in TEST_MODE returns immediately:
```python
{
    "raw_input": job.get("content", "")[:120],
    "articulated_intent": f"[DRILL] {job.get('type')} job",
    "job_type": job["type"],
    "deliverable": job.get("deliverable", "draft"),
    "estimated_scenes": 5,
    "estimated_cost_low_usd": 0.0,
    "estimated_cost_high_usd": 0.0,
    "pipeline_phases": ["drill"],
}
```

`_confirm_plan` in TEST_MODE: check `os.environ.get("TEST_MODE")` at top of method,
return True immediately without printing or calling `input()`. This preserves the existing
test behavior where `builtins.input = lambda: "skip"` is the only gate bypass.

### Detailed Success Criteria

- `test/test_scenarios.py` passes all assertions unchanged with TEST_MODE=true.
- In live mode (TEST_MODE not set), submitting a `storyboard/draft` job prints a plan block
  before any agent is invoked, and waits for user input when trust score < 0.75.
- After a completed `storyboard` run, the manifest YAML contains a `brief` key with a
  non-empty `articulated_intent` string.
- After a completed `footage_edit` run, the manifest YAML contains both `brief` and
  `footage` keys.
- The evaluator prompt contains HoP's articulated intent string, not the raw `job["content"]`
  string, for any run where plan generation succeeded.
- Aborting at the plan confirmation gate (typing 'abort') produces a return value with
  `status == "aborted"` and does not create any agent cost log entries.
- No existing tool signatures in `tools.py` are modified.
- The `_merge_selected_clips` helper and all other existing HoP methods remain unchanged.
