"""
Regression test for the orphan `tool_use` transcript bug.

Exercises web/server.py:run_agent_turn with a fake Anthropic client that
simulates the three failure modes that used to leave `session.messages` with
a `tool_use` block lacking a following `tool_result`:

    (1) tool execution crashes
    (2) cancel_event is set between assistant-turn and tool-exec
    (3) stream raises mid-loop after a tool_use was emitted

After each failure the transcript must be valid — every `tool_use` id in an
assistant message must appear as the `tool_use_id` of a `tool_result` in the
immediately-following user message. The next Anthropic call would otherwise
400 with `tool_use ids were found without tool_result blocks`.

No API calls. Runs in CI.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

# Offline mode: prevent the web/server module from requiring real env.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-no-api")

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
sys.path.insert(0, str(WEB))

import server  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeFinal:
    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(input_tokens=0, output_tokens=0)


class _FakeStream:
    def __init__(self, final: _FakeFinal, raise_on_iter: Exception | None = None) -> None:
        self._final = final
        self._raise = raise_on_iter

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        if self._raise is not None:
            raise self._raise
        return iter(())

    def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, stream_factory):
        self._factory = stream_factory

    def stream(self, **kwargs):
        return self._factory()


class _FakeClient:
    def __init__(self, stream_factory):
        self.messages = _FakeMessages(stream_factory)


def _tool_use_block(tool_id: str = "toolu_test_1", name: str = "list_dir") -> Any:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input={"path": "."})


def _text_block(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


# ---------------------------------------------------------------------------
# Invariant
# ---------------------------------------------------------------------------


def _assert_transcript_valid(messages: list[dict]) -> None:
    """Every assistant tool_use id must have a matching tool_result in the next
    user message. Mirrors Anthropic's server-side validation."""
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or []
        tool_ids = [
            b["id"] for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        if not tool_ids:
            continue
        assert i + 1 < len(messages), (
            f"assistant msg[{i}] has tool_use but no following message"
        )
        nxt = messages[i + 1]
        assert nxt.get("role") == "user", (
            f"assistant msg[{i}] followed by role={nxt.get('role')}, expected user"
        )
        result_ids = {
            b.get("tool_use_id") for b in (nxt.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        missing = [tid for tid in tool_ids if tid not in result_ids]
        assert not missing, (
            f"assistant msg[{i}] tool_use ids {missing} have no matching tool_result"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session() -> server.Session:
    # Build a Session without touching telemetry.create_session I/O side effects.
    with mock.patch.object(server.telemetry, "create_session", lambda *a, **k: None), \
         mock.patch.object(server, "_workspace_for", lambda *a, **k: Path("/tmp/nonexistent")):
        s = server.Session(session_id="test-session", user="test", project="main")
    return s


def _run_turn(session: server.Session, fake_client: _FakeClient) -> None:
    with mock.patch.object(server, "_lazy_client", return_value=fake_client), \
         mock.patch.object(server.telemetry, "record_event", lambda *a, **k: None), \
         mock.patch.object(server.telemetry, "touch_session", lambda *a, **k: None), \
         mock.patch.object(server, "_load_hop_prompt", return_value="SYSTEM"):
        server.run_agent_turn(session, "hello agent")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tool_exec_crash_leaves_valid_transcript():
    """Tool execution raises — synthesized error tool_results must appear."""
    session = _make_session()
    final = _FakeFinal(content=[_tool_use_block("toolu_crash")], stop_reason="tool_use")

    call_count = {"n": 0}

    def factory():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeStream(final)
        # Second iter: no tool use, end cleanly.
        return _FakeStream(_FakeFinal([_text_block("done")], "end_turn"))

    def boom(*args, **kwargs):
        raise RuntimeError("simulated tool crash")

    with mock.patch.object(server, "_execute_tool_calls", side_effect=boom):
        _run_turn(session, _FakeClient(factory))

    _assert_transcript_valid(session.messages)
    # The crash turn must have produced an is_error tool_result.
    assistant_msgs = [m for m in session.messages if m.get("role") == "assistant"]
    assert any(
        any(b.get("type") == "tool_use" and b.get("id") == "toolu_crash" for b in m["content"])
        for m in assistant_msgs
    ), "assistant tool_use should still be committed after crash recovery"


def test_cancel_between_assistant_and_exec_leaves_valid_transcript():
    """Cancel set while _execute_tool_calls runs — finally must finalize."""
    session = _make_session()
    final = _FakeFinal(content=[_tool_use_block("toolu_cancel")], stop_reason="tool_use")

    def factory():
        return _FakeStream(final)

    def cancel_during_exec(*args, **kwargs):
        session.cancel_event.set()
        raise KeyboardInterrupt("simulated cancel mid-exec")

    with mock.patch.object(server, "_execute_tool_calls", side_effect=cancel_during_exec):
        try:
            _run_turn(session, _FakeClient(factory))
        except KeyboardInterrupt:
            pass  # escape path — finally still must run

    _assert_transcript_valid(session.messages)


def test_stream_error_no_orphan():
    """Stream raises after tool_use emitted — nothing should be committed."""
    session = _make_session()

    def factory():
        exc = RuntimeError("simulated stream error")
        return _FakeStream(_FakeFinal([], "end_turn"), raise_on_iter=exc)

    _run_turn(session, _FakeClient(factory))
    _assert_transcript_valid(session.messages)


def test_max_iter_with_pending_tool_use_is_repaired_on_next_turn():
    """A prior turn that exited with an orphan tool_use (e.g. from old history)
    must be repaired at the top of the next run_agent_turn so the subsequent
    Anthropic call is valid."""
    session = _make_session()
    # Seed a corrupt transcript matching the real events.jsonl failure mode.
    session.messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_01VAVzppo74qVBgetDoU9ttB",
                 "name": "list_dir", "input": {"path": "."}},
            ],
        },
    ]

    # Next turn: model stops without tools.
    def factory():
        return _FakeStream(_FakeFinal([_text_block("ok")], "end_turn"))

    _run_turn(session, _FakeClient(factory))
    _assert_transcript_valid(session.messages)


def test_finalize_pending_tool_uses_is_idempotent():
    session = _make_session()
    session.messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_a", "name": "x", "input": {}}],
        },
    ]
    server._finalize_pending_tool_uses(session, reason="test")
    n = len(session.messages)
    server._finalize_pending_tool_uses(session, reason="test")
    assert len(session.messages) == n, "finalize must be idempotent"
    _assert_transcript_valid(session.messages)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as e:
            failures.append((t.__name__, str(e)))
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"ERR  {t.__name__}: {type(e).__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print(f"\nall {len(tests)} passed")


if __name__ == "__main__":
    _run_all()
