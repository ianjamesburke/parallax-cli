"""
Thin wrappers around the bundled video-production scripts.
Scripts live alongside this repo at packs/video/scripts/.

In TEST_MODE: logs what would have been called, returns mock paths.
In real mode: runs the actual scripts via subprocess.
"""

import os
import json
import subprocess
from pathlib import Path
from typing import Optional

TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

# Bundled scripts — resolved relative to repo root (core/ is one level under root).
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "packs" / "video" / "scripts"


def _log_mock(action: str, kwargs: dict):
    """Print a test-mode mock call for visibility."""
    print(f"[video_tools TEST] Would call: {action}({json.dumps(kwargs, default=str)})")


def generate_still(
    manifest_path: str,
    scene_index: int,
    ref_image: Optional[str] = None,
) -> str:
    """
    Generate a single still image for a scene defined in the manifest.

    TEST MODE: Logs the call and returns a mock output path.
    REAL MODE: Runs generate-still.py with the given manifest and scene index.
    """
    if TEST_MODE:
        mock_output = f"logs/runs/mock_stills/scene_{scene_index:02d}.png"
        _log_mock("generate_still", {
            "manifest_path": manifest_path,
            "scene_index": scene_index,
            "ref_image": ref_image,
        })
        return mock_output

    try:
        script = SCRIPTS_DIR / "generate-still.py"
        cmd = ["python3", str(script), "--manifest", manifest_path, "--scene", str(scene_index)]
        if ref_image:
            cmd += ["--ref-image", ref_image]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        # Expect the script to print the output path on stdout
        output_path = result.stdout.strip().splitlines()[-1]
        return output_path
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"[video_tools] generate-still.py failed for scene {scene_index}: {e.stderr}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"[video_tools] generate_still error: {e}") from e


def assemble(manifest_path: str) -> str:
    """
    Assemble stills into a video using the manifest.

    TEST MODE: Logs call and returns mock output path.
    REAL MODE: Runs assemble.py.
    """
    if TEST_MODE:
        mock_output = "logs/runs/mock_output/assembled.mp4"
        _log_mock("assemble", {"manifest_path": manifest_path})
        return mock_output

    try:
        script = SCRIPTS_DIR / "assemble.py"
        result = subprocess.run(
            ["python3", str(script), "--manifest", manifest_path],
            check=True,
            capture_output=True,
            text=True,
        )
        output_path = result.stdout.strip().splitlines()[-1]
        return output_path
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"[video_tools] assemble.py failed: {e.stderr}") from e
    except Exception as e:
        raise RuntimeError(f"[video_tools] assemble error: {e}") from e


def burn_captions(manifest_path: str, video_path: str, output_path: str) -> str:
    """
    Burn captions into the assembled video.

    TEST MODE: Logs call and returns mock output path.
    REAL MODE: Runs burn-captions.py (or generate-caption.py equivalent).
    """
    if TEST_MODE:
        mock_output = output_path.replace(".mp4", "_captioned.mp4")
        _log_mock("burn_captions", {
            "manifest_path": manifest_path,
            "video_path": video_path,
            "output_path": output_path,
        })
        return mock_output

    try:
        script = SCRIPTS_DIR / "burn-captions.py"
        subprocess.run(
            ["python3", str(script), "--manifest", manifest_path,
             "--video", video_path, "--output", output_path],
            check=True,
            capture_output=True,
            text=True,
        )
        return output_path
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"[video_tools] burn-captions.py failed: {e.stderr}") from e
    except Exception as e:
        raise RuntimeError(f"[video_tools] burn_captions error: {e}") from e


def align_scenes(manifest_path: str) -> str:
    """
    Align scene timing in the manifest (e.g., sync to voiceover).

    TEST MODE: Logs call and returns manifest path unchanged.
    REAL MODE: Runs align-scenes.py and returns updated manifest path.
    """
    if TEST_MODE:
        _log_mock("align_scenes", {"manifest_path": manifest_path})
        return manifest_path

    try:
        script = SCRIPTS_DIR / "align-scenes.py"
        result = subprocess.run(
            ["python3", str(script), "--manifest", manifest_path],
            check=True,
            capture_output=True,
            text=True,
        )
        output_path = result.stdout.strip().splitlines()[-1]
        return output_path
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"[video_tools] align-scenes.py failed: {e.stderr}") from e
    except Exception as e:
        raise RuntimeError(f"[video_tools] align_scenes error: {e}") from e
