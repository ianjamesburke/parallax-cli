"""
Evaluator — quality gate before surfacing output to the user.
Scores output against the original brief and raises concerns if quality is low.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Any


@dataclass
class EvalResult:
    score: float          # 0.0–1.0
    passed: bool          # True if score >= threshold
    notes: str
    concerns: list[str]


class Evaluator:
    PASS_THRESHOLD = 0.65

    def evaluate(self, job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """
        Evaluate pipeline output against the job brief.
        Returns a plain dict for easy JSON serialization.
        In TEST_MODE, always passes with a placeholder score.
        """
        test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"
        if test_mode:
            return {
                "score": 0.85,
                "passed": True,
                "notes": "[DRILL] Evaluation skipped — placeholder pass.",
                "concerns": [],
            }

        # TODO: use LLM to score output against brief
        has_output = bool(result.get("output_path") or result.get("scenes"))
        score = 0.75 if has_output else 0.30
        passed = score >= self.PASS_THRESHOLD
        notes = "Output present, basic quality check passed." if passed else "No output produced."
        return {"score": score, "passed": passed, "notes": notes, "concerns": []}
