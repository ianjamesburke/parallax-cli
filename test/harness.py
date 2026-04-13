"""
Test harness for the NRTV agent network.
Controls test mode: placeholder images, macOS TTS, zero external API cost.
"""

import os
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone

try:
    from PIL import Image, ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"


class TestHarness:
    """
    Controls test mode behavior across the agent network.

    When TEST_MODE=true:
    - Image generation returns gray placeholder PNGs with prompt text overlaid
    - Voiceover uses macOS `say` instead of ElevenLabs
    - All prompts and decisions are logged to logs/runs/{run_id}/test_log.jsonl
    - Zero cost to external APIs (Gemini, ElevenLabs)
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.test_mode = TEST_MODE
        self.log_path = Path(f"logs/runs/{run_id}/test_log.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def generate_image(
        self,
        prompt: str,
        output_path: str,
        scene_index: int = 0,
        agent_name: str = "",
        width: int = 1080,
        height: int = 1920,
    ) -> str:
        """
        TEST MODE: Returns gray placeholder PNG with prompt text overlaid.
        REAL MODE: Calls Gemini image generation via tools/video_tools.py.
        """
        if self.test_mode:
            return self._make_placeholder(prompt, output_path, scene_index, agent_name, width, height)
        else:
            # v2: call generate-still.py via tools/video_tools.py
            raise NotImplementedError("Real image generation: call generate-still.py via tools/video_tools.py")

    def generate_voiceover(self, text: str, output_path: str, **_kwargs) -> str:
        """
        TEST MODE: Uses macOS `say` command to generate AIFF audio.
        REAL MODE: Calls ElevenLabs API via tools/voiceover_tools.py.
        """
        if self.test_mode:
            self._log("voiceover_request", {"text": text, "output_path": output_path, "mode": "macos_say"})
            try:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["say", "-o", output_path, "--data-format=aiff", text],
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"[harness] macOS `say` failed for output {output_path}: {e.stderr.decode()}"
                ) from e
            return output_path
        else:
            # v2: call ElevenLabs via tools/voiceover_tools.py
            raise NotImplementedError("Real voiceover: call ElevenLabs via tools/voiceover_tools.py")

    def _make_placeholder(
        self, prompt: str, output_path: str, scene_index: int, agent_name: str, width: int, height: int
    ) -> str:
        """Generate a gray placeholder image with prompt text and TEST MODE watermark."""
        if not PIL_AVAILABLE:
            # PIL not available — write a text file as fallback
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(
                f"TEST PLACEHOLDER\nScene: {scene_index}\nAgent: {agent_name}\nPrompt: {prompt}"
            )
            self._log("image_generated", {
                "scene_index": scene_index, "agent": agent_name,
                "prompt": prompt, "output": output_path, "mode": "text_fallback",
            })
            return output_path

        img = Image.new("RGB", (width, height), color=(80, 80, 80))  # type: ignore[name-defined]
        draw = ImageDraw.Draw(img)  # type: ignore[name-defined]

        # TEST MODE watermark banner
        draw.rectangle([0, 0, width, 120], fill=(200, 50, 50))
        draw.text((20, 35), "TEST MODE ACTIVE", fill="white")

        # Scene metadata
        draw.text((20, 140), f"Scene {scene_index} | Agent: {agent_name}", fill=(200, 200, 200))
        draw.text((20, 180), f"Run: {self.run_id}", fill=(150, 150, 150))

        # Prompt text (word-wrapped at ~45 chars)
        y = 240
        words = prompt.split()
        line = ""
        for word in words:
            if len(line + word) > 45:
                draw.text((20, y), line, fill=(230, 230, 230))
                y += 36
                line = word + " "
            else:
                line += word + " "
            if y > height - 100:
                draw.text((20, y), "...", fill=(230, 230, 230))
                break
        if line and y <= height - 100:
            draw.text((20, y), line, fill=(230, 230, 230))

        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            img.save(output_path)
        except Exception as e:
            raise RuntimeError(f"[harness] Could not save placeholder to {output_path}: {e}") from e

        self._log(
            "image_generated",
            {
                "scene_index": scene_index,
                "agent": agent_name,
                "prompt": prompt,
                "output": output_path,
                "mode": "test_placeholder",
            },
        )
        return output_path

    def _log(self, event_type: str, data: dict):
        """Append a structured event to this run's test log."""
        try:
            entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event_type, **data}
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"[harness] WARNING: Could not write test log entry ({event_type}): {e}")
