"""
Evaluator — quality gate before surfacing output to the human.

Inspects actual output files (audio, video, stills) and scores against the
original brief using an LLM. Named concerns are emitted so the human knows
exactly who let what pass.

Uses core.llm (claude CLI backend — no API key needed).
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Optional  # noqa: F401 — used in nested fn


MODEL = "claude-sonnet-4-6"

SYSTEM = """You are the QA evaluator at NRTV video production. You receive a job
brief and a structured report of what was actually produced. Your job is to score
it ruthlessly against the brief.

You check:
1. Did the output match what was asked for? (deliverable type, format, content)
2. Does audio exist if the brief implied VO or dialogue?
3. Does the character/subject match the brief?
4. Are there obvious technical failures (file missing, zero duration, no scenes)?
5. Would a client watching this be confused about what they ordered?

Name every failure specifically. Do not hedge. If audio is missing and the brief
asked for a talking character, that is a hard fail.

Return JSON only:
{
  "approved": bool,
  "score": float (0-1),
  "issues": ["specific issue 1", "specific issue 2"],
  "responsible": ["agent_name: reason they failed"],
  "recommendation": "Approve" or "Revise — <one sentence>"
}"""


def _probe_file(path: str) -> dict:
    """Run ffprobe on a file. Returns stream info or error."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return {"error": r.stderr[:200]}
        return json.loads(r.stdout)
    except Exception as e:
        return {"error": str(e)}


