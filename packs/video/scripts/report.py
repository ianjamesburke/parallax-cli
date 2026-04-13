#!/usr/bin/env python3
"""Generate a cost and iteration report for a video production project.

Usage:
  report.py --manifest path/to/manifest.yaml
  report.py --manifest path/to/manifest.yaml --full   # also open the markdown report

Prints a compact inline summary and writes output/session_report.md.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cost_tracker import summarize  # type: ignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_manifest(manifest_path: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(manifest_path.read_text())
    except Exception as e:
        print(f"ERROR: could not load manifest: {e}", file=sys.stderr)
        sys.exit(1)


def scan_outputs(output_dir: Path) -> list[dict]:
    """Scan output/ for video files, extract version and stage from filename."""
    if not output_dir.exists():
        return []

    files = []
    for f in sorted(output_dir.iterdir()):
        if f.suffix not in {".mp4", ".mov", ".fcpxml", ".xml"}:
            continue
        stat = f.stat()
        name = f.stem  # e.g. Straw-Char-Mockup_v2_captioned

        # Parse version and stage from filename
        version = None
        stage = None
        parts = name.split("_")
        for i, p in enumerate(parts):
            if p.startswith("v") and p[1:].isdigit():
                version = p
                stage = "_".join(parts[i + 1:]) or "draft"
                break

        files.append({
            "path": f,
            "name": f.name,
            "version": version or "?",
            "stage": stage or f.suffix.lstrip("."),
            "size_mb": round(stat.st_size / 1024 / 1024, 1),
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        })
    return files


def count_stills(project_dir: Path) -> int:
    stills_dir = project_dir / "assets" / "stills"
    if not stills_dir.exists():
        return 0
    return len(list(stills_dir.glob("scene_*.png")))


def identify_final(outputs: list[dict]) -> str | None:
    """Best guess at the final output: highest version, preferring 'final' > 'captioned' > 'draft'."""
    stage_rank = {"final": 3, "captioned": 2, "draft": 1}
    ranked = sorted(
        outputs,
        key=lambda f: (f["version"], stage_rank.get(f["stage"], 0)),
        reverse=True,
    )
    return ranked[0]["name"] if ranked else None


def iteration_analysis(outputs: list[dict]) -> dict:
    """Count outputs by version and stage to surface waste."""
    versions: dict[str, list] = {}
    for f in outputs:
        versions.setdefault(f["version"], []).append(f["stage"])

    total = len(outputs)
    final_name = identify_final(outputs)
    unused = total - 1 if total > 0 else 0  # everything except final is "not final"

    return {
        "total_outputs": total,
        "unused_outputs": unused,
        "versions": versions,
        "final": final_name,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_cost(c: float) -> str:
    if c == 0:
        return "—"
    return f"${c:.3f}" if c < 0.10 else f"${c:.2f}"


def print_inline_summary(manifest: dict, project_dir: Path, outputs: list[dict], cost_summary: dict):
    proj = manifest.get("project", {})
    pid = proj.get("id", project_dir.name)
    brand = proj.get("brand", "")
    total_cost = cost_summary["total"]

    print()
    print(f"── Report: {pid}" + (f" ({brand})" if brand else "") + " " + "─" * 20)

    # Output files table
    if outputs:
        print(f"  {'File':<45} {'Size':>6}  Path")
        for f in outputs:
            marker = " ← final" if f["name"] == identify_final(outputs) else ""
            print(f"  {f['name']:<45} {f['size_mb']:>5.1f}MB  output/{f['name']}{marker}")
    else:
        print("  No output files found.")

    print()

    # Services + cost
    if cost_summary["services"]:
        print(f"  Services:  {', '.join(cost_summary['services'])}")
    else:
        print("  Services:  (no API calls tracked this session)")

    print(f"  Est. cost: {fmt_cost(total_cost)}")

    # Iteration note
    itr = iteration_analysis(outputs)
    if itr["unused_outputs"] > 0:
        print(f"  Iterations: {itr['total_outputs']} outputs ({itr['unused_outputs']} non-final)")

    report_path = project_dir / "output" / "session_report.md"
    print(f"  Full report: {report_path}")
    print("─" * 60)
    print()


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def write_markdown_report(manifest: dict, project_dir: Path, outputs: list[dict], cost_summary: dict):
    proj = manifest.get("project", {})
    pid = proj.get("id", project_dir.name)
    brand = proj.get("brand", "")
    version = proj.get("version", 1)
    scenes = manifest.get("scenes", [])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# Production Report — {pid}",
        f"",
        f"**Brand:** {brand or '—'}  ",
        f"**Manifest version:** v{version}  ",
        f"**Generated:** {now}  ",
        f"",
    ]

    # --- Cost breakdown ---
    lines += [
        "## Cost Estimate",
        "",
        "| Service | Detail | Cost |",
        "|---------|--------|------|",
    ]
    events = cost_summary["events"]
    for e in events:
        if e["type"] == "still":
            lines.append(f"| Gemini image gen | Scene {e['scene']} ({e['model']}) | {fmt_cost(e['cost'])} |")
        elif e["type"] == "vo":
            detail = f"{e['provider']} — {e.get('voice', '')} — {e.get('chars', 0):,} chars"
            lines.append(f"| Voiceover | {detail} | {fmt_cost(e['cost'])} |")
        elif e["type"] == "fal":
            lines.append(f"| FAL {e['model']} | {e['duration_s']}s output | {fmt_cost(e['cost'])} |")

    total = cost_summary["total"]
    lines += [
        f"| **Total** | | **{fmt_cost(total)}** |",
        "",
    ]

    # --- Output files ---
    itr = iteration_analysis(outputs)
    lines += [
        "## Output Files",
        "",
        f"**{itr['total_outputs']} total** — {itr['unused_outputs']} non-final iteration(s)",
        "",
        "| File | Version | Stage | Size |",
        "|------|---------|-------|------|",
    ]
    final_name = itr["final"]
    for f in outputs:
        marker = " ✓" if f["name"] == final_name else ""
        lines.append(f"| `{f['name']}`{marker} | {f['version']} | {f['stage']} | {f['size_mb']}MB |")
    lines.append("")

    # --- Scenes ---
    if scenes:
        lines += [
            "## Scenes",
            "",
            "| # | Duration | VO | Still | Type |",
            "|---|----------|----|-------|------|",
        ]
        for s in scenes:
            dur = f"{s.get('end_s', 0) - s.get('start_s', 0):.1f}s"
            vo = s.get("vo_text", "")[:50]
            still = "✓" if s.get("still_status") == "complete" else "pending"
            stype = s.get("type", "normal")
            lines.append(f"| {s['index']} | {dur} | {vo} | {still} | {stype} |")
        lines.append("")

    # --- API call log ---
    if events:
        lines += [
            "## API Call Log",
            "",
            "| Time | Type | Detail | Cost |",
            "|------|------|--------|------|",
        ]
        for e in events:
            ts = e.get("ts", "")[:16]
            t = e["type"]
            if t == "still":
                detail = f"Scene {e['scene']} — {e['model']}"
            elif t == "vo":
                detail = f"{e.get('provider')} / {e.get('voice')} / {e.get('chars', 0):,} chars"
            elif t == "fal":
                detail = f"{e['model']} — {e['duration_s']}s"
            else:
                detail = str(e)
            lines.append(f"| {ts} | {t} | {detail} | {fmt_cost(e.get('cost', 0))} |")
        lines.append("")

    report_path = project_dir / "output" / "session_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines))
    return report_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate project cost and iteration report")
    parser.add_argument("--manifest", required=True, help="Path to manifest.yaml")
    parser.add_argument("--full", action="store_true", help="Open the markdown report after writing")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    project_dir = manifest_path.parent

    manifest = load_manifest(manifest_path)
    outputs = scan_outputs(project_dir / "output")
    cost_summary = summarize(project_dir)

    print_inline_summary(manifest, project_dir, outputs, cost_summary)
    report_path = write_markdown_report(manifest, project_dir, outputs, cost_summary)

    if args.full:
        os.system(f"open '{report_path}'")


if __name__ == "__main__":
    main()
