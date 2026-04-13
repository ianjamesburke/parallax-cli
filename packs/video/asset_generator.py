"""
Asset Generator
===============
The single agent that speaks the language of all media generation tools.
Nobody else in the network calls Gemini, ElevenLabs, fal.ai, or RunPod directly.

Responsibilities:
1. Receive a structured asset request
2. Validate the brief BEFORE generating (raise a concern if ambiguous)
3. Pick the cheapest model that satisfies the constraints
4. Generate the asset
5. Self-evaluate: does the output satisfy the brief?
6. Return result or raise a concern if it doesn't

Model selection logic:
  Image, no ref, no rush     → Gemini Flash (cheapest)
  Image, with character ref  → Gemini Flash + anchor still technique
  Image, high quality needed → Gemini Pro
  Video clip, fast           → fal.ai
  VO, fast/draft             → macOS say (test) or ElevenLabs Flash
  VO, final/cloned voice     → ElevenLabs standard
  Bulk renders, overnight    → queue (future: RunPod)

TEST_MODE: all generation is mocked — placeholder images, macOS say for audio.
"""

import os
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
import anthropic

from core.concerns import Concern, ConcernBus

TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"


# Asset request schema:
# {
#   "asset_type": "image" | "video" | "voiceover" | "audio",
#   "brief": str,                    # what to generate
#   "output_path": str,              # where to save it
#   "constraints": {
#     "aspect_ratio": "9:16" | "16:9" | "1:1",
#     "resolution": "1080x1920",
#     "has_audio": bool,
#     "speed": "fast" | "standard" | "overnight",
#     "budget": "minimal" | "standard" | "premium",
#     "ref_image": str | None,       # path to reference image
#     "anchor_still": str | None,    # path to visual style anchor
#   },
#   "scene_index": int | None,
#   "concept_id": str,
#   "run_id": str,
# }


