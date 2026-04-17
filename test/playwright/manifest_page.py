"""
Playwright smoke test — /manifest page.

Seeds a realistic workspace manifest.yaml + a fake still, boots parallax
chat pointed at that scratch dir, and opens /manifest in the browser.
Asserts the page renders the brief, scene count, and the still thumbnail.

Fast — no LLM calls, no TEST MODE pipeline. Just the HTML/JSON contract.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Force per-user layout on so the seeded workspace lands under
# parallax/users/<u>/<p>/ — matches what the test expects.
os.environ["PARALLAX_PER_USER_WORKSPACES"] = "1"

from playwright.sync_api import sync_playwright

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _helpers import start_server, stop_server  # noqa: E402

SCRATCH_DIR = Path("/tmp/parallax-beta-e2e-manifest")


# 1x1 PNG used as a fake scene still.
_PNG_1x1 = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F"
    "15C4890000000D4944415478DA6364F8FFFFFF3F0005FE02FE6A5BB5F200"
    "00000049454E44AE426082"
)


def _seed_workspace() -> Path:
    """Create an alice/alpha workspace with a minimal manifest + one still."""
    workspace = SCRATCH_DIR / "parallax" / "users" / "alice" / "alpha"
    for sub in ("stills", "input", "output", "drafts", "audio", "logs"):
        (workspace / sub).mkdir(parents=True, exist_ok=True)
    still_path = workspace / "stills" / "scene_01.png"
    still_path.write_bytes(_PNG_1x1)
    (workspace / "manifest.yaml").write_text(
        'project: "alpha"\n'
        'concept_id: "TEST-001"\n'
        'brief: "A serene mountain cabin at dawn, soft golden light"\n'
        'scenes:\n'
        '  - number: 1\n'
        '    title: "Opening"\n'
        '    duration: 3.0\n'
        '    still: "stills/scene_01.png"\n'
        '    motion: zoom_in\n'
        '    vo_text: "Morning light spills over the ridge"\n'
        '  - number: 2\n'
        '    title: "Reveal"\n'
        '    duration: 2.5\n'
        '    still: "stills/scene_01.png"\n'
        '    motion: zoom_drift_right\n'
        '    vo_text: "A cabin, a fire, silence"\n'
        'voice:\n'
        '  voice_name: george\n'
        '  voice_id: george\n'
    )
    return workspace


def main() -> int:
    print(f"[test] starting parallax chat in {SCRATCH_DIR}")
    proc, url = start_server(SCRATCH_DIR)
    print(f"[test] server URL: {url}")

    try:
        workspace = _seed_workspace()
        print(f"[test] seeded workspace: {workspace.relative_to(SCRATCH_DIR)}/")

        # API contract first — cheaper to debug than a browser failure.
        q = urllib.parse.urlencode({"user": "alice", "project": "alpha"})
        base = url.rstrip("/")
        with urllib.request.urlopen(f"{base}/api/manifest?{q}", timeout=10) as r:
            assert r.status == 200, f"/api/manifest returned {r.status}"
            api = json.loads(r.read())
        assert api["exists"], f"api reports no manifest: {api}"
        assert api["concept_id"] == "TEST-001"
        assert len(api["scenes"]) == 2
        assert api["scenes"][0]["still_url"] == "/media/stills/scene_01.png"
        assert api["scenes"][0]["vo_text"].startswith("Morning light")
        print(f"[test] API OK: 2 scenes, concept={api['concept_id']}")

        # Browser render.
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1200, "height": 900})
            page = context.new_page()
            page.on("console", lambda m: print(f"[browser:{m.type}] {m.text}"))
            page.on("pageerror", lambda e: print(f"[browser:error] {e}"))

            target = f"{base}/manifest?{q}"
            print(f"[test] navigating to {target}")
            page.goto(target, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_selector(".meta-strip", timeout=5000)

            title = page.title()
            assert "Manifest" in title, f"unexpected title: {title!r}"

            # Brief + scenes must make it into the DOM
            body = page.locator("body").inner_text()
            assert "TEST-001" in body, "concept_id missing from page"
            assert "mountain cabin" in body.lower(), "brief missing from page"
            assert "SCENE 1" in body and "SCENE 2" in body, "scene labels missing"
            assert "Morning light spills over the ridge" in body, "vo_text missing"

            # At least one scene thumbnail rendered as an <img>
            thumbs = page.locator(".scene-thumb img").count()
            assert thumbs >= 2, f"expected >=2 scene thumbnails, got {thumbs}"

            shot = SCRATCH_DIR / "manifest_page.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[test] screenshot: {shot}")
            browser.close()

        print("[test] OK")
        return 0
    except Exception as e:
        import traceback
        print(f"[test] FAILED: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        print("[test] stopping server")
        stop_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())
