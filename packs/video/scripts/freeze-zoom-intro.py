#!/usr/bin/env python3
"""
Add a freeze-frame zoom intro to a video with a voiceover overlay.

The output video has two parts:
  1. First frame of the source video, slow-zooming in, with voiceover audio.
  2. The original video playing normally (with its original audio).

For text overlays, use render-animation.py --template caption --mode overlay
to generate a transparent PNG, then composite separately.

Usage:
  freeze-zoom-intro.py --input video.mp4 --voiceover narration.mp3 --output out.mp4
  freeze-zoom-intro.py --input video.mp4 --voiceover narration.mp3 --max-zoom 1.4

Flags:
  --input       Source video file (required)
  --voiceover   Audio file for the intro narration (required)
  --output      Output video path (default: <input_stem>_intro.mp4)
  --max-zoom    Maximum zoom level at end of intro (default: 1.3)
  --fps         Output framerate (default: 30)
  --overlay     Optional transparent PNG to composite on the intro (e.g. caption)
  --dry-run     Print plan without writing files
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def get_duration(path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        print(f"[freeze-zoom] ffprobe failed on {path}: {e}", file=sys.stderr)
        raise


def get_video_size(path: str) -> tuple[int, int]:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
           "-of", "default=noprint_wrappers=1", path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        w = h = 0
        for line in result.stdout.splitlines():
            if line.startswith("width="):
                w = int(line.split("=")[1])
            elif line.startswith("height="):
                h = int(line.split("=")[1])
        if not w or not h:
            raise ValueError(f"Could not read dimensions from {path}")
        return w, h
    except Exception as e:
        print(f"[freeze-zoom] Failed to get video size: {e}", file=sys.stderr)
        raise


def extract_first_frame(video_path: str, out_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", video_path, "-vframes", "1", "-f", "image2", out_path]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[freeze-zoom] Failed to extract first frame: {e}", file=sys.stderr)
        raise


def build_freeze_zoom_clip(
    frame_path: str,
    audio_path: str,
    output_path: str,
    duration: float,
    width: int,
    height: int,
    fps: int,
    max_zoom: float,
    overlay_path: str | None,
) -> None:
    """Create a freeze-frame clip that slow-zooms in, with voiceover audio."""
    total_frames = int(duration * fps)
    delta = max_zoom - 1.0

    # Linear zoom via output frame number — no drift, no jitter
    zoom_expr = f"1+{delta:.6f}*on/{max(total_frames - 1, 1)}"
    x_expr = "round((iw-iw/zoom)/2)"
    y_expr = "round((ih-ih/zoom)/2)"
    zoompan = (
        f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}'"
        f":d={total_frames}:fps={fps}:s={width}x{height}"
    )

    if overlay_path:
        # Composite transparent PNG on top of zoomed frame
        filter_chain = (
            f"[0:v]{zoompan}[vz];"
            f"[2:v]format=rgba[cap];"
            f"[vz][cap]overlay=0:0[v]"
        )
        inputs = [
            "-loop", "1", "-framerate", str(fps), "-i", frame_path,
            "-i", audio_path,
            "-loop", "1", "-i", overlay_path,
        ]
    else:
        filter_chain = f"[0:v]{zoompan}[v]"
        inputs = [
            "-loop", "1", "-framerate", str(fps), "-i", frame_path,
            "-i", audio_path,
        ]

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_chain,
        "-map", "[v]", "-map", "1:a",
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    label = f"zoom 1.0 → {max_zoom}, {total_frames} frames"
    if overlay_path:
        label += " + overlay"
    print(f"[freeze-zoom] Building {duration:.2f}s intro clip ({label})...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[freeze-zoom] Intro clip build failed: {e}", file=sys.stderr)
        raise


def concat_clips(intro_path: str, main_path: str, output_path: str) -> None:
    """Concatenate intro clip + main video via filter_complex (frame-accurate, in sync)."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", intro_path,
        "-i", main_path,
        "-filter_complex",
        "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[vout][aout]",
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    print("[freeze-zoom] Concatenating intro + main video...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[freeze-zoom] Concat failed: {e}", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description="Add freeze-frame zoom intro with voiceover")
    parser.add_argument("--input", required=True, help="Source video file")
    parser.add_argument("--voiceover", required=True, help="Intro narration audio file")
    parser.add_argument("--output", help="Output video path")
    parser.add_argument("--overlay", help="Transparent PNG to composite on intro (e.g. caption)")
    parser.add_argument("--max-zoom", type=float, default=1.3, help="Max zoom level (default: 1.3)")
    parser.add_argument("--fps", type=int, default=30, help="Output framerate (default: 30)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    vo_path = Path(args.voiceover).resolve()

    for p, label in [(input_path, "--input"), (vo_path, "--voiceover")]:
        if not p.exists():
            print(f"[freeze-zoom] {label} not found: {p}", file=sys.stderr)
            sys.exit(1)

    overlay_path = None
    if args.overlay:
        overlay_path = str(Path(args.overlay).resolve())
        if not Path(overlay_path).exists():
            print(f"[freeze-zoom] --overlay not found: {overlay_path}", file=sys.stderr)
            sys.exit(1)

    output_path = Path(args.output).resolve() if args.output else (
        input_path.parent / f"{input_path.stem}_intro.mp4"
    )

    vo_duration = get_duration(str(vo_path))
    width, height = get_video_size(str(input_path))

    print(f"[freeze-zoom] Input:      {input_path}")
    print(f"[freeze-zoom] Voiceover:  {vo_path} ({vo_duration:.2f}s)")
    print(f"[freeze-zoom] Output:     {output_path}")
    print(f"[freeze-zoom] Video size: {width}x{height}")
    print(f"[freeze-zoom] Max zoom:   {args.max_zoom}x over {vo_duration:.2f}s")
    if overlay_path:
        print(f"[freeze-zoom] Overlay:    {overlay_path}")

    if args.dry_run:
        print("\n[freeze-zoom] --dry-run: no files written.")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        frame_path = str(Path(tmpdir) / "first_frame.png")
        intro_path = str(Path(tmpdir) / "intro.mp4")

        print("[freeze-zoom] Extracting first frame...")
        try:
            extract_first_frame(str(input_path), frame_path)
        except Exception:
            sys.exit(1)

        try:
            build_freeze_zoom_clip(
                frame_path, str(vo_path), intro_path,
                vo_duration, width, height, args.fps, args.max_zoom,
                overlay_path,
            )
        except Exception:
            sys.exit(1)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            concat_clips(intro_path, str(input_path), str(output_path))
        except Exception:
            sys.exit(1)

    final_duration = get_duration(str(output_path))
    print(f"\n[freeze-zoom] Done: {output_path}")
    print(f"  Intro:  {vo_duration:.2f}s (freeze-frame zoom)")
    print(f"  Main:   {final_duration - vo_duration:.2f}s")
    print(f"  Total:  {final_duration:.2f}s")


if __name__ == "__main__":
    main()
