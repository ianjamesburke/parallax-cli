"""
Production Skill Tools
======================
Thin wrappers around the bundled video-production scripts at
packs/video/scripts/*.py that Parallax agents can call as subprocess tools.

Each tool:
1. Validates inputs
2. Builds the subprocess command
3. Runs it and captures output
4. Returns structured result {success, output_path, stdout, stderr}

In TEST_MODE, tools return stubs without invoking real scripts.
"""

import os
import json
import subprocess
from pathlib import Path
from typing import Optional


# Bundled scripts live alongside this file. Parallax is self-contained —
# no external skill dependency.
SKILL_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SKILL_DIR / "scripts"

TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"


def _run(cmd: list[str], timeout: int = 300) -> dict:
    """Run a subprocess and return structured result."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "returncode": -1, "stdout": "", "stderr": "Timed out"}
    except Exception as e:
        return {"success": False, "returncode": -1, "stdout": "", "stderr": str(e)}


def _script(name: str) -> str:
    """Resolve a script path."""
    return str(SCRIPTS_DIR / name)


# ── Scene Planning ───────────────────────────────────────────────────────────

def plan_scenes(manifest_path: str, wpm: int = 180, force: bool = False) -> dict:
    """
    Break a script into scenes with visual descriptions and timing.
    Calls plan-scenes.py which uses Gemini for scene planning.

    Args:
        manifest_path: path to project manifest.yaml
        wpm: words per minute estimate
        force: re-plan even if scenes already exist

    Returns:
        {success, scenes_count, stdout, stderr}
    """
    if TEST_MODE:
        return {
            "success": True,
            "scenes_count": 5,
            "stdout": "[DRILL] Planned 5 scenes",
            "stderr": "",
            "tool": "plan_scenes",
        }

    cmd = ["python3", _script("plan-scenes.py"), "--manifest", manifest_path, "--wpm", str(wpm)]
    if force:
        cmd.append("--force")
    result = _run(cmd)
    result["tool"] = "plan_scenes"
    return result


def plan_scenes_for_agent(manifest_path: str) -> dict:
    """
    Get the scene planning prompt without calling Gemini — lets a Parallax agent
    (like StoryboardPlanner) do the creative planning instead.

    Returns the prompt text that would be sent to Gemini.
    """
    if TEST_MODE:
        return {
            "success": True,
            "prompt": "[DRILL] Scene planning prompt for storyboard",
            "tool": "plan_scenes_for_agent",
        }

    cmd = ["python3", _script("plan-scenes.py"), "--manifest", manifest_path, "--print-prompt"]
    result = _run(cmd)
    result["tool"] = "plan_scenes_for_agent"
    if result["success"]:
        result["prompt"] = result["stdout"]
    return result


def ingest_agent_scenes(manifest_path: str, scenes_json_path: str) -> dict:
    """
    Write agent-planned scenes into the manifest (bypasses Gemini).
    Calls plan-scenes.py --from-json.
    """
    if TEST_MODE:
        return {
            "success": True,
            "stdout": "[DRILL] Ingested agent scenes into manifest",
            "stderr": "",
            "tool": "ingest_agent_scenes",
        }

    cmd = ["python3", _script("plan-scenes.py"), "--manifest", manifest_path, "--from-json", scenes_json_path]
    return {**_run(cmd), "tool": "ingest_agent_scenes"}


# ── Image Generation ─────────────────────────────────────────────────────────

def generate_still(manifest_path: str, scene: str, ref_image: Optional[str] = None,
                   chain: bool = False, parallel: bool = False,
                   variants: Optional[int] = None) -> dict:
    """
    Generate scene still(s) via Gemini image generation.

    Args:
        manifest_path: path to manifest
        scene: scene index or range ("1" or "1-5")
        ref_image: optional reference image path
        chain: sequential generation with cross-scene context
        parallel: fully concurrent, no cross-scene context
        variants: generate N variants of the scene
    """
    if TEST_MODE:
        try:
            import yaml
            from PIL import Image, ImageDraw, ImageFont
            manifest = yaml.safe_load(open(manifest_path))
            project_dir = Path(manifest_path).parent
            # Parse scene range
            if "-" in str(scene):
                start, end = str(scene).split("-")
                scene_indices = list(range(int(start), int(end) + 1))
            else:
                scene_indices = [int(scene)]
            scenes_data = {s["index"]: s for s in manifest.get("scenes", [])}
            generated = []
            for idx in scene_indices:
                out_path = project_dir / f"scene_{idx:03d}.png"
                img = Image.new("RGB", (1080, 1920), color=(30, 30, 60))
                draw = ImageDraw.Draw(img)
                draw.rectangle([40, 40, 1040, 1880], outline=(100, 100, 180), width=4)
                label = f"SCENE {idx}"
                draw.text((540, 800), label, fill=(200, 200, 255), anchor="mm")
                scene_info = scenes_data.get(idx, {})
                vo = scene_info.get("vo_text", "")[:80]
                draw.text((540, 960), vo, fill=(180, 180, 180), anchor="mm")
                img.save(str(out_path))
                generated.append(str(out_path))
            return {
                "success": True,
                "stdout": f"[DRILL] Placeholder stills: {', '.join(generated)}",
                "stderr": "",
                "tool": "generate_still",
            }
        except Exception as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"[DRILL] Placeholder generation failed: {e}",
                "tool": "generate_still",
            }

    cmd = ["python3", _script("generate-still.py"), "--manifest", manifest_path, "--scene", str(scene)]
    if ref_image:
        cmd.extend(["--ref-image", ref_image])
    if chain:
        cmd.append("--chain")
    if parallel:
        cmd.append("--parallel")
    if variants:
        cmd.extend(["--variants", str(variants)])
    return {**_run(cmd, timeout=600), "tool": "generate_still"}


def generate_char_ref(input_images: list[str], output_path: str,
                      describe: Optional[str] = None,
                      style: Optional[str] = None) -> dict:
    """
    Generate a 4-panel character reference sheet from inspiration images.
    """
    if TEST_MODE:
        return {
            "success": True,
            "output_path": output_path,
            "stdout": f"[DRILL] Generated character reference → {output_path}",
            "stderr": "",
            "tool": "generate_char_ref",
        }

    cmd = ["python3", _script("generate-char-ref.py")]
    for img in input_images:
        cmd.extend(["--input", img])
    cmd.extend(["--output", output_path])
    if describe:
        cmd.extend(["--describe", describe])
    if style:
        cmd.extend(["--style", style])
    return {**_run(cmd, timeout=600), "tool": "generate_char_ref"}


# ── Assembly & Rendering ─────────────────────────────────────────────────────

def assemble(manifest_path: str, output: Optional[str] = None,
             draft: bool = False, animatic: bool = False,
             scenes: Optional[str] = None, no_audio: bool = False) -> dict:
    """
    Assemble video from manifest scenes + voiceover.

    Args:
        manifest_path: path to manifest
        output: output filename (auto-named if not set)
        draft: all scenes as Ken Burns (fast pacing review)
        animatic: text-only wireframe (no images)
        scenes: scene range '1-5' or '1,3,7'
        no_audio: skip VO mix
    """
    if TEST_MODE:
        import subprocess as _sub, json as _json, yaml as _yaml
        manifest = _yaml.safe_load(open(manifest_path))
        project_dir = Path(manifest_path).parent
        out_name = output or f"{manifest.get('project', {}).get('id', 'draft')}_v0.0.1_draft.mp4"
        out_path = project_dir / "output" / out_name if not Path(out_name).is_absolute() else Path(out_name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Build a real short black video so evaluator can ffprobe it
        vo_audio = manifest.get("voiceover", {}).get("audio_file")
        scenes = manifest.get("scenes", [])
        duration = sum(s.get("duration_s", 2.0) for s in scenes) or 4.0
        cmd = ["ffmpeg", "-y"]
        if vo_audio and Path(vo_audio).exists():
            cmd += ["-i", vo_audio]
            cmd += ["-f", "lavfi", "-i", f"color=black:size=1080x1920:duration={duration}:rate=30"]
            cmd += ["-map", "1:v", "-map", "0:a", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(out_path)]
        else:
            cmd += ["-f", "lavfi", "-i", f"color=black:size=1080x1920:duration={duration}:rate=30"]
            cmd += ["-c:v", "libx264", str(out_path)]
        r = _sub.run(cmd, capture_output=True)
        success = r.returncode == 0
        return {
            "success": success,
            "output_path": str(out_path),
            "stdout": f"Output: {out_path} ({round(out_path.stat().st_size / 1e6, 1)}MB)" if success and out_path.exists() else "[DRILL] assemble failed",
            "stderr": r.stderr.decode()[:200] if not success else "",
            "tool": "assemble",
        }

    cmd = ["python3", _script("assemble.py"), "--manifest", manifest_path]
    if output:
        cmd.extend(["--output", output])
    if draft:
        cmd.append("--draft")
    if animatic:
        cmd.append("--animatic")
    if scenes:
        cmd.extend(["--scenes", scenes])
    if no_audio:
        cmd.append("--no-audio")
    return {**_run(cmd, timeout=600), "tool": "assemble"}


def burn_captions(manifest_path: str, video: str, output: Optional[str] = None,
                  block_bg: bool = False, safe_bottom: Optional[int] = None) -> dict:
    """
    Burn word-by-word captions onto assembled video.
    """
    if TEST_MODE:
        return {
            "success": True,
            "output_path": output or "[DRILL] auto-named",
            "stdout": "[DRILL] Burned captions",
            "stderr": "",
            "tool": "burn_captions",
        }

    cmd = ["python3", _script("burn-captions.py"), "--manifest", manifest_path, "--video", video]
    if output:
        cmd.extend(["--output", output])
    if block_bg:
        cmd.append("--block-bg")
    if safe_bottom:
        cmd.extend(["--safe-bottom", str(safe_bottom)])
    return {**_run(cmd, timeout=600), "tool": "burn_captions"}


def burn_overlay(input_path: str, output_path: str, text: str,
                 position: str = "lower-third",
                 fontcolor: str = "black",
                 stroke_color: str = "white",
                 stroke_width: int = 3,
                 fontsize: int = 42,
                 font: str = "Helvetica",
                 start: float = 0.0,
                 end: Optional[float] = None,
                 font_file: Optional[str] = None) -> dict:
    """
    Burn a single persistent text overlay onto a video (lower-third, brand tag,
    brief-requested label). Unlike burn_captions (word-level from a VO manifest)
    this draws one arbitrary string across the whole video or a time window.
    """
    if TEST_MODE:
        return {
            "success": True,
            "output_path": output_path,
            "stdout": f"[DRILL] Burned overlay '{text}' → {output_path}",
            "stderr": "",
            "tool": "burn_overlay",
        }

    cmd = [
        "python3", _script("burn-overlay.py"),
        "--input", input_path,
        "--output", output_path,
        "--text", text,
        "--position", position,
        "--fontcolor", fontcolor,
        "--stroke-color", stroke_color,
        "--stroke-width", str(stroke_width),
        "--fontsize", str(fontsize),
        "--font", font,
        "--start", str(start),
    ]
    if end is not None:
        cmd.extend(["--end", str(end)])
    if font_file:
        cmd.extend(["--font-file", font_file])
    result = _run(cmd, timeout=600)
    if result.get("success"):
        result["output_path"] = output_path
    result["tool"] = "burn_overlay"
    return result


def render_animation(template: str, output: str, duration: float = 0,
                     mode: str = "video", params: Optional[dict] = None,
                     width: int = 1080, height: int = 1920,
                     params_file: Optional[str] = None) -> dict:
    """
    Render HTML animation template to mp4/PNG via Playwright.
    """
    if TEST_MODE:
        return {
            "success": True,
            "output_path": output,
            "stdout": f"[DRILL] Rendered animation {template} → {output}",
            "stderr": "",
            "tool": "render_animation",
        }

    cmd = [
        "python3", _script("render-animation.py"),
        "--template", template,
        "--output", output,
        "--duration", str(duration),
        "--mode", mode,
        "--width", str(width),
        "--height", str(height),
    ]
    if params:
        cmd.extend(["--params", json.dumps(params)])
    if params_file:
        cmd.extend(["--params-file", params_file])
    return {**_run(cmd, timeout=600), "tool": "render_animation"}


# ── Audio ────────────────────────────────────────────────────────────────────

def generate_voiceover(manifest_path: str, voice: Optional[str] = None) -> dict:
    """
    Generate voiceover from script in manifest via ElevenLabs.
    """
    if TEST_MODE:
        return {
            "success": True,
            "stdout": "[DRILL] Generated voiceover",
            "stderr": "",
            "tool": "generate_voiceover",
        }

    cmd = ["python3", _script("generate-voiceover.py"), "--manifest", manifest_path]
    if voice:
        cmd.extend(["--voice", voice])
    return {**_run(cmd, timeout=300), "tool": "generate_voiceover"}


def align_scenes(manifest_path: str) -> dict:
    """
    Replace estimated scene timing with actual VO word timestamps.
    Must run after generate-voiceover.
    """
    if TEST_MODE:
        return {
            "success": True,
            "stdout": "[DRILL] Aligned scenes to VO timing",
            "stderr": "",
            "tool": "align_scenes",
        }

    cmd = ["python3", _script("align-scenes.py"), "--manifest", manifest_path]
    return {**_run(cmd), "tool": "align_scenes"}


# ── Audio Processing ──────────────────────────────────────────────────────────

def trim_silence(manifest: str, min_silence: Optional[float] = None,
                 pad: Optional[float] = None, threshold: Optional[int] = None,
                 dry_run: bool = False) -> dict:
    """Trim silence gaps in voiceover while preserving word timestamps.

    Args:
        manifest: path to project manifest JSON (required)
        min_silence: minimum silence duration to trim (seconds)
        pad: padding to preserve on each side (seconds)
        threshold: silence threshold (dB)
        dry_run: show trims without modifying files
    """
    if TEST_MODE:
        return {"success": True, "stdout": "[DRILL] Trimmed silence", "stderr": "", "tool": "trim_silence"}
    cmd = ["python3", _script("trim-silence.py"), "--manifest", manifest]
    if min_silence is not None:
        cmd.extend(["--min-silence", str(min_silence)])
    if pad is not None:
        cmd.extend(["--pad", str(pad)])
    if threshold is not None:
        cmd.extend(["--threshold", str(threshold)])
    if dry_run:
        cmd.append("--dry-run")
    return {**_run(cmd), "tool": "trim_silence"}


def normalize_audio(input_path: str, output_path: Optional[str] = None) -> dict:
    """Normalize audio loudness (EBU R128, -14 LUFS target)."""
    if TEST_MODE:
        return {"success": True, "stdout": "[DRILL] Normalized audio", "stderr": "", "tool": "normalize_audio"}
    cmd = ["python3", _script("normalize-audio.py"), input_path]
    if output_path:
        cmd.extend(["--output", output_path])
    return {**_run(cmd), "tool": "normalize_audio"}


def music_duck(video: str, music: str, output: str) -> dict:
    """Sidechain compression — music ducks under speech."""
    if TEST_MODE:
        return {"success": True, "stdout": "[DRILL] Applied music ducking", "stderr": "", "tool": "music_duck"}
    cmd = ["python3", _script("apply-music-duck.py"), "--video", video, "--music", music, "--output", output]
    return {**_run(cmd, timeout=300), "tool": "music_duck"}


# ── Footage Intake & Analysis ────────────────────────────────────────────────

def index_clip(input_path: str, vo_manifest: Optional[str] = None,
               min_silence: Optional[float] = None, threshold: Optional[int] = None,
               pad: Optional[float] = None, force: bool = False,
               recompute: bool = False) -> dict:
    """
    Analyze source video: transcribe, extract word-level timestamps, write clip-index manifest.
    This is the entry point for any raw footage. Built-in caching — skips re-processing if manifest exists.
    """
    if TEST_MODE:
        # Create a stub _meta/*.yaml so HoP can find the cached manifest
        import yaml as _yaml
        clip_p = Path(input_path)
        meta_dir = clip_p.parent / "_meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        cached = meta_dir / f"{clip_p.stem}.yaml"
        if not cached.exists():
            # Get real duration via ffprobe if available, else default
            duration = 30.0
            try:
                import subprocess as _sp
                probe = _sp.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", input_path],
                    capture_output=True, text=True, timeout=10,
                )
                if probe.returncode == 0:
                    import json as _json
                    duration = float(_json.loads(probe.stdout).get("format", {}).get("duration", 30.0))
            except Exception:
                pass
            # Generate synthetic clips (simulate silence-based cuts)
            num_clips = max(1, int(duration / 8))  # ~8s per clip
            clip_dur = duration / num_clips
            clips = []
            for i in range(num_clips):
                clips.append({
                    "index": i,
                    "source_start": round(i * clip_dur, 2),
                    "source_end": round((i + 1) * clip_dur, 2),
                    "duration": round(clip_dur, 1),
                })
            stub = {
                "source": input_path,
                "duration_s": round(duration, 1),
                "transcript": f"[DRILL] Synthetic transcript for {clip_p.name}",
                "clips": clips,
            }
            with open(cached, "w") as _f:
                _yaml.dump(stub, _f, default_flow_style=False)
        return {"success": True, "stdout": f"[DRILL] Indexed clip → {cached}", "stderr": "", "tool": "index_clip"}
    cmd = ["python3", _script("index-clip.py"), "--input", input_path]
    if vo_manifest:
        cmd.extend(["--vo-manifest", vo_manifest])
    if min_silence is not None:
        cmd.extend(["--min-silence", str(min_silence)])
    if threshold is not None:
        cmd.extend(["--threshold", str(threshold)])
    if pad is not None:
        cmd.extend(["--pad", str(pad)])
    if force:
        cmd.append("--force")
    if recompute:
        cmd.append("--recompute")
    return {**_run(cmd, timeout=600), "tool": "index_clip"}


def inspect_media(input_path: str, preview: bool = False,
                  frames: Optional[int] = None, cols: Optional[int] = None) -> dict:
    """Quick media inspection — duration, size, streams via ffprobe."""
    if TEST_MODE:
        return {"success": True, "stdout": "[DRILL] 1920x1080 24fps 48s", "stderr": "", "tool": "inspect_media"}
    cmd = ["python3", _script("inspect-media.py"), input_path]
    if preview:
        cmd.append("--preview")
    if frames is not None:
        cmd.extend(["--frames", str(frames)])
    if cols is not None:
        cmd.extend(["--cols", str(cols)])
    return {**_run(cmd, timeout=30), "tool": "inspect_media"}


def assemble_clips(clip_manifests: list[str], output: str,
                   clips: Optional[str] = None, stream_copy: bool = False,
                   dry_run: bool = False) -> dict:
    """Assemble final video from multiple clip-index manifests."""
    if TEST_MODE:
        import subprocess as _sp
        import yaml as _yaml
        # Calculate total duration from manifests
        total_dur = 0
        for mp in clip_manifests:
            try:
                with open(mp) as _f:
                    data = _yaml.safe_load(_f)
                total_dur += data.get("duration_s", 10.0)
            except Exception:
                total_dur += 10.0
        total_dur = min(total_dur, 60.0)  # cap drill videos at 60s
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        # Create a real black video with silent audio so evaluator can ffprobe it
        r = _sp.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", f"color=black:size=1920x1080:duration={total_dur}:rate=30",
             "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={total_dur}",
             "-map", "0:v", "-map", "1:a", "-shortest",
             "-c:v", "libx264", "-c:a", "aac", output],
            capture_output=True,
        )
        success = r.returncode == 0
        return {
            "success": success,
            "output_path": output if success else None,
            "stdout": f"[DRILL] Assembled clips → {output}" if success else "[DRILL] assemble_clips failed",
            "stderr": r.stderr.decode()[:200] if not success else "",
            "tool": "assemble_clips",
        }
    cmd = ["python3", _script("assemble-clips.py"), "--manifests"] + clip_manifests + ["--output", output]
    if clips:
        cmd.extend(["--clips", clips])
    if stream_copy:
        cmd.append("--stream-copy")
    if dry_run:
        cmd.append("--dry-run")
    return {**_run(cmd, timeout=900), "tool": "assemble_clips"}


def suggest_clips(manifest: str) -> dict:
    """Suggest which clips to keep from a clip-index manifest."""
    if TEST_MODE:
        return {"success": True, "stdout": "[DRILL] Suggested clips", "stderr": "", "tool": "suggest_clips"}
    cmd = ["python3", _script("suggest-clips.py"), "--manifest", manifest]
    return {**_run(cmd, timeout=60), "tool": "suggest_clips"}


def generate_lipsync(audio: str, output: str, fps: int = 30,
                     mode: str = "word", manifest: Optional[str] = None,
                     vo_manifest: Optional[str] = None) -> dict:
    """Generate per-frame mouth/speaking arrays from audio for character animation.
    Also writes a vo_manifest.json (word-level timestamps) if vo_manifest path provided.
    """
    if TEST_MODE:
        import json as _json
        if vo_manifest:
            _stub_words = [
                {"word": "This", "start": 0.0, "end": 0.4},
                {"word": "supplement", "start": 0.5, "end": 1.1},
                {"word": "gives", "start": 1.2, "end": 1.5},
                {"word": "you", "start": 1.6, "end": 1.8},
                {"word": "energy.", "start": 1.9, "end": 2.4},
                {"word": "Try", "start": 2.8, "end": 3.0},
                {"word": "it", "start": 3.1, "end": 3.2},
                {"word": "today.", "start": 3.3, "end": 3.8},
            ]
            Path(vo_manifest).parent.mkdir(parents=True, exist_ok=True)
            with open(vo_manifest, "w") as _f:
                _json.dump({"words": _stub_words, "text": "This supplement gives you energy. Try it today."}, _f)
        return {"success": True, "stdout": "[DRILL] Generated lipsync", "stderr": "", "tool": "generate_lipsync"}
    cmd = ["python3", _script("generate-lipsync.py"), "--audio", audio, "--output", output,
           "--fps", str(fps), "--mode", mode]
    if manifest:
        cmd.extend(["--manifest", manifest])
    if vo_manifest:
        cmd.extend(["--vo-manifest", vo_manifest])
    return {**_run(cmd, timeout=120), "tool": "generate_lipsync"}


# ── Effects & Filters ────────────────────────────────────────────────────────

def apply_grade(input_path: str, output_path: str, preset: str = "warm") -> dict:
    """Color grading via ffmpeg eq + colorbalance."""
    if TEST_MODE:
        return {"success": True, "stdout": f"[DRILL] Applied grade {preset}", "stderr": "", "tool": "apply_grade"}
    cmd = ["python3", _script("apply-grade.py"), input_path, "--output", output_path, "--preset", preset]
    return {**_run(cmd, timeout=300), "tool": "apply_grade"}


def apply_grain(input_path: str, output_path: str) -> dict:
    """Apply film grain overlay."""
    if TEST_MODE:
        return {"success": True, "stdout": "[DRILL] Applied grain", "stderr": "", "tool": "apply_grain"}
    cmd = ["python3", _script("apply-grain.py"), input_path, "--output", output_path]
    return {**_run(cmd, timeout=300), "tool": "apply_grain"}


def generate_caption_image(text: str, output: str, style: str = "banner",
                           fontsize: Optional[int] = None,
                           font: Optional[str] = None,
                           fg_color: str = "white",
                           bg_color: Optional[str] = None,
                           position: Optional[str] = None,
                           resolution: Optional[str] = None,
                           uppercase: bool = False,
                           block_bg: bool = False) -> dict:
    """Generate a text overlay PNG for captions/banners/title cards.

    Args:
        text: text to render
        output: output PNG path
        style: banner | caption | title
        fontsize: font size (auto-calculated if omitted)
        font: sans | serif | mono | display | rounded
        fg_color: foreground/text color
        bg_color: background color
        position: top | bottom | center | headline | midscreen | caption
        resolution: e.g. "1080x1920"
        uppercase: force uppercase
        block_bg: draw block background behind text
    """
    if TEST_MODE:
        return {"success": True, "output_path": output,
                "stdout": f"[DRILL] Generated caption: {text[:40]}", "stderr": "", "tool": "generate_caption_image"}
    cmd = ["python3", _script("generate-caption.py"), "--text", text, "--output", output,
           "--style", style, "--fg-color", fg_color]
    if fontsize:
        cmd.extend(["--fontsize", str(fontsize)])
    if font:
        cmd.extend(["--font", font])
    if bg_color:
        cmd.extend(["--bg-color", bg_color])
    if position:
        cmd.extend(["--position", position])
    if resolution:
        cmd.extend(["--resolution", resolution])
    if uppercase:
        cmd.append("--uppercase")
    if block_bg:
        cmd.append("--block-bg")
    return {**_run(cmd), "tool": "generate_caption_image"}


def extend_scene(manifest_path: str, scene: int, duration: float) -> dict:
    """Manually extend scene duration, update downstream VO timing."""
    if TEST_MODE:
        return {"success": True, "stdout": f"[DRILL] Extended scene {scene} to {duration}s", "stderr": "", "tool": "extend_scene"}
    cmd = ["python3", _script("extend-scene.py"), "--manifest", manifest_path, "--scene", str(scene), "--duration", str(duration)]
    return {**_run(cmd), "tool": "extend_scene"}


# ── Raw ffmpeg (escape hatch for custom filters) ─────────────────────────────

def ffmpeg(args: list[str], timeout: int = 900) -> dict:
    """
    Run an arbitrary ffmpeg command. Escape hatch for effects that don't have
    a dedicated script (e.g., rainbow flashing text overlay).
    Agents should prefer named tools over this.
    """
    if TEST_MODE:
        return {"success": True, "stdout": f"[DRILL] ffmpeg {' '.join(args[:4])}...", "stderr": "", "tool": "ffmpeg"}
    # Ensure output directory exists (common failure: _work/ doesn't exist)
    for i, a in enumerate(args):
        if a == "-y" and i + 1 < len(args):
            # The arg after -y is often the output path
            out_dir = Path(args[i + 1]).parent
            out_dir.mkdir(parents=True, exist_ok=True)
    # Also check the last arg (often the output)
    if args:
        last_arg = args[-1]
        if not last_arg.startswith("-") and ("/" in last_arg or "." in last_arg):
            Path(last_arg).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg"] + args
    return {**_run(cmd, timeout=timeout), "tool": "ffmpeg"}


# ── Manifest Writing ─────────────────────────────────────────────────────────

def write_manifest_scenes(manifest_path: str, scenes: list) -> dict:
    """
    Write structured scene/footage data into the manifest.
    Merges with existing manifest (preserves brief, project, config blocks).
    Validates the merged result before writing — raises ManifestValidationError
    with agent-friendly error messages if validation fails.

    Args:
        manifest_path: path to manifest.yaml
        scenes: list of scene dicts, each with:
            index (int), type (str: video|text_overlay|still|effect_overlay),
            source (str: absolute path), start_s (float), end_s (float),
            rotate (int, optional), description (str, optional),
            overlay_text (str, optional), estimated_duration_s (float, optional)
    """
    import yaml as _yaml
    from packs.video.manifest_validator import validate_or_raise, ManifestValidationError

    try:
        existing = {}
        if Path(manifest_path).exists():
            with open(manifest_path) as f:
                existing = _yaml.safe_load(f) or {}

        # Build footage section from scenes
        source_clips = []
        for s in scenes:
            clip = {
                "path": s.get("source", ""),
                "start_s": s.get("start_s", 0.0),
                "end_s": s.get("end_s", 0.0),
            }
            if s.get("rotate"):
                clip["rotate"] = s["rotate"]
            if s.get("description"):
                clip["label"] = s["description"]
            if s.get("type") and s["type"] != "video":
                clip["type"] = s["type"]
            if s.get("overlay_text"):
                clip["overlay_text"] = s["overlay_text"]
            if s.get("estimated_duration_s"):
                clip["estimated_duration_s"] = s["estimated_duration_s"]
            source_clips.append(clip)

        existing["footage"] = {
            "source_clips": source_clips,
            "assembly_order": list(range(len(source_clips))),
        }

        # Preserve config if not set
        if "config" not in existing:
            existing["config"] = {"resolution": "1920x1080", "fps": 30}

        # Always stamp manifest_version
        existing["manifest_version"] = "0.1.0"

        # Validate before writing — raises ManifestValidationError if invalid
        validate_or_raise(existing)

        Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "w") as f:
            _yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

        return {
            "success": True,
            "stdout": f"Wrote {len(source_clips)} scenes to {manifest_path}",
            "stderr": "",
            "tool": "write_manifest_scenes",
        }
    except ManifestValidationError:
        raise  # let the agent loop surface the validation error directly
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"write_manifest_scenes failed: {e}",
            "tool": "write_manifest_scenes",
        }


# ── Project Setup ────────────────────────────────────────────────────────────

def init_project(slug: str, format: str = "ad-vertical",
                 from_inbox: Optional[str] = None) -> dict:
    """
    Initialize a new video project with standard folder structure.
    """
    if TEST_MODE:
        return {
            "success": True,
            "stdout": f"[DRILL] Initialized project {slug}",
            "stderr": "",
            "tool": "init_project",
        }

    cmd = ["python3", _script("init-project.py"), "--slug", slug, "--format", format]
    if from_inbox:
        cmd.extend(["--from-inbox", from_inbox])
    return {**_run(cmd), "tool": "init_project"}


# ── Tool Registry ────────────────────────────────────────────────────────────

TOOL_REGISTRY = {
    # Scene planning
    "plan_scenes": plan_scenes,
    "plan_scenes_for_agent": plan_scenes_for_agent,
    "ingest_agent_scenes": ingest_agent_scenes,
    # Image generation
    "generate_still": generate_still,
    "generate_char_ref": generate_char_ref,
    # Assembly & rendering
    "assemble": assemble,
    "assemble_clips": assemble_clips,
    "burn_captions": burn_captions,
    "render_animation": render_animation,
    # Audio
    "generate_voiceover": generate_voiceover,
    "align_scenes": align_scenes,
    "trim_silence": trim_silence,
    "normalize_audio": normalize_audio,
    "music_duck": music_duck,
    # Footage intake
    "index_clip": index_clip,
    "inspect_media": inspect_media,
    "suggest_clips": suggest_clips,
    "generate_lipsync": generate_lipsync,
    # Effects
    "apply_grade": apply_grade,
    "apply_grain": apply_grain,
    "generate_caption_image": generate_caption_image,
    "extend_scene": extend_scene,
    # Escape hatch
    "ffmpeg": ffmpeg,
    # Manifest writing
    "write_manifest_scenes": write_manifest_scenes,
    # Project setup
    "init_project": init_project,
}


def tool_signatures() -> str:
    """
    Return human-readable tool signatures for LLM prompts.
    Agents need this to know the exact parameter names.
    """
    return """TOOL SIGNATURES (use these exact parameter names):

