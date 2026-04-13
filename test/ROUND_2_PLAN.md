# Parallax Drill — Round 2 Test Plan

## Round 1 Analysis

### Scenario 1: Rugiet 15s Script Brief
- **Tested:** ScriptWriter routing, evaluation, PreWatchBrief generation, confidence threshold
- **Worked:** All checks passed. Script, evaluation, and brief all present. Confidence 0.9 exceeded 0.5 floor.
- **Blind spots:** No budget gate exercised. No cost tracking. No brand constraint validation (brand file provided but no check that constraints were respected). Review was auto-matched (predicted=actual), so trust calibration was never stressed.

### Scenario 2: Still Variations — Product Shots
- **Tested:** JuniorEditor routing for stills jobs
- **Worked:** Correct agent routed (junior_editor), evaluation present, output present.
- **Blind spots:** No SeniorEditor escalation tested. No low-confidence path. Budget gate absent. No check that 3 variations were actually produced.

### Scenario 3: Revision — Scene Regeneration
- **Tested:** JuniorEditor with edit instructions (revision job type)
- **Worked:** Output and evaluation present.
- **Blind spots:** No `prior_run_id` in the job — so PreWatchBrief diff logic (`_diff_from_prior`) was never exercised. The revision scenario doesn't actually chain to a prior run. Changes field always says "First iteration."

### Scenario 4: Script Brief — No Brand File
- **Tested:** Graceful handling of missing brand file
- **Worked:** Script and evaluation generated without error.
- **Blind spots:** No assertion that the system logged a warning about missing brand. No check that brand constraints were skipped vs. hallucinated.

### Scenario 5: B-Roll Edit Request
- **Tested:** JuniorEditor with broll_edit job type
- **Worked:** All checks passed. Trust increase proposal surfaced after 10 consecutive correct predictions.
- **Blind spots:** Trust proposal was surfaced but never acted on (no acceptance/rejection path tested).

### Top 3 Gaps

1. **BudgetGate is completely untested.** `budget.py` has per-decision caps, velocity caps, budget exhaustion, and escalation logic — none of it fires in any scenario. No `evaluate_options` or `record_spend` calls appear in drills.

2. **PreWatchBrief diff path is dead.** No scenario passes a `prior_run_id`, so `_diff_from_prior` never runs. The iteration chain — the core revision workflow — is untested.

3. **Review mismatch / trust degradation never happens.** Every review auto-matches the predicted rating (delta=0). The trust system is only tested on its happy path. No scenario forces a prediction miss, trust decrease, or blocking concern propagation.

---

## Round 2 Scenarios

### 1. Budget Exhaustion — Escalation on Overspend
**Description:** Pre-load a concept budget with $19.50 spent of $20.00 total, then submit a job whose cheapest option costs $1.00. The gate should refuse autonomous execution and escalate.

```python
{
    "name": "Budget Exhaustion — Escalation Required",
    "description": "Budget nearly depleted — verify escalation when cheapest option exceeds remaining",
    "job": {
        "type": "script_brief",
        "content": "Create a 15-second ad for a luxury watch brand. Cinematic tone.",
        "brand_file": None,
        "concept_id": "BUDGET-EXHAUST-001",
        "test_mode": True,
    },
    "setup": {
        "pre_spend": 19.50,
        "total_budget": 20.00,
    },
    "expected": {
        "budget_escalated": True,
        "autonomous": False,
        "reason_contains": "Budget exhausted",
    },
}
```

### 2. Velocity Cap — Rapid Spend in Short Window
**Description:** Record 15 small spends ($0.80 each = $12.00) within a 30-minute window against a $10.00 velocity cap. Next decision should be paused.

```python
{
    "name": "Velocity Cap — Rapid Fire Pause",
    "description": "Many cheap calls in a short window breach velocity cap",
    "job": {
        "type": "still_variations",
        "content": "Generate product shot variations. Quick iterations.",
        "brand_file": None,
        "concept_id": "VELOCITY-001",
        "test_mode": True,
    },
    "setup": {
        "pre_spends": [{"cost": 0.80, "action": f"gen_variation_{i}"} for i in range(15)],
        "velocity_cap": 10.0,
        "velocity_window_minutes": 30,
    },
    "expected": {
        "velocity_ok": False,
        "autonomous": False,
        "reason_contains": "Velocity cap hit",
    },
}
```

