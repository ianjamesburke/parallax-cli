"""
ScriptWriter agent — generates video scripts from briefs, ideas, voice transcripts,
or B-roll descriptions. Uses claude-sonnet-4-6.
"""

import json
import re
from pathlib import Path
from typing import Optional

import anthropic
from core.cost_tracker import log_call

MODEL = "claude-sonnet-4-6"

SYSTEM = """You are an expert video scriptwriter at NRTV, a video production agency.
You write punchy, emotionally resonant short-form video scripts (typically 15–60 seconds).

Your outputs always include:
- A complete script (narration/dialogue only, no stage directions)
- A scene-by-scene breakdown (visual description per scene)
- Voiceover lines (exactly what will be spoken, in order)
- A confidence score (0.0–1.0) for how well the brief was fulfilled

Writing style:
- Hook in the first 3 words
- Conversational, not corporate
- End with a clear CTA or emotional payoff
- Respect brand constraints if provided

If brand guidelines are provided, honor them strictly."""


class ScriptWriter:
    """
    Generates scripts from job briefs.

    v1: Calls claude-sonnet-4-6 directly via messages.create.
    v2: Register as persistent agent via client.beta.agents.
    """

    MODEL = MODEL

    def write(self, job: dict) -> dict:
        """
        Generate a script from a job dict.

        Args:
            job: {type, content, brand_file, concept_id, run_id (optional), test_mode (optional)}

        Returns:
            {script: str, scenes: list, vo_lines: list, confidence: float, agent: str}
        """
        import os
        if os.environ.get("TEST_MODE", "false").lower() == "true":
            if job.get("_mock_response"):
                print("[ScriptWriter] DRILL — using mock response")
                return job["_mock_response"]
            print("[ScriptWriter] DRILL — returning stub script")
            return {
                "script": f"[DRILL] Script for: {job.get('content', '')[:80]}",
                "scenes": [
                    {"index": 1, "visual": "[DRILL] Hook shot", "vo": "One dose for him."},
                    {"index": 2, "visual": "[DRILL] Product shot", "vo": "Multiple rounds for you."},
                    {"index": 3, "visual": "[DRILL] Payoff shot", "vo": "Try Rugiet Ready today."},
                ],
                "vo_lines": ["One dose for him.", "Multiple rounds for you.", "Try Rugiet Ready today."],
                "confidence": 0.9,
                "agent": "script_writer",
            }

        from core.llm import complete as llm_complete

        brand_context = ""
        if job.get("brand_file"):
            brand_context = self._load_brand(job["brand_file"])

        prompt = self._build_prompt(job, brand_context)

        try:
            response = llm_complete(
                model=self.MODEL,
                system=SYSTEM,
                prompt=prompt,
                max_tokens=2048,
            )
        except Exception as e:
            raise RuntimeError(f"[ScriptWriter] API call failed: {e}") from e

        # Log cost
        try:
            log_call(
                concept_id=job.get("concept_id", "UNKNOWN"),
                agent="script_writer",
                run_id=job.get("run_id", "unknown"),
                input_tokens=response["input_tokens"],
                output_tokens=response["output_tokens"],
                model=self.MODEL,
            )
        except Exception as e:
            print(f"[ScriptWriter] WARNING: cost logging failed: {e}")

        return self._parse_response(response["text"])

    def _build_prompt(self, job: dict, brand_context: str) -> str:
        parts = [f"Job type: {job.get('type', 'script_brief')}"]
        parts.append(f"Brief:\n{job.get('content', '')}")
        if brand_context:
            parts.append(f"Brand guidelines:\n{brand_context}")
        parts.append(
            "\nReturn your response as JSON with these keys:\n"
            "  script (str): full narration\n"
            "  scenes (list of str): visual description per scene\n"
            "  vo_lines (list of str): voiceover lines in order\n"
            "  confidence (float 0-1): how well you fulfilled the brief\n"
            "\nReturn only the JSON object, no markdown fences."
        )
        return "\n\n".join(parts)

    def _load_brand(self, brand_file: str) -> str:
        """Read brand YAML/JSON file and return as string context."""
        try:
            path = Path(brand_file)
            if not path.exists():
                print(f"[ScriptWriter] WARNING: brand_file not found: {brand_file}")
                return ""
            return path.read_text()
        except Exception as e:
            print(f"[ScriptWriter] WARNING: could not read brand_file {brand_file}: {e}")
            return ""

    def _parse_response(self, text: str) -> dict:
        """Parse JSON from model response. Fallback to raw text if parse fails."""
        try:
            # Strip markdown fences if present
            clean = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
            data = json.loads(clean)
            return {
                "script": data.get("script", ""),
                "scenes": data.get("scenes", []),
                "vo_lines": data.get("vo_lines", []),
                "confidence": float(data.get("confidence", 0.7)),
                "agent": "script_writer",
            }
        except Exception as e:
            print(f"[ScriptWriter] WARNING: could not parse JSON response ({e}) — returning raw")
            return {
                "script": text,
                "scenes": [],
                "vo_lines": [],
                "confidence": 0.5,
                "agent": "script_writer",
            }