ffmpeg(args: list[str]) — Run arbitrary ffmpeg. args is a LIST of arguments, NOT a single string. Example: args=["-i", "in.mov", "-c:v", "libx264", "-y", "out.mp4"]
index_clip(input_path: str, vo_manifest: str|None=None, min_silence: float|None=None, threshold: int|None=None, pad: float|None=None, force: bool=False, recompute: bool=False) — Transcribe + index a video clip; cached by default
inspect_media(input_path: str, preview: bool=False, frames: int|None=None, cols: int|None=None) — Get media info (duration, resolution, codecs)
suggest_clips(manifest: str) — Suggest which clips to keep from a clip-index manifest
generate_lipsync(audio: str, output: str, fps: int=30, mode: str="word", manifest: str|None=None) — Generate per-frame mouth/speaking arrays from audio for character animation
trim_silence(manifest: str, min_silence: float|None, pad: float|None, threshold: int|None, dry_run: bool=False) — Trim silence from VO using project manifest
normalize_audio(input_path: str, output_path: str|None) — Normalize loudness to -14 LUFS
generate_caption_image(text: str, output: str, style: str="banner", fontsize: int|None=None, font: str|None=None, fg_color: str="white", bg_color: str|None=None, position: str|None=None, resolution: str|None=None, uppercase: bool=False, block_bg: bool=False) — Generate text overlay PNG
generate_still(manifest_path: str, scene: str, ref_image: str|None, chain: bool=False, variants: int|None=None) — Generate scene stills
assemble(manifest_path: str, output: str|None, draft: bool=False, scenes: str|None) — Assemble video from manifest
assemble_clips(clip_manifests: list[str], output: str, clips: str|None=None, stream_copy: bool=False, dry_run: bool=False) — Assemble from clip-index manifests; clips e.g. "0,2,4-6" to filter specific clips
burn_captions(manifest_path: str, video: str, output: str|None, block_bg: bool=False) — Burn captions onto video
render_animation(template: str, output: str, duration: float=0, mode: str="video", params: dict|None=None, width: int=1080, height: int=1920, params_file: str|None=None) — Render HTML template to video. Available templates include: caption, caption-fade, caption-line, caption-word-reveal, title-blur, title-centered, title-subtitle, text-typewriter, zoom-reveal, stats-counter, bouncing-ball, parallax-layers, and many more artistic ones.
generate_voiceover(manifest_path: str, voice: str|None) — Generate VO via ElevenLabs
align_scenes(manifest_path: str) — Align scene timing to actual VO timestamps
apply_grade(input_path: str, output_path: str, preset: str="warm") — Color grade
apply_grain(input_path: str, output_path: str) — Add film grain
extend_scene(manifest_path: str, scene: int, duration: float) — Extend a scene's duration
init_project(slug: str, format: str="ad-vertical") — Initialize project folder
music_duck(video: str, music: str, output: str) — Duck music under speech
write_manifest_scenes(manifest_path: str, scenes: list[dict]) — Write scene data to manifest. Each scene: {index, type, source, start_s, end_s, rotate?, description?}"""


def call_tool(name: str, **kwargs) -> dict:
    """
    Call a registered tool by name.
    Agents use this as their interface to production capabilities.
    Strips unknown kwargs to be forgiving of LLM hallucinated params.
    """
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        return {"success": False, "error": f"Unknown tool: {name}. Available: {list(TOOL_REGISTRY.keys())}"}
    try:
        import inspect
        sig = inspect.signature(fn)
        valid_params = set(sig.parameters.keys())
        filtered = {k: v for k, v in kwargs.items() if k in valid_params}
        dropped = set(kwargs.keys()) - valid_params
        if dropped:
            print(f"  [tools] Dropped unknown params for {name}: {dropped}")
        return fn(**filtered)
    except Exception as e:
        return {"success": False, "error": str(e), "tool": name}
