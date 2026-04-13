"""
Manifest validation — wraps write_manifest_scenes() with schema enforcement.

validate_manifest() is the public API. It returns (is_valid, errors) so callers
can decide whether to raise or log. write_manifest_scenes() calls it internally
and raises ManifestValidationError on failure — the agent loop sees this as a
tool_result error and the editor can self-correct on the next turn.
"""

from pydantic import ValidationError

from packs.video.manifest_schema import Manifest, MANIFEST_VERSION


class ManifestValidationError(Exception):
    """Raised when a manifest fails schema validation.

    The message is intentionally agent-friendly: each line is one field error
    with its dot-path, so the editor can identify exactly which field to fix.
    """


def validate_manifest(manifest_dict: dict) -> tuple[bool, list[str]]:
    """
    Validate a manifest dict against the Manifest schema.

    Returns:
        (True, [])           — manifest is valid
        (False, [<errors>])  — one error string per failing field
    """
    # Inject default manifest_version if absent (backwards compat with pre-0.1.0 files)
    if "manifest_version" not in manifest_dict:
        manifest_dict = {**manifest_dict, "manifest_version": MANIFEST_VERSION}

    try:
        Manifest.model_validate(manifest_dict)
        return True, []
    except ValidationError as e:
        errors = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        return False, errors


def validate_or_raise(manifest_dict: dict) -> None:
    """
    Validate a manifest dict and raise ManifestValidationError if invalid.

    Used by write_manifest_scenes() before writing to disk.
    """
    is_valid, errors = validate_manifest(manifest_dict)
    if not is_valid:
        msg = "Manifest validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ManifestValidationError(msg)
