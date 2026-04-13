"""
AssistantEditor agent — translates raw client revision notes into structured
revision briefs with success metrics. Uses claude-haiku-4-5.
"""

import json
import re
from pathlib import Path
from typing import Optional

import anthropic
from core.cost_tracker import log_call

MODEL = "claude-haiku-4-5-20251001"
ESCALATION_THRESHOLD = 0.65

SYSTEM = """You are an assistant editor at NRTV.
Your job is to translate vague client notes into precise, actionable revision briefs.

Client notes are often ambiguous ("make it pop", "the pacing feels off").
You translate these into specific, measurable edit instructions.

For each revision request, output:
- What exactly needs to change (with location: scene/timestamp/element)
- What "done" looks like (success criteria)
- Any constraints the editor must respect
- Priority (critical / high / normal / nice-to-have)
- Your confidence that you understood the request correctly

Return JSON only."""


class AssistantEditor:
    """
    Translates raw client revision notes into structured briefs for the editing team.

    v1: Calls claude-haiku-4-5 directly.
    v2: Register as persistent agent via client.beta.agents.
    """

    MODEL = MODEL
    ESCALATION_THRESHOLD = ESCALATION_THRESHOLD

    def translate(
        self,
        client_note: str,
        project_history: Optional[str] = None,
        job: Optional[dict] = None,
    ) -> dict:
        """
        Translate a raw client revision note into a structured brief.

        Args:
            client_note: raw text from the client (e.g. "make the logo bigger")
            project_history: optional prior context (previous edits, brand notes)
            job: original job dict for concept_id / run_id tracking

        Returns:
            {changes: list[{what, where, success_criteria}], constraints: list,
             priority: str, confidence: float, agent: str}
        """
        client = anthropic.Anthropic()
        job = job or {}
        run_id = job.get("run_id", "unknown")
        concept_id = job.get("concept_id", "UNKNOWN")

        prompt = self._build_prompt(client_note, project_history)

        try:
            response = client.messages.create(
                model=self.MODEL,
                max_tokens=1024,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            raise RuntimeError(f"[AssistantEditor] API call failed: {e}") from e

        # Log cost
        try:
            log_call(
                concept_id=concept_id,
                agent="assistant_editor",
                run_id=run_id,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=self.MODEL,
            )
        except Exception as e:
            print(f"[AssistantEditor] WARNING: cost logging failed: {e}")

        return self._parse_response(response.content[0].text)

    def _build_prompt(self, client_note: str, project_history: Optional[str]) -> str:
        parts = [f'Client revision note:\n"{client_note}"']
        if project_history:
            parts.append(f"Project history / context:\n{project_history}")
        parts.append(
            "\nStructure this into a revision brief. Return JSON:\n"
            "  changes (list): each item has {what: str, where: str, success_criteria: str}\n"
            "  constraints (list of str): things the editor must NOT change\n"
            "  priority (str): 'critical' | 'high' | 'normal' | 'nice-to-have'\n"
            "  confidence (float 0-1): how clearly you understood the request\n"
            "\nReturn only the JSON object."
        )
        return "\n\n".join(parts)

    def _parse_response(self, text: str) -> dict:
        """Parse JSON from model response."""
        try:
            clean = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
            data = json.loads(clean)
            return {
                "changes": data.get("changes", []),
                "constraints": data.get("constraints", []),
                "priority": data.get("priority", "normal"),
                "confidence": float(data.get("confidence", 0.7)),
                "agent": "assistant_editor",
            }
        except Exception as e:
            print(f"[AssistantEditor] WARNING: could not parse response ({e})")
            return {
                "changes": [],
                "constraints": [],
                "priority": "normal",
                "confidence": 0.3,
                "agent": "assistant_editor",
            }
