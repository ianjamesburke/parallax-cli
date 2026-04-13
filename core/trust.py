"""
Trust Score System
==================
Parallax agents start with trust=0.1 — they ask about nearly everything.
As HoP correctly predicts user decisions, trust increases.
At trust=1.0 the system operates fully autonomously.

Trust is earned through prediction accuracy, not time.
Every decision HoP makes is predicted first, then compared to the actual outcome.
Correct predictions accumulate into evidence for trust increases.

Trust is stored in logs/trust.json and persists across runs.

Thresholds:
  0.0 - 0.4   Low      Show multiple choice, user picks
  0.4 - 0.75  Medium   Show HoP recommendation, ask confirm/override
  0.75 - 1.0  High     Act on prediction, notify user (no gate)

Trust increases:
  After every 10 consecutive correct predictions: trust += 0.05 (max 1.0)
  HoP surfaces a proposal to the user: "I've been right 10/10 — raise autonomy?"
  User can approve, decline, or set a custom value.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import uuid


from core.paths import TRUST_FILE


@dataclass
class Prediction:
    prediction_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    run_id: str = ""
    concept_id: str = ""
    situation: str = ""          # what HoP was deciding
    options: list = field(default_factory=list)
    predicted: str = ""          # what HoP predicted user would want
    actual: Optional[str] = None # what user actually chose (None until resolved)
    correct: Optional[bool] = None
    resolved_at: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class TrustState:
    score: float = 0.1
    total_predictions: int = 0
    correct_predictions: int = 0
    consecutive_correct: int = 0   # resets on any wrong prediction
    last_increase_at: Optional[str] = None
    last_decrease_at: Optional[str] = None
    history: list = field(default_factory=list)  # list of Prediction dicts, last 100


class TrustScore:
    """
    Manages the trust score for a Parallax agent team.
    Persists to logs/trust.json.

    Usage:
        trust = TrustScore()
        pred = trust.predict(run_id, concept_id, situation="...", options=["A", "B", "C"])
        # ... present options to user, get their choice ...
        trust.record_outcome(pred.prediction_id, actual="A")
    """

    def __init__(self, trust_file: Path = TRUST_FILE):
        self.trust_file = trust_file
        self.state = self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> TrustState:
        try:
            if self.trust_file.exists():
                data = json.loads(self.trust_file.read_text())
                return TrustState(**{k: v for k, v in data.items() if k != "history"},
                                  history=data.get("history", []))
        except Exception as e:
            print(f"[trust] Could not load trust state, starting fresh: {e}")
        return TrustState()

    def _save(self):
        try:
            self.trust_file.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(self.state)
            # Keep only last 100 predictions in history
            data["history"] = data["history"][-100:]
            self.trust_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"[trust] ERROR saving trust state: {e}")
            raise

    # ── Core API ─────────────────────────────────────────────────────────────

    def predict(self, run_id: str, concept_id: str, situation: str,
                options: list[str], llm_prediction: str) -> Prediction:
        """
        Record a prediction before asking the user.
        llm_prediction: what the LLM thinks the user will choose.
        Returns a Prediction object — store the prediction_id to resolve later.
        """
        pred = Prediction(
            run_id=run_id,
            concept_id=concept_id,
            situation=situation,
            options=options,
            predicted=llm_prediction,
        )
        self.state.history.append(asdict(pred))
        self._save()
        return pred

    def record_outcome(self, prediction_id: str, actual: str) -> bool:
        """
        Record what the user actually chose. Returns True if prediction was correct.
        Triggers trust adjustment logic.
        """
        # Find prediction in history
        pred_dict = next((p for p in self.state.history if p["prediction_id"] == prediction_id), None)
        if not pred_dict:
            print(f"[trust] WARNING: prediction {prediction_id} not found in history")
            return False

        correct = (actual.strip().lower() == pred_dict["predicted"].strip().lower())

        # Update prediction record
        pred_dict["actual"] = actual
        pred_dict["correct"] = correct
        pred_dict["resolved_at"] = datetime.now(timezone.utc).isoformat()

        # Update state
        self.state.total_predictions += 1
        if correct:
            self.state.correct_predictions += 1
            self.state.consecutive_correct += 1
        else:
            self.state.consecutive_correct = 0

        self._save()
        return correct

    def maybe_increase_trust(self) -> Optional[str]:
        """
        Check if trust should increase. Returns a proposal string if yes, None if not.
        Call this after record_outcome.
        Trigger: 10 consecutive correct predictions.
        """
        if self.state.consecutive_correct > 0 and self.state.consecutive_correct % 10 == 0:
            new_score = min(1.0, round(self.state.score + 0.05, 2))
            if new_score > self.state.score:
                return (
                    f"I've correctly predicted your preference {self.state.consecutive_correct} times "
                    f"in a row (accuracy: {self.accuracy_30d():.0%}). "
                    f"Want to raise my autonomy from {self.state.score:.2f} → {new_score:.2f}? "
                    f"(yes / no / set X.XX)"
                )
        return None

    def apply_trust_change(self, new_score: float, reason: str = "user-approved"):
        """Directly set the trust score (called after user approves an increase proposal)."""
        old = self.state.score
        self.state.score = round(max(0.0, min(1.0, new_score)), 2)
        if self.state.score > old:
            self.state.last_increase_at = datetime.now(timezone.utc).isoformat()
        else:
            self.state.last_decrease_at = datetime.now(timezone.utc).isoformat()
        self.state.consecutive_correct = 0  # reset streak after any change
        self._save()
        print(f"[trust] Score: {old:.2f} → {self.state.score:.2f} ({reason})")

    # ── Query API ─────────────────────────────────────────────────────────────

    @property
    def score(self) -> float:
        return self.state.score

    def autonomy_level(self) -> str:
        """Human-readable description of current trust level."""
        s = self.state.score
        if s < 0.4:
            return "low — presenting all options for user selection"
        elif s < 0.75:
            return "medium — recommending actions, asking for confirmation"
        else:
            return "high — acting autonomously, notifying user"

    def accuracy_30d(self) -> float:
        """Rolling prediction accuracy over last 30 resolved predictions."""
        resolved = [p for p in self.state.history[-30:] if p.get("correct") is not None]
        if not resolved:
            return 0.0
        return sum(1 for p in resolved if p["correct"]) / len(resolved)

    def snapshot(self) -> dict:
        """Return trust state snapshot for run log headers."""
        return {
            "score": self.state.score,
            "autonomy_level": self.autonomy_level(),
            "total_predictions": self.state.total_predictions,
            "correct_predictions": self.state.correct_predictions,
            "consecutive_correct": self.state.consecutive_correct,
            "accuracy_30d": round(self.accuracy_30d(), 3),
            "last_increase_at": self.state.last_increase_at,
        }