### 3. JuniorEditor to SeniorEditor Escalation
**Description:** Submit a complex revision with forced low confidence (mock agent returns confidence < 0.5). Verify the system escalates from JuniorEditor to SeniorEditor.

```python
{
    "name": "Junior → Senior Escalation on Low Confidence",
    "description": "Force low confidence to trigger SeniorEditor escalation",
    "job": {
        "type": "revision",
        "content": "Complete creative overhaul — new color grade, re-edit all 6 scenes, change pacing from slow to fast cut. This is a major rework.",
        "brand_file": "brands/rugiet.yaml",
        "concept_id": "ESCALATE-001",
        "test_mode": True,
        "force_low_confidence": True,
    },
    "expected": {
        "agent_used": "senior_editor",
        "confidence_below": 0.5,
        "escalation_logged": True,
    },
}
```

### 4. Iteration Chain — Brief Then Revise with prior_run_id
**Description:** Run a script brief, capture its run_id, then submit a revision job referencing that run_id as `prior_run_id`. Verify the second run's PreWatchBrief contains actual diff content (not "First iteration").

```python
{
    "name": "Iteration Chain — Brief Then Revise",
    "description": "Two-step chain: generate script, then revise referencing prior run_id",
    "job_sequence": [
        {
            "type": "script_brief",
            "content": "Create a 15-second ad for a fitness app. Energetic tone.",
            "brand_file": None,
            "concept_id": "CHAIN-001",
            "test_mode": True,
        },
        {
            "type": "revision",
            "content": "The hook is too generic. Make it more specific — reference a real pain point about gym intimidation.",
            "brand_file": None,
            "concept_id": "CHAIN-001",
            "prior_run_id": "{{RUN_1_ID}}",
            "test_mode": True,
        },
    ],
    "expected": {
        "iteration_2_changes_not_contains": "First iteration",
        "iteration_2_brief_present": True,
        "iteration_number": 2,
    },
}
```

### 5. PreWatchBrief Diff — Verify Change Detection
**Description:** Run two iterations where the second has a different agent, different confidence, and different eval score. Verify `_diff_from_prior` detects all three changes.

```python
{
    "name": "PreWatchBrief Diff — Multi-Field Change Detection",
    "description": "Verify diff detects agent change, confidence change, and eval score change",
    "job_sequence": [
        {
            "type": "script_brief",
            "content": "Create a 30-second ad for a meal kit service.",
            "brand_file": None,
            "concept_id": "DIFF-001",
            "test_mode": True,
            "mock_result": {"agent": "junior_editor", "confidence": 0.6, "evaluation": {"score": 0.65}},
        },
        {
            "type": "revision",
            "content": "Major revision — escalated to senior.",
            "brand_file": None,
            "concept_id": "DIFF-001",
            "prior_run_id": "{{RUN_1_ID}}",
            "test_mode": True,
            "mock_result": {"agent": "senior_editor", "confidence": 0.9, "evaluation": {"score": 0.92}},
        },
    ],
    "expected": {
        "changes_contain": ["Routing changed", "Confidence", "Eval score"],
    },
}
```

### 6. Review Rating Mismatch — Trust Degradation
**Description:** Run a scenario where PreWatchBrief predicts rating 8, but human gives rating 4 (delta of -4). Verify the prediction is marked incorrect and trust does not increase.

```python
{
    "name": "Review Rating Mismatch — Trust Hit",
    "description": "Human rates far below prediction — verify trust records miss",
    "job": {
        "type": "script_brief",
        "content": "Create a 15-second ad for a pet food brand. Playful tone.",
        "brand_file": None,
        "concept_id": "MISMATCH-001",
        "test_mode": True,
    },
    "review_override": {
        "rating": 4,
    },
    "expected": {
        "prediction_correct": False,
        "delta_abs_gte": 3,
        "trust_increase_proposal": None,
    },
}
```

### 7. Concern Propagation — Blocking Concern Halts Pipeline
**Description:** Inject a blocking concern (severity 1.0, blocking=True) into the ConcernBus before evaluation. Verify the PreWatchBrief surfaces it as [BLOCKING] and the predicted rating drops accordingly.

