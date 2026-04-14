"""
Playwright smoke test — /costs page.

Boots `parallax chat` in a scratch workspace, navigates the headless
browser to /costs, waits for the report to render, takes a screenshot,
and asserts the page title contains "Cost".

Reuses the server boot helper from baseline_e2e.py — same spawn pattern,
same URL-regex probe, same output draining.

Run:
    python3 test/playwright/costs_page.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from baseline_e2e import _start_server, SCRATCH_DIR  # noqa: E402


def main() -> int:
    print(f"[test] starting parallax chat in {SCRATCH_DIR}")
    proc, url = _start_server()
    print(f"[test] server URL: {url}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1200, "height": 900})
            page = context.new_page()
            page.on("console", lambda msg: print(f"[browser:{msg.type}] {msg.text}"))
            page.on("pageerror", lambda err: print(f"[browser:error] {err}"))

            costs_url = url.rstrip("/") + "/costs"
            print(f"[test] navigating to {costs_url}")
            page.goto(costs_url, wait_until="domcontentloaded", timeout=15000)

            # Wait for the JS fetch-and-render to land the grand total into
            # the DOM. `load()` replaces the "—" placeholder with a $ value.
            page.wait_for_function(
                "() => {"
                "  const el = document.getElementById('grand-total');"
                "  return el && el.textContent && el.textContent.trim().startsWith('$');"
                "}",
                timeout=10000,
            )

            title = page.title()
            print(f"[test] page title: {title!r}")
            assert "Cost" in title, f"expected 'Cost' in page title, got {title!r}"

            # Sanity-check that at least one section rendered.
            page.wait_for_selector("section.cost-section", timeout=5000)

            shot = SCRATCH_DIR / "costs_page.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[test] screenshot: {shot}")

            browser.close()
        print("[test] OK")
        return 0
    except Exception as e:
        print(f"[test] FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        print("[test] stopping server")
        try:
            import signal
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
