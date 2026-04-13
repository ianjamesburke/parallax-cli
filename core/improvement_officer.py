"""
ImprovementOfficer agent — offline, run manually.
Analyzes run logs to identify systemic issues and propose improvements.
Uses claude-opus-4-6 for deep reasoning.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
from core.cost_tracker import log_call, cost_report

MODEL = "claude-opus-4-6"
RUNS_DIR = Path("logs/runs")
IMPROVEMENTS_DIR = Path("logs/improvements")

SYSTEM = """You are the Improvement Officer at NRTV — an analyst who reviews production logs
to identify systemic problems and propose concrete fixes.

You look for:
- Which agents escalated most and why
- Which job types required the most iterations
- Which concepts cost the most and whether that was justified
- Patterns suggesting agent instructions, thresholds, or routing logic need updating

Your proposals are specific and actionable. You cite evidence from logs.
You prioritize by impact: what change would save the most time or cost?

Return JSON only."""


class ImprovementOfficer:
    """
    Offline analyst. Reviews all run logs and proposes systemic improvements.
    Run manually — not part of the live pipeline.

    v1: Reads JSONL logs, calls claude-opus-4-6, writes report.
    v2: Could replay runs with agent swaps to A/B test improvements.
    """

    MODEL = MODEL

    def analyze(self, concept_id: Optional[str] = None) -> dict:
        """
        Analyze logs and produce improvement proposals.

        Args:
            concept_id: if set, focus on a single concept; otherwise analyze all

        Returns:
            {proposals: list[{agent, issue, proposed_change, evidence, priority}],
             summary: str, report_path: str}
        """
        client = anthropic.Anthropic()

        log_data = self._collect_logs(concept_id)
        if not log_data:
            print("[ImprovementOfficer] No run logs found to analyze.")
            return {"proposals": [], "summary": "No data.", "report_path": ""}

        prompt = self._build_prompt(log_data, concept_id)

        try:
            response = client.messages.create(
                model=self.MODEL,
                max_tokens=4096,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            raise RuntimeError(f"[ImprovementOfficer] API call failed: {e}") from e

        # Log cost to a special "improvement_officer" concept
        try:
            log_call(
                concept_id=concept_id or "ALL",
                agent="improvement_officer",
                run_id=f"improvement_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=self.MODEL,
            )
        except Exception as e:
            print(f"[ImprovementOfficer] WARNING: cost logging failed: {e}")

        result = self._parse_response(response.content[0].text)
        report_path = self._write_report(result, concept_id)
        result["report_path"] = str(report_path)

        print(f"[ImprovementOfficer] Report written to {report_path}")
        return result

    def _collect_logs(self, concept_id: Optional[str]) -> dict:
        """Collect all relevant run logs and cost data."""
        data = {"runs": [], "cost_reports": []}

        if not RUNS_DIR.exists():
            return data

        run_dirs = list(RUNS_DIR.iterdir()) if RUNS_DIR.exists() else []

        for run_dir in run_dirs:
            if not run_dir.is_dir():
                continue
            run_data = {"run_id": run_dir.name}

            for json_file in run_dir.glob("*.json"):
                try:
                    with open(json_file) as f:
                        run_data[json_file.stem] = json.load(f)
                except Exception as e:
                    print(f"[ImprovementOfficer] Could not read {json_file}: {e}")

            for jsonl_file in run_dir.glob("*.jsonl"):
                try:
                    entries = []
                    with open(jsonl_file) as f:
                        for line in f:
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                    run_data[jsonl_file.stem] = entries
                except Exception as e:
                    print(f"[ImprovementOfficer] Could not read {jsonl_file}: {e}")

            # Filter by concept_id if specified
            job = run_data.get("job", {})
            if concept_id and job.get("concept_id") != concept_id:
                continue

            data["runs"].append(run_data)

        # Add cost reports
        from pathlib import Path as P
        costs_dir = P("logs/costs")
        if costs_dir.exists():
            for cost_file in costs_dir.glob("*.jsonl"):
                cid = cost_file.stem
                if concept_id and cid != concept_id:
                    continue
                try:
                    data["cost_reports"].append(cost_report(cid))
                except Exception as e:
                    print(f"[ImprovementOfficer] Could not generate cost report for {cid}: {e}")

        return data

    def _build_prompt(self, log_data: dict, concept_id: Optional[str]) -> str:
        scope = f"concept {concept_id}" if concept_id else "all concepts"
        return (
            f"Analyze these production logs for {scope}:\n\n"
            f"{json.dumps(log_data, indent=2, default=str)}\n\n"
            "Identify systemic issues and propose improvements. Return JSON:\n"
            "  proposals (list): each has {agent, issue, proposed_change, evidence, priority}\n"
            "    priority: 'high' | 'medium' | 'low'\n"
            "  summary (str): 2-3 sentence executive summary\n"
            "\nReturn only the JSON object."
        )

    def _parse_response(self, text: str) -> dict:
        """Parse JSON from model response."""
        import re
        try:
            clean = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
            data = json.loads(clean)
            return {
                "proposals": data.get("proposals", []),
                "summary": data.get("summary", ""),
            }
        except Exception as e:
            print(f"[ImprovementOfficer] WARNING: could not parse response ({e})")
            return {
                "proposals": [],
                "summary": f"Parse error: {e}",
            }

    def _write_report(self, result: dict, concept_id: Optional[str]) -> Path:
        """Write improvement report to logs/improvements/{date}.json."""
        try:
            IMPROVEMENTS_DIR.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
            scope = concept_id or "all"
            report_path = IMPROVEMENTS_DIR / f"{date_str}_{scope}.json"
            with open(report_path, "w") as f:
                json.dump(
                    {
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "scope": concept_id or "all",
                        **result,
                    },
                    f,
                    indent=2,
                )
            return report_path
        except Exception as e:
            raise RuntimeError(f"[ImprovementOfficer] Failed to write report: {e}") from e
