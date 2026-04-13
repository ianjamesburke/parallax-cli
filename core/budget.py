"""
Cost-Gated Autonomy
====================
Replaces the abstract trust score with dollar-denominated decision gates.

Three layers of budget protection:
1. Per-decision cap   — one action can't exceed $X without escalation
2. Per-concept budget  — total spend for a concept (ad project) is capped at $Y
3. Session velocity    — if cumulative spend in the last N minutes exceeds $Z, pause

Every decision HoP faces includes:
- What the options are
- What each costs (estimated)
- What the max loss is if the wrong one is picked (rework cost)
- Whether HoP proceeded autonomously or escalated

The gate: if max_loss < per_decision_cap AND budget_remaining > option_cost,
proceed autonomously. Otherwise surface to human with cost context.

Budget state persists in logs/budgets/{concept_id}.json.
"""

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


from core.paths import BUDGETS_DIR


@dataclass
class Spend:
    """A single recorded spend event."""
    spend_id: str
    run_id: str
    agent: str
    action: str           # what was done
    cost_usd: float       # actual cost
    estimated_usd: float  # what was predicted before execution
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class DecisionOption:
    """One option in a decision, with cost estimates."""
    name: str
    estimated_cost: float   # cost to execute this option
    rework_cost: float      # cost to undo + redo if this was wrong
    description: str = ""


