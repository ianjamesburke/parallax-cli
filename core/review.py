"""
Review CLI
==========
Structured human review: rating + notes → fed back into trust system.

The pre-watch brief gives a hypothesis; the review records the actual.
If predicted_rating matches actual ±1, it's a correct prediction.

Usage:
    review = ReviewSession(run_id, concept_id, trust)
    review.collect(pre_watch_brief)  # interactive: prompts for rating + notes
    # OR non-interactive:
    review.record(rating=7, notes="hook is good, scene 4 drags")
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.trust import TrustScore


class ReviewSession:
    """
    Collects a human review for a completed run and feeds it into the trust system.
    """

    RATING_TOLERANCE = 1  # predicted ±1 counts as correct

    def __init__(self, run_id: str, concept_id: str, trust: TrustScore):
        self.run_id = run_id
        self.concept_id = concept_id
        self.trust = trust
        from core.paths import run_dir
        self.run_dir = run_dir(run_id)

    def collect(self, pre_watch_brief: dict, output_path: str = "") -> dict:
        """
        Interactive review: display brief, prompt for rating + notes.
        Returns the review record.
        """
        from core.pre_watch_brief import PreWatchBrief
        pwb = PreWatchBrief(self.run_id, self.concept_id)
        print(pwb.display(pre_watch_brief, output_path))

        # Prompt for rating
        print("  Rating (1-10): ", end="", flush=True)
        try:
            raw_rating = input().strip()
            rating = int(raw_rating)
            rating = max(1, min(10, rating))
        except (ValueError, EOFError, KeyboardInterrupt):
            print("  [Skipped — no rating recorded]")
            return {"skipped": True}

        # Prompt for notes
        print("  Notes: ", end="", flush=True)
        try:
            notes = input().strip()
        except (EOFError, KeyboardInterrupt):
            notes = ""

        return self.record(rating, notes, pre_watch_brief)

    def record(self, rating: int, notes: str,
               pre_watch_brief: Optional[dict] = None) -> dict:
        """
        Non-interactive: record a review directly.
        Compares to predicted rating and updates trust.
        """
        predicted_rating = (pre_watch_brief or {}).get("predicted_rating", 5)
        correct = abs(rating - predicted_rating) <= self.RATING_TOLERANCE

        # Record as a trust prediction outcome
        # We create a synthetic prediction for the rating
        pred = self.trust.predict(
            run_id=self.run_id,
            concept_id=self.concept_id,
            situation=f"Predicted human rating for run {self.run_id}",
            options=[str(i) for i in range(1, 11)],
            llm_prediction=str(predicted_rating),
        )
        self.trust.record_outcome(pred.prediction_id, str(rating))

        # Check for trust increase
        proposal = self.trust.maybe_increase_trust()

        review = {
            "run_id": self.run_id,
            "concept_id": self.concept_id,
            "rating": rating,
            "notes": notes,
            "predicted_rating": predicted_rating,
            "prediction_correct": correct,
            "delta": rating - predicted_rating,
            "trust_score_after": self.trust.score,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "trust_increase_proposal": proposal,
        }

        # Save review to run dir
        try:
            review_path = self.run_dir / "review.json"
            review_path.write_text(json.dumps(review, indent=2))
        except Exception as e:
            print(f"[Review] WARNING: could not save review: {e}")

        # Print result
        match_str = "CORRECT" if correct else f"OFF BY {abs(rating - predicted_rating)}"
        print(f"\n  [Review] Rating: {rating}/10 | Predicted: {predicted_rating}/10 | {match_str}")
        print(f"  [Review] Trust: {self.trust.score:.2f} | Streak: {self.trust.state.consecutive_correct}")

        if proposal:
            print(f"\n  [Trust] {proposal}")

        return review

    def display_summary(self, review: dict) -> str:
        """Format a saved review for display."""
        lines = [
            f"Review — {review['concept_id']} / Run {review['run_id']}",
            f"  Rating: {review['rating']}/10 (predicted {review['predicted_rating']}/10)",
            f"  Match: {'✓' if review['prediction_correct'] else '✗'} (delta {review['delta']:+d})",
            f"  Notes: {review.get('notes', '—')}",
            f"  Trust after: {review['trust_score_after']:.2f}",
        ]
        return "\n".join(lines)
