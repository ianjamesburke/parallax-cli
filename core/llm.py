"""
LLM Backend
============
Unified interface for making LLM calls. Tries in order:
1. Claude Code CLI (if running inside a Claude Code session — uses OAuth, cheapest)
2. Anthropic API (if ANTHROPIC_API_KEY is set and has credits)

Agents call llm.complete() instead of anthropic.Anthropic().messages.create().
"""

import json
import os
import shutil
import subprocess
from typing import Optional


def _has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _in_claude_code() -> bool:
    return os.environ.get("CLAUDECODE") == "1"


def complete(model: str, system: str, prompt: str,
             max_tokens: int = 2048, run_id: str = "") -> dict:
    """
    Make an LLM call using the best available backend.

    Returns:
        {
            text: str,           # the model's response text
            input_tokens: int,
            output_tokens: int,
            model: str,
            backend: str,        # "api" or "claude_cli"
        }
    """
    if _in_claude_code():
        return _call_claude_cli(model, system, prompt, max_tokens)
    elif _has_api_key():
        return _call_api(model, system, prompt, max_tokens)
    else:
        raise RuntimeError(
            "[llm] No LLM backend available. Either:\n"
            "  1. Run inside a Claude Code session (uses OAuth)\n"
            "  2. Set ANTHROPIC_API_KEY environment variable\n"
        )


def _call_api(model: str, system: str, prompt: str, max_tokens: int) -> dict:
    """Direct Anthropic API call."""
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return {
        "text": response.content[0].text,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "model": model,
        "backend": "api",
    }


def _call_claude_cli(model: str, system: str, prompt: str, max_tokens: int) -> dict:
    """
    Call Claude via the claude CLI using existing OAuth session.
    Falls back to this when no API key is available but we're inside Claude Code.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        raise RuntimeError("[llm] claude CLI not found in PATH")

    # Pass system prompt separately so it doesn't bloat the user message
    cmd = [
        claude_path,
        "-p",
        "--output-format", "json",
        "--model", model,
        "--max-turns", "3",
    ]
    if system:
        cmd.extend(["--system-prompt", system])

    try:
        # Only send the user prompt via stdin (system is a flag)
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=300
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("[llm] claude CLI call timed out (300s)")

    if result.returncode != 0:
        # Check stdout too — claude CLI sometimes puts errors there
        error_text = result.stderr[:300] or result.stdout[:300]
        raise RuntimeError(f"[llm] claude CLI failed (rc={result.returncode}): {error_text}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"[llm] claude CLI returned invalid JSON: {result.stdout[:200]}")

    if data.get("is_error"):
        raise RuntimeError(f"[llm] claude CLI error: {data.get('result', 'unknown')}")

    # Extract token usage from the response
    usage = data.get("usage", {})
    input_tokens = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    return {
        "text": data.get("result", ""),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": model,
        "backend": "claude_cli",
        "cost_usd": data.get("total_cost_usd", 0),
    }


def available_backends() -> list[str]:
    """List which backends are available right now."""
    backends = []
    if _has_api_key():
        backends.append("api")
    if _in_claude_code():
        backends.append("claude_cli")
    return backends
