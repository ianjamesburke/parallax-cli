"""
Concern Propagation System
==========================
Every agent in the network can raise a concern before, during, or after a task.
Concerns bubble upward through the hierarchy until any layer can self-heal them.
Unresolvable concerns reach the Head of Production, who decides whether to
surface to the human.

Concern severity guide:
  0.0 - 0.3  Low     — agent notes it but proceeds with a reasonable default
  0.3 - 0.6  Medium  — agent pauses, tries to self-heal, escalates if it can't
  0.6 - 0.8  High    — blocks execution, must be resolved before proceeding
  0.8 - 1.0  Critical — escalates directly to Head of Production / human

Self-healing: any layer receiving a concern can attempt to resolve it.
If resolution confidence > 0.75, the concern is closed at that layer.
If not, it propagates up.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import json
import uuid


@dataclass
class Concern:
    """A structured concern raised by any agent in the network."""
    concern_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    raised_by: str = ""                    # agent name that raised it
    severity: float = 0.5                  # 0.0 - 1.0
    blocking: bool = False                 # if True, execution pauses until resolved
    message: str = ""                      # human-readable description
    context: dict = field(default_factory=dict)  # relevant state at time of raise
    proposed_default: Optional[str] = None # what the agent would do if allowed to self-resolve
    resolved: bool = False
    resolved_by: Optional[str] = None
    resolution: Optional[str] = None
    raised_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Concern":
        return cls(**d)


class ConcernBus:
    """
    Routes concerns upward through the agent hierarchy.
    Any layer can intercept and attempt to self-heal a concern.

    Hierarchy (lowest to highest):
      asset_generator → junior_editor → senior_editor → assistant_editor
      → evaluator → head_of_production → human
    """

    HIERARCHY = [
        "asset_generator",
        "junior_editor",
        "senior_editor",
        "assistant_editor",
        "evaluator",
        "head_of_production",
        "human",
    ]

    def __init__(self, run_id: str, concept_id: str):
        self.run_id = run_id
        self.concept_id = concept_id
        self.concerns: list[Concern] = []
        self.log_path = Path(f"logs/runs/{run_id}/concerns.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def raise_concern(self, concern: Concern) -> Concern:
        """
        Raise a concern. Logs it and returns it for the caller to handle.
        If non-blocking and severity < 0.3, auto-resolves with proposed_default.
        """
        self.concerns.append(concern)
        self._log(concern, "raised")

        # Auto-resolve low-severity, non-blocking concerns with their proposed default
        if not concern.blocking and concern.severity < 0.3 and concern.proposed_default:
            return self.resolve(
                concern,
                resolved_by="auto",
                resolution=f"Auto-resolved: {concern.proposed_default}"
            )
        return concern

    def attempt_self_heal(self, concern: Concern, healer_agent: str,
                          proposed_resolution: str, confidence: float) -> bool:
        """
        Any layer can attempt to self-heal a concern.
        Returns True if healing was accepted (confidence > 0.75), False if it should propagate.
        """
        if confidence >= 0.75:
            self.resolve(concern, resolved_by=healer_agent, resolution=proposed_resolution)
            print(f"[{healer_agent}] Self-healed concern {concern.concern_id}: {proposed_resolution}")
            return True
        else:
            self._log(concern, f"heal_attempted_by_{healer_agent}_failed (confidence={confidence:.2f})")
            return False

    def resolve(self, concern: Concern, resolved_by: str, resolution: str) -> Concern:
        """Mark a concern as resolved."""
        concern.resolved = True
        concern.resolved_by = resolved_by
        concern.resolution = resolution
        self._log(concern, "resolved")
        return concern

    def unresolved(self) -> list[Concern]:
        """Return all unresolved concerns."""
        return [c for c in self.concerns if not c.resolved]

    def blocking_concerns(self) -> list[Concern]:
        """Return all unresolved blocking concerns. Execution should pause if any exist."""
        return [c for c in self.unresolved() if c.blocking]

    def escalate_to_human(self, concern: Concern) -> str:
        """
        Surface a concern to the human. Returns their response.
        Called by HeadOfProduction when it cannot self-heal.
        """
        print(f"\n[HoP → You] Concern raised by {concern.raised_by}:")
        print(f"  Severity: {concern.severity:.0%} | Blocking: {concern.blocking}")
        print(f"  Issue: {concern.message}")
        if concern.proposed_default:
            print(f"  Proposed default: {concern.proposed_default}")
        print("\nHow to resolve (or press Enter to accept proposed default):")
        response = input("> ").strip()
        resolution = response if response else (concern.proposed_default or "proceed")
        self.resolve(concern, resolved_by="human", resolution=resolution)
        return resolution

    def summary(self) -> dict:
        """Return a summary of all concerns for the run log."""
        return {
            "total": len(self.concerns),
            "resolved": sum(1 for c in self.concerns if c.resolved),
            "unresolved": len(self.unresolved()),
            "blocking": len(self.blocking_concerns()),
            "by_agent": {
                agent: [c.to_dict() for c in self.concerns if c.raised_by == agent]
                for agent in set(c.raised_by for c in self.concerns)
            }
        }

    def _log(self, concern: Concern, event: str):
        entry = {"event": event, **concern.to_dict()}
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"[ConcernBus] WARNING: could not write concern log: {e}")
