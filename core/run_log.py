"""
Run Log
=======
Every Parallax job opens one RunLog. It is the single source of truth
for what happened during a run — every agent call, every decision,
every concern, every cost, and the trust state at the time.

The improvement officer reads these logs to replay runs and propose
systemic improvements. The cost tracker writes to per-concept JSONL
files; the run log consolidates everything into one JSON per run.

File: logs/runs/{run_id}/run.json
Updated atomically throughout the run.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class AgentCall:
    """Record of a single LLM API call made during the run."""
    call_id: str
    agent: str
    model: str
    timestamp: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    purpose: str       # what the call was for, e.g. "brief_validation", "script_generation"
    input_summary: str # first 200 chars of the input
    output_summary: str # first 200 chars of the output


@dataclass
class Decision:
    """A judgment call made by HoP — with prediction and actual outcome."""
    decision_id: str
    timestamp: str
    situation: str
    options: list
    prediction: str         # what HoP predicted user would want
    prediction_id: str      # links to TrustScore history
    actual: Optional[str]   # what user actually chose
    correct: Optional[bool]
    autonomy_level: str     # "low" | "medium" | "high" — trust level at time of decision
    trust_score_at_time: float
    resolution_method: str  # "user_selected" | "user_confirmed" | "autonomous"


@dataclass
class RunLog:
    run_id: str
    concept_id: str
    started_at: str
    job_type: str
    job_summary: str        # first 200 chars of the job content
    trust_snapshot: dict    # from TrustScore.snapshot() — state at run START
    pack: str               # which specialist pack was used, e.g. "video"
    test_mode: bool = False
    completed_at: Optional[str] = None
    status: str = "running"  # running | completed | failed | escalated
    decisions: list = field(default_factory=list)    # list of Decision dicts
    concerns: list = field(default_factory=list)     # from ConcernBus.summary()
    agent_calls: list = field(default_factory=list)  # list of AgentCall dicts
    cost_summary: dict = field(default_factory=dict) # from CostTracker.cost_report()
    final_output: Optional[dict] = None
    notes: str = ""


class RunLogger:
    """
    Opens and manages a run log for a single job execution.
    Write to it throughout the run; it saves atomically after each update.

    Usage:
        logger = RunLogger(run_id, concept_id, job, trust.snapshot(), pack="video")
        logger.log_agent_call(...)
        logger.log_decision(...)
        logger.complete(output=result, cost_summary=cost_report)
    """

    def __init__(self, run_id: str, concept_id: str, job: dict,
                 trust_snapshot: dict, pack: str = "video", test_mode: bool = False):
        from core.paths import run_dir
        self.run_dir = run_dir(run_id)
        self.log_path = self.run_dir / "run.json"

        job_content = job.get("content", "")
        if isinstance(job_content, str):
            job_summary = job_content[:200]
        else:
            job_summary = str(job_content)[:200]

        self.log = RunLog(
            run_id=run_id,
            concept_id=concept_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            job_type=job.get("type", "unknown"),
            job_summary=job_summary,
            trust_snapshot=trust_snapshot,
            pack=pack,
            test_mode=test_mode,
        )
        self._write()

    def log_agent_call(self, agent: str, model: str, purpose: str,
                       input_tokens: int, output_tokens: int, cost_usd: float,
                       input_summary: str = "", output_summary: str = ""):
        import uuid
        call = AgentCall(
            call_id=str(uuid.uuid4())[:8],
            agent=agent,
            model=model,
            timestamp=datetime.now(timezone.utc).isoformat(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            purpose=purpose,
            input_summary=input_summary[:200],
            output_summary=output_summary[:200],
        )
        self.log.agent_calls.append(asdict(call))
        self._write()

    def log_decision(self, situation: str, options: list, prediction: str,
                     prediction_id: str, actual: Optional[str], correct: Optional[bool],
                     autonomy_level: str, trust_score: float,
                     resolution_method: str = "user_selected"):
        import uuid
        decision = Decision(
            decision_id=str(uuid.uuid4())[:8],
            timestamp=datetime.now(timezone.utc).isoformat(),
            situation=situation,
            options=options,
            prediction=prediction,
            prediction_id=prediction_id,
            actual=actual,
            correct=correct,
            autonomy_level=autonomy_level,
            trust_score_at_time=trust_score,
            resolution_method=resolution_method,
        )
        self.log.decisions.append(asdict(decision))
        self._write()

    def log_concerns(self, concern_summary: dict):
        self.log.concerns = concern_summary.get("by_agent", {})
        self._write()

    def complete(self, output: Optional[dict] = None,
                 cost_summary: Optional[dict] = None,
                 status: str = "completed", notes: str = ""):
        self.log.completed_at = datetime.now(timezone.utc).isoformat()
        self.log.status = status
        self.log.final_output = output
        self.log.cost_summary = cost_summary or {}
        self.log.notes = notes
        self._write()

    def _write(self):
        """Atomic write — never leaves a partial file."""
        tmp = self.log_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self.log), indent=2))
            tmp.replace(self.log_path)
        except Exception as e:
            print(f"[run_log] ERROR writing run log {self.log_path}: {e}")
            raise
