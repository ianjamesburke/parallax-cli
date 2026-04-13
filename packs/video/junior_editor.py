"""
JuniorEditor agent — executes video edits, returns confidence score,
escalates to SeniorEditor if confidence < 0.70.
Uses claude-haiku-4-5 for cost efficiency.
"""

import json
import re
from typing import Optional

MODEL = "claude-haiku-4-5-20251001"
ESCALATION_THRESHOLD = 0.70

SYSTEM = """You are a junior video editor at NRTV.
You receive edit jobs and execute them precisely.

For each job, you:
1. Analyze what needs to be done
2. Describe the exact edit steps you would take
3. Rate your confidence (0.0–1.0) that you can execute correctly
4. Note anything ambiguous or beyond your skill level

Be honest about your confidence. It is better to escalate than to make a bad edit.
Return JSON only — no prose outside the JSON object."""


class JuniorEditor:
    """
    Executes video edits at a junior level.
    Returns confidence score; escalates to SeniorEditor if below threshold.

    v1: Calls claude-haiku-4-5 directly.
    v2: Register as persistent agent via client.beta.agents.
    """

    MODEL = MODEL
    ESCALATION_THRESHOLD = ESCALATION_THRESHOLD

    def execute(self, job: dict, run_id: Optional[str] = None) -> dict:
        """
        Attempt to execute a video edit job.

        Args:
            job: {type, content, brand_file, concept_id, ...}
            run_id: for cost logging

        Returns:
            {output: dict, confidence: float, notes: str, escalate: bool, agent: str}
        """
        import os
        if os.environ.get("TEST_MODE", "false").lower() == "true":
            if job.get("_mock_response"):
                print("[JuniorEditor] DRILL — using mock response")
                return job["_mock_response"]
            print("[JuniorEditor] DRILL — returning stub edit plan")
            return {
                "output": {"plan": f"[DRILL] Edit plan for: {job.get('content', '')[:80]}"},
                "confidence": 0.75,
                "notes": "[DRILL] Placeholder — no LLM call made",
                "escalate": False,
                "agent": "junior_editor",
            }

        from core.agent_loop import run_with_tools
        run_id = run_id or job.get("run_id", "unknown")

        prompt = self._build_prompt(job)
        tool_names = self._get_tools(job)

        try:
            response = run_with_tools(
                model=self.MODEL,
                system=SYSTEM,
                prompt=prompt,
                tool_names=tool_names,
                max_tokens=4096,
                cost_context={
                    "concept_id": job.get("concept_id", "UNKNOWN"),
                    "agent": "junior_editor",
                    "run_id": run_id,
                },
            )
        except Exception as e:
            raise RuntimeError(f"[JuniorEditor] API call failed: {e}") from e

        result = self._parse_response(response["text"])

        # Log reasoning to run log
        self._log_reasoning(run_id, job, result)

        return result

    def _build_prompt(self, job: dict) -> str:
        parts = [
            f"Edit job type: {job.get('type', 'unknown')}",
            f"Instructions:\n{job.get('content', '')}",
        ]

        # Project working directory — ALL output files go here
        work_dir = job.get("work_dir")
        if work_dir:
            parts.append(
                f"PROJECT DIRECTORY: {work_dir}\n"
                f"Write ALL intermediate and output files under this directory.\n"
                f"  {work_dir}/input/   — symlink or copy source files here\n"
                f"  {work_dir}/assets/  — intermediate files (rotated clips, trimmed segments, text cards)\n"
                f"  {work_dir}/output/  — final deliverables only\n"
                f"NEVER write output files next to the source clips."
            )

        # Include clip data if available (footage_edit jobs)
        clip_data = job.get("clip_data", [])
        if clip_data:
            parts.append("SOURCE CLIPS:")
            for cd in clip_data:
                parts.append(f"\n--- {cd['name']} ---")
                parts.append(f"Path: {cd['path']}")
                info_stdout = cd.get("info", {}).get("stdout", "")
                if info_stdout:
                    parts.append(f"Media info: {info_stdout.strip()}")
                index_stdout = cd.get("index", {}).get("stdout", "")
                if index_stdout:
                    parts.append(f"Transcript:\n{index_stdout.strip()}")

        # Include real clip index data (footage_edit jobs with proper indexing)
        clip_index_data = job.get("clip_index_data", [])
        if clip_index_data:
            parts.append("INDEXED SOURCE CLIPS (with detected cut points):")
            for cd in clip_index_data:
                parts.append(f"\n--- {cd['name']} ({cd['duration_s']:.1f}s total) ---")
                parts.append(f"Manifest: {cd['manifest']}")
                parts.append(f"Transcript: {cd['transcript'][:500]}")
                clips = cd.get("clips", [])
                parts.append(f"Auto-detected clips ({len(clips)} total after silence removal):")
                for c in clips[:20]:  # limit to 20 clips
                    parts.append(f"  [{c['index']}] {c['source_start']:.2f}s – {c['source_end']:.2f}s ({c['duration']:.1f}s)")

        # Output format depends on mode
        if clip_index_data:
            parts.append(
                "\nYour job is creative direction only — DO NOT guess timecodes.\n"
                "The auto-detected clips above are already cut correctly at silence boundaries.\n"
                "Return JSON:\n"
                "  output.selected_clips (str): comma-separated clip indices to keep, e.g. '0,2,4-6'\n"
                "    Leave null to use all clips.\n"
                "  output.notes (str): creative reasoning — what you kept and why\n"
                "  confidence (float 0-1)\n"
                "  escalate (bool)\n"
                "\nReturn only the JSON object."
            )
        elif job.get("output_mode") == "manifest" or job.get("type") == "footage_edit":
            parts.append(self._manifest_prompt(job))
        else:
            # Include available tools with exact signatures
            tools = job.get("available_tools", [])
            if tools:
                from packs.video.tools import tool_signatures
                parts.append(tool_signatures())
                parts.append(
                    "Plan tool calls in output.tool_calls as [{tool: str, args: dict}, ...]. "
                    "Use EXACT parameter names from signatures above. HoP executes them in order. "
                    "Do NOT include extra keys like 'notes' in args — only the parameters listed."
                )
            parts.append(
                "\nDescribe how you would execute this edit. Return JSON:\n"
                "  output (dict): what you produced or would produce — include tool_calls if using tools\n"
                "  confidence (float 0-1): your confidence in correctness\n"
                "  notes (str): any caveats, ambiguities, or concerns\n"
                "  escalate (bool): true if you need senior review\n"
                "\nReturn only the JSON object."
            )
        return "\n\n".join(parts)

    def _get_tools(self, job: dict) -> list:
        """Return relevant tool names for this job context."""
        if job.get("clip_index_data"):
            # Footage edit: can inspect media and get AI suggestions — no ffmpeg
            return ["inspect_media", "suggest_clips"]
        return []

    def _manifest_prompt(self, job: dict) -> str:
        """Prompt the editor to output a manifest instead of tool_calls."""
        return """
Your job is to write a MANIFEST — a YAML structure that describes the edit.
The assembler will render the final video from this manifest in one pass.
You do NOT need to plan ffmpeg commands or tool calls. Just describe the edit.

Return JSON with output.manifest containing:

  config:
    resolution: "1920x1080"    # or "1080x1920" for vertical
    fps: 30

  scenes:
    - index: 1
      type: video              # video | text_overlay | still | effect_overlay
      source: "/absolute/path/to/source.MOV"
      start_s: 0.0             # start timecode in source
      end_s: 45.0              # end timecode in source
      rotate: 180              # optional: 90, 180, 270
      description: "Main content section"

    - index: 2
      type: text_overlay
      overlay_text: "Title card text here"
      estimated_duration_s: 3.0

    - index: 3
      type: effect_overlay
      base_scene: 1            # applies filter to output of scene 1
      filter: "drawtext=text='HELLO':fontsize=120:fontcolor=red:x=(w-text_w)/2:y=(h-text_h)/2"
      estimated_duration_s: 2.0

Scene types:
  - video: extract a segment from source footage. Use source (absolute path), start_s, end_s, rotate.
  - text_overlay: generate a text card. Use overlay_text, estimated_duration_s.
  - effect_overlay: apply ffmpeg filter over a base. Use base_scene (index) or source, filter (ffmpeg -vf string).

Source files are NEVER modified. The manifest references them by absolute path.
The assembler handles rotation, scaling, concatenation, and encoding.

VALIDATION: The manifest is validated against a schema each time you write it.
If your write fails validation, the error message will be returned in the tool_result
for your next turn. Read the error carefully — it tells you exactly which field is wrong.
Fix the field and try again. Common errors:
  - scenes[N].end_s: must be greater than start_s
  - scenes[N].source: required for video scenes
  - scenes[N].overlay_text: required for text_overlay scenes
  - scenes[N].filter: required for effect_overlay scenes
  - scenes[N].rotate: must be one of [90, 180, 270]
  - footage.source_clips[N].end_s: must be greater than start_s

Return JSON:
  output.manifest (dict): the manifest structure above
  confidence (float 0-1): your confidence
  notes (str): any caveats
  escalate (bool): true if you need senior review

Return only the JSON object."""

    def _parse_response(self, text: str) -> dict:
        """Parse JSON from model response."""
        try:
            clean = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
            data = json.loads(clean)
            confidence = float(data.get("confidence", 0.5))
            return {
                "output": data.get("output", {}),
                "confidence": confidence,
                "notes": data.get("notes", ""),
                "escalate": confidence < self.ESCALATION_THRESHOLD,
                "agent": "junior_editor",
            }
        except Exception as e:
            print(f"[JuniorEditor] WARNING: could not parse response ({e})")
            return {
                "output": {},
                "confidence": 0.0,
                "notes": f"Parse error: {e}\nRaw: {text[:200]}",
                "escalate": True,
                "agent": "junior_editor",
            }

    def _log_reasoning(self, run_id: str, job: dict, result: dict):
        """Log tool calls and reasoning to the run log."""
        try:
            from core.paths import run_dir
            log_dir = run_dir(run_id)
            log_path = log_dir / "junior_editor.json"
            with open(log_path, "w") as f:
                json.dump({"job_type": job.get("type"), "result": result}, f, indent=2)
        except Exception as e:
            print(f"[JuniorEditor] WARNING: could not write run log: {e}")
