"""
SeniorEditor agent — handles escalated edits from JuniorEditor.
Uses claude-sonnet-4-6 for higher capability. Receives junior's notes to avoid repeating analysis.
"""

import json
import re
from typing import Optional

MODEL = "claude-sonnet-4-6"
ESCALATION_THRESHOLD = 0.85

SYSTEM = """You are a senior video editor at NRTV with years of experience.
You handle complex edits that junior editors escalated because they lacked confidence.

You receive:
- The original edit job
- Notes from the junior editor explaining what stumped them

Your job:
1. Understand why the junior escalated
2. Execute the edit with full precision
3. Explain your approach so junior editors can learn
4. Rate your own confidence (0.85+ expected at senior level)

Return JSON only."""


class SeniorEditor:
    """
    Handles escalated video edits that JuniorEditor couldn't handle confidently.

    v1: Calls claude-sonnet-4-6 directly.
    v2: Register as persistent agent via client.beta.agents.
    """

    MODEL = MODEL
    ESCALATION_THRESHOLD = ESCALATION_THRESHOLD

    def execute(self, job: dict, junior_notes: Optional[str] = None, run_id: Optional[str] = None) -> dict:
        """
        Execute a video edit, incorporating context from the failed junior attempt.

        Args:
            job: original job dict
            junior_notes: what the junior editor couldn't handle
            run_id: for cost logging

        Returns:
            {output: dict, confidence: float, notes: str, escalate: bool, agent: str, junior_reason: str}
        """
        import os
        if os.environ.get("TEST_MODE", "false").lower() == "true":
            if job.get("_mock_response"):
                print("[SeniorEditor] DRILL — using mock response")
                return job["_mock_response"]
            print("[SeniorEditor] DRILL — returning stub edit plan")
            return {
                "output": {"plan": f"[DRILL] Senior edit for: {job.get('content', '')[:80]}"},
                "confidence": 0.92,
                "notes": f"[DRILL] Placeholder — junior escalated because: {junior_notes or 'N/A'}",
                "escalate": False,
                "agent": "senior_editor",
                "junior_reason": junior_notes or "",
            }

        from core.agent_loop import run_with_tools
        run_id = run_id or job.get("run_id", "unknown")

        prompt = self._build_prompt(job, junior_notes)
        tool_names = self._get_tools(job)
        cost_context = {
            "concept_id": job.get("concept_id", "UNKNOWN"),
            "agent": "senior_editor",
            "run_id": run_id,
        }

        def _call(p: str) -> dict:
            try:
                return run_with_tools(
                    model=self.MODEL,
                    system=SYSTEM,
                    prompt=p,
                    tool_names=tool_names,
                    max_tokens=8192,
                    cost_context=cost_context,
                )
            except Exception as e:
                raise RuntimeError(f"[SeniorEditor] API call failed: {e}") from e

        response = _call(prompt)

        # Parse with retry-on-failure. On first parse failure, retry ONCE with
        # an explicit schema reminder appended. If the retry also fails, raise
        # — do NOT silently fall back. A broken editor must surface immediately.
        try:
            result = self._parse_response(response["text"])
        except RuntimeError as first_err:
            print(f"[SeniorEditor] Parse failed on first attempt: {first_err}")
            print("[SeniorEditor] Retrying ONCE with schema reminder...")
            retry_prompt = prompt + "\n\n" + self._schema_reminder(job)
            retry_response = _call(retry_prompt)
            try:
                result = self._parse_response(retry_response["text"])
            except RuntimeError as retry_err:
                raw = (retry_response.get("text") or "")[:500]
                raise RuntimeError(
                    f"[SeniorEditor] JSON parse failed after retry. "
                    f"First error: {first_err}. Retry error: {retry_err}. "
                    f"Raw response (truncated 500 chars): {raw}"
                ) from retry_err

        result["junior_reason"] = junior_notes or ""

        self._log_escalation(run_id, job, junior_notes, result)

        return result

    def _build_prompt(self, job: dict, junior_notes: Optional[str]) -> str:
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
                "\nExecute this edit. Return JSON:\n"
                "  output (dict): what you produced or would produce — include tool_calls if using tools\n"
                "  confidence (float 0-1): your confidence in correctness\n"
                "  notes (str): your approach and what was tricky\n"
                "  escalate (bool): true only if genuinely unresolvable\n"
                "\nReturn only the JSON object."
            )

        if junior_notes:
            parts.append(f"Junior editor escalation notes:\n{junior_notes}")
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
  - still: hold a static image. Use still (path), estimated_duration_s.

