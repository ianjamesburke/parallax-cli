"""
Playwright E2E — beta layout isolation + cross-project read.

Does NOT drive the full Claude pipeline (fast, no LLM calls). Instead it
exercises the server's layout resolution directly:

  1. Start parallax chat in a fresh scratch dir.
  2. Drop a fake "raw.png" at the master dir root so we can later verify
     cross-project reads resolve against PROJECT_DIR.
  3. Hit /api/servers and confirm this process is registered (Phase 2a).
  4. Create two projects under two different users via /api/projects +
     ?user=/?project= query args. Assert the filesystem now has
        parallax/users/alice/alpha/
        parallax/users/bob/alpha/
     with the canonical subdirs (stills/, input/, output/, drafts/,
     audio/, logs/).
  5. Fetch /media/raw.png?user=alice&project=alpha — the server should
     resolve it against PROJECT_DIR (master dir root), not the per-user
     workspace, so alice (who has never seen this file from her project's
     perspective) can still read it.
  6. Open the browser once and screenshot the chat UI under
     ?user=alice&project=alpha so the project badge shows alpha.

Run:
    python3 test/playwright/layout_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _helpers import start_server, stop_server  # noqa: E402

SCRATCH_DIR = Path("/tmp/parallax-beta-e2e-layout")

# Flip the per-user layout on for this test specifically — the default
# changed to opt-in, but layout_e2e is the regression gate for the users/
# nesting, so we force it on via the env var.
os.environ["PARALLAX_PER_USER_WORKSPACES"] = "1"


def _get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")}


def main() -> int:
    print(f"[test] starting parallax chat in {SCRATCH_DIR}")
    proc, url = start_server(SCRATCH_DIR)
    print(f"[test] server URL: {url}")

    try:
        # Drop a fake raw image at the master dir so we can test cross-
        # project reads later. A 1x1 PNG is the shortest valid file.
        raw_png = SCRATCH_DIR / "raw.png"
        raw_png.write_bytes(
            bytes.fromhex(
                "89504E470D0A1A0A0000000D49484452000000010000000108060000001F"
                "15C4890000000D4944415478DA6364F8FFFFFF3F0005FE02FE6A5BB5F200"
                "00000049454E44AE426082"
            )
        )

        # (3) Server registry contains an entry for this scratch dir. The
        # registry records the parallax_web child's pid, which is not the
        # same as `proc.pid` (the `parallax chat` wrapper), so we match on
        # resolved cwd instead.
        base = url.rstrip("/")
        status, body = _get(f"{base}/api/servers")
        assert status == 200, f"/api/servers returned {status}"
        data = json.loads(body)
        servers = data.get("servers") or []
        scratch_resolved = str(SCRATCH_DIR.resolve())
        cwds = {str(Path(s.get("cwd") or "").resolve()) for s in servers}
        assert scratch_resolved in cwds, (
            f"our scratch dir {scratch_resolved} not in registry cwds {cwds}"
        )
        print(f"[test] registry OK: {len(servers)} server(s) live, ours is listed")

        # (4) Create two isolated projects under two different users.
        for user, project in (("alice", "alpha"), ("bob", "alpha")):
            q = urllib.parse.urlencode({"user": user, "project": project})
            s, j = _post_json(f"{base}/api/projects?{q}", {"name": project})
            assert s == 200, f"create project failed: {s} {j}"
            ws = SCRATCH_DIR / "parallax" / "users" / user / project
            assert ws.is_dir(), f"workspace not scaffolded: {ws}"
            for sub in ("stills", "input", "output", "drafts", "audio", "logs"):
                assert (ws / sub).is_dir(), f"missing {sub} in {ws}"
            print(f"[test] scaffold OK: {ws.relative_to(SCRATCH_DIR)}/")

        # alice's workspace is distinct from bob's.
        alice = SCRATCH_DIR / "parallax" / "users" / "alice" / "alpha"
        bob = SCRATCH_DIR / "parallax" / "users" / "bob" / "alpha"
        assert alice != bob and alice.exists() and bob.exists()
        print(f"[test] isolation OK: alice != bob")

        # (5) Cross-project read: /media/raw.png via alice's workspace
        # should resolve against the master dir (PROJECT_DIR), because
        # alice has no raw.png in her own workspace. The server's two-tier
        # sandbox falls back to PROJECT_DIR when the path escapes the
        # workspace.
        q = urllib.parse.urlencode({"user": "alice", "project": "alpha"})
        status, body = _get(f"{base}/media/raw.png?{q}")
        assert status == 200, f"cross-project read failed: {status}"
        assert body.startswith(b"\x89PNG"), "served file is not a PNG"
        print(f"[test] cross-project read OK: /media/raw.png ({len(body)} bytes)")

        # (6) Browser snapshot under alice's alpha project.
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            page = context.new_page()
            page.on("pageerror", lambda err: print(f"[browser:error] {err}"))
            q = urllib.parse.urlencode({"user": "alice", "project": "alpha"})
            target = f"{base}/?{q}"
            print(f"[test] navigating to {target}")
            page.goto(target, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_selector("#project-badge", timeout=5000)
            shot = SCRATCH_DIR / "layout_final.png"
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
