#!/usr/bin/env python3
"""
Manifest-driven video assembler.

Usage:
  assemble.py --manifest path/to/manifest.json --draft
  assemble.py --manifest path/to/manifest.json --draft
  assemble.py --manifest path/to/manifest.json --scenes 1-5 --draft

Flags:
  --draft    All scenes as Ken Burns / stills (fast pacing review)
  --draft    All scenes as Ken Burns stills
  --scenes   Range (e.g. "1-5") or comma-separated (e.g. "1,3,7")
  --output   Output filename (default: auto-named from manifest)
  --no-audio Skip VO mix (visual-only assembly)
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manifest_schema import load_manifest


def parse_scene_range(s: str) -> list[int]:
    """Parse '1-5' or '1,3,7' into list of scene indices."""
    scenes = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            scenes.extend(range(int(a), int(b) + 1))
        else:
            scenes.append(int(part))
    return scenes


def make_kb_clip(image_path: str, duration: float, output_path: str, resolution="1080x1920", scene_index: int = 0):
    """Smooth Ken Burns via Pillow float-precision EXTENT transform + LANCZOS resampling.

    Replaces zoompan entirely. zoompan quantizes crop coordinates to integers
    each frame, causing unavoidable sub-pixel jitter at slow zoom rates. This
    approach computes exact float crop boxes per frame — zero quantization.

    Source is upscaled to 1.5× output to give headroom for up to 1.5× zoom.
    Frames are piped as raw RGB to ffmpeg for h264 encoding.
    """
    try:
        from PIL import Image
        _RESAMPLE = Image.Resampling.BICUBIC
        _LANCZOS = Image.Resampling.LANCZOS
        _EXTENT = Image.Transform.EXTENT
    except ImportError:
        print("ERROR: Pillow required for Ken Burns. Run: pip install Pillow", file=sys.stderr)
        sys.exit(1)

    out_w, out_h = [int(x) for x in resolution.split("x")]
    fps = 30
    total_frames = max(1, round(duration * fps))

    # Motion presets: (start_zoom, end_zoom, pan_x, pan_y)
    # pan_x/y: fraction of available drift headroom (0 = center, ±1 = full drift)
    motions = [
        (1.0,  1.15,  0.0,  0.0),   # zoom in, center
        (1.15, 1.0,   0.0,  0.0),   # zoom out, center
        (1.0,  1.12,  0.4,  0.0),   # zoom in, drift right
        (1.0,  1.12, -0.4,  0.0),   # zoom in, drift left
        (1.0,  1.12,  0.0,  0.4),   # zoom in, drift down
        (1.0,  1.12,  0.0, -0.4),   # zoom in, drift up
    ]
    start_zoom, end_zoom, pan_x, pan_y = motions[scene_index % len(motions)]

    # Prepare source: scale to 1.5× output, center-crop to exact dimensions
    src_w, src_h = round(out_w * 1.5), round(out_h * 1.5)
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:
        raise RuntimeError(f"[make_kb_clip] Cannot open {image_path}: {e}") from e

    scale = max(src_w / img.width, src_h / img.height)
    scaled = img.resize((round(img.width * scale), round(img.height * scale)), _LANCZOS)  # LANCZOS for upscale
    x0 = (scaled.width - src_w) // 2
    y0 = (scaled.height - src_h) // 2
    img = scaled.crop((x0, y0, x0 + src_w, y0 + src_h))
    cx, cy = src_w / 2.0, src_h / 2.0

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24", "-r", str(fps),
        "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-vframes", str(total_frames),
        output_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    stdin = proc.stdin
    assert stdin is not None
    try:
        for n in range(total_frames):
            t = n / max(total_frames - 1, 1)
            zoom = start_zoom + (end_zoom - start_zoom) * t

            # Crop area in source pixels at this zoom level
            crop_w = src_w / zoom
            crop_h = src_h / zoom

            # Available drift space (source headroom beyond the crop)
            avail_x = (src_w - crop_w) / 2
            avail_y = (src_h - crop_h) / 2

            left = cx - crop_w / 2 + pan_x * avail_x * t
            top  = cy - crop_h / 2 + pan_y * avail_y * t

            frame = img.transform(
                (out_w, out_h),
                _EXTENT,
                (left, top, left + crop_w, top + crop_h),
                _RESAMPLE,  # BICUBIC — EXTENT doesn't support LANCZOS
            )
            stdin.write(frame.tobytes())
    except Exception as e:
        proc.kill()
        raise RuntimeError(f"[make_kb_clip] Frame write failed for {image_path}: {e}") from e
    finally:
        stdin.close()
        proc.wait()


def make_still_clip(image_path: str, duration: float, output_path: str, resolution="1024x1024"):
    """Static hold of image for duration (no motion)."""
    w, h = resolution.split("x")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", image_path,
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        output_path,
    ]
    subprocess.run(cmd, check=True)


def make_animatic_clip(vo_text: str, action_text: str, scene_idx, duration: float, output_path: str, resolution="1080x1920"):
    """Render a two-panel animatic frame: VO text (top) + action description (bottom).

    Uses Pillow for layout so text wraps correctly. Outputs a static hold clip.
    No image gen required — this is the free wireframe pass.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("ERROR: Pillow required for animatic mode. Run: pip install Pillow", file=sys.stderr)
        sys.exit(1)

    import textwrap
    import tempfile

    w_px, h_px = [int(x) for x in resolution.split("x")]
    pad = int(w_px * 0.07)

    img = Image.new("RGB", (w_px, h_px), (18, 18, 22))
    draw = ImageDraw.Draw(img)

    # Font resolution
    def load_font(size):
        for path in [
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        ]:
            try:
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue
        return ImageFont.load_default()

    sz_scene = max(24, w_px // 30)
    sz_vo = max(36, w_px // 18)
    sz_action = max(28, w_px // 24)
    font_scene = load_font(sz_scene)
    font_vo = load_font(sz_vo)
    font_action = load_font(sz_action)

    usable_w = w_px - 2 * pad
    divider_y = int(h_px * 0.55)

    # Scene label + timing
    label = f"Scene {scene_idx}  ·  {duration:.1f}s"
    draw.text((pad, pad), label, fill=(100, 100, 120), font=font_scene)

    # VO text block (top section, white)
    vo_chars_per_line = max(20, usable_w // (sz_vo // 2 + 2))
    vo_wrapped = textwrap.fill(vo_text or "", width=vo_chars_per_line)
    vo_y = pad * 2 + sz_scene
    draw.multiline_text((pad, vo_y), vo_wrapped, fill=(240, 240, 240), font=font_vo, spacing=8)

    # Divider
    draw.line([(pad, divider_y), (w_px - pad, divider_y)], fill=(60, 60, 80), width=2)

    # Section label
    draw.text((pad, divider_y + 12), "ON SCREEN", fill=(80, 100, 160), font=font_scene)

    # Action text block (bottom section, muted)
    action_chars = max(25, usable_w // (sz_action // 2 + 1))
    action_wrapped = textwrap.fill(action_text or "(no action description)", width=action_chars)
    action_y = divider_y + sz_scene + 24
    draw.multiline_text((pad, action_y), action_wrapped, fill=(160, 160, 180), font=font_action, spacing=6)

    # Save frame PNG then encode to video
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        frame_path = f.name
    try:
        img.save(frame_path, "PNG")
        make_still_clip(frame_path, duration, output_path, resolution=resolution)
    finally:
        if os.path.exists(frame_path):
            os.unlink(frame_path)


def make_text_overlay_clip(text: str, duration: float, output_path: str, resolution="1024x1024"):
    """Black background with centered white text (week banners etc).
    Uses Pillow for text rendering (no drawtext/libfreetype dependency).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("ERROR: Pillow required for text overlays. Run: pip install Pillow", file=sys.stderr)
        sys.exit(1)

    out_w, out_h = [int(x) for x in resolution.split("x")]
    img = Image.new("RGB", (out_w, out_h), "black")
    draw = ImageDraw.Draw(img)

    # Try to find a good font, fall back to default
    font = None
    fontsize = min(out_w, out_h) // 12
    for font_path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSText.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    ]:
        if Path(font_path).exists():
            try:
                font = ImageFont.truetype(font_path, fontsize)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    # Word-wrap text to fit ~80% of width
    max_text_w = int(out_w * 0.8)
    words = text.split()
    lines = []
    current_line = ""
    for word in words:
        test = f"{current_line} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_text_w and current_line:
            lines.append(current_line)
            current_line = word
        else:
            current_line = test
    if current_line:
        lines.append(current_line)

    # Draw centered
    line_height = draw.textbbox((0, 0), "Ay", font=font)[3] + 8
    total_h = line_height * len(lines)
    y_start = (out_h - total_h) // 2
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        x = (out_w - lw) // 2
        draw.text((x, y_start + i * line_height), line, fill="white", font=font)

    # Save frame then encode to video
    frame_path = output_path.replace(".mp4", "_frame.png")
    img.save(frame_path)
    make_still_clip(frame_path, duration, output_path, resolution=resolution)
    try:
        os.remove(frame_path)
    except OSError:
        pass


def concat_clips(clip_paths: list[str], output_path: str):
    """Concatenate clips via ffmpeg concat demuxer."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")
        concat_list = f.name
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            output_path,
        ]
        subprocess.run(cmd, check=True)
    finally:
        os.unlink(concat_list)


def mix_audio(video_path: str, audio_path: str, output_path: str, trim_to: float | None = None):
    """Mix VO audio onto video. Optionally trim to duration."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_path, "-i", audio_path,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-map", "0:v:0", "-map", "1:a:0",
    ]
    if trim_to:
        cmd.extend(["-t", str(trim_to)])
    cmd.append(output_path)
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description="Manifest-driven video assembler")
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--animatic", action="store_true", help="Text-only wireframe: VO text + action description per scene, no image gen")
    parser.add_argument("--draft", action="store_true", help="All scenes as Ken Burns")
    parser.add_argument("--scenes", help="Scene range: '1-5' or '1,3,7'")
    parser.add_argument("--output", help="Output filename")
    parser.add_argument("--no-audio", action="store_true", help="Skip VO mix")
    args = parser.parse_args()

    if not args.animatic and not args.draft:
        args.draft = True  # Default to draft

    manifest_path = Path(args.manifest)
    manifest = load_manifest(str(manifest_path))
    project_dir = manifest_path.parent

    # Read resolution from manifest config block, default to 1080x1920 (vertical)
    config = manifest.get("config", {})
    resolution = config.get("resolution", "1080x1920")

    scenes = manifest.get("scenes", [])
    if args.scenes:
        selected = parse_scene_range(args.scenes)
        scenes = [s for s in scenes if s["scene_index"] in selected]

    if not scenes:
        print("No scenes to assemble", file=sys.stderr)
        sys.exit(1)

    # Build clips
    tmp_dir = Path(tempfile.mkdtemp(prefix="vp_assemble_"))
    clip_paths = []

    for i, scene in enumerate(scenes):
        idx = scene.get("index") or scene.get("scene_index")
        duration = scene.get("end_s", scene.get("estimated_duration_s", 3.0))
        if scene.get("start_s") is not None and scene.get("end_s") is not None:
            duration = scene["end_s"] - scene["start_s"]

        clip_path = str(tmp_dir / f"clip_{i:03d}.mp4")

        # Animatic mode — two-panel text: VO script + action description
        if args.animatic:
            vo_text = scene.get("vo_text") or scene.get("voiceover_text", "")
            action_text = scene.get("action") or scene.get("starting_frame", "")
            make_animatic_clip(vo_text, action_text, idx, duration, clip_path, resolution=resolution)
            print(f"ANIMATIC {idx} done")

        # Text overlay scenes (week banners)
        elif scene.get("type") == "text_overlay" or scene.get("text_overlay"):
            text = scene.get("overlay_text") or scene.get("vo_text") or scene.get("voiceover_text", "")
            make_text_overlay_clip(text, duration, clip_path, resolution=resolution)
            print(f"TEXT {idx} done")

        # Video clip — extract segment from source footage
        elif scene.get("type") == "video" and scene.get("source"):
            source = scene["source"]
            if not Path(source).is_absolute():
                source = str(project_dir / source)
            vf_filters = []
            # Rotation
            rotate = scene.get("rotate")
            if rotate == 180:
                vf_filters.append("transpose=2,transpose=2")
            elif rotate == 90:
                vf_filters.append("transpose=1")
            elif rotate == 270:
                vf_filters.append("transpose=2")
            # Scale to target resolution
            out_w, out_h = [int(x) for x in resolution.split("x")]
            vf_filters.append(f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2")
            vf = ",".join(vf_filters)
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-i", source]
            ss = scene.get("start_s")
            to = scene.get("end_s")
            if ss is not None:
                cmd.extend(["-ss", str(ss)])
            if to is not None:
                cmd.extend(["-to", str(to)])
            cmd.extend(["-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        "-c:a", "aac", "-ar", "48000", clip_path])
            subprocess.run(cmd, check=True)
            print(f"VIDEO {idx} done ({Path(source).name} {ss or 0:.1f}s-{to or '?'}s)")

        # Effect overlay — apply filter to a generated clip from a previous scene
        elif scene.get("type") == "effect_overlay":
            # effect_overlay scenes apply ffmpeg filters over a base clip
            base_ref = scene.get("base_scene")
            if base_ref is not None:
                base_clip = str(tmp_dir / f"clip_{base_ref - 1:03d}.mp4")
            else:
                base_clip = scene.get("source", "")
                if not Path(base_clip).is_absolute():
                    base_clip = str(project_dir / base_clip)
            vf = scene.get("filter", "")
            if vf and Path(base_clip).exists():
                cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                       "-i", base_clip, "-vf", vf, "-c:a", "copy", clip_path]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0:
                    print(f"EFFECT {idx} done")
                else:
                    # Filter failed (e.g. drawtext not compiled in) — use base clip without effect
                    import shutil
                    shutil.copy2(base_clip, clip_path)
                    print(f"EFFECT {idx} filter failed, using base clip (error: {r.stderr[:100]})", file=sys.stderr)
            elif Path(base_clip).exists():
                import shutil
                shutil.copy2(base_clip, clip_path)
                print(f"EFFECT {idx} passthrough (no filter)")
            else:
                print(f"EFFECT {idx} SKIP — base clip not found: {base_clip}", file=sys.stderr)
                continue

        # Static still hold
        elif not args.animatic and (scene.get("type") == "still" or scene.get("still")):
            img = str(project_dir / f"scene_{idx:03d}.png")
            make_still_clip(img, duration, clip_path, resolution=resolution)
            print(f"STILL {idx} done")

        # Draft mode = all KB
        elif args.draft:
            img = scene.get("still_path") or str(project_dir / f"scene_{idx:03d}.png")
            if not Path(img).is_absolute():
                img = str(project_dir / img)
            make_kb_clip(img, duration, clip_path, resolution=resolution, scene_index=i)
            print(f"KB {idx} done")

        clip_paths.append(clip_path)

    # Concat
    mode = "animatic" if args.animatic else "draft"
    slug = (manifest.get("project") or {}).get("id") or manifest.get("project_id", "output")
    output_name = args.output or f"{slug}_{mode}.mp4"

    # Final renders go into output/ subfolder, not project root
    output_dir = project_dir / "output"
    output_dir.mkdir(exist_ok=True)

    video_only = str(project_dir / f"_tmp_{Path(output_name).name}")
    concat_clips(clip_paths, video_only)
    print(f"Concat done: {len(clip_paths)} clips")

    # Audio mix
    # If --output contains a path separator, treat it as relative to project_dir
    if args.output and os.sep in args.output:
        final_path = str(project_dir / args.output)
        Path(final_path).parent.mkdir(parents=True, exist_ok=True)
    else:
        final_path = str(output_dir / output_name)
    if args.no_audio:
        os.rename(video_only, final_path)
    else:
        vo = manifest.get("voiceover", {})
        audio_file = vo.get("audio_file")
        if audio_file:
            # Check audio/ subfolder first, then project root for backwards compat
            audio_path = str(project_dir / "audio" / audio_file)
            if not Path(audio_path).exists():
                audio_path = str(project_dir / audio_file)
            last_scene = scenes[-1]
            trim_to = last_scene.get("end_s")
            mix_audio(video_only, audio_path, final_path, trim_to=trim_to)
            os.unlink(video_only)
            print(f"Audio mixed, trimmed to {trim_to}s")
        else:
            os.rename(video_only, final_path)
            print("No VO found, video-only output")

    # Cleanup temp clips
    for p in clip_paths:
        if os.path.exists(p):
            os.unlink(p)
    tmp_dir.rmdir()

    size_mb = Path(final_path).stat().st_size / 1024 / 1024
    print(f"Output: {final_path} ({size_mb:.1f}MB)")

    # Update project registry
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from registry import register
        register(str(manifest_path))
    except Exception:
        pass


if __name__ == "__main__":
    main()
