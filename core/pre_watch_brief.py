"""
PreWatchBrief
=============
Generates a hypothesis before the human watches the output.
Surfaces: what changed, concerns raised, predicted rating, predicted feedback.

The prediction is logged through the trust system — if the human's actual
rating matches ±1, it counts as a correct prediction.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class PreWatchBrief:
    """
    Generates a structured pre-watch brief for a completed run.
    Designed to make human review faster by front-loading context:
    the reviewer watches to confirm or override, not to discover.
    """

    def __init__(self, run_id: str, concept_id: str):
        self.run_id = run_id
        self.concept_id = concept_id
        from core.paths import run_dir
        self.run_dir = run_dir(run_id)

    def generate(self, result: dict, concerns: list,
                 prior_run_id: Optional[str] = None) -> dict:
        """
        Build the pre-watch brief from run artifacts.

        Args:
            result: the pipeline output dict from HoP
            concerns: list of concern dicts from ConcernBus
            prior_run_id: if this is an iteration, the run_id of the previous version

        Returns:
            {
                changes: list[str],        # what's different from prior iteration
                concerns_summary: list[str],
                predicted_rating: int,     # 1-10
                predicted_feedback: str,   # one sentence: what HoP thinks you'll say
                confidence: float,         # how confident in the prediction
                iteration: int,            # which iteration this is
            }
        """
        changes = self._diff_from_prior(prior_run_id) if prior_run_id else ["First iteration — no prior to compare"]
        concern_lines = self._summarize_concerns(concerns)
        predicted_rating, predicted_feedback, confidence = self._predict_rating(result, concerns, changes)

        # Determine iteration number
        iteration = self._count_iterations()

        brief = {
            "run_id": self.run_id,
            "concept_id": self.concept_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "changes": changes,
            "concerns_summary": concern_lines,
            "predicted_rating": predicted_rating,
            "predicted_feedback": predicted_feedback,
            "confidence": confidence,
            "iteration": iteration,
        }

        # Save to run dir
        try:
            brief_path = self.run_dir / "pre_watch_brief.json"
            brief_path.write_text(json.dumps(brief, indent=2))
        except Exception as e:
            print(f"[PreWatchBrief] WARNING: could not save brief: {e}")

        return brief

    def _diff_from_prior(self, prior_run_id: str) -> list[str]:
        """Compare this run's result to the prior run and list changes."""
        changes = []
        from core.paths import run_dir as get_run_dir
        prior_dir = get_run_dir(prior_run_id)

        try:
            prior_result = json.loads((prior_dir / "result.json").read_text())
            current_result = json.loads((self.run_dir / "result.json").read_text())
        except FileNotFoundError:
            return ["Could not load prior run for comparison"]
        except Exception as e:
            return [f"Comparison failed: {e}"]

        # Compare scenes if present
        prior_scenes = prior_result.get("scenes", [])
        current_scenes = current_result.get("scenes", [])
        if len(current_scenes) != len(prior_scenes):
            changes.append(f"Scene count: {len(prior_scenes)} → {len(current_scenes)}")

        # Compare agents involved
        prior_agent = prior_result.get("agent", "")
        current_agent = current_result.get("agent", "")
        if prior_agent != current_agent:
            changes.append(f"Routing changed: {prior_agent} → {current_agent}")

        # Compare confidence
        prior_conf = prior_result.get("confidence", 0)
        current_conf = current_result.get("confidence", 0)
        if abs(prior_conf - current_conf) > 0.05:
            changes.append(f"Confidence: {prior_conf:.0%} → {current_conf:.0%}")

        # Compare eval score
        prior_eval = prior_result.get("evaluation", {}).get("score", 0)
        current_eval = current_result.get("evaluation", {}).get("score", 0)
        if abs(prior_eval - current_eval) > 0.05:
            changes.append(f"Eval score: {prior_eval:.0%} → {current_eval:.0%}")

        if not changes:
            changes.append("No significant changes detected from prior iteration")

        return changes

    def _summarize_concerns(self, concerns: list) -> list[str]:
        """Flatten concerns into human-readable one-liners."""
        if not concerns:
            return ["No concerns raised"]
        lines = []
        for c in concerns:
            if isinstance(c, dict):
                severity = c.get("severity", 0)
                msg = c.get("message", "Unknown concern")
                blocking = " [BLOCKING]" if c.get("blocking") else ""
                lines.append(f"({severity:.0%}){blocking} {msg}")
            else:
                lines.append(str(c))
        return lines

    def _predict_rating(self, result: dict, concerns: list,
                        changes: list) -> tuple[int, str, float]:
        """
        Predict what the human will rate this (1-10) and what they'll say.
        This is heuristic for now — v2 can use an LLM call.
        """
        # Start at 7, adjust based on signals
        rating = 7
        feedback_parts = []
        confidence = 0.5

        # Eval score influence
        eval_score = result.get("evaluation", {}).get("score", 0.75)
        if eval_score >= 0.9:
            rating += 1
            feedback_parts.append("strong output quality")
        elif eval_score < 0.6:
            rating -= 2
            feedback_parts.append("quality may fall short of brief")

        # Agent confidence influence
        agent_conf = result.get("confidence", 0.8)
        if agent_conf >= 0.9:
            rating += 1
        elif agent_conf < 0.6:
            rating -= 1
            feedback_parts.append("agent had low confidence")

        # Concerns drag rating down
        blocking_concerns = [c for c in concerns if isinstance(c, dict) and c.get("blocking")]
        if blocking_concerns:
            rating -= 2
            feedback_parts.append(f"{len(blocking_concerns)} blocking concern(s)")
        elif len(concerns) > 2:
            rating -= 1
            feedback_parts.append("multiple non-blocking concerns")

        # Clamp
        rating = max(1, min(10, rating))

        if not feedback_parts:
            feedback = "Looks solid — expecting minor tweaks at most."
        else:
            feedback = "Potential issues: " + "; ".join(feedback_parts) + "."

        return rating, feedback, confidence

    def _count_iterations(self) -> int:
        """Count how many runs exist for this concept."""
        from core.paths import RUNS_DIR
        logs_dir = RUNS_DIR
        if not logs_dir.exists():
            return 1
        count = 0
        for run_dir in logs_dir.iterdir():
            if run_dir.is_dir():
                job_path = run_dir / "job.json"
                if job_path.exists():
                    try:
                        job = json.loads(job_path.read_text())
                        if job.get("concept_id") == self.concept_id:
                            count += 1
                    except Exception:
                        pass
        return max(count, 1)

    def display(self, brief: dict, output_path: str = "") -> str:
        """Format the brief for terminal display. Returns the formatted string."""
        lines = []
        lines.append(f"[HoP] PRE-WATCH BRIEF — Concept {brief['concept_id']} / Iteration {brief['iteration']}")
        lines.append(f"  Run: {brief['run_id']}")
        lines.append("")

        lines.append("  Changes:")
        for change in brief["changes"]:
            lines.append(f"    • {change}")
        lines.append("")

        lines.append("  Concerns:")
        for concern in brief["concerns_summary"]:
            lines.append(f"    • {concern}")
        lines.append("")

        lines.append(f"  Predicted rating: {brief['predicted_rating']}/10 — \"{brief['predicted_feedback']}\"")
        lines.append(f"  Prediction confidence: {brief['confidence']:.0%}")
        lines.append("")

        if output_path:
            lines.append(f"  Watch: {output_path}")
            lines.append("")

        return "\n".join(lines)
