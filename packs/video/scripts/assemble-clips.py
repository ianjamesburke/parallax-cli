#!/usr/bin/env python3
"""
Assemble a final video from one or more clip-index manifests.
Reads source footage paths and clip timestamps — no intermediate chopped files needed.

Re-encodes by default (H.264 + AAC) for frame-accurate, sync-safe cuts.
Use --stream-copy for a fast pass when your source has compatible codecs and
short keyframe intervals (may produce A/V desync on some footage).

Writes an assembly manifest alongside the output recording what was assembled.

Usage:
  assemble-clips.py --manifests footage/_meta/clip1.yaml --output final.mp4
  assemble-clips.py --manifests footage/_meta/clip1.yaml footage/_meta/clip2.yaml --output combined.mp4

  # Only use specific clip indices from a single manifest:
  assemble-clips.py --manifests footage/_meta/test.yaml --clips 0,2,4-6 --output highlights.mp4

  # Fast stream copy (no re-encode) — may desync on some footage:
  assemble-clips.py --manifests footage/_meta/test.yaml --stream-copy --output fast.mp4

Flags:
  --manifests     One or more clip-index YAML files (required)
  --output        Output video path (default: <first_manifest_stem>_assembled.mp4)
  --clips         Clip filter: comma-separated indices or ranges e.g. 0,2,4-6
                  Only applies when a single manifest is given
  --stream-copy   Skip re-encode; fast but may desync on some footage
  --dry-run       Print clip list without writing output
"""
import argparse
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _dump_yaml(data: dict) -> str:
    try:
        import yaml
        return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except ImportError:
        import json
        return "# install pyyaml for proper YAML formatting\n" + json.dumps(data, indent=2)


def load_manifest(path: Path) -> dict:
    try:
        import yaml
        data = yaml.safe_load(path.read_text())
    except ImportError:
        import json
        data = json.loads(path.read_text())
    except Exception as e:
        print(f"[assemble-clips] Failed to load {path}: {e}", file=sys.stderr)
        raise

    if not data or data.get("format") != "clip-index":
        print(f"[assemble-clips] {path} is not a clip-index manifest (format: {data.get('format')})",
              file=sys.stderr)
        sys.exit(1)
    return data


def parse_clip_filter(spec: str) -> set[int]:
    indices: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            indices.update(range(int(lo), int(hi) + 1))
        else:
            indices.add(int(part))
    return indices


def resolve_source(stored_path: str, manifest_path: str) -> str:
    """Return a usable source path, falling back to sibling of the _meta/ folder if the stored path is missing."""
    p = Path(stored_path)
    if p.exists():
        return stored_path
    # Fallback: look for the filename next to the _meta/ directory (i.e. _meta/../<filename>)
    if manifest_path:
        sibling = Path(manifest_path).parent.parent / p.name
        if sibling.exists():
            print(f"[assemble-clips] Re-linked {p.name} → {sibling}")
            return str(sibling)
    print(f"[assemble-clips] WARNING: source not found at {stored_path} and no fallback located", file=sys.stderr)
    return stored_path  # Let ffmpeg fail with a clear error


def build_entries(manifests: list[dict], clip_filter: set[int] | None) -> list[dict]:
    entries = []
    for m in manifests:
        manifest_path = m.get("_manifest_path", "")
        source = resolve_source(m["source"], manifest_path)
        for clip in m.get("clips", []):
            if clip_filter is not None and clip["index"] not in clip_filter:
                continue
            entries.append({
                "source": source,
                "start": clip["source_start"],
                "end": clip["source_end"],
                "duration": clip["duration"],
                "clip_index": clip["index"],
                "source_manifest": manifest_path,
            })
    return entries