```python
{
    "name": "Blocking Concern — Pipeline Halt",
    "description": "Blocking concern injected — verify it surfaces and drags predicted rating",
    "job": {
        "type": "script_brief",
        "content": "Create a 15-second ad for a supplement brand.",
        "brand_file": None,
        "concept_id": "BLOCKING-001",
        "test_mode": True,
    },
    "injected_concerns": [
        {"severity": 1.0, "blocking": True, "message": "Script contains unsubstantiated health claim — FDA risk"},
    ],
    "expected": {
        "concerns_summary_contains": "[BLOCKING]",
        "predicted_rating_lte": 6,
        "has_pre_watch_brief": True,
    },
}
```

### 8. Brand Constraint Violation Detection
**Description:** Provide a brand file with explicit constraints (e.g., "never mention competitors by name") and a brief that deliberately violates them. Verify the evaluator or concern system flags the violation.

```python
{
    "name": "Brand Constraint Violation",
    "description": "Brief violates brand constraint — verify detection",
    "job": {
        "type": "script_brief",
        "content": "Create a 15-second ad for Rugiet Ready. Compare directly to Viagra and Cialis by name, saying Rugiet is better than both.",
        "brand_file": "brands/rugiet.yaml",
        "concept_id": "BRAND-VIOLATE-001",
        "test_mode": True,
    },
    "expected": {
        "concern_raised": True,
        "concern_contains": "competitor",
    },
}
```

### 9. Per-Decision Cap Exceeded — Single Expensive Action
**Description:** Present a decision where all options exceed the per-decision cap ($2.00). The cheapest option costs $3.50. Verify the gate escalates even though budget has plenty remaining.

```python
{
    "name": "Per-Decision Cap — All Options Expensive",
    "description": "Every option exceeds per-decision cap — must escalate regardless of budget",
    "job": {
        "type": "revision",
        "content": "Full creative reshoot — regenerate all scenes with new style.",
        "brand_file": None,
        "concept_id": "PERCAP-001",
        "test_mode": True,
    },
    "setup": {
        "total_budget": 50.0,
        "per_decision_cap": 2.0,
        "options": [
            {"name": "regenerate_all_basic", "estimated_cost": 3.50, "rework_cost": 7.00},
            {"name": "regenerate_all_premium", "estimated_cost": 8.00, "rework_cost": 16.00},
        ],
    },
    "expected": {
        "autonomous": False,
        "reason_contains": "exceeds per-decision cap",
        "budget_remaining_gte": 40.0,
    },
}
```

### 10. Mixed Job Sequence — Full Pipeline Stress
**Description:** Run 4 jobs in sequence on the same concept_id: script_brief, still_variations, revision (with prior_run_id), broll_edit. Verify budget accumulates correctly across all 4, PreWatchBrief iteration count increments, and the final trust state reflects all 4 reviews.

```python
{
    "name": "Mixed Job Sequence — Full Pipeline",
    "description": "Script → stills → revision → b-roll on one concept — end-to-end",
    "job_sequence": [
        {
            "type": "script_brief",
            "content": "Create a 15-second ad for a sleep supplement. Calm, ASMR-adjacent.",
            "brand_file": None,
            "concept_id": "MIXED-001",
            "test_mode": True,
        },
        {
            "type": "still_variations",
            "content": "Generate 3 product shots for the sleep supplement. Dark blue tones, moonlit.",
            "brand_file": None,
            "concept_id": "MIXED-001",
            "test_mode": True,
        },
        {
            "type": "revision",
            "content": "Scene 2 is too bright — darken it and slow the pacing.",
            "brand_file": None,
            "concept_id": "MIXED-001",
            "prior_run_id": "{{RUN_1_ID}}",
            "test_mode": True,
        },
        {
            "type": "broll_edit",
            "content": "Cut 8 seconds of b-roll: pillow fluff, moonlight through window, clock face.",
            "brand_file": None,
            "concept_id": "MIXED-001",
            "test_mode": True,
        },
    ],
    "expected": {
        "total_jobs_completed": 4,
        "budget_spent_gt": 0,
        "final_iteration_gte": 4,
        "trust_predictions_gte": 4,
    },
}
```