@dataclass
class ConceptBudget:
    """Budget state for a single concept (ad project)."""
    concept_id: str
    total_budget: float = 20.0       # default $20 per concept
    per_decision_cap: float = 2.0    # single action can't exceed this without asking
    velocity_cap: float = 10.0       # max spend in velocity_window_minutes
    velocity_window_minutes: int = 30
    spent: float = 0.0
    spend_log: list = field(default_factory=list)   # list of Spend dicts
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class BudgetGate:
    """
    Cost-gated autonomy for HoP decisions.

    Usage:
        gate = BudgetGate("RUG-003")
        gate.set_budget(total=20.0, per_decision=2.0)

        # Before a decision:
        decision = gate.evaluate_options([
            DecisionOption("regenerate_scene", 0.02, 0.04),
            DecisionOption("regenerate_all", 2.50, 5.00),
        ])
        # decision.autonomous == True if cheapest option is safe
        # decision.escalate == True if max_loss exceeds cap

        # After execution:
        gate.record_spend(run_id, agent, action, cost, estimated)
    """

    def __init__(self, concept_id: str):
        self.concept_id = concept_id
        self.budget = self._load()

    # ── Persistence ──────────────────────────────────────────────────────

    def _budget_path(self) -> Path:
        return BUDGETS_DIR / f"{self.concept_id}.json"

    def _load(self) -> ConceptBudget:
        try:
            path = self._budget_path()
            if path.exists():
                data = json.loads(path.read_text())
                return ConceptBudget(**{k: v for k, v in data.items()})
        except Exception as e:
            print(f"[budget] Could not load budget for {self.concept_id}, starting fresh: {e}")
        return ConceptBudget(concept_id=self.concept_id)

    def _save(self):
        try:
            BUDGETS_DIR.mkdir(parents=True, exist_ok=True)
            path = self._budget_path()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.budget), indent=2))
            tmp.replace(path)
        except Exception as e:
            print(f"[budget] ERROR saving budget for {self.concept_id}: {e}")
            raise

    # ── Configuration ────────────────────────────────────────────────────

    def set_budget(self, total: Optional[float] = None,
                   per_decision: Optional[float] = None,
                   velocity: Optional[float] = None,
                   velocity_window: Optional[int] = None):
        """Update budget parameters. Only changes what's passed."""
        if total is not None:
            self.budget.total_budget = total
        if per_decision is not None:
            self.budget.per_decision_cap = per_decision
        if velocity is not None:
            self.budget.velocity_cap = velocity
        if velocity_window is not None:
            self.budget.velocity_window_minutes = velocity_window
        self._save()

    # ── Decision Gate ────────────────────────────────────────────────────

    def evaluate_options(self, options: list[DecisionOption]) -> dict:
        """
        Evaluate a set of options against budget constraints.

        Returns:
            {
                autonomous: bool,        # True if HoP can proceed without asking
                recommended: str,        # name of cheapest viable option
                max_loss: float,         # worst-case rework cost across all options
                budget_remaining: float,
                velocity_ok: bool,       # False if recent spend is too high
                reason: str,             # why autonomous or why escalating
                options_with_cost: list,  # each option with cost context
            }
        """
        if not options:
            return {
                "autonomous": False,
                "recommended": None,
                "max_loss": 0,
                "budget_remaining": self.remaining,
                "velocity_ok": True,
                "reason": "No options provided",
                "options_with_cost": [],
            }

        # Sort by execution cost (cheapest first)
        sorted_opts = sorted(options, key=lambda o: o.estimated_cost)
        cheapest = sorted_opts[0]
        max_loss = max(o.rework_cost for o in options)

        budget_remaining = self.remaining
        velocity_ok = self._check_velocity()

        # Annotate each option
        options_with_cost = []
        for opt in options:
            affordable = opt.estimated_cost <= budget_remaining
            within_cap = opt.estimated_cost <= self.budget.per_decision_cap
            options_with_cost.append({
                "name": opt.name,
                "description": opt.description,
                "estimated_cost": opt.estimated_cost,
                "rework_cost": opt.rework_cost,
                "affordable": affordable,
                "within_decision_cap": within_cap,
            })

        # Decision logic
        can_afford_cheapest = cheapest.estimated_cost <= budget_remaining
        cheapest_within_cap = cheapest.estimated_cost <= self.budget.per_decision_cap
        max_loss_acceptable = max_loss <= self.budget.per_decision_cap

        if not can_afford_cheapest:
            autonomous = False
            reason = f"Budget exhausted — ${budget_remaining:.2f} remaining, cheapest option costs ${cheapest.estimated_cost:.2f}"
        elif not velocity_ok:
            autonomous = False
            reason = f"Velocity cap hit — spent too much in last {self.budget.velocity_window_minutes} min"
        elif not cheapest_within_cap:
            autonomous = False
            reason = f"Cheapest option (${cheapest.estimated_cost:.2f}) exceeds per-decision cap (${self.budget.per_decision_cap:.2f})"
        elif not max_loss_acceptable:
            autonomous = False
            reason = f"Max rework cost (${max_loss:.2f}) exceeds per-decision cap — wrong choice is expensive"
        else:
            autonomous = True
            reason = f"Within budget (${budget_remaining:.2f} left), within cap (${cheapest.estimated_cost:.2f}), max loss acceptable (${max_loss:.2f})"

        return {
            "autonomous": autonomous,
            "recommended": cheapest.name,
            "max_loss": max_loss,
            "budget_remaining": budget_remaining,
            "velocity_ok": velocity_ok,
            "reason": reason,
            "options_with_cost": options_with_cost,
        }

    # ── Spend Tracking ───────────────────────────────────────────────────

    def record_spend(self, run_id: str, agent: str, action: str,
                     cost_usd: float, estimated_usd: float = 0.0) -> Spend:
        """Record actual spend after an action executes."""
        import uuid
        spend = Spend(
            spend_id=str(uuid.uuid4())[:8],
            run_id=run_id,
            agent=agent,
            action=action,
            cost_usd=cost_usd,
            estimated_usd=estimated_usd,
        )
        self.budget.spent += cost_usd
        self.budget.spend_log.append(asdict(spend))
        # Keep only last 500 spend entries
        self.budget.spend_log = self.budget.spend_log[-500:]
        self._save()
        return spend

    @property
    def remaining(self) -> float:
        return max(0.0, self.budget.total_budget - self.budget.spent)

    def _check_velocity(self) -> bool:
        """Check if recent spend exceeds the velocity cap."""
        window_seconds = self.budget.velocity_window_minutes * 60
        cutoff = time.time() - window_seconds
        recent_spend = 0.0
        for entry in self.budget.spend_log:
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                if ts.timestamp() > cutoff:
                    recent_spend += entry["cost_usd"]
            except (KeyError, ValueError):
                continue
        return recent_spend <= self.budget.velocity_cap

    # ── Reporting ────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Budget state for run log headers."""
        return {
            "concept_id": self.concept_id,
            "total_budget": self.budget.total_budget,
            "spent": round(self.budget.spent, 4),
            "remaining": round(self.remaining, 4),
            "per_decision_cap": self.budget.per_decision_cap,
            "velocity_cap": self.budget.velocity_cap,
            "velocity_ok": self._check_velocity(),
            "spend_count": len(self.budget.spend_log),
        }

    def display(self) -> str:
        """Human-readable budget status."""
        snap = self.snapshot()
        pct = (snap["spent"] / snap["total_budget"] * 100) if snap["total_budget"] > 0 else 0
        lines = [
            f"[Budget] {self.concept_id}: ${snap['spent']:.2f} / ${snap['total_budget']:.2f} ({pct:.0f}%)",
            f"  Remaining: ${snap['remaining']:.2f}",
            f"  Per-decision cap: ${snap['per_decision_cap']:.2f}",
            f"  Velocity: {'OK' if snap['velocity_ok'] else 'EXCEEDED'} (${snap['velocity_cap']:.2f} / {self.budget.velocity_window_minutes} min)",
        ]
        return "\n".join(lines)