Source files are NEVER modified. The manifest references them by absolute path.
The assembler handles rotation, scaling, concatenation, and encoding.

IMPORTANT for footage edits:
- Use the transcript to identify exact timecodes for cuts
- Each scene references the SOURCE clip directly — no intermediate files
- Rotation is a field on the scene, not a separate step
- For text overlays (title cards, end cards), use type: text_overlay
- For effects (rainbow flash text, drawtext overlays), use type: effect_overlay with an ffmpeg -vf filter string

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
  notes (str): your approach and what was tricky
  escalate (bool): true only if genuinely unresolvable

Return only the JSON object."""

    def _parse_response(self, text: str) -> dict:
        """
        Parse JSON from model response.

        Raises RuntimeError on any parse failure — the caller is responsible
        for retry logic. This replaces the prior silent-fallback behavior
        that caused HoP to assemble-all-clips when the LLM returned prose.
        """
        try:
            clean = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
            data = json.loads(clean)
        except Exception as e:
            raise RuntimeError(
                f"could not parse JSON response ({e}); raw (500 chars): {text[:500]}"
            ) from e

        confidence = float(data.get("confidence", 0.85))
        return {
            "output": data.get("output", {}),
            "confidence": confidence,
            "notes": data.get("notes", ""),
            "escalate": data.get("escalate", False),
            "agent": "senior_editor",
        }

    def _schema_reminder(self, job: dict) -> str:
        """
        Build a call-specific JSON-schema reminder for retrying a failed parse.
        The shape differs per output mode — clip-selection vs manifest vs
        generic tool-calls — so we describe only the schema relevant to this job.
        """
        header = (
            "RESPOND WITH VALID JSON ONLY. No prose before or after. "
            "No markdown code fences. No commentary. A single JSON object matching this schema:"
        )
        if job.get("clip_index_data"):
            schema = (
                '{\n'
                '  "output": {\n'
                '    "selected_clips": "0,2,4-6",   // comma-separated clip indices to keep, or null for all\n'
                '    "notes": "creative reasoning"\n'
                '  },\n'
                '  "confidence": 0.9,\n'
                '  "escalate": false\n'
                '}'
            )
        elif job.get("output_mode") == "manifest" or job.get("type") == "footage_edit":
            schema = (
                '{\n'
                '  "output": { "manifest": { "config": {...}, "scenes": [...] } },\n'
                '  "confidence": 0.9,\n'
                '  "notes": "approach summary",\n'
                '  "escalate": false\n'
                '}'
            )
        else:
            schema = (
                '{\n'
                '  "output": { "tool_calls": [{"tool": "...", "args": {...}}] },\n'
                '  "confidence": 0.9,\n'
                '  "notes": "approach summary",\n'
                '  "escalate": false\n'
                '}'
            )
        return f"{header}\n{schema}"

    def _log_escalation(self, run_id: str, job: dict, junior_notes: Optional[str], result: dict):
        """Log why junior escalated and what senior did."""
        try:
            from core.paths import run_dir
            log_dir = run_dir(run_id)
            log_path = log_dir / "senior_editor.json"
            with open(log_path, "w") as f:
                json.dump(
                    {
                        "job_type": job.get("type"),
                        "junior_escalation_reason": junior_notes,
                        "senior_result": result,
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            print(f"[SeniorEditor] WARNING: could not write run log: {e}")