def get_duration(path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def assemble_reencode(entries: list[dict], output_path: str) -> None:
    """Frame-accurate assembly via filter_complex trim+concat. Always in sync."""
    v_parts = [
        f"[{i}:v]trim=start={e['start']}:end={e['end']},setpts=PTS-STARTPTS[v{i}]"
        for i, e in enumerate(entries)
    ]
    a_parts = [
        f"[{i}:a]atrim=start={e['start']}:end={e['end']},asetpts=N/SR/TB[a{i}]"
        for i, e in enumerate(entries)
    ]
    n = len(entries)
    interleaved = "".join(f"[v{i}][a{i}]" for i in range(n))
    concat_filter = f"{interleaved}concat=n={n}:v=1:a=1[vout][aout]"
    filter_complex = "; ".join(v_parts + a_parts + [concat_filter])

    inputs = []
    for e in entries:
        inputs += ["-i", e["source"]]

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    print(f"[assemble-clips] Re-encoding {n} clip(s) (frame-accurate)...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[assemble-clips] ffmpeg failed: {e}", file=sys.stderr)
        raise


def assemble_stream_copy(entries: list[dict], output_path: str) -> None:
    """Fast stream-copy via concat demuxer. May desync if source lacks keyframes at cut points."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        concat_file = f.name
        for e in entries:
            f.write(f"file '{e['source']}'\n")
            f.write(f"inpoint {e['start']}\n")
            f.write(f"outpoint {e['end']}\n")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", concat_file,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        output_path,
    ]
    print(f"[assemble-clips] Stream-copying {len(entries)} clip(s) (fast, no re-encode)...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[assemble-clips] Stream copy failed:\n{result.stderr}", file=sys.stderr)
            print("[assemble-clips] Try without --stream-copy for frame-accurate re-encode.", file=sys.stderr)
            sys.exit(1)
    finally:
        Path(concat_file).unlink(missing_ok=True)


def write_assembly_manifest(
    manifest_path: Path,
    entries: list[dict],
    source_manifests: list[dict],
    output_path: Path,
    stream_copy: bool,
) -> None:
    # Concatenate transcripts from all source manifests (in order, deduplicated by path)
    seen_sources: set[str] = set()
    transcript_parts: list[str] = []
    for m in source_manifests:
        src = m.get("source", "")
        if src not in seen_sources:
            seen_sources.add(src)
            t = m.get("transcript", "").strip()
            if t:
                transcript_parts.append(t)
    transcript = " ".join(transcript_parts).strip()

    manifest = {
        "format": "assembly",
        "output": str(output_path),
        "assembled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": "stream-copy" if stream_copy else "reencode-h264",
        "transcript": transcript,
        "clips": [
            {
                "source": e["source"],
                "source_manifest": e["source_manifest"],
                "clip_index": e["clip_index"],
                "source_start": e["start"],
                "source_end": e["end"],
                "duration": e["duration"],
            }
            for e in entries
        ],
    }
    manifest_path.write_text(_dump_yaml(manifest))
    print(f"[assemble-clips] Assembly manifest: {manifest_path}")


def main():
    parser = argparse.ArgumentParser(description="Assemble clips from index manifests")
    parser.add_argument("--manifests", nargs="+", required=True)
    parser.add_argument("--output", help="Output video path")
    parser.add_argument("--clips", help="Clip filter e.g. 0,2,4-6 (single manifest only)")
    parser.add_argument("--stream-copy", action="store_true",
                        help="Fast stream copy — may desync on some footage")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest_paths = [Path(p).resolve() for p in args.manifests]
    for p in manifest_paths:
        if not p.exists():
            print(f"[assemble-clips] Manifest not found: {p}", file=sys.stderr)
            sys.exit(1)

    manifests = []
    for p in manifest_paths:
        m = load_manifest(p)
        m["_manifest_path"] = str(p)
        manifests.append(m)

    clip_filter = None
    if args.clips:
        if len(manifests) > 1:
            print("[assemble-clips] --clips only supported with a single manifest", file=sys.stderr)
            sys.exit(1)
        clip_filter = parse_clip_filter(args.clips)

    entries = build_entries(manifests, clip_filter)
    if not entries:
        print("[assemble-clips] No clips matched.", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output).resolve() if args.output else (
        manifest_paths[0].parent.parent / f"{manifest_paths[0].stem}_assembled.mp4"
    )
    assembly_manifest_path = output_path.with_suffix(".yaml")

    total_dur = sum(e["duration"] for e in entries)
    print(f"[assemble-clips] {len(entries)} clip(s) → ~{total_dur:.2f}s")
    for e in entries:
        src = Path(e["source"]).name
        print(f"  [{e['clip_index']}] {src}  {e['start']:.3f}s – {e['end']:.3f}s  ({e['duration']:.3f}s)")
    print(f"[assemble-clips] Output:   {output_path}")

    if args.dry_run:
        print("\n[assemble-clips] --dry-run: no files written.")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.stream_copy:
        assemble_stream_copy(entries, str(output_path))
    else:
        assemble_reencode(entries, str(output_path))

    actual_dur = get_duration(str(output_path))
    print(f"\n[assemble-clips] Done: {output_path} ({actual_dur:.2f}s)")

    write_assembly_manifest(assembly_manifest_path, entries, manifests, output_path, args.stream_copy)


if __name__ == "__main__":
    main()
