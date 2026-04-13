"""
Pydantic v2 schema for the Parallax video manifest.

Covers the fields actually written by write_manifest_scenes() and the
_manifest_prompt() editors. Validates on every write so the agent loop
sees errors on the next turn and can self-correct.
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

MANIFEST_VERSION = "0.1.0"

VALID_SCENE_TYPES = {"video", "text_overlay", "still", "effect_overlay"}
VALID_ROTATIONS = {90, 180, 270}
MAX_TRACK = 7


# ── Scene (editor output shape) ───────────────────────────────────────────────

class Scene(BaseModel):
    """One scene entry in the manifest's scenes list, as written by editors."""
    index: int
    type: Literal["video", "text_overlay", "still", "effect_overlay"]

    # video / still
    source: Optional[str] = None
    start_s: Optional[float] = None
    end_s: Optional[float] = None
    rotate: Optional[int] = None
    description: Optional[str] = None

    # text_overlay
    overlay_text: Optional[str] = None
    estimated_duration_s: Optional[float] = None

    # effect_overlay
    base_scene: Optional[int] = None
    filter: Optional[str] = None

    # still
    still: Optional[str] = None

    @field_validator("rotate")
    @classmethod
    def rotate_must_be_valid(cls, v):
        if v is not None and v not in VALID_ROTATIONS:
            raise ValueError(f"rotate must be one of {sorted(VALID_ROTATIONS)}, got {v}")
        return v

    @model_validator(mode="after")
    def check_type_requirements(self):
        if self.type == "video":
            if not self.source:
                raise ValueError("video scene requires 'source'")
            if self.start_s is None:
                raise ValueError("video scene requires 'start_s'")
            if self.end_s is None:
                raise ValueError("video scene requires 'end_s'")
            if self.end_s <= self.start_s:
                raise ValueError(
                    f"end_s ({self.end_s}) must be > start_s ({self.start_s})"
                )
        elif self.type == "text_overlay":
            if not self.overlay_text:
                raise ValueError("text_overlay scene requires 'overlay_text'")
        elif self.type == "still":
            path = self.still or self.source
            if not path:
                raise ValueError("still scene requires 'still' or 'source'")
        elif self.type == "effect_overlay":
            if not self.filter:
                raise ValueError("effect_overlay scene requires 'filter'")
            if self.base_scene is None and not self.source:
                raise ValueError("effect_overlay scene requires 'base_scene' or 'source'")
        return self


# ── Footage section (written by write_manifest_scenes) ───────────────────────

class SourceClip(BaseModel):
    """One entry in footage.source_clips — the assembled clip list."""
    path: str
    start_s: float
    end_s: float
    rotate: Optional[int] = None
    label: Optional[str] = None
    type: Optional[str] = None  # None or "video" means video clip; others skip time validation
    overlay_text: Optional[str] = None
    estimated_duration_s: Optional[float] = None

    @field_validator("rotate")
    @classmethod
    def rotate_must_be_valid(cls, v):
        if v is not None and v not in VALID_ROTATIONS:
            raise ValueError(f"rotate must be one of {sorted(VALID_ROTATIONS)}, got {v}")
        return v

    @model_validator(mode="after")
    def end_after_start(self):
        # Only enforce time bounds for video clips; text/effect/still clips use estimated_duration_s
        clip_type = self.type or "video"
        if clip_type == "video" and self.end_s <= self.start_s:
            raise ValueError(
                f"end_s ({self.end_s}) must be > start_s ({self.start_s})"
            )
        return self


class Footage(BaseModel):
    source_clips: list[SourceClip]
    assembly_order: list[int]

    @model_validator(mode="after")
    def assembly_order_in_range(self):
        n = len(self.source_clips)
        for i in self.assembly_order:
            if i < 0 or i >= n:
                raise ValueError(
                    f"assembly_order index {i} out of range for {n} source_clips"
                )
        return self


# ── Config section ────────────────────────────────────────────────────────────

class Config(BaseModel):
    resolution: str = "1920x1080"
    fps: int = 30


# ── Top-level Manifest ────────────────────────────────────────────────────────

class Manifest(BaseModel):
    manifest_version: str = MANIFEST_VERSION
    brief: Optional[dict] = None
    config: Optional[Config] = None
    scenes: Optional[list[Scene]] = None
    footage: Optional[Footage] = None

    @field_validator("manifest_version")
    @classmethod
    def version_compatible(cls, v):
        if v != MANIFEST_VERSION:
            raise ValueError(
                f"manifest_version '{v}' not supported (expected '{MANIFEST_VERSION}')"
            )
        return v
