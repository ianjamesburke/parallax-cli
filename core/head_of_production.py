"""
HeadOfProduction — orchestrator agent for the NRTV video agent network.
Routes jobs, manages concept IDs, owns the human check-in gate.

v1: Implemented as direct API calls (not registered beta agents).
v2: Migrate to client.beta.agents for persistent state + multi-agent delegation.
"""

import json
import os
import re
import signal
import uuid
import random
import string
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import anthropic

from core.trust import TrustScore
from core.budget import BudgetGate, DecisionOption
from core.run_log import RunLogger

def _llm_complete(model, system, prompt, max_tokens=1024):
    """Lazy LLM call — uses best available backend."""
    from core.llm import complete
    return complete(model=model, system=system, prompt=prompt, max_tokens=max_tokens)


class HeadOfProduction:
    """
    Orchestrator agent. Routes jobs, manages concept IDs, owns human check-in gate.

    v1: Direct API calls — stateless between jobs, state lives in log files.
    v2: Register via client.beta.agents for persistent sessions.
    """

    MODEL = "claude-sonnet-4-6"

    SYSTEM = """You are the Head of Production at NRTV, a video production agency.
You receive job briefs and orchestrate a team of specialist agents to produce video content.

Your responsibilities:
1. Understand what the client wants
2. Identify what information is missing or ambiguous
3. Route to the right specialists
4. Maintain quality through the Evaluator
5. Always check in with the human producer before sending anything to the client

You communicate clearly and concisely. When routing internally you are decisive.
When surfacing to the human you explain your reasoning briefly."""

    def __init__(self, skip_health_check: bool = False):
        self.run_id = str(uuid.uuid4())[:8]
        self._current_concept_id = ""
        self._current_plan = None
        if not skip_health_check:
            self._health_check()

    def generate_concept_id(self) -> str:
        """Generate a new concept ID in ABC-001 format (3 uppercase letters + hyphen + 3 digits)."""
        letters = "".join(random.choices(string.ascii_uppercase, k=3))
        number = random.randint(1, 999)
        return f"{letters}-{number:03d}"

    def receive_job(self, job: dict) -> dict:
        """
        Main entry point for the agent network.

        Args:
            job: {
                type: str,          # script_brief | still_variations | revision | voice_memo | broll_edit
                content: str|dict,  # brief text or file path
                brand_file: str,    # path to brand YAML (optional)
                concept_id: str,    # existing concept ID (optional — generated if absent)
                test_mode: bool,    # propagated to all agents
            }

        Returns:
            {status, result or questions, concept_id, run_id}
        """
        # Assign concept ID
        if not job.get("concept_id"):
            job["concept_id"] = self.generate_concept_id()
        self._current_concept_id = job["concept_id"]

        # Inject run_id for downstream cost tracking
        job["run_id"] = self.run_id

        # Initialize budget gate + prediction tracker + run logger
        self._budget = BudgetGate(job["concept_id"])
        trust = TrustScore()  # kept for prediction accuracy tracking, not gating
        budget_snap = self._budget.snapshot()
        run_logger = RunLogger(
            run_id=self.run_id,
            concept_id=job["concept_id"],
            job=job,
            trust_snapshot={**trust.snapshot(), "budget": budget_snap},
            pack="video",
            test_mode=job.get("test_mode", False),
        )

        # Log job (legacy job.json kept for backward compat)
        from core.paths import run_dir as get_run_dir
        run_dir = get_run_dir(self.run_id)
        try:
            with open(run_dir / "job.json", "w") as f:
                json.dump(
                    {**job, "run_id": self.run_id, "started_at": datetime.now(timezone.utc).isoformat()},
                    f,
                    indent=2,
                )
        except Exception as e:
            raise RuntimeError(f"[HoP] Failed to log job: {e}") from e

        print(f"\n[HoP] Job received. Concept: {job['concept_id']} | Run: {self.run_id}")
        print(f"[HoP] Budget: ${budget_snap['remaining']:.2f} remaining (cap: ${budget_snap['per_decision_cap']:.2f}/action)")
        print("[HoP] Checking with the team on this — will be back with any questions.\n")

        # Install interrupt handler — marks run as interrupted if killed mid-flight
        _prev_handler = signal.getsignal(signal.SIGINT)
        def _on_interrupt(signum, frame):
            print("\n[HoP] Interrupted — marking run as interrupted")
            run_logger.complete(status="interrupted", notes="SIGINT received")
            # Restore and re-raise so the process actually exits
            signal.signal(signal.SIGINT, _prev_handler)
            raise KeyboardInterrupt
        signal.signal(signal.SIGINT, _on_interrupt)

        # Gather clarifying questions from specialist perspective
        # Skipped when caller asserts the brief is fully specified (e.g. `parallax run --yes`).
        if job.get("skip_clarifications") or os.environ.get("PARALLAX_SKIP_CLARIFICATIONS", "").lower() == "true":
            print("[HoP] skip_clarifications set — proceeding without clarification gate")
            questions = []
        else:
            questions = self._gather_clarifications(job)

        if questions:
            # Make a cost-gated decision about whether to ask
            decision = self._make_decision(
                situation="Client job requires clarification — should I ask or proceed?",
                options=["ask_client", "proceed_with_defaults", "escalate_to_senior"],
                run_logger=run_logger,
                trust=trust,
                context={"num_questions": len(questions), "job_type": job.get("type")},
                cost_options=[
                    DecisionOption("ask_client", 0.0, 0.0, "Free — but delays the job"),
                    DecisionOption("proceed_with_defaults", 0.50, 2.00, "Risk rework if assumptions wrong"),
                    DecisionOption("escalate_to_senior", 0.10, 0.10, "Quick senior sanity check"),
                ],
            )
            if decision == "ask_client":
                approved_questions = self._human_clarification_gate(questions)
                if approved_questions:
                    print("\n[HoP → Client] Clarification questions:")
                    for q in approved_questions:
                        print(f"  • {q['question']}")
                    run_logger.complete(
                        output={"questions": approved_questions},
                        status="awaiting_clarification",
                    )
                    return {
                        "status": "awaiting_clarification",
                        "questions": approved_questions,
                        "concept_id": job["concept_id"],
                        "run_id": self.run_id,
                    }
            # Otherwise fall through and proceed

        # ── Plan confirmation gate ──────────────────────────────────────
        plan = self._generate_plan(job)
        self._current_plan = plan

        # Store trust info for brief metadata
        self._plan_trust_score = trust.score
        self._plan_auto_confirmed = trust.score >= 0.75

        if not self._confirm_plan(plan, trust, job):
            run_logger.complete(status="aborted", notes="User aborted at plan confirmation")
            signal.signal(signal.SIGINT, _prev_handler)
            return {
                "status": "aborted",
                "plan": plan,
                "concept_id": job["concept_id"],
                "run_id": self.run_id,
            }

        # Write brief to manifest for storyboard/footage_edit jobs
        if job.get("type") in ("storyboard", "footage_edit"):
            from core.paths import project_dir as get_project_dir
            work_dir = get_project_dir(job["concept_id"])
            manifest_path = str(work_dir / "manifest.yaml")
            self._write_brief_to_manifest(manifest_path, plan, job)
            job["manifest_path"] = manifest_path

        # Route to specialist and evaluate
        result = self._route(job)

        # Finalize run log
        try:
            from core.cost_tracker import cost_report
            cost = cost_report(job["concept_id"])
        except Exception:
            cost = {}
        run_logger.complete(output=result, cost_summary=cost)

        # Restore original signal handler
        signal.signal(signal.SIGINT, _prev_handler)

        # Generate pre-watch brief for human review
        try:
            from core.pre_watch_brief import PreWatchBrief
            pwb = PreWatchBrief(self.run_id, job["concept_id"])
            concerns = result.get("evaluation", {}).get("concerns", [])
            brief = pwb.generate(result, concerns, prior_run_id=job.get("prior_run_id"))
            result["pre_watch_brief"] = brief
        except Exception as e:
            print(f"[HoP] WARNING: pre-watch brief failed: {e}")

        return result

    def _gather_clarifications(self, job: dict) -> list:
        """
        Ask a specialist-perspective model what's missing or ambiguous in the brief.
        Returns questions with importance >= 0.6.
        """
        import os
        if os.environ.get("TEST_MODE", "false").lower() == "true":
            return []  # No clarifications in drill mode

        prompt = (
            f"Job brief:\n"
            f"Type: {job['type']}\n"
            f"Content: {job.get('content', '')}\n"
            f"Brand: {job.get('brand_file', 'not specified')}\n\n"
            "What information, if any, is missing or ambiguous that would prevent you from "
            "doing this job well?\n"
            "For each question, rate 0.0–1.0 how important it is to ask "
            "(1.0 = cannot proceed without this).\n"
            'Return JSON: [{"question": str, "importance": float, "reason": str}]\n'
            "Return empty array if no clarifications needed."
        )

        try:
            response = _llm_complete(
                model=self.MODEL,
                system="You are a specialist video production agent reviewing a job brief for clarity.",
                prompt=prompt,
                max_tokens=1024,
            )
            text = response["text"]
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                questions = json.loads(match.group())
                return [q for q in questions if q.get("importance", 0) >= 0.6]
        except Exception as e:
            print(f"[HoP] WARNING: clarification gathering failed: {e}")
        return []

    def _human_clarification_gate(self, questions: list) -> list:
        """
        Present curated questions to the human producer for review before client sends.
        Returns the approved subset.
        """
        print("\n[HoP → You] Before I reach out to the client, review these clarification questions:")
        for i, q in enumerate(questions, 1):
            print(f"  {i}. [{q['importance']:.0%} important] {q['question']}")
            print(f"     Why: {q.get('reason', '')}")

        print("\nApprove all (press Enter), remove numbers (e.g. '2,3'), or 'skip' to proceed without asking:")
        try:
            response = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Non-interactive mode (e.g. tests) — skip all questions
            return []

        if response == "skip":
            return []
        elif response == "":
            return questions
        else:
            try:
                remove = set(int(x.strip()) for x in response.split(","))
                return [q for i, q in enumerate(questions, 1) if i not in remove]
            except Exception:
                return questions

    # ── Plan confirmation gate ────────────────────────────────────────────────

    # Hardcoded cost estimate table (job_type → (low, high) USD)
    _COST_TABLE = {
        "script_brief": (0.01, 0.05),
        "storyboard/stills_only": (0.30, 1.00),
        "storyboard/draft": (0.50, 1.50),
        "storyboard/full": (1.00, 3.00),
        "footage_edit": (0.20, 0.80),
        "generate_stills": (0.10, 0.50),
        "lipsync_ad": (0.30, 1.00),
    }
    _DEFAULT_COST = (0.10, 1.00)

    # Pipeline phases by job type
    _PIPELINE_PHASES = {
        "script_brief": ["script", "evaluate"],
        "storyboard/stills_only": ["script", "storyboard", "stills", "evaluate"],
        "storyboard/draft": ["script", "storyboard", "stills", "draft_assembly", "evaluate"],
        "storyboard/full": ["script", "storyboard", "stills", "voiceover", "assembly", "captions", "evaluate"],
        "footage_edit": ["index_clips", "editor_review", "assembly", "overlay_burn", "evaluate"],
        "generate_stills": ["generate", "evaluate"],
    }

    def _generate_plan(self, job: dict) -> dict:
        """
        Generate a production plan for the job. In TEST_MODE, returns a stub
        immediately without any LLM call. Otherwise, makes one LLM call to
        articulate the job, then computes cost from a hardcoded lookup table.
        """
        import os
        test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"

        if test_mode:
            return {
                "raw_input": str(job.get("content", ""))[:120],
                "articulated_intent": f"[DRILL] {job.get('type')} job",
                "job_type": job.get("type", "unknown"),
                "deliverable": job.get("deliverable", "draft"),
                "estimated_scenes": 5,
                "estimated_cost_low_usd": 0.0,
                "estimated_cost_high_usd": 0.0,
                "pipeline_phases": ["drill"],
            }

        # Compute cost lookup key
        job_type = job.get("type", "unknown")
        deliverable = job.get("deliverable", "")
        if job_type == "storyboard" and deliverable:
            cost_key = f"storyboard/{deliverable}"
        else:
            cost_key = job_type
        low, high = self._COST_TABLE.get(cost_key, self._DEFAULT_COST)
        phases = self._PIPELINE_PHASES.get(cost_key, ["execute", "evaluate"])

        # LLM call to articulate the job
        try:
            response = _llm_complete(
                model=self.MODEL,
                system="You are the Head of Production. Restate the job in one sentence.",
                prompt=(
                    f"Job type: {job_type}\n"
                    f"Deliverable: {deliverable}\n"
                    f"Content: {str(job.get('content', ''))[:500]}\n\n"
                    "Restate this job as a single clear sentence describing what will be produced."
                ),
                max_tokens=256,
            )
            articulated = response["text"].strip()
        except Exception as e:
            print(f"[HoP] WARNING: plan articulation LLM call failed: {e}")
            articulated = str(job.get("content", ""))[:120]

        # Extract brand name from brand_file for display in plan
        brand_name = None
        brand_file = job.get("brand_file")
        if brand_file:
            try:
                import yaml as _yaml
                brand_data = _yaml.safe_load(open(brand_file))
                brand_name = (
                    brand_data.get("brand", {}).get("name")
                    or brand_data.get("name")
                    or Path(brand_file).stem
                )
            except Exception:
                brand_name = Path(brand_file).stem if brand_file else None

        return {
            "raw_input": str(job.get("content", ""))[:500],
            "articulated_intent": articulated,
            "job_type": job_type,
            "deliverable": deliverable or job_type,
            "estimated_scenes": job.get("reference_scene_count"),
            "estimated_cost_low_usd": low,
            "estimated_cost_high_usd": high,
            "pipeline_phases": phases,
            "brand_name": brand_name,
            "brand_file": brand_file,
        }

    def _confirm_plan(self, plan: dict, trust: "TrustScore", job: dict) -> bool:
        """
        Present the production plan and get confirmation.
        Also prompts for brand if not already set (when trust < 0.75).
        Returns True to proceed, False to abort.
        In TEST_MODE, returns True immediately without printing or calling input().
        """
        import os
        if os.environ.get("TEST_MODE", "false").lower() == "true":
            return True

        score = trust.score

        if score >= 0.75:
            brand_suffix = f" | brand: {plan['brand_name']}" if plan.get("brand_name") else ""
            print(f'[HoP] Auto-proceeding (trust: {score:.0%}): {plan["articulated_intent"]}{brand_suffix}')
            return True

        # Print full plan for human review
        scenes_display = plan.get("estimated_scenes") or "TBD"
        low = plan.get("estimated_cost_low_usd", 0)
        high = plan.get("estimated_cost_high_usd", 0)
        brand_display = plan.get("brand_name") or "not set"
        print(f"\n[HoP \u2192 You] Production Plan")
        print(f"  Job:         {plan.get('job_type', 'unknown')}")
        print(f"  Intent:      {plan.get('articulated_intent', '')}")
        print(f"  Deliverable: {plan.get('deliverable', '')}")
        print(f"  Brand:       {brand_display}")
        print(f"  Scenes:      {scenes_display}")
        print(f"  Est. cost:   ${low:.2f}\u2013${high:.2f}")
        pipeline_str = " \u2192 ".join(plan.get("pipeline_phases", []))
        print(f"  Pipeline:    {pipeline_str}")

        # Prompt for brand if not set
        if not plan.get("brand_file"):
            try:
                brand_input = input("Brand file? (path to brand YAML, or Enter to skip): ").strip()
                if brand_input:
                    job["brand_file"] = brand_input
                    plan["brand_file"] = brand_input
                    # Try to extract brand name for confirmation display
                    try:
                        import yaml as _yaml
                        brand_data = _yaml.safe_load(open(brand_input))
                        plan["brand_name"] = (
                            brand_data.get("brand", {}).get("name")
                            or brand_data.get("name")
                            or Path(brand_input).stem
                        )
                    except Exception:
                        plan["brand_name"] = Path(brand_input).stem
                    print(f"  [HoP] Brand set: {plan['brand_name']}")
            except (EOFError, KeyboardInterrupt):
                pass

        # TODO: medium trust (0.4-0.75) should use a 5-second countdown.
        # For MVP, medium trust behaves like low trust (requires Enter).
        try:
            response = input("Proceed? (Enter / abort): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return True

        if response in ("abort", "n"):
            return False
        return True

    def _write_brief_to_manifest(self, manifest_path: str, plan: dict, job: dict):
        """
        Write the brief block into the manifest YAML before any agent runs.
        Creates the manifest with project, config, and brief blocks.
        """
        import yaml

        brief_data = {
            **plan,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "trust_score_at_confirmation": getattr(self, "_plan_trust_score", 0.0),
            "auto_confirmed": getattr(self, "_plan_auto_confirmed", False),
        }

        manifest = {
            "project": {
                "id": job["concept_id"],
                "slug": job["concept_id"],
                "version": "0.0.1",
            },
            "config": {"resolution": "1080x1920", "fps": 30},
            "brief": brief_data,
        }

        try:
            Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
            with open(manifest_path, "w") as f:
                yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            print(f"[HoP] WARNING: could not write brief to manifest: {e}")

    def _make_decision(self, situation: str, options: list[str],
                       run_logger: "RunLogger", trust: "TrustScore",
                       context: dict = None,
                       cost_options: list[DecisionOption] | None = None) -> str:
        """
        HoP makes a judgment call. Gates on COST, not trust.

        Always:
        1. Predicts what user would pick (logged for improvement)
        2. Evaluates cost of each option against budget
        3. If max_loss < per_decision_cap and budget available → proceed autonomously
        4. Otherwise → surface options with cost context to human
        5. Always logs the full decision for audit

        Returns the chosen option string.
        """
        import os
        test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"

        # Step 1: predict internally
        if test_mode:
            predicted = options[0]
        else:
            prompt = f"""You are the Head of Production making an internal decision.

Situation: {situation}
Options: {options}
Context: {json.dumps(context or {})}

Which option would the user most likely want? Reply with ONLY the option string, exactly as written."""
            try:
                response = _llm_complete(
                    model=self.MODEL, system="", prompt=prompt, max_tokens=64,
                )
                predicted_raw = response["text"].strip()
                predicted = min(options, key=lambda o: 0 if o.lower() in predicted_raw.lower() else 1)
            except Exception as e:
                print(f"[HoP] WARNING: prediction LLM call failed: {e}")
                predicted = options[0]

        # Log prediction for accuracy tracking (learning signal, not a gate)
        pred = trust.predict(
            run_id=self.run_id,
            concept_id=self._current_concept_id,
            situation=situation,
            options=options,
            llm_prediction=predicted,
        )

        # Step 2: cost-gated autonomy
        if cost_options:
            evaluation = self._budget.evaluate_options(cost_options)
            autonomous = evaluation["autonomous"]
        else:
            # No cost data → default to escalate (safe)
            autonomous = False
            evaluation = {"reason": "No cost estimates provided — escalating by default",
                          "max_loss": 0, "budget_remaining": self._budget.remaining,
                          "options_with_cost": []}

        if autonomous:
            actual = predicted
            resolution_method = "autonomous_cost_gated"
            print(f"[HoP] Autonomous: {predicted} — {evaluation['reason']}")
        else:
            # Surface to human with cost context
            print(f"\n[HoP] Decision needed — {situation}")
            print(f"  Reason: {evaluation['reason']}")
            print(f"  Budget: ${self._budget.remaining:.2f} remaining")
            for i, opt in enumerate(options, 1):
                marker = " ← recommended" if opt == predicted else ""
                # Find cost info if available
                cost_info = ""
                for co in evaluation.get("options_with_cost", []):
                    if co["name"] == opt:
                        cost_info = f" (${co['estimated_cost']:.2f}, rework: ${co['rework_cost']:.2f})"
                        break
                print(f"  {i}. {opt}{cost_info}{marker}")
            print(f"  {len(options) + 1}. escalate — surface everything to human producer")

            try:
                raw = input("Your choice (number or text): ").strip()
            except (EOFError, KeyboardInterrupt):
                raw = ""
            try:
                choice_idx = int(raw) - 1
                if choice_idx == len(options):
                    actual = "escalate"
                else:
                    actual = options[choice_idx]
            except (ValueError, IndexError):
                actual = raw if raw in options else predicted
            resolution_method = "user_selected"

        # Step 3: record prediction outcome
        correct = trust.record_outcome(pred.prediction_id, actual)

        # Step 4: log the decision with cost context
        run_logger.log_decision(
            situation=situation, options=options,
            prediction=predicted, prediction_id=pred.prediction_id,
            actual=actual, correct=correct,
            autonomy_level=f"cost_gated (cap=${self._budget.budget.per_decision_cap:.2f})",
            trust_score=trust.score,
            resolution_method=resolution_method,
        )

        return actual

    def _route(self, job: dict) -> dict:
        """
        Route the job to the correct specialist agent based on job type.
        footage_edit jobs go directly to SeniorEditor (JuniorEditor is dead code, kept for future experiments).
        Always runs through Evaluator before surfacing.
        """
        from packs.video.script_writer import ScriptWriter
        from packs.video.junior_editor import JuniorEditor
        from packs.video.senior_editor import SeniorEditor
        from packs.video.storyboard_planner import StoryboardPlanner
        from core.evaluator import Evaluator

        job_type = job.get("type")
        result = {}

        try:
            if job_type == "script_brief":
                result = ScriptWriter().write(job)

            elif job_type == "storyboard":
                # Deliverable controls how far down the pipeline we go:
                #   scripts_only  → ScriptWriter only, return script + scenes text
                #   stills_only   → Script + StoryboardPlanner + generate stills, no video
                #   draft         → + assemble Ken Burns draft
                #   full          → + VO + captions + final assembly
                import subprocess as _sp
                import yaml as _yaml
                from packs.video.tools import (generate_still, assemble, generate_voiceover,
                                               align_scenes, burn_captions, generate_lipsync)
                from core.paths import project_dir as get_project_dir

                deliverable = job.get("deliverable", "stills_only")
                work_dir = get_project_dir(job["concept_id"])
                assets_dir = work_dir / "assets"
                audio_dir = assets_dir / "audio"
                output_dir = work_dir / "output"
                for _d in [assets_dir, audio_dir, output_dir]:
                    _d.mkdir(parents=True, exist_ok=True)
                manifest_path = str(work_dir / "manifest.yaml")
                vo_manifest_path = None

                # Phase 0a: Reference video analysis — count shots to mirror structure
                audio_source = job.get("audio_source")
                reference_video = job.get("reference_video") or (
                    audio_source if audio_source and Path(audio_source).suffix.lower()
                    in (".mov", ".mp4", ".m4v", ".avi", ".mkv") else None
                )
                if reference_video and not job.get("reference_scene_count"):
                    print(f"[HoP] Analyzing reference video structure: {Path(reference_video).name}")
                    # ffmpeg scene detection — threshold 0.3 catches most real cuts
                    scene_r = _sp.run(
                        ["ffprobe", "-v", "quiet", "-print_format", "json",
                         "-show_frames", "-select_streams", "v",
                         "-skip_frame", "noref",
                         "-read_intervals", "%+#9999",
                         "-f", "lavfi",
                         f"movie={reference_video},select=gt(scene\\,0.3)",
                         "-show_entries", "frame=pkt_pts_time"],
                        capture_output=True, text=True, timeout=30,
                    )
                    # Simpler approach: use ffmpeg scene filter and count lines
                    scene_r2 = _sp.run(
                        ["ffmpeg", "-i", reference_video,
                         "-vf", "select='gt(scene,0.25)',showinfo",
                         "-f", "null", "-"],
                        capture_output=True, text=True, timeout=30,
                    )
                    # Count "pts_time" occurrences in stderr (each = a detected cut)
                    cut_times = []
                    for line in scene_r2.stderr.splitlines():
                        if "pts_time:" in line and "showinfo" in line:
                            try:
                                t = float(line.split("pts_time:")[1].split()[0])
                                cut_times.append(t)
                            except Exception:
                                pass
                    ref_scene_count = max(1, len(cut_times) + 1)  # cuts + 1 = scenes
                    job["reference_scene_count"] = ref_scene_count
                    job["reference_cut_times"] = cut_times
                    print(f"[HoP] Reference has ~{ref_scene_count} scenes "
                          f"({len(cut_times)} detected cuts)")

                # Phase 0b: Recycled VO — extract audio + transcribe
                if audio_source:
                    vo_wav = audio_dir / "voiceover.wav"
                    if not vo_wav.exists():
                        print(f"[HoP] Extracting VO from: {Path(audio_source).name}")
                        extract = _sp.run(
                            ["ffmpeg", "-i", audio_source, "-vn", "-acodec", "pcm_s16le",
                             "-ar", "16000", "-ac", "1", "-y", str(vo_wav)],
                            capture_output=True,
                        )
                        if extract.returncode != 0:
                            raise RuntimeError(f"[HoP] Audio extraction failed: {extract.stderr.decode()[:200]}")
                    else:
                        print(f"[HoP] Cached VO audio: {vo_wav.name}")

                    vo_manifest_path = str(audio_dir / "vo_manifest.json")
                    if not Path(vo_manifest_path).exists():
                        print("[HoP] Transcribing VO...")
                        ls_result = generate_lipsync(
                            audio=str(vo_wav),
                            output=str(audio_dir / "lipsync.json"),
                            vo_manifest=vo_manifest_path,
                        )
                        if not ls_result["success"]:
                            raise RuntimeError(f"[HoP] Transcription failed: {ls_result.get('stderr','')[:200]}")
                    else:
                        print("[HoP] Cached VO transcript")

                    with open(vo_manifest_path) as _f:
                        vo_data = json.load(_f)
                    words = vo_data.get("words", [])

                    # Split transcript into sentences
                    sentences, current = [], []
                    for w in words:
                        current.append(w["word"])
                        if w["word"].rstrip().endswith((".", "!", "?")):
                            sentences.append(" ".join(current))
                            current = []
                    if current:
                        sentences.append(" ".join(current))

                    print(f"[HoP] Recycled VO: {len(sentences)} sentences, "
                          f"{len(words)} words")

                    # Auto-trim WAV to last spoken word + 0.5s tail.
                    # Prevents 47s of silence from inflating the assembled video.
                    if words:
                        speech_end = words[-1]["end"] + 0.5
                        trimmed_wav = audio_dir / "voiceover_trimmed.wav"
                        if not trimmed_wav.exists():
                            _sp.run(
                                ["ffmpeg", "-i", str(vo_wav), "-t", str(speech_end),
                                 "-y", str(trimmed_wav)],
                                capture_output=True, check=True,
                            )
                            print(f"[HoP] Trimmed VO to {speech_end:.1f}s "
                                  f"(was {vo_data.get('duration', '?')}s)")
                        # Use trimmed version for assembly
                        vo_wav = trimmed_wav

                    # Inject as script so StoryboardPlanner gets real VO lines
                    job["script"] = {"script": " ".join(sentences), "vo_lines": sentences}

                # Phase 1: Script (skip if recycled VO already injected)
                script_data = job.get("script")
                if not script_data:
                    print("[HoP] Generating script...")
                    script_data = ScriptWriter().write(job)
                    job["script"] = script_data

                if deliverable == "scripts_only":
                    result = script_data
                    result["deliverable"] = "scripts_only"
                else:
                    # Phase 2: Storyboard planning
                    # Inject character description so StoryboardPlanner writes consistent prompts
                    char_ref = job.get("character_ref") or job.get("ref_image")
                    if char_ref and not job.get("character"):
                        # Derive character description from the brief — ref image is passed
                        # separately to generate-still.py for visual grounding
                        job["character"] = (
                            "A visual reference image is provided for this character. "
                            "Based on the job brief and any character details mentioned, "
                            "describe the character's appearance in each scene's starting_frame "
                            "with consistent specific details: clothing color, facial features, "
                            "body type, art style. Every scene MUST describe the same character."
                        )

                    result = StoryboardPlanner().plan(job, run_id=self.run_id)
                    result["script"] = script_data.get("script", "")
                    result["vo_lines"] = script_data.get("vo_lines", [])
                    result["deliverable"] = deliverable

                    # Phase 3: Write manifest from planned scenes
                    scenes = result.get("scenes", [])
                    resources_supplied = []
                    # Script resource (required for character-ad format)
                    resources_supplied.append({
                        "type": "script",
                        "content": script_data.get("script", job.get("content", "")),
                    })
                    # Character reference (required for character-ad format)
                    ref_image = job.get("ref_image") or job.get("character_ref")
                    if ref_image:
                        resources_supplied.append({
                            "type": "character_reference",
                            "path": str(ref_image),
                        })
                    else:
                        resources_supplied.append({
                            "type": "character_reference",
                            "content": job.get("character", "No character reference provided"),
                        })

                    # Read existing manifest (brief was written at confirmation)
                    existing_manifest = {}
                    if Path(manifest_path).exists():
                        with open(manifest_path) as _f:
                            existing_manifest = _yaml.safe_load(_f) or {}

                    # Merge new data, preserving brief
                    manifest_data = {
                        **existing_manifest,
                        "project": {
                            "id": job["concept_id"],
                            "slug": job["concept_id"],
                            "version": "0.0.1",
                            "format": "character-ad",
                        },
                        "config": {"resolution": "1080x1920", "fps": 30},
                        "resources": {"supplied": resources_supplied},
                        "scenes": scenes,
                    }
                    if vo_manifest_path:
                        manifest_data["vo_manifest"] = vo_manifest_path
                        # "voiceover.audio_file" is the key assemble.py reads.
                        # Absolute path overrides assemble.py's project_dir/audio/ prefix.
                        # Use trimmed WAV so assembled duration matches actual speech.
                        _vo_audio = audio_dir / "voiceover_trimmed.wav"
                        if not _vo_audio.exists():
                            _vo_audio = audio_dir / "voiceover.wav"
                        manifest_data["voiceover"] = {
                            "source": "recycled",
                            "audio_file": str(_vo_audio),
                        }
                    with open(manifest_path, "w") as _f:
                        _yaml.dump(manifest_data, _f, default_flow_style=False,
                                   sort_keys=False, allow_unicode=True)
                    print(f"[HoP] Manifest: {manifest_path} ({len(scenes)} scenes)")

                    # Phase 4: Generate stills
                    ref_image = job.get("ref_image") or job.get("character_ref")
                    if scenes:
                        print(f"[HoP] Generating stills for {len(scenes)} scenes...")
                        still_result = generate_still(
                            manifest_path, scene=f"1-{len(scenes)}",
                            chain=True, ref_image=ref_image,
                        )
                        result["stills"] = still_result

                    # Phase 5: Ken Burns draft
                    if deliverable in ("draft", "full"):
                        if vo_manifest_path:
                            print("[HoP] Aligning scenes to VO timing...")
                            align_result = align_scenes(manifest_path)
                            result["align"] = align_result
                        print("[HoP] Assembling Ken Burns draft...")
                        draft_result = assemble(manifest_path, draft=True)
                        # Inject output_path if not set (_run() doesn't populate it)
                        if draft_result.get("success") and not draft_result.get("output_path"):
                            # Find the output file — assemble.py auto-names it
                            output_candidates = list(output_dir.glob("*.mp4"))
                            if output_candidates:
                                draft_result["output_path"] = str(
                                    max(output_candidates, key=lambda p: p.stat().st_mtime)
                                )
                        result["draft"] = draft_result

                    # Phase 6: ElevenLabs VO + captions (full, no recycled audio)
                    if deliverable == "full" and not audio_source:
                        print("[HoP] Generating voiceover...")
                        vo_result = generate_voiceover(manifest_path)
                        result["voiceover"] = vo_result
                        if vo_result.get("success"):
                            align_scenes(manifest_path)
                            final_result = assemble(manifest_path)
                            result["assembly"] = final_result
                            output_path = final_result.get("output_path", "")
                            if output_path and final_result.get("success"):
                                caption_result = burn_captions(manifest_path, video=output_path)
                                result["captions"] = caption_result

            elif job_type == "generate_stills":
                # Lightweight: just generate N images from a prompt + optional ref
                # No manifest, no scenes, no pipeline — just images back
                from packs.video.tools import generate_still, generate_char_ref
                from core.paths import run_dir as get_run_dir
                count = job.get("count", 5)
                ref_image = job.get("ref_image")
                char_ref = job.get("character_ref")

                if char_ref and not ref_image:
                    # Generate a character ref sheet first, then use it
                    print("[HoP] Generating character reference sheet...")
                    ref_result = generate_char_ref(
                        input_images=[char_ref],
                        output_path=str(get_run_dir(self.run_id) / "char_ref.png"),
                        describe=job.get("character", ""),
                    )
                    result = {"char_ref": ref_result}
                    if ref_result.get("success"):
                        ref_image = ref_result.get("output_path")

                # If we have a manifest, use generate_still with variants
                manifest_path = job.get("manifest_path")
                if manifest_path:
                    print(f"[HoP] Generating {count} still variations...")
                    still_result = generate_still(
                        manifest_path, scene="1", variants=count, ref_image=ref_image,
                    )
                    result = {**result, "stills": still_result, "count": count, "agent": "tools"}
                else:
                    # No manifest — just pass through to asset generator or tools
                    print(f"[HoP] Generating {count} variations (no manifest)...")
                    result = {
                        **result,
                        "stills": {"success": True, "count": count,
                                   "prompt": job.get("content", ""),
                                   "ref_image": ref_image},
                        "count": count,
                        "agent": "tools",
                    }

            elif job_type in ("still_variations", "revision", "broll_edit"):
                jr = JuniorEditor().execute(job, run_id=self.run_id)
                if jr.get("confidence", 1.0) < 0.70:
                    print(
                        f"[HoP] Junior Editor confidence {jr['confidence']:.0%} — escalating to Senior Editor."
                    )
                    result = SeniorEditor().execute(
                        job, junior_notes=jr.get("notes"), run_id=self.run_id
                    )
                else:
                    result = jr

            elif job_type == "footage_edit":
                # Proper footage pipeline:
                #   1. index-clip.py → clip-index manifests with real silence/cut data (cached)
                #   2. Editor makes creative decisions (which clips to keep, order, effects)
                #   3. assemble-clips.py renders from real timecodes in one pass
                from packs.video.tools import index_clip, inspect_media, assemble_clips, suggest_clips
                from core.paths import project_dir as get_project_dir
                import yaml

                clips = job.get("clips", [])
                if not clips:
                    raise RuntimeError("[HoP] footage_edit requires 'clips' list of file paths")

                work_dir = get_project_dir(job["concept_id"])
                output_dir = work_dir / "output"
                output_dir.mkdir(parents=True, exist_ok=True)
                job["work_dir"] = str(work_dir)
                print(f"[HoP] Project: {work_dir}")

                # Phase 1: Index all clips (index-clip.py handles caching internally)
                # NOTE: we resolve symlinks upfront. index-clip.py internally does
                # Path(input).resolve() and writes its manifest to the *real* target's
                # _meta/ dir. If we kept the symlink path here, HoP would look in the
                # symlink's parent and never find the manifest. Resolving once makes
                # HoP, index-clip.py, and downstream assembly all agree on one path.
                clip_manifests = []
                clip_index_data = []
                for raw_clip_path in clips:
                    clip_p = Path(raw_clip_path).resolve()
                    clip_path = str(clip_p)
                    meta_dir = clip_p.parent / "_meta"
                    cached = meta_dir / f"{clip_p.stem}.yaml"

                    if cached.exists():
                        print(f"[HoP] Using cached index: {clip_p.name}")
                    else:
                        print(f"[HoP] Indexing: {clip_p.name}")
                        idx_result = index_clip(clip_path)
                        if not idx_result["success"]:
                            print(f"[HoP] WARNING: index failed for {clip_p.name}: {idx_result.get('stderr','')[:200]}")
                            continue

                    if not cached.exists():
                        print(f"[HoP] WARNING: expected manifest not found after indexing: {cached}")
                        continue

                    clip_manifests.append(str(cached))

                    # Load real clip data for editor context
                    try:
                        with open(cached) as f:
                            data = yaml.safe_load(f)
                        clips_list = data.get("clips", [])
                        clip_index_data.append({
                            "path": clip_path,
                            "name": clip_p.name,
                            "manifest": str(cached),
                            "duration_s": data.get("duration_s", 0),
                            "transcript": data.get("transcript", ""),
                            "clips": clips_list,  # [{index, source_start, source_end, duration}]
                        })
                        print(f"[HoP]   {clip_p.name}: {len(clips_list)} detected clips, {data.get('duration_s', 0):.1f}s")
                    except Exception as e:
                        print(f"[HoP] WARNING: could not read manifest {cached}: {e}")

                if not clip_manifests:
                    raise RuntimeError("[HoP] No clip manifests produced — all indexing failed")

                # Phase 2: Editor selects which clips to keep (creative decisions only, no timecodes)
                # JuniorEditor is bypassed — SeniorEditor handles footage_edit directly.
                job["clip_index_data"] = clip_index_data
                job["output_mode"] = "manifest"
                result = SeniorEditor().execute(job, run_id=self.run_id)

                # Phase 3: Assemble from real clip-index manifests
                output_path = str(output_dir / f"{job['concept_id']}_v0.0.1_draft.mp4")

                # If editor returned a manifest structure, persist it before assembly
                editor_manifest = result.get("output", {}).get("manifest")
                if editor_manifest and job.get("manifest_path"):
                    try:
                        from packs.video.tools import write_manifest_scenes
                        scenes = editor_manifest.get("scenes", [])
                        if scenes:
                            write_manifest_scenes(job["manifest_path"], scenes)
                            print(f"[HoP] Wrote {len(scenes)} manifest scenes from editor")
                    except Exception as e:
                        print(f"[HoP] WARNING: could not persist editor manifest: {e}")

                selected_clips = result.get("output", {}).get("selected_clips")  # e.g., "0,2,4-6"

                # Handle clip selection: the editor may return per-file indices
                # like "9657:[3,4,5]". assemble-clips.py only supports --clips with
                # a single manifest, so when the editor selects clips from multiple
                # manifests we merge them into a single filtered manifest.
                effective_manifests = clip_manifests
                effective_clips = None

                if selected_clips:
                    if len(clip_manifests) == 1:
                        # Simple case: pass --clips directly
                        effective_clips = selected_clips
                    else:
                        # Multi-manifest: write a merged manifest with only selected clips
                        merged = self._merge_selected_clips(
                            selected_clips, clip_index_data, work_dir
                        )
                        if merged:
                            effective_manifests = [merged]
                            # All clips in the merged manifest are selected, no filter needed

                clip_label = selected_clips if selected_clips else "all"
                print(f"[HoP] Assembling clips ({clip_label})...")
                asm_result = assemble_clips(
                    effective_manifests, output_path, clips=effective_clips
                )

                # Inject output_path into assembly result for evaluator
                if asm_result.get("success") and not asm_result.get("output_path"):
                    asm_result["output_path"] = output_path
                result["assembly"] = asm_result
                if asm_result.get("success"):
                    print(f"[HoP] Assembly complete: {output_path}")
                else:
                    print(f"[HoP] Assembly failed: {asm_result.get('stderr','')[:300]}")

                # Track the pre-overlay assembly output so we can route it to
                # drafts/ if the overlay phase actually fires. If no overlays,
                # the assembly output IS the final and goes straight to output/.
                pre_overlay_path = asm_result.get("output_path") if asm_result.get("success") else None
                overlays_applied = False

                # Phase: overlay_burn — parse brief for persistent text overlays
                # and burn them onto the assembly output. No-op when the brief
                # doesn't mention overlays/text/captions. Chains multiple overlays
                # through intermediate files under work_dir. Failures here are
                # logged but don't fail the pipeline — overlays are additive.
                if asm_result.get("success"):
                    brief = job.get("content", "") or ""
                    try:
                        overlays = self._parse_overlay_intent(brief)
                    except Exception as e:
                        # LLM parse failure is a real bug, not a silent no-op
                        raise RuntimeError(
                            f"[HoP] overlay_burn: failed to parse overlay intent from brief: {e}"
                        ) from e

                    if overlays:
                        from packs.video.tools import burn_overlay
                        print(f"[HoP] Overlay burn: {len(overlays)} overlay(s) requested")
                        current_video = asm_result["output_path"]
                        for i, ov in enumerate(overlays, 1):
                            ov_text = ov.get("text", "").strip()
                            if not ov_text:
                                print(f"[HoP]   overlay {i}: empty text, skipping")
                                continue
                            overlay_out = str(
                                work_dir / f"{job['concept_id']}_v0.0.1_overlay{i}.mp4"
                            )
                            print(f"[HoP]   overlay {i}/{len(overlays)}: '{ov_text[:60]}'")
                            try:
                                kwargs = {
                                    "input_path": current_video,
                                    "output_path": overlay_out,
                                    "text": ov_text,
                                    "position": ov.get("position", "lower-third"),
                                    "fontcolor": ov.get("fontcolor", "black"),
                                    "stroke_color": ov.get("stroke_color", "white"),
                                }
                                if ov.get("start") is not None:
                                    kwargs["start"] = float(ov["start"])
                                if ov.get("end") is not None:
                                    kwargs["end"] = float(ov["end"])
                                bo = burn_overlay(**kwargs)
                            except Exception as e:
                                print(
                                    f"[HoP]   overlay {i} raised: {e} — "
                                    f"passing previous video through unchanged"
                                )
                                continue
                            if bo.get("success"):
                                current_video = overlay_out
                                print(f"[HoP]   overlay {i} burned: {overlay_out}")
                            else:
                                print(
                                    f"[HoP]   overlay {i} failed: "
                                    f"{(bo.get('stderr') or '')[:300]} — "
                                    f"passing previous video through unchanged"
                                )
                        # Publish final overlay output via the same mechanism
                        # assembly used, so evaluate picks it up transparently.
                        asm_result["output_path"] = current_video
                        result["assembly"] = asm_result
                        if current_video != pre_overlay_path:
                            overlays_applied = True
                    else:
                        print("[HoP] Overlay burn: no overlays requested, skipping")

                # Phase: publish final to project_root/output/ using the
                # canonical <concept>_v<version>.mp4 naming. When overlays
                # fired, the pre-overlay assembly gets saved to drafts/ for
                # history. The final lives ONLY in output/ so the user can
                # grab it without digging through .parallax/.
                if asm_result.get("success") and job.get("project_root"):
                    try:
                        import shutil as _shutil
                        from core.project_layout import (
                            next_version,
                            update_latest_symlink,
                            extract_abs_video_path,
                        )
                        project_root = Path(job["project_root"]).resolve()
                        (project_root / "output").mkdir(parents=True, exist_ok=True)
                        (project_root / "drafts").mkdir(parents=True, exist_ok=True)

                        version = next_version(project_root, job["concept_id"])
                        final_name = f"{job['concept_id']}_v{version}.mp4"
                        final_path = project_root / "output" / final_name

                        # Copy the true final (post-overlay if overlays fired,
                        # else the assembly output) to output/.
                        _shutil.copy2(asm_result["output_path"], final_path)
                        print(f"[HoP] final: {final_path}")

                        # If overlays fired, archive the pre-overlay cut in drafts/.
                        if overlays_applied and pre_overlay_path and Path(pre_overlay_path).exists():
                            draft_name = f"{job['concept_id']}_v{version}_assembly.mp4"
                            draft_path = project_root / "drafts" / draft_name
                            try:
                                _shutil.copy2(pre_overlay_path, draft_path)
                                print(f"[HoP] draft: {draft_path}")
                                result["draft_path"] = str(draft_path)
                            except Exception as _e:
                                print(f"[HoP] WARNING: could not archive draft: {_e}")

                        # Point output/latest.mp4 at the new final.
                        update_latest_symlink(project_root, final_path)

                        # If the brief mentions an absolute destination path, copy
                        # the final there as well. Only on success.
                        abs_target = extract_abs_video_path(job.get("content", "") or "")
                        if abs_target:
                            try:
                                target_p = Path(abs_target)
                                target_p.parent.mkdir(parents=True, exist_ok=True)
                                _shutil.copy2(final_path, target_p)
                                print(f"[HoP] copied: {target_p}")
                                result["copied_to"] = str(target_p)
                            except Exception as _e:
                                print(f"[HoP] WARNING: could not copy to {abs_target}: {_e}")

                        # Publish the canonical final path so callers see it.
                        asm_result["output_path"] = str(final_path)
                        result["assembly"] = asm_result
                        result["final_path"] = str(final_path)
                        result["version"] = version
                    except Exception as _e:
                        print(f"[HoP] WARNING: final publish to project_root failed: {_e}")

                # Write footage data to manifest for auditability
                if job.get("manifest_path"):
                    try:
                        _manifest = {}
                        if Path(job["manifest_path"]).exists():
                            with open(job["manifest_path"]) as _f:
                                _manifest = yaml.safe_load(_f) or {}
                        _manifest["footage"] = {
                            "clips": clips,
                            "clip_manifests": clip_manifests,
                            "selected_clips": selected_clips,
                            "output_path": asm_result.get("output_path"),
                        }
                        with open(job["manifest_path"], "w") as _f:
                            yaml.dump(_manifest, _f, default_flow_style=False, sort_keys=False)
                    except Exception as e:
                        print(f"[HoP] WARNING: could not write footage manifest: {e}")

            elif job_type == "lipsync_ad":
                # Character lipsync ad pipeline:
                #   1. Extract audio from source video/audio
                #   2. Generate lipsync data (mouth[] array per frame)
                #   3. Render character animation template with lipsync
                import subprocess as _sp
                from packs.video.tools import generate_lipsync, render_animation
                from core.paths import project_dir as get_project_dir

                audio_source = job.get("audio_source")
                if not audio_source:
                    raise RuntimeError("[HoP] lipsync_ad requires 'audio_source' path")

                template = job.get("template", "devil-lipsync")
                work_dir = get_project_dir(job["concept_id"])
                assets_dir = work_dir / "assets"
                output_dir = work_dir / "output"
                assets_dir.mkdir(parents=True, exist_ok=True)
                output_dir.mkdir(parents=True, exist_ok=True)

                # Extract audio to WAV (16kHz mono — optimal for Whisper)
                audio_wav = str(assets_dir / "voiceover.wav")
                print(f"[HoP] Extracting audio from: {Path(audio_source).name}")
                extract = _sp.run(
                    ["ffmpeg", "-i", audio_source, "-vn", "-acodec", "pcm_s16le",
                     "-ar", "16000", "-ac", "1", "-y", audio_wav],
                    capture_output=True,
                )
                if extract.returncode != 0:
                    raise RuntimeError(f"[HoP] Audio extraction failed: {extract.stderr.decode()[:200]}")

                # Get audio duration
                probe = _sp.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio_wav],
                    capture_output=True, text=True,
                )
                duration = 0.0
                try:
                    probe_data = json.loads(probe.stdout)
                    duration = float(probe_data.get("format", {}).get("duration", 0))
                except Exception:
                    pass
                if not duration:
                    duration = job.get("duration", 10.0)

                # Optionally cap duration (e.g., first N sentences only)
                max_duration = job.get("max_duration")
                if max_duration and max_duration < duration:
                    trimmed_wav = str(assets_dir / "voiceover_trimmed.wav")
                    _sp.run(
                        ["ffmpeg", "-i", audio_wav, "-t", str(max_duration), "-y", trimmed_wav],
                        capture_output=True, check=True,
                    )
                    audio_wav = trimmed_wav
                    duration = max_duration
                    print(f"[HoP] Trimmed to {duration:.1f}s")
                else:
                    print(f"[HoP] Audio duration: {duration:.1f}s")

                # Generate lipsync data
                lipsync_path = str(assets_dir / "lipsync.json")
                print("[HoP] Generating lipsync data...")
                ls_result = generate_lipsync(audio_wav, lipsync_path)
                if not ls_result["success"]:
                    raise RuntimeError(f"[HoP] Lipsync generation failed: {ls_result.get('stderr','')[:200]}")

                # Render character animation
                output_path = str(output_dir / f"{job['concept_id']}_v0.0.1_lipsync.mp4")
                extra_params = job.get("template_params", {})
                print(f"[HoP] Rendering template '{template}' at {duration:.1f}s...")
                render_result = render_animation(
                    template=template,
                    output=output_path,
                    duration=duration,
                    params=extra_params,
                    params_file=lipsync_path,
                )

                result = {
                    "audio_wav": audio_wav,
                    "lipsync": ls_result,
                    "render": render_result,
                    "output_path": output_path if render_result.get("success") else None,
                    "agent": "hop_lipsync_ad",
                }
                if render_result.get("success"):
                    print(f"[HoP] Lipsync render complete: {output_path}")
                else:
                    print(f"[HoP] Render failed: {render_result.get('stderr','')[:300]}")

            elif job_type == "voice_memo":
                from core.transcription_tools import transcribe
                try:
                    transcript = transcribe(job["content"])
                except Exception as e:
                    raise RuntimeError(f"[HoP] Transcription failed: {e}") from e
                job["content"] = transcript["text"]
                job["type"] = "script_brief"
                result = ScriptWriter().write(job)

            else:
                result = {"error": f"Unknown job type: {job_type}"}

        except Exception as e:
            raise RuntimeError(f"[HoP] Routing failed for job type '{job_type}': {e}") from e

        # Inject articulated intent for evaluator
        if self._current_plan:
            job["manifest_brief"] = self._current_plan.get("articulated_intent", job.get("content", ""))

        # Always evaluate before surfacing
        try:
            eval_result = Evaluator().evaluate(job, result)
            result["evaluation"] = eval_result
        except Exception as e:
            print(f"[HoP] WARNING: Evaluator failed: {e}")
            result["evaluation"] = {"error": str(e)}

        # Log final result
        try:
            from core.paths import run_dir as get_run_dir
            result_dir = get_run_dir(self.run_id)
            with open(result_dir / "result.json", "w") as f:
                json.dump(result, f, indent=2)
        except Exception as e:
            print(f"[HoP] WARNING: could not write result log: {e}")

        return {
            "status": "complete",
            "concept_id": job["concept_id"],
            "run_id": self.run_id,
            **result,
        }

    def _parse_overlay_intent(self, brief: str) -> list[dict]:
        """
        Extract persistent text-overlay directives from a creative brief via a
        single focused LLM call. Returns a list of overlay dicts (possibly
        empty). Each dict has keys: text, position, start, end, fontcolor,
        stroke_color. Empty list means the brief mentions no overlays and the
        overlay_burn phase is a no-op.

        Raises on LLM/JSON failure — a parse bug here is a real bug, not a
        silent no-op (silent no-op is reserved for "brief says nothing about
        overlays").
        """
        if not brief or not brief.strip():
            return []

        # Cheap gate: if none of these words appear, skip the LLM call entirely.
        # Keeps creative briefs like "cut a 15s teaser" from accidentally
        # triggering overlay processing (and from spending a token).
        lowered = brief.lower()
        trigger_words = (
            "overlay", "text", "caption", "subtitle", "title",
            "lower-third", "lower third", "label", "watermark", "brand",
        )
        if not any(w in lowered for w in trigger_words):
            return []

        system = (
            "You extract persistent text-overlay directives from video "
            "production briefs. You always respond with a single JSON object "
            "and nothing else."
        )
        prompt = (
            "Read the brief below and extract any requests for PERSISTENT text "
            "that should be BURNED onto the video (lower-thirds, on-screen "
            "labels, brand tags, arbitrary drawtext). Do NOT extract "
            "voiceover-driven word-by-word captions (those come from a separate "
            "system). Do NOT extract PNG headline cards. Only extract plain text "
            "strings the editor wants drawn on top of the frame.\n\n"
            "Return strict JSON with this exact shape:\n"
            '{"overlays": [{"text": "...", "position": "lower-third", '
            '"start": 0, "end": null, "fontcolor": "black", '
            '"stroke_color": "white"}]}\n\n'
            "Rules:\n"
            "- If the brief mentions NO persistent text overlays, return "
            '{"overlays": []}.\n'
            "- position must be one of: lower-third, upper-third, top, bottom, "
            "center. Default lower-third.\n"
            "- start/end are seconds. end=null means show for the full duration.\n"
            "- fontcolor and stroke_color default to black/white.\n"
            "- Only include overlays the brief EXPLICITLY requests. Do not "
            "invent text.\n"
            "- Return ONLY the JSON object. No prose, no code fences.\n\n"
            f"BRIEF:\n{brief}"
        )

        response = _llm_complete(
            model=self.MODEL,
            system=system,
            prompt=prompt,
            max_tokens=1024,
        )
        text = (response.get("text") or "").strip()
        if not text:
            raise RuntimeError("overlay-intent LLM returned empty response")

        # Strip markdown code fences if the model added them despite instructions.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            # Try to extract the first JSON object substring as a fallback.
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise RuntimeError(
                    f"overlay-intent LLM returned non-JSON: {text[:200]}"
                ) from e
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError as e2:
                raise RuntimeError(
                    f"overlay-intent LLM returned non-JSON: {text[:200]}"
                ) from e2

        overlays = data.get("overlays", [])
        if not isinstance(overlays, list):
            raise RuntimeError(
                f"overlay-intent LLM returned non-list overlays: {type(overlays).__name__}"
            )
        return overlays

    def _merge_selected_clips(self, selected: str, clip_index_data: list,
                               work_dir: Path) -> str | None:
        """
        Write a merged single manifest containing only the editor's selected clips.

        The editor may return selections in various formats:
          - Simple: "0,2,4-6" — treated as indices into the combined clip list
          - Per-file: "9657:[3,4,5]" or "IMG_9657:[3,4,5]" — per-file indices
          - Mixed: "0,1,9657:[3,4,5]"

        Returns path to the merged manifest, or None on failure.
        """
        import re
        import yaml

        # Build per-file lookup: stem/number → clip_index_data entry
        file_map = {}
        for cd in clip_index_data:
            stem = Path(cd["path"]).stem
            file_map[stem] = cd
            # Also map by just numeric suffix (e.g., "9657" for "IMG_9657")
            nums = re.findall(r'\d+', stem)
            if nums:
                file_map[nums[-1]] = cd

        # Parse per-file selections — handles both formats:
        #   Bracketed: "9657:[3,4,5]" or "IMG_9657:[3,4,5,6-8]"
        #   Repeated:  "9657:3,9657:4,9657:5"
        per_file_indices: dict[str, set[int]] = {}

        # First try bracketed format: KEY:[indices]
        for match in re.finditer(r'([^,\[\]]+?):\[?([\d,\s\-]+)\]', selected):
            file_key = match.group(1).strip()
            indices_str = match.group(2).strip()
            indices = set()
            for part in indices_str.split(","):
                part = part.strip()
                if "-" in part:
                    s, e = part.split("-", 1)
                    indices.update(range(int(s), int(e) + 1))
                else:
                    indices.add(int(part))
            per_file_indices.setdefault(file_key, set()).update(indices)

        # Then try KEY:INDEX pairs (e.g., "9657:3,9657:4")
        if not per_file_indices:
            for match in re.finditer(r'([A-Za-z0-9_]+):(\d+)', selected):
                file_key = match.group(1).strip()
                idx = int(match.group(2))
                per_file_indices.setdefault(file_key, set()).add(idx)

        # If no per-file notation found, treat as combined indices
        if not per_file_indices:
            # Simple combined indices — build from all clip_index_data in order
            all_clips = []
            for cd in clip_index_data:
                for clip in cd.get("clips", []):
                    all_clips.append((cd["path"], clip))
            wanted = set()
            for part in selected.split(","):
                part = part.strip()
                if "-" in part:
                    s, e = part.split("-", 1)
                    wanted.update(range(int(s), int(e) + 1))
                elif part.isdigit():
                    wanted.add(int(part))

            merged_clips = []
            for i, (source, clip) in enumerate(all_clips):
                if i in wanted:
                    merged_clips.append({**clip, "_source": source})

        else:
            # Per-file selections — resolve and filter
            merged_clips = []
            for file_key, indices in per_file_indices.items():
                cd = file_map.get(file_key)
                if not cd:
                    print(f"[HoP] WARNING: clip selection key '{file_key}' not found")
                    continue
                for clip in cd.get("clips", []):
                    if clip["index"] in indices:
                        merged_clips.append({**clip, "_source": cd["path"]})

        if not merged_clips:
            print(f"[HoP] WARNING: no clips matched selection '{selected}' — assembling all")
            return None

        # Write merged manifest
        merged_path = work_dir / "merged_selection.yaml"
        # Use the source from first clip (all clips reference by absolute path anyway)
        manifest_data = {
            "format": "clip-index",
            "source": merged_clips[0]["_source"],
            "duration_s": sum(c.get("duration", 0) for c in merged_clips),
            "transcript": f"[merged] {len(merged_clips)} clips from editor selection",
            "clips": [],
        }
        for i, c in enumerate(merged_clips):
            manifest_data["clips"].append({
                "index": i,
                "source_start": c["source_start"],
                "source_end": c["source_end"],
                "duration": c["duration"],
                "_original_source": c["_source"],
            })
        # Multi-source: assemble-clips uses manifest["source"] for all clips.
        # If clips come from different sources we need per-clip source override.
        # Check if all clips share the same source:
        sources = set(c["_source"] for c in merged_clips)
        if len(sources) > 1:
            # Multiple sources — write one manifest per source
            # and return them as a list... but we can only return one path.
            # Fallback: just return None and let all clips assemble.
            print(f"[HoP] WARNING: selected clips span {len(sources)} source files — "
                  f"merged manifest not supported, assembling all clips")
            return None

        # Clean up internal fields
        for c in manifest_data["clips"]:
            c.pop("_original_source", None)

        with open(merged_path, "w") as f:
            yaml.dump(manifest_data, f, default_flow_style=False)
        print(f"[HoP] Merged selection: {len(merged_clips)} clips → {merged_path}")
        return str(merged_path)

    def _health_check(self):
        """Run health check on first instantiation. Warn but don't block in TEST_MODE."""
        import os
        if os.environ.get("TEST_MODE", "false").lower() == "true":
            return
        try:
            from core.health import check_all, display
            result = check_all()
            print(display(result))
            if not result["ready"]:
                print("[HoP] WARNING: Some critical dependencies are missing. Some operations will fail.")
        except Exception as e:
            print(f"[HoP] WARNING: health check failed: {e}")
