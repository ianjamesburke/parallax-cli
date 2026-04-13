#!/usr/bin/env python3
"""
Manifest validator tests.

Verifies that the Pydantic schema accepts valid manifests and rejects bad ones
with clear field-path error messages the agent loop can act on.

Usage:
    TEST_MODE=true python3.11 test/test_manifest_validator.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["TEST_MODE"] = "true"


from packs.video.manifest_validator import validate_manifest, validate_or_raise, ManifestValidationError


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(name, fn):
    try:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"  [{status}] {name}")
        for f in failures:
            print(f"         {f}")
        return not failures
    except Exception as e:
        print(f"  [ERROR] {name}: {e}")
        return False


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_valid_manifest_passes():
    """A well-formed manifest with video and text_overlay scenes passes."""
    manifest = {
        "manifest_version": "0.1.0",
        "config": {"resolution": "1920x1080", "fps": 30},
        "scenes": [
            {
                "index": 1,
                "type": "video",
                "source": "/tmp/clip.mp4",
                "start_s": 0.0,
                "end_s": 10.0,
            },
            {
                "index": 2,
                "type": "text_overlay",
                "overlay_text": "Hello world",
                "estimated_duration_s": 3.0,
            },
        ],
    }
    is_valid, errors = validate_manifest(manifest)
    failures = []
    if not is_valid:
        failures.append(f"Expected valid manifest to pass, got errors: {errors}")
    return failures


def test_missing_version_defaults_to_current():
    """A manifest without manifest_version is treated as 0.1.0 (backwards compat)."""
    manifest = {
        "config": {"resolution": "1920x1080", "fps": 30},
        "scenes": [
            {
                "index": 1,
                "type": "video",
                "source": "/tmp/clip.mp4",
                "start_s": 0.0,
                "end_s": 5.0,
            }
        ],
    }
    is_valid, errors = validate_manifest(manifest)
    failures = []
    if not is_valid:
        failures.append(f"Missing version should default to 0.1.0, got errors: {errors}")
    return failures


def test_wrong_version_rejected():
    """An unrecognised manifest_version is rejected with a clear error."""
    manifest = {
        "manifest_version": "99.0.0",
        "config": {"resolution": "1920x1080", "fps": 30},
    }
    is_valid, errors = validate_manifest(manifest)
    failures = []
    if is_valid:
        failures.append("Expected invalid manifest_version to be rejected")
    if not any("manifest_version" in e for e in errors):
        failures.append(f"Error message should mention 'manifest_version', got: {errors}")
    return failures


def test_end_before_start_rejected():
    """A video scene with end_s <= start_s is rejected with the field path."""
    manifest = {
        "manifest_version": "0.1.0",
        "scenes": [
            {
                "index": 1,
                "type": "video",
                "source": "/tmp/clip.mp4",
                "start_s": 10.0,
                "end_s": 5.0,   # bad: end before start
            }
        ],
    }
    is_valid, errors = validate_manifest(manifest)
    failures = []
    if is_valid:
        failures.append("Expected end_s < start_s to be rejected")
    # Error should contain field path pointing into scenes
    if not any("end_s" in e or "scenes" in e for e in errors):
        failures.append(f"Error message should reference scenes or end_s, got: {errors}")
    return failures


def test_invalid_rotate_rejected():
    """A rotate value not in [90, 180, 270] is rejected."""
    manifest = {
        "manifest_version": "0.1.0",
        "scenes": [
            {
                "index": 1,
                "type": "video",
                "source": "/tmp/clip.mp4",
                "start_s": 0.0,
                "end_s": 5.0,
                "rotate": 45,   # invalid
            }
        ],
    }
    is_valid, errors = validate_manifest(manifest)
    failures = []
    if is_valid:
        failures.append("Expected rotate=45 to be rejected")
    if not any("rotate" in e for e in errors):
        failures.append(f"Error message should mention 'rotate', got: {errors}")
    return failures


def test_missing_required_fields_rejected():
    """A video scene missing 'source' is rejected with a field-level error."""
    manifest = {
        "manifest_version": "0.1.0",
        "scenes": [
            {
                "index": 1,
                "type": "video",
                # source missing
                "start_s": 0.0,
                "end_s": 5.0,
            }
        ],
    }
    is_valid, errors = validate_manifest(manifest)
    failures = []
    if is_valid:
        failures.append("Expected video scene missing 'source' to be rejected")
    if not any("source" in e or "scenes" in e for e in errors):
        failures.append(f"Error message should reference 'source' or scenes, got: {errors}")
    return failures


def test_error_messages_contain_field_path():
    """Error messages must contain a dot-path so the agent can find the field."""
    manifest = {
        "manifest_version": "0.1.0",
        "footage": {
            "source_clips": [
                {"path": "/tmp/clip.mp4", "start_s": 5.0, "end_s": 2.0}  # bad
            ],
            "assembly_order": [0],
        },
    }
    is_valid, errors = validate_manifest(manifest)
    failures = []
    if is_valid:
        failures.append("Expected footage clip with end_s < start_s to be rejected")
    # Each error should look like "some.field.path: message"
    for e in errors:
        if ":" not in e:
            failures.append(f"Error missing field path separator ':': {e!r}")
    return failures


def test_write_manifest_scenes_validates_before_write():
    """write_manifest_scenes() raises ManifestValidationError for invalid data."""
    from packs.video.tools import write_manifest_scenes

    tmp = tempfile.mktemp(suffix=".yaml")
    # bad scene: end_s before start_s
    scenes = [{"source": "/tmp/clip.mp4", "start_s": 10.0, "end_s": 2.0}]
    failures = []
    try:
        write_manifest_scenes(tmp, scenes)
        failures.append("Expected ManifestValidationError to be raised, but write succeeded")
    except ManifestValidationError as e:
        msg = str(e)
        if "end_s" not in msg and "start_s" not in msg:
            failures.append(f"Error message should mention end_s/start_s, got: {msg}")
    except Exception as e:
        failures.append(f"Wrong exception type raised: {type(e).__name__}: {e}")
    return failures


def test_write_manifest_scenes_stamps_version():
    """write_manifest_scenes() writes manifest_version: 0.1.0 to disk."""
    import yaml
    from packs.video.tools import write_manifest_scenes

    tmp = tempfile.mktemp(suffix=".yaml")
    scenes = [{"source": "/tmp/clip.mp4", "start_s": 0.0, "end_s": 5.0}]
    failures = []
    try:
        result = write_manifest_scenes(tmp, scenes)
        if not result.get("success"):
            failures.append(f"write_manifest_scenes returned failure: {result}")
            return failures
        with open(tmp) as f:
            written = yaml.safe_load(f)
        if written.get("manifest_version") != "0.1.0":
            failures.append(
                f"Expected manifest_version '0.1.0', got: {written.get('manifest_version')!r}"
            )
    except Exception as e:
        failures.append(f"Unexpected error: {e}")
    return failures


# ── Runner ────────────────────────────────────────────────────────────────────

TESTS = [
    ("valid manifest passes", test_valid_manifest_passes),
    ("missing version defaults to 0.1.0", test_missing_version_defaults_to_current),
    ("wrong version rejected", test_wrong_version_rejected),
    ("end_s before start_s rejected", test_end_before_start_rejected),
    ("invalid rotate value rejected", test_invalid_rotate_rejected),
    ("video scene missing source rejected", test_missing_required_fields_rejected),
    ("error messages contain field path", test_error_messages_contain_field_path),
    ("write_manifest_scenes validates before write", test_write_manifest_scenes_validates_before_write),
    ("write_manifest_scenes stamps manifest_version", test_write_manifest_scenes_stamps_version),
]


def main():
    print("manifest validator tests")
    print("=" * 50)
    passed = 0
    failed = 0
    for name, fn in TESTS:
        ok = _run(name, fn)
        if ok:
            passed += 1
        else:
            failed += 1
    print("=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