def _parse_selected_clips(selected: str) -> list:
    """Parse a clip-selection string like '0,2,4-6' into a sorted list of ints."""
    out = []
    if not selected:
        return out
    for part in str(selected).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                out.extend(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return sorted(set(out))


def _inspect_footage_edit(job: dict, result: dict) -> dict:
    """
    Inspect footage_edit output. This rubric checks clip-assembly correctness:
      - Output file exists
      - Output duration matches sum(selected clip durations) within +/- 5%
      - Number of clips in assembly matches what the editor selected

    It deliberately does NOT reference scene_count, draft_success, or
    stills_success — those belong to the stills pipeline and would cause
    false "Revise" verdicts here.
    """
    report = {"job_type": "footage_edit"}

    # Find the output file — for footage_edit, HoP writes it to result["assembly"]
    assembly = result.get("assembly", {}) or {}
    output_path = (
        result.get("output_path")
        or assembly.get("output_path")
    )

    if not output_path:
        stdout = assembly.get("stdout", "")
        for line in stdout.splitlines():
            if line.startswith("Output:"):
                output_path = line.split("Output:")[-1].strip().split(" ")[0]
                break

    report["output_file"] = output_path
    actual_duration = 0.0
    actual_has_audio = False
    actual_resolution = None
    output_size_mb = None

    if output_path:
        p = Path(output_path)
        report["output_exists"] = p.exists()
        if p.exists():
            output_size_mb = round(p.stat().st_size / 1e6, 1)
            probe = _probe_file(str(p))
            streams = probe.get("streams", [])
            video_streams = [s for s in streams if s.get("codec_type") == "video"]
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            actual_has_audio = bool(audio_streams)
            report["has_video"] = bool(video_streams)
            report["has_audio"] = actual_has_audio
            fmt = probe.get("format", {})
            actual_duration = round(float(fmt.get("duration", 0)), 2)
            report["duration_s"] = actual_duration
            if video_streams:
                v = video_streams[0]
                actual_resolution = f"{v.get('width')}x{v.get('height')}"
                report["resolution"] = actual_resolution
    else:
        report["output_exists"] = False

    report["output_size_mb"] = output_size_mb

    # Compute expected clip set + duration from the editor's selection
    clip_index_data = job.get("clip_index_data", []) or []
    selected_clips_str = (result.get("output", {}) or {}).get("selected_clips")

    # Flatten all clips across manifests into a global-indexed list mirroring
    # the senior_editor prompt, which concatenates clips in manifest order.
    flat_clips = []
    for cd in clip_index_data:
        for c in cd.get("clips", []) or []:
            flat_clips.append(c)

    total_available = len(flat_clips)
    if selected_clips_str:
        indices = _parse_selected_clips(selected_clips_str)
        # Clamp to available range
        indices = [i for i in indices if 0 <= i < total_available]
        expected_clips = [flat_clips[i] for i in indices]
    else:
        # No filter = all clips kept
        expected_clips = flat_clips

    expected_count = len(expected_clips)
    expected_duration = round(sum(float(c.get("duration", 0)) for c in expected_clips), 2)

    report["expected_clip_count"] = expected_count
    report["expected_duration_s"] = expected_duration
    report["selected_clips_spec"] = selected_clips_str or "(all)"

    # Duration tolerance: +/- 5% OR +/- 0.5s (whichever is larger — short
    # edits need an absolute floor since 5% of 2s is unrealistic).
    tolerance = max(expected_duration * 0.05, 0.5)
    if expected_duration > 0 and actual_duration > 0:
        duration_delta = abs(actual_duration - expected_duration)
        report["duration_delta_s"] = round(duration_delta, 2)
        report["duration_within_tolerance"] = duration_delta <= tolerance
    else:
        report["duration_delta_s"] = None
        report["duration_within_tolerance"] = None

    # Clip count check: we can't cheaply count scenes inside the rendered file,
    # so we trust the assembly success flag + selected count as the source of
    # truth. Surface the selected count so the LLM can reason about it.
    report["assembly_success"] = assembly.get("success")
    report["assembly_stderr"] = (assembly.get("stderr") or "")[:300]

    return report


def _inspect_output(result: dict) -> dict:
    """
    Inspect actual output files and return a factual report.
    This is the ground truth the LLM evaluates against.

    Used by the stills pipeline rubric. Footage_edit jobs use
    _inspect_footage_edit() instead.
    """
    report = {}

    # Find the primary output file — check explicit path, then parse stdout for "Output: ..."
    def _extract_path_from_stdout(sub: dict) -> Optional[str]:
        stdout = sub.get("stdout", "")
        for line in stdout.splitlines():
            if line.startswith("Output:"):
                return line.split("Output:")[-1].strip().split(" ")[0]
        return None

    output_path = (
        result.get("output_path")
        or result.get("draft", {}).get("output_path")
        or result.get("assembly", {}).get("output_path")
        or _extract_path_from_stdout(result.get("draft", {}))
        or _extract_path_from_stdout(result.get("assembly", {}))
    )

    if output_path:
        p = Path(output_path)
        report["output_file"] = str(output_path)
        report["output_exists"] = p.exists()
        if p.exists():
            report["output_size_mb"] = round(p.stat().st_size / 1e6, 1)
            probe = _probe_file(str(p))
            streams = probe.get("streams", [])
            video_streams = [s for s in streams if s.get("codec_type") == "video"]
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            report["has_video"] = bool(video_streams)
            report["has_audio"] = bool(audio_streams)
            fmt = probe.get("format", {})
            report["duration_s"] = round(float(fmt.get("duration", 0)), 1)
            if video_streams:
                v = video_streams[0]
                report["resolution"] = f"{v.get('width')}x{v.get('height')}"
        else:
            report["output_exists"] = False
    else:
        report["output_file"] = None
        report["output_exists"] = False

    # Stills
    stills = result.get("stills", {})
    report["stills_success"] = stills.get("success", False) if stills else None

    # Scenes planned
    scenes = result.get("scenes", [])
    report["scene_count"] = len(scenes)

    # Draft / assembly success flags
    draft = result.get("draft", {})
    assembly = result.get("assembly", {})
    report["draft_success"] = draft.get("success") if draft else None
    # assembly_success: check both "assembly" and fall back to "draft" if assembly is absent
    if assembly:
        report["assembly_success"] = assembly.get("success")
    elif draft:
        report["assembly_success"] = draft.get("success")
    else:
        report["assembly_success"] = None

    # Any stderr errors surfaced
    errors = []
    for key in ("draft", "assembly", "stills", "voiceover", "lipsync", "render"):
        sub = result.get(key, {})
        if sub and not sub.get("success") and sub.get("stderr"):
            errors.append(f"{key}: {sub['stderr'][:150]}")
    report["errors"] = errors

    return report


class Evaluator:
    """
    Reviews production output for brief compliance and technical completeness.
    Uses core.llm so no API key is needed (falls back to claude CLI).
    """

    MODEL = MODEL

    def evaluate(self, job: dict, result: dict,
                 prior_version: Optional[dict] = None) -> dict:
        """
        Evaluate output against the brief. Inspects actual files first,
        then LLM-scores the inspection report against the brief.

        Returns:
            {approved, score, issues, responsible, recommendation, inspection, agent}
        """
        from core.llm import complete as llm_complete
        from core.cost_tracker import log_call

        run_id = job.get("run_id", "unknown")
        concept_id = job.get("concept_id", "UNKNOWN")

        # Dispatch rubric by job type. footage_edit jobs go through a
        # clip-assembly rubric; everything else uses the stills-pipeline rubric.
        job_type = job.get("type")
        is_footage_edit = job_type == "footage_edit"

        # Step 1: Factual inspection — what was actually produced
        if is_footage_edit:
            inspection = _inspect_footage_edit(job, result)
        else:
            inspection = _inspect_output(result)

        # Step 2: LLM scores the inspection against the brief
        if is_footage_edit:
            prompt = self._build_prompt_footage_edit(job, inspection, prior_version)
        else:
            prompt = self._build_prompt(job, inspection, prior_version)

        try:
            response = llm_complete(
                model=self.MODEL,
                system=SYSTEM,
                prompt=prompt,
                max_tokens=1024,
            )
        except Exception as e:
            print(f"[Evaluator] WARNING: LLM call failed ({e}) — using inspection-only score")
            if is_footage_edit:
                return self._inspection_only_score_footage_edit(inspection)
            return self._inspection_only_score(inspection)

        try:
            log_call(
                concept_id=concept_id,
                agent="evaluator",
                run_id=run_id,
                input_tokens=response["input_tokens"],
                output_tokens=response["output_tokens"],
                model=self.MODEL,
            )
        except Exception as e:
            print(f"[Evaluator] WARNING: cost logging failed: {e}")

        eval_result = self._parse_response(response["text"])
        eval_result["inspection"] = inspection

        # Always surface to human
        self._surface(eval_result)

        return eval_result

    def _build_prompt(self, job: dict, inspection: dict,
                      prior_version: Optional[dict]) -> str:
        import os
        test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"
        parts = [
            f"Job type: {job.get('type')}",
            f"Brief: {job.get('manifest_brief') or job.get('content', '')}",
            f"Deliverable requested: {job.get('deliverable', 'not specified')}",
            f"Audio source provided: {'yes' if job.get('audio_source') else 'no'}",
            f"\nWhat was actually produced:\n{json.dumps(inspection, indent=2)}",
        ]
        if test_mode:
            parts.append(
                "\nNOTE: This is a TEST_MODE (DRILL) run. Placeholder black frames are "
                "expected instead of real visuals — do NOT penalize for black video, small "
                "file size, or synthetic audio. Only check structural completeness:\n"
                "  - Output file exists on disk\n"
                "  - Audio track present (if VO or audio source was provided)\n"
                "  - Scene count ≥ 1 in the storyboard/manifest data (scene_count in the "
                "inspection report may show 0 if scenes are in the manifest but not in "
                "the video container metadata — this is expected in TEST_MODE)\n"
                "  - Duration > 0\n"
                "  - assembly_success or draft_success is true\n"
                "For footage_edit jobs in TEST_MODE: the assembled video is a synthetic "
                "black placeholder — it will NOT contain real source footage or source audio. "
                "A silent audio track is included as a structural marker. Do NOT penalize "
                "for content not matching source clips.\n"
                "A TEST_MODE run that passes these structural checks should score ≥ 0.85 "
                "and be Approved."
            )
        if prior_version:
            parts.append(f"Prior version:\n{json.dumps(prior_version, indent=2)}")
        parts.append(
            "\nScore this against the brief. Be specific about failures. "
            "Return JSON only."
        )
        return "\n\n".join(parts)

    def _build_prompt_footage_edit(self, job: dict, inspection: dict,
                                    prior_version: Optional[dict]) -> str:
        """
        Build the evaluator prompt for footage_edit jobs.

        Unlike the stills-pipeline prompt, this rubric does NOT reference
        scene_count, draft_success, or stills_success — those fields don't
        exist for clip assemblies and caused spurious low scores.
        """
        import os
        test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"
        parts = [
            f"Job type: {job.get('type')}",
            f"Brief: {job.get('manifest_brief') or job.get('content', '')}",
            f"\nWhat was actually produced (footage_edit inspection):\n"
            f"{json.dumps(inspection, indent=2)}",
        ]
        parts.append(
            "\nRubric for footage_edit jobs — score ONLY against these criteria:\n"
            "  1. output_exists must be true — the assembled file must be on disk.\n"
            "  2. duration_within_tolerance must be true — actual duration must be "
            "     within +/- 5% (or +/- 0.5s floor) of expected_duration_s, which is "
            "     the sum of the durations of the clips the editor selected.\n"
            "  3. assembly_success must be true.\n"
            "  4. expected_clip_count > 0 — the editor must have selected at least "
            "     one clip to keep.\n"
            "\nThis is a raw clip edit. The only deliverable is the assembled mp4. "
            "Use ONLY the fields in the inspection report above. Do not invent fields "
            "or apply rubrics from the stills pipeline."
        )
        if test_mode:
            parts.append(
                "\nNOTE: This is TEST_MODE (DRILL). The assembled video is a synthetic "
                "placeholder generated by a stub assembler — its duration, size, and "
                "visual content will NOT match what a real assembly would produce. "
                "IGNORE duration_within_tolerance, output_size_mb, and any visual "
                "quality concerns. Only check that output_exists is true and "
                "assembly_success is true. A TEST_MODE run that meets those two "
                "checks should score >= 0.85 and be Approved."
            )
        if prior_version:
            parts.append(f"\nPrior version:\n{json.dumps(prior_version, indent=2)}")
        parts.append("\nScore this against the rubric. Return JSON only.")
        return "\n\n".join(parts)

    def _inspection_only_score(self, inspection: dict) -> dict:
        """Fallback score when LLM is unavailable — pure file inspection."""
        issues = []
        if not inspection.get("output_exists"):
            issues.append("Output file does not exist")
        if inspection.get("output_exists") and not inspection.get("has_audio"):
            issues.append("Output video has no audio track")
        if not inspection.get("stills_success") and inspection.get("stills_success") is not None:
            issues.append("Still generation failed")

        score = max(0.0, 1.0 - len(issues) * 0.3)
        return {
            "approved": not issues,
            "score": score,
            "issues": issues,
            "responsible": [],
            "recommendation": "Approve" if not issues else f"Revise — {issues[0]}",
            "agent": "evaluator",
        }

    def _inspection_only_score_footage_edit(self, inspection: dict) -> dict:
        """
        Fallback score for footage_edit when LLM is unavailable.
        Checks clip-assembly correctness only — never references stills fields.
        """
        issues = []
        if not inspection.get("output_exists"):
            issues.append("Output file does not exist")
        if inspection.get("output_exists") and inspection.get("assembly_success") is False:
            issues.append("Assembly reported failure")
        if inspection.get("expected_clip_count", 0) == 0:
            issues.append("Editor selected zero clips to keep")
        within = inspection.get("duration_within_tolerance")
        if within is False:
            delta = inspection.get("duration_delta_s")
            expected = inspection.get("expected_duration_s")
            issues.append(
                f"Output duration differs from expected clip sum by {delta}s "
                f"(expected ~{expected}s)"
            )

        score = max(0.0, 1.0 - len(issues) * 0.3)
        return {
            "approved": not issues,
            "score": score,
            "issues": issues,
            "responsible": [],
            "recommendation": "Approve" if not issues else f"Revise — {issues[0]}",
            "agent": "evaluator",
        }

    def _parse_response(self, text: str) -> dict:
        try:
            clean = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
            data = json.loads(clean)
            return {
                "approved": bool(data.get("approved", False)),
                "score": float(data.get("score", 0.5)),
                "issues": data.get("issues", []),
                "responsible": data.get("responsible", []),
                "recommendation": data.get("recommendation", ""),
                "agent": "evaluator",
            }
        except Exception as e:
            print(f"[Evaluator] WARNING: parse failed ({e})")
            return {
                "approved": False,
                "score": 0.0,
                "issues": [f"Evaluator parse error: {e}. Raw: {text[:100]}"],
                "responsible": ["evaluator: parse failure"],
                "recommendation": "Revise — evaluation failed",
                "agent": "evaluator",
            }

    def _surface(self, eval_result: dict):
        """Print evaluation report to the human producer."""
        print("\n" + "=" * 50)
        print("[Evaluator → You] QA Report")
        print("=" * 50)
        print(f"Score:  {eval_result['score']:.0%}  |  {eval_result['recommendation']}")
        insp = eval_result.get("inspection", {})
        print(f"Output: {'✓ exists' if insp.get('output_exists') else '✗ MISSING'}  "
              f"| audio={'✓' if insp.get('has_audio') else '✗ MISSING'}  "
              f"| {insp.get('duration_s', '?')}s  "
              f"| {insp.get('resolution', '?')}")
        if eval_result["issues"]:
            print("Issues:")
            for issue in eval_result["issues"]:
                print(f"  • {issue}")
        if eval_result.get("responsible"):
            print("Responsible:")
            for r in eval_result["responsible"]:
                print(f"  → {r}")
        print()
