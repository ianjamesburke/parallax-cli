"""
StoryboardPlanner agent — takes a script + character description and plans
a scene-by-scene storyboard with image generation prompts.

This is the creative direction agent. It decides WHAT to show and HOW to make
each scene visually dynamic. Then it hands off to the production tools
(generate_still, assemble) for execution.

Uses claude-sonnet-4-6 for creative quality.
"""

import json
import re
import os
from typing import Optional

MODEL = "claude-sonnet-4-6"

SYSTEM = """You are a storyboard director at NRTV, a video production agency.
You receive a script (narration/VO lines) and a character description, and you plan
every scene of the video storyboard.

For each scene you specify:
- index: scene number (1-based)
- vo_text: the exact voiceover line for this scene
- starting_frame: a detailed image generation prompt describing the visual.
  This prompt goes directly to Gemini image generation — be specific about:
  composition, camera angle, lighting, mood, character pose/expression, background.
  Include the character's visual traits so every frame is consistent.
- action: what happens during the scene (camera motion, Ken Burns direction)
- ken_burns: object with {start_scale, end_scale, start_offset_x, start_offset_y,
  end_offset_x, end_offset_y} for camera motion. Most scenes should have subtle motion.
- duration_s: estimated duration based on VO pacing

Key creative principles:
- Every scene must be visually DISTINCT — vary angles, distances, lighting
- Character should be in dynamic situations, not static poses
- First scene (hook) needs the most arresting visual
- Product scenes need clear, well-lit product visibility
- Vary between close-ups, medium shots, and wide shots
- Consider visual flow — each scene should feel like a natural cut from the previous

Return JSON only: {"scenes": [...]}"""


