"""
JSONL-based cost tracker for the NRTV agent network.
Every agent call logs token usage and cost to per-concept JSONL files in logs/costs/.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# Token pricing per million tokens (as of 2026-04)
PRICING = {
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
}

from core.paths import LOG_ROOT
COSTS_DIR = LOG_ROOT / "logs" / "costs"


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute USD cost for a call given model and token counts."""
    pricing = PRICING.get(model)
    if not pricing:
        # Unknown model — default to sonnet pricing as conservative estimate
        pricing = PRICING["claude-sonnet-4-6"]
    cost = (input_tokens / 1_000_000) * pricing["input"]
    cost += (output_tokens / 1_000_000) * pricing["output"]
    return round(cost, 6)


def log_call(
    concept_id: str,
    agent: str,
    run_id: str,
    input_tokens: int,
    output_tokens: int,
    model: str,
) -> float:
    """
    Log a single agent API call to logs/costs/{concept_id}.jsonl.
    Returns the cost in USD for this call.
    """
    try:
        COSTS_DIR.mkdir(parents=True, exist_ok=True)
        cost = _compute_cost(model, input_tokens, output_tokens)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "concept_id": concept_id,
            "agent": agent,
            "run_id": run_id,
            "model": model,
            "tokens_in": input_tokens,
            "tokens_out": output_tokens,
            "cost_usd": cost,
        }
        log_path = COSTS_DIR / f"{concept_id}.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return cost
    except Exception as e:
        print(f"[cost_tracker] ERROR logging call for {concept_id}/{agent}: {e}")
        raise


def get_run_cost(run_id: str) -> float:
    """Return total cost in USD for all calls in a given run, across all concepts."""
    try:
        total = 0.0
        for path in COSTS_DIR.glob("*.jsonl"):
            try:
                with open(path) as f:
                    for line in f:
                        entry = json.loads(line)
                        if entry.get("run_id") == run_id:
                            total += entry.get("cost_usd", 0.0)
            except Exception as e:
                print(f"[cost_tracker] ERROR reading {path}: {e}")
        return round(total, 6)
    except Exception as e:
        print(f"[cost_tracker] ERROR computing run cost for {run_id}: {e}")
        raise


def get_concept_cost(concept_id: str) -> float:
    """Return total cost in USD across all runs for a concept."""
    try:
        log_path = COSTS_DIR / f"{concept_id}.jsonl"
        if not log_path.exists():
            return 0.0
        total = 0.0
        with open(log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    total += entry.get("cost_usd", 0.0)
                except json.JSONDecodeError as e:
                    print(f"[cost_tracker] Skipping malformed line in {log_path}: {e}")
        return round(total, 6)
    except Exception as e:
        print(f"[cost_tracker] ERROR computing concept cost for {concept_id}: {e}")
        raise


def cost_report(concept_id: str) -> dict:
    """
    Return a formatted cost breakdown dict for a concept.
    Structure: {concept_id, total_usd, by_agent: {agent_name: {calls, tokens_in, tokens_out, cost_usd}}}
    """
    try:
        log_path = COSTS_DIR / f"{concept_id}.jsonl"
        if not log_path.exists():
            return {"concept_id": concept_id, "total_usd": 0.0, "by_agent": {}}

        by_agent = {}
        with open(log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    agent = entry.get("agent", "unknown")
                    if agent not in by_agent:
                        by_agent[agent] = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
                    by_agent[agent]["calls"] += 1
                    by_agent[agent]["tokens_in"] += entry.get("tokens_in", 0)
                    by_agent[agent]["tokens_out"] += entry.get("tokens_out", 0)
                    by_agent[agent]["cost_usd"] += entry.get("cost_usd", 0.0)
                except json.JSONDecodeError as e:
                    print(f"[cost_tracker] Skipping malformed line in {log_path}: {e}")

        # Round floats
        for agent_data in by_agent.values():
            agent_data["cost_usd"] = round(agent_data["cost_usd"], 6)

        total = sum(a["cost_usd"] for a in by_agent.values())
        return {
            "concept_id": concept_id,
            "total_usd": round(total, 6),
            "by_agent": by_agent,
        }
    except Exception as e:
        print(f"[cost_tracker] ERROR generating cost report for {concept_id}: {e}")
        raise


class CostTracker:
    """
    Thin class wrapper around module-level cost tracking functions.
    Agents import this for convenience; all state is stored in JSONL files.
    """

    def log_call(self, concept_id: str, agent: str, run_id: str,
                 input_tokens: int, output_tokens: int, model: str, **_extra) -> float:
        return log_call(concept_id, agent, run_id, input_tokens, output_tokens, model)

    def get_run_cost(self, run_id: str) -> float:
        return get_run_cost(run_id)

    def get_concept_cost(self, concept_id: str) -> float:
        return get_concept_cost(concept_id)

    def cost_report(self, concept_id: str) -> dict:
        return cost_report(concept_id)
