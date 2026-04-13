#!/usr/bin/env python3
"""Generate a character reference sheet from a source inspiration image.

Takes one or more inspiration images (photo, screenshot, sketch) and generates
a 4-panel character reference sheet: 2 full-body poses + 2 head close-ups.
Opens the result for immediate review.

Run this BEFORE init-project when your project has a non-human or animated character.
The output goes into your project's input/ folder as a character reference.

Usage:
  generate-char-ref.py --input source.png --output project/input/char_ref.png
  generate-char-ref.py --input source.png --describe "purple alien, muscular, almond eyes"
  generate-char-ref.py --input src1.png src2.png --output char_ref.png
  generate-char-ref.py --input source.png --style "flat 2D cartoon, bold outlines"
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api_config import get_gemini_client
from config import get_model_name

CHAR_REF_PROMPT = """Create a simple CHARACTER REFERENCE SHEET for visual effects / CGI use.

{character_block}

LAYOUT — 4 panels only, clean and simple:
- TWO FULL BODY poses (one standing neutral front view, one action pose like flexing or walking)
- TWO HEAD CLOSE-UPS (one neutral expression, one exaggerated/excited expression)

STYLE REQUIREMENTS:
- Match the style of the reference image(s) exactly — {style_note}
- Consistent proportions, lighting, and textures across all panels
- Clean white or neutral gray background

CRITICAL:
- Draw a SCENE (reference sheet layout), NOT a character sheet annotation
- NO TEXT ANYWHERE — no labels, no annotations, no captions, no titles
- No text on clothing
- No watermarks
- Exactly 4 panels — keep it simple
- Busy reference sheets do NOT improve generation quality"""


def build_prompt(description: str, style: str) -> str:
    if description:
        char_block = f"CHARACTER DESCRIPTION:\n{description}"
    else:
        char_block = "CHARACTER DESCRIPTION:\n- Match the character shown in the reference image(s) exactly"

    if style:
        style_note = style
    else:
        style_note = "photorealistic CGI stays CGI, cartoon stays cartoon, flat 2D stays flat 2D"

    return CHAR_REF_PROMPT.format(character_block=char_block, style_note=style_note)


def generate(input_paths: list[Path], output_path: Path, description: str, style: str, attempt: int = 1):
    from google.genai import types

    client = get_gemini_client()
    prompt = build_prompt(description, style)

    contents = []
    for p in input_paths:
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        try:
            contents.append(types.Part.from_bytes(data=p.read_bytes(), mime_type=mime))
        except Exception as e:
            print(f"WARNING: Could not load {p.name}: {e}")

    if not contents:
        print("ERROR: No input images could be loaded.", file=sys.stderr)
        sys.exit(1)

    contents.append(prompt)

    print(f"Generating character reference sheet (attempt {attempt})...")
    try:
        response = client.models.generate_content(
            model=get_model_name("image_generation"),
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["image", "text"],
            ),
        )
    except Exception as e:
        print(f"ERROR: Gemini call failed: {e}", file=sys.stderr)
        sys.exit(1)

    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(part.inline_data.data)
            print(f"Saved: {output_path}")
            return True

    print("ERROR: No image in response.", file=sys.stderr)
    return False


def main():
    parser = argparse.ArgumentParser(description="Generate a 4-panel character reference sheet")
    parser.add_argument("--input", "-i", nargs="+", required=True, help="Inspiration image(s)")
    parser.add_argument("--output", "-o", required=True, help="Output path for character sheet PNG")
    parser.add_argument("--describe", default="", help="Optional character description (style, features, outfit)")
    parser.add_argument("--style", default="", help="Art style override (e.g. 'flat 2D cartoon, bold outlines')")
    parser.add_argument("--retries", type=int, default=2, help="Max generation attempts (default: 2)")
    args = parser.parse_args()

    input_paths = []
    for p in args.input:
        resolved = Path(p).expanduser()
        if not resolved.exists():
            print(f"ERROR: Input not found: {resolved}", file=sys.stderr)
            sys.exit(1)
        input_paths.append(resolved)

    output_path = Path(args.output).expanduser()

    for attempt in range(1, args.retries + 1):
        ok = generate(input_paths, output_path, args.describe, args.style, attempt)
        if ok:
            break
        if attempt < args.retries:
            print(f"Retrying ({attempt}/{args.retries})...")
    else:
        print("All attempts failed.", file=sys.stderr)
        sys.exit(1)

    # Open for review
    try:
        subprocess.run(["open", str(output_path)], check=True)
        print("\nOpened for review. Check:")
        print("  1. 4 panels present (2 full body, 2 head close-ups)")
        print("  2. Character consistent across all panels")
        print("  3. No text, labels, or annotations")
        print("  4. Style matches your source reference")
        print(f"\nIf it looks good, use it as a ref in init-project:")
        print(f"  python3 init-project.py --slug YourBrand-Concept --script script.txt --refs {output_path}")
        print(f"\nIf not, re-run with --describe to guide the style.")
    except Exception:
        print(f"\nReview the output at: {output_path}")


if __name__ == "__main__":
    main()