class StoryboardPlanner:
    """
    Plans scene-by-scene storyboard with image generation prompts.

    Input: script (vo_lines), character description, optional brand constraints
    Output: scenes array ready for manifest + still generation
    """

    MODEL = MODEL

    def plan(self, job: dict, run_id: Optional[str] = None) -> dict:
        """
        Plan a storyboard from a script and character description.

        Args:
            job: {
                type: "storyboard",
                content: str,           # the brief / what the video is about
                script: dict,           # output from ScriptWriter (has vo_lines, scenes)
                character: str,         # character description or ref image path
                brand_file: str,        # optional brand constraints
                concept_id: str,
                run_id: str,
                test_mode: bool,
            }
            run_id: for cost logging

        Returns:
            {scenes: list, confidence: float, notes: str, agent: str}
        """
        if os.environ.get("TEST_MODE", "false").lower() == "true":
            if job.get("_mock_response"):
                print("[StoryboardPlanner] DRILL — using mock response")
                return job["_mock_response"]
            print("[StoryboardPlanner] DRILL — returning stub storyboard")
            vo_lines = job.get("script", {}).get("vo_lines", [
                "Hook line one.", "Product benefit.", "Call to action."
            ])
            # Honour reference_scene_count if provided — distribute VO lines across N scenes
            target_count = job.get("reference_scene_count") or len(vo_lines)
            scenes = []
            for i in range(target_count):
                line = vo_lines[i % len(vo_lines)]
                scenes.append({
                    "index": i + 1,
                    "vo_text": line if i < len(vo_lines) else "",
                    "starting_frame": f"[DRILL] Scene {i+1}: dynamic shot",
                    "action": "Ken Burns slow zoom in",
                    "ken_burns": {"start_scale": 1.0, "end_scale": 1.15,
                                  "start_offset_x": 0, "start_offset_y": 0,
                                  "end_offset_x": 0, "end_offset_y": 0},
                    "duration_s": max(2.0, len(line.split()) / 3.0),
                })
            return {
                "scenes": scenes,
                "confidence": 0.88,
                "notes": f"[DRILL] Storyboard planned — {target_count} scenes",
                "agent": "storyboard_planner",
            }

        from core.agent_loop import run_with_tools
        run_id = run_id or job.get("run_id", "unknown")

        prompt = self._build_prompt(job)

        # Storyboard is a planning agent — no tool use in the loop today.
        # run_with_tools falls back to llm_complete when tool_names=[].
        # Add tools here in the future (e.g., plan_scenes_for_agent) as needs evolve.
        try:
            response = run_with_tools(
                model=self.MODEL,
                system=SYSTEM,
                prompt=prompt,
                tool_names=[],
                max_tokens=16384,
                cost_context={
                    "concept_id": job.get("concept_id", "UNKNOWN"),
                    "agent": "storyboard_planner",
                    "run_id": run_id,
                },
            )
        except Exception as e:
            raise RuntimeError(f"[StoryboardPlanner] API call failed: {e}") from e

        result = self._parse_response(response["text"])

        self._log_plan(run_id, job, result)

        return result

    def _build_prompt(self, job: dict) -> str:
        parts = []

        # Brief context
        parts.append(f"Video brief:\n{job.get('content', '')}")

        # Script / VO lines
        script_data = job.get("script", {})
        if script_data.get("vo_lines"):
            parts.append("Voiceover lines (in order):")
            for i, line in enumerate(script_data["vo_lines"], 1):
                parts.append(f"  {i}. {line}")
        elif script_data.get("script"):
            parts.append(f"Full script:\n{script_data['script']}")

        # Character description
        character = job.get("character", "")
        if character:
            parts.append(f"Character description:\n{character}")

        # Brand constraints
        if job.get("brand_file"):
            from pathlib import Path
            try:
                brand_text = Path(job["brand_file"]).read_text()
                parts.append(f"Brand guidelines:\n{brand_text}")
            except Exception:
                pass

        # Reference video structure — mirror shot count if provided
        ref_scene_count = job.get("reference_scene_count")
        ref_cut_times = job.get("reference_cut_times", [])
        if ref_scene_count:
            cut_desc = (
                f"at {', '.join(f'{t:.1f}s' for t in ref_cut_times[:10])}"
                if ref_cut_times else ""
            )
            parts.append(
                f"REFERENCE VIDEO STRUCTURE: The original video has approximately "
                f"{ref_scene_count} scenes (cuts detected {cut_desc}). "
                f"Plan EXACTLY {ref_scene_count} scenes to mirror the original's pacing. "
                f"Distribute the VO lines across those scenes — some scenes may share a "
                f"VO line if the original had rapid cuts. Vary framing to match the "
                f"original's rhythm: close-ups where cuts were fast, wider shots where "
                f"they were slow."
            )

        parts.append(
            "\nPlan the storyboard. For each scene create a detailed image generation "
            "prompt (starting_frame). Return JSON: {\"scenes\": [...]}\n"
            "Each scene: {index, vo_text, starting_frame, action, ken_burns, duration_s}\n"
            "Return only the JSON object."
        )

        return "\n\n".join(parts)

    def _parse_response(self, text: str) -> dict:
        # Strategy 1: Strip markdown fences and parse
        try:
            clean = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
            data = json.loads(clean)
            scenes = data.get("scenes", [])
            if scenes:
                return {
                    "scenes": scenes,
                    "confidence": 0.85,
                    "notes": f"Planned {len(scenes)} scenes",
                    "agent": "storyboard_planner",
                }
        except Exception:
            pass

        # Strategy 2: Brace-matching — find the outermost {...} containing "scenes"
        try:
            start = text.index("{")
            depth = 0
            end = start
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            candidate = text[start:end]
            data = json.loads(candidate)
            scenes = data.get("scenes", [])
            if scenes:
                return {
                    "scenes": scenes,
                    "confidence": 0.85,
                    "notes": f"Planned {len(scenes)} scenes (extracted via brace-match)",
                    "agent": "storyboard_planner",
                }
        except Exception:
            pass

        print(f"[StoryboardPlanner] WARNING: could not parse response")
        print(f"[StoryboardPlanner] Raw (first 500 chars): {text[:500]}")
        return {
            "scenes": [],
            "confidence": 0.3,
            "notes": f"Parse error — all extraction strategies failed\nRaw: {text[:200]}",
            "agent": "storyboard_planner",
        }

    def _log_plan(self, run_id: str, job: dict, result: dict):
        try:
            from core.paths import run_dir
            log_dir = run_dir(run_id)
            log_path = log_dir / "storyboard_planner.json"
            with open(log_path, "w") as f:
                json.dump({
                    "job_type": job.get("type"),
                    "vo_lines_count": len(job.get("script", {}).get("vo_lines", [])),
                    "scenes_planned": len(result.get("scenes", [])),
                    "result": result,
                }, f, indent=2)
        except Exception as e:
            print(f"[StoryboardPlanner] WARNING: could not write run log: {e}")