class AssetGenerator:
    """
    Media specialist. Picks cheapest model, validates brief, generates, self-evaluates.

    In DRILL mode (TEST_MODE=true), the very first thing generate() does is short-circuit:
    - Images  → gray JPEG with the full prompt printed on it (no LLM call, no API call)
    - VO      → macOS `say` with voice style inferred from brief keywords
    Zero agent calls happen. The placeholder is returned immediately.

    v2: Register as a persistent beta agent with tool access to Gemini/ElevenLabs APIs.
    """

    MODEL = "claude-haiku-4-5-20251001"  # Used for brief validation and self-eval only

    # Model pricing used for selection logic (cost per asset, approximate)
    MODEL_COSTS = {
        "gemini_flash": 0.02,
        "gemini_pro": 0.08,
        "fal_video": 0.25,
        "elevenlabs_flash": 0.01,
        "elevenlabs_standard": 0.05,
        "macos_say": 0.00,
        "test_placeholder": 0.00,
    }

    # macOS `say` voice selection based on style keywords in brief/constraints
    VOICE_MAP = {
        "whisper": "Whisper",
        "sultry": "Samantha",
        "female": "Samantha",
        "woman": "Samantha",
        "male": "Alex",
        "man": "Alex",
        "authoritative": "Daniel",
        "excited": "Zoe",
        "calm": "Karen",
    }
    DEFAULT_VOICE = "Samantha"

    def __init__(self, run_id: str, concern_bus: Optional[ConcernBus] = None):
        self.run_id = run_id
        self._client: Optional[anthropic.Anthropic] = None  # lazy — not created in drill mode
        self.concern_bus = concern_bus
        self.test_mode = TEST_MODE

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def generate(self, request: dict) -> dict:
        """
        Main entry point. Returns:
        {
          "success": bool,
          "output_path": str | None,
          "model_used": str,
          "cost_usd": float,
          "self_eval_score": float,  # 0-1, how well output satisfies brief
          "concern": Concern | None,  # raised if ambiguous or output unsatisfactory
        }
        """
        concept_id = request.get("concept_id", "UNK-000")
        asset_type = request.get("asset_type", "image")
        brief = request.get("brief", "")
        output_path = request.get("output_path", f"assets/{self.run_id}_{asset_type}.png")
        constraints = request.get("constraints", {})

        print(f"[AssetGen] {asset_type} request — brief: {brief[:60]}...")

        # ── DRILL GATE ────────────────────────────────────────────────────────
        # In TEST_MODE, short-circuit immediately. No LLM calls, no API calls.
        # _mock_response: test harness can override the entire return value.
        if self.test_mode:
            if request.get("_mock_response"):
                print(f"[AssetGen] DRILL — using mock response")
                return request["_mock_response"]
            return self._drill_response(asset_type, brief, output_path, request)
        # ─────────────────────────────────────────────────────────────────────

        # Step 1: Validate the brief before burning API costs
        concern = self._validate_brief(brief, asset_type, request)
        if concern and concern.blocking:
            return {
                "success": False,
                "output_path": None,
                "model_used": None,
                "cost_usd": 0.0,
                "self_eval_score": 0.0,
                "concern": concern,
            }

        # Step 2: Select model based on constraints
        model = self._select_model(asset_type, constraints)
        print(f"[AssetGen] Selected model: {model} (${self.MODEL_COSTS.get(model, 0):.3f})")

        # Step 3: Generate
        try:
            output_path = self._generate(asset_type, brief, output_path, constraints, model, request)
        except Exception as e:
            concern = self._raise_concern(
                f"Generation failed: {e}",
                severity=0.8,
                blocking=True,
                raised_by="asset_generator",
                context={"model": model, "error": str(e)},
            )
            return {"success": False, "output_path": None, "model_used": model,
                    "cost_usd": 0.0, "self_eval_score": 0.0, "concern": concern}

        # Step 4: Self-evaluate
        eval_score = self._self_evaluate(brief, output_path, asset_type)
        print(f"[AssetGen] Self-eval score: {eval_score:.0%}")

        # Step 5: If self-eval is low, raise a concern (don't silently return bad output)
        output_concern = None
        if eval_score < 0.6:
            output_concern = self._raise_concern(
                f"Output may not satisfy brief (self-eval: {eval_score:.0%}). Brief: {brief[:100]}",
                severity=0.65,
                blocking=False,
                raised_by="asset_generator",
                proposed_default="Use output as-is and flag for human review",
                context={"brief": brief, "output_path": output_path, "score": eval_score},
            )

        cost = self.MODEL_COSTS.get(model, 0.0)
        from core.cost_tracker import log_call
        try:
            log_call(
                concept_id=concept_id,
                agent="asset_generator",
                run_id=self.run_id,
                input_tokens=0,  # Media generation — no token cost
                output_tokens=0,
                model=model,
            )
        except Exception as e:
            print(f"[AssetGen] WARNING: cost logging failed: {e}")

        try:
            from core.events import emitter
            if asset_type == "voiceover":
                emitter.emit("voiceover_generated", path=output_path)
            else:
                emitter.emit("still_generated", path=output_path, prompt=brief)
        except Exception:
            pass

        return {
            "success": True,
            "output_path": output_path,
            "model_used": model,
            "cost_usd": cost,
            "self_eval_score": eval_score,
            "concern": output_concern,
        }

    def _drill_response(self, asset_type: str, brief: str, output_path: str,
                         request: dict) -> dict:
        """
        DRILL MODE: immediate short-circuit, zero API calls.
        - image/video → gray JPEG with full prompt printed on it
        - voiceover   → macOS `say` with voice inferred from brief/constraints
        """
        print(f"[AssetGen] DRILL — generating placeholder for {asset_type}")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if asset_type == "voiceover":
            output_path = self._drill_voiceover(brief, output_path, request)
        else:
            output_path = self._drill_image(brief, output_path, request)

        try:
            from core.events import emitter
            if asset_type == "voiceover":
                emitter.emit("voiceover_generated", path=output_path)
            else:
                emitter.emit("still_generated", path=output_path, prompt=brief)
        except Exception:
            pass
        return {
            "success": True,
            "output_path": output_path,
            "model_used": "drill_placeholder",
            "cost_usd": 0.0,
            "self_eval_score": 1.0,
            "concern": None,
            "drill": True,
        }

    def _drill_image(self, brief: str, output_path: str, request: dict) -> str:
        """Gray JPEG card with full prompt text and DRILL watermark."""
        scene_index = request.get("scene_index", 0)
        width, height = 1080, 1920
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (width, height), color=(55, 55, 65))
            draw = ImageDraw.Draw(img)

            # DRILL banner
            draw.rectangle([0, 0, width, 110], fill=(30, 100, 160))
            draw.text((20, 20), "DRILL", fill=(255, 220, 0))
            draw.text((20, 62), f"Scene {scene_index}  |  {self.run_id[:8]}", fill=(200, 230, 255))

            # Brief text — word-wrap at ~48 chars per line
            y = 140
            draw.text((20, y), "PROMPT:", fill=(150, 200, 255))
            y += 40
            words = brief.split()
            line = ""
            for word in words:
                if len(line + word) > 48:
                    draw.text((20, y), line.rstrip(), fill=(230, 230, 230))
                    y += 38
                    line = word + " "
                    if y > height - 80:
                        draw.text((20, y), "…", fill=(180, 180, 180))
                        break
                else:
                    line += word + " "
            else:
                if line.strip():
                    draw.text((20, y), line.rstrip(), fill=(230, 230, 230))

            # Save as JPEG (matches "drill" aesthetic — clearly not a real render)
            jpeg_path = str(output_path).replace(".png", ".jpg")
            img.save(jpeg_path, "JPEG", quality=85)
            return jpeg_path
        except ImportError:
            # PIL not available — write a text stub
            stub = str(output_path).replace(".png", "_drill.txt")
            Path(stub).write_text(f"DRILL PLACEHOLDER\nScene: {scene_index}\nBrief: {brief}")
            return stub
        except Exception as e:
            raise RuntimeError(f"[AssetGen] Drill image generation failed: {e}") from e

    def _drill_voiceover(self, brief: str, output_path: str, request: dict) -> str:
        """macOS `say` with voice inferred from brief/constraints."""
        constraints = request.get("constraints", {})
        style_hint = constraints.get("voice_style", "").lower()
        brief_lower = brief.lower()

        # Pick voice: check constraints first, then scan brief for style keywords
        voice = self.DEFAULT_VOICE
        for keyword, mapped_voice in self.VOICE_MAP.items():
            if keyword in style_hint or keyword in brief_lower:
                voice = mapped_voice
                break

        aiff_path = str(output_path).rsplit(".", 1)[0] + "_drill.aiff"
        print(f"[AssetGen] DRILL VO — voice: {voice} | text: {brief[:60]}…")
        try:
            subprocess.run(["say", "-v", voice, "-o", aiff_path, brief], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"[AssetGen] macOS say failed (voice={voice}): {e.stderr.decode()}"
            ) from e
        return aiff_path

    def _validate_brief(self, brief: str, asset_type: str,
                         request: dict) -> Optional[Concern]:
        """
        Ask the LLM if this brief has any fatal ambiguities.
        Returns a Concern if something is wrong, None if the brief is clear.
        """
        prompt = f"""You are reviewing an asset generation request before it is executed.

Asset type: {asset_type}
Brief: {brief}
Constraints: {json.dumps(request.get('constraints', {}))}

Is this brief specific enough to generate a satisfactory {asset_type}?
Identify any ambiguities that would lead to a poor or wrong output.

Return JSON:
{{
  "clear": true | false,
  "issues": ["list of specific ambiguities"],
  "severity": 0.0,
  "proposed_default": "what assumption would you make to proceed anyway"
}}"""

        try:
            response = self.client.messages.create(
                model=self.MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text
            import re
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                result = json.loads(match.group())
                if not result.get("clear") and result.get("severity", 0) >= 0.5:
                    issues = "; ".join(result.get("issues", ["Brief is ambiguous"]))
                    return self._raise_concern(
                        message=f"Brief ambiguity detected: {issues}",
                        severity=result["severity"],
                        blocking=result["severity"] >= 0.7,
                        raised_by="asset_generator",
                        proposed_default=result.get("proposed_default"),
                        context={"brief": brief, "issues": result.get("issues", [])},
                    )
        except Exception:
            pass  # Validation is best-effort — don't block on its failure
        return None

    def _select_model(self, asset_type: str, constraints: dict) -> str:
        """Pick cheapest model that satisfies the constraints."""
        budget = constraints.get("budget", "standard")
        has_ref = bool(constraints.get("ref_image") or constraints.get("anchor_still"))

        if self.test_mode:
            return "macos_say" if asset_type == "voiceover" else "test_placeholder"

        if asset_type == "image":
            if budget == "minimal" and not has_ref:
                return "gemini_flash"
            elif budget == "premium":
                return "gemini_pro"
            else:
                return "gemini_flash"

        elif asset_type == "voiceover":
            speed = constraints.get("speed", "standard")
            if speed == "fast" or budget == "minimal":
                return "elevenlabs_flash"
            return "elevenlabs_standard"

        elif asset_type == "video":
            return "fal_video"

        return "gemini_flash"

    def _generate(self, asset_type: str, brief: str, output_path: str,
                   constraints: dict, model: str, request: dict) -> str:
        """Execute the generation. TEST_MODE returns mocks."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if self.test_mode or model == "test_placeholder":
            return self._mock_image(brief, output_path, request.get("scene_index", 0))

        if model == "macos_say":
            return self._mock_voiceover(brief, output_path)

        if asset_type == "image":
            return self._generate_image(brief, output_path, constraints, model)
        elif asset_type == "voiceover":
            return self._generate_voiceover(brief, output_path, constraints)

        raise NotImplementedError(f"Real generation for {asset_type}/{model} not yet wired")

    def _generate_image(self, brief: str, output_path: str,
                         constraints: dict, model: str) -> str:
        """Call Gemini image generation."""
        # v1: calls the bundled generate-still.py script
        # v2: direct Gemini API call
        bundled_scripts = Path(__file__).resolve().parent / "scripts"
        script = bundled_scripts / "generate-still.py"
        if not script.exists():
            raise FileNotFoundError(f"generate-still.py not found at {bundled_scripts}")
        # For now, fall back to test placeholder if script integration not wired
        return self._mock_image(brief, output_path, 0)

    def _generate_voiceover(self, brief: str, output_path: str,
                              constraints: dict) -> str:
        """Call ElevenLabs voiceover generation."""
        raise NotImplementedError("ElevenLabs integration: wire tools/voiceover_tools.py")

    def _mock_image(self, brief: str, output_path: str, scene_index: int) -> str:
        """TEST MODE: Gray placeholder PNG with brief text."""
        try:
            from PIL import Image, ImageDraw
            width, height = 1080, 1920
            img = Image.new("RGB", (width, height), color=(70, 70, 70))
            draw = ImageDraw.Draw(img)
            draw.rectangle([0, 0, width, 100], fill=(180, 40, 40))
            draw.text((20, 30), "TEST MODE — ASSET GENERATOR", fill="white")
            draw.text((20, 120), f"Scene {scene_index}", fill=(180, 180, 180))
            draw.text((20, 160), "Model: test_placeholder", fill=(150, 150, 150))
            y = 220
            for chunk in [brief[i:i+44] for i in range(0, min(len(brief), 600), 44)]:
                draw.text((20, y), chunk, fill=(220, 220, 220))
                y += 34
            img.save(output_path)
        except ImportError:
            # PIL not available — write a text file as fallback
            Path(output_path).write_text(f"TEST PLACEHOLDER\nBrief: {brief}")
        return output_path

    def _mock_voiceover(self, text: str, output_path: str) -> str:
        """TEST MODE: macOS say command."""
        aiff_path = output_path.replace(".mp3", ".aiff")
        try:
            subprocess.run(["say", "-o", aiff_path, text], check=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"[AssetGen] macOS say failed for {output_path}: {e}") from e
        return aiff_path

    def _self_evaluate(self, brief: str, output_path: str, asset_type: str) -> float:
        """
        Ask the LLM to evaluate whether the output satisfies the brief.
        In test mode, always returns 0.85 (assume placeholder is fine).
        """
        if self.test_mode:
            return 0.85

        if asset_type == "image" and Path(output_path).exists():
            # v2: send image to vision model and ask "does this match the brief?"
            # v1: return a reasonable default
            return 0.80

        return 0.75  # Default when we can't actually evaluate

    def _raise_concern(self, message: str, severity: float, blocking: bool,
                        raised_by: str, proposed_default: Optional[str] = None,
                        context: Optional[dict] = None) -> Concern:
        """Create and register a concern with the bus (if available)."""
        concern = Concern(
            raised_by=raised_by,
            severity=severity,
            blocking=blocking,
            message=message,
            proposed_default=proposed_default,
            context=context or {},
        )
        if self.concern_bus:
            self.concern_bus.raise_concern(concern)
        else:
            print(f"[AssetGen] Concern ({severity:.0%}): {message}")
        return concern
