#!/usr/bin/env python3
"""
Apply datamosh effect to a video.

Datamoshing works by encoding video as MPEG4 (which uses I-frames for full
reference frames and P-frames for motion-delta frames), then flipping I-frame
VOP type bits to P-frame in the bitstream. Without a periodic I-frame reset,
motion vectors accumulate and the image smears/bleeds — the classic datamosh look.

The effect intensity is controlled by:
  --keep-clean N   Number of I-frames to leave untouched at the start (default: 1)
                   1 = one clean reference frame, then immediate smear
                   0 = full chaos from frame 1 (may produce a black/garbage opener)
                   5 = ~5 seconds clean intro before mosh kicks in (at default GOP)
  --gop N          Keyframe interval for the intermediate MPEG4 encode (default: 15)
                   Smaller = more I-frames = more opportunities for corruption
                   15 → one I-frame every ~0.5s at 30fps
                   30 → one I-frame every ~1s at 30fps

Usage:
  apply-datamosh.py --input video.mp4 --output moshed.mp4
  apply-datamosh.py --input video.mp4 --output moshed.mp4 --keep-clean 5 --gop 30
  apply-datamosh.py --input video.mp4 --output moshed.mp4 --keep-clean 0 --gop 10
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


# VOP (Video Object Plane) start code in MPEG4 bitstream
VOP_START = bytes([0x00, 0x00, 0x01, 0xB6])

# VOP coding type bits (top 2 bits of byte after VOP_START)
VOP_TYPE_I = 0b00  # Intra — full reference frame
VOP_TYPE_P = 0b01  # Predictive — delta from previous


def encode_to_mpeg4_avi(input_path: str, avi_path: str, gop: int, bitrate: str) -> None:
    """Encode input video to MPEG4 AVI with specified GOP size."""
    result = subprocess.run([
        'ffmpeg', '-i', input_path,
        '-vcodec', 'mpeg4',
        '-g', str(gop),
        '-bf', '0',          # No B-frames — cleaner mosh
        '-b:v', bitrate,
        '-an',               # Strip audio (re-added at end)
        avi_path, '-y'
    ], capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[datamosh] MPEG4 encode failed:\n{result.stderr[-600:]}", file=sys.stderr)
        sys.exit(1)


def flip_iframes(avi_path: str, mosh_path: str, keep_clean: int) -> dict:
    """
    Parse AVI bitstream, find MPEG4 I-VOPs, flip their type bits to P-VOP.
    Returns stats dict.
    """
    with open(avi_path, 'rb') as f:
        data = bytearray(f.read())

    iframe_count = 0
    modified = 0
    i = 0

    while i < len(data) - 5:
        pos = data.find(VOP_START, i)
        if pos == -1:
            break

        next_byte = data[pos + 4]
        coding_type = (next_byte >> 6) & 0x3

        if coding_type == VOP_TYPE_I:
            iframe_count += 1
            if iframe_count > keep_clean:
                # Flip top 2 bits: 00 (I) → 01 (P)
                data[pos + 4] = (next_byte & 0x3F) | (VOP_TYPE_P << 6)
                modified += 1

        i = pos + 4

    with open(mosh_path, 'wb') as f:
        f.write(data)

    return {
        'total_iframes': iframe_count,
        'flipped': modified,
        'kept_clean': iframe_count - modified,
    }


def avi_to_mp4(mosh_avi: str, output_path: str, audio_source: str | None) -> None:
    """Re-encode moshed AVI to MP4, optionally muxing in audio from original."""
    cmd = ['ffmpeg', '-i', mosh_avi]
    if audio_source:
        cmd += ['-i', audio_source]
    cmd += ['-map', '0:v:0']
    if audio_source:
        cmd += ['-map', '1:a?']
    cmd += [
        '-c:v', 'libx264', '-crf', '16', '-preset', 'fast',
        '-c:a', 'aac', '-b:a', '192k',
        '-r', '30',
        output_path, '-y'
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[datamosh] MP4 encode failed:\n{result.stderr[-600:]}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description='Apply datamosh effect by corrupting MPEG4 I-frames.'
    )
    parser.add_argument('--input',       required=True, help='Input video (any format ffmpeg accepts)')
    parser.add_argument('--output',      required=True, help='Output MP4 path')
    parser.add_argument('--keep-clean',  type=int, default=1,
                        help='I-frames to leave intact at start (default: 1)')
    parser.add_argument('--gop',         type=int, default=15,
                        help='Keyframe interval for intermediate MPEG4 encode (default: 15)')
    parser.add_argument('--bitrate',     default='8M',
                        help='MPEG4 encode bitrate (default: 8M)')
    parser.add_argument('--audio',       default=None,
                        help='Optional separate audio file to mux in (e.g. music_section.wav)')
    parser.add_argument('--no-audio',    action='store_true',
                        help='Strip audio from output')
    args = parser.parse_args()

    input_path = str(Path(args.input).resolve())
    output_path = str(Path(args.output).resolve())

    if not os.path.exists(input_path):
        print(f"[datamosh] Input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        avi_path  = os.path.join(tmpdir, 'source.avi')
        mosh_path = os.path.join(tmpdir, 'mosh.avi')

        print(f"[datamosh] Encoding to MPEG4 AVI (GOP={args.gop}, bitrate={args.bitrate})...")
        encode_to_mpeg4_avi(input_path, avi_path, args.gop, args.bitrate)

        print(f"[datamosh] Flipping I-frames (keep_clean={args.keep_clean})...")
        stats = flip_iframes(avi_path, mosh_path, args.keep_clean)
        print(f"[datamosh] I-frames: {stats['total_iframes']} total | "
              f"{stats['flipped']} flipped | {stats['kept_clean']} kept clean")

        audio_source = None
        if not args.no_audio:
            audio_source = args.audio if args.audio else input_path

        print(f"[datamosh] Encoding output MP4...")
        avi_to_mp4(mosh_path, output_path, audio_source)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"[datamosh] Done: {output_path} ({size_mb:.1f} MB)")


if __name__ == '__main__':
    main()
