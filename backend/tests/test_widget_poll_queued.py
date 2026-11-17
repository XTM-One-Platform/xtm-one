"""The widget waits for the answer to a message queued behind a turn in flight.

A message sent while the session's turn runs is answered with ``queued`` and
the widget polls the session. The in-flight turn's answer lands after the
queued message ([U1, U2, A1]) while the session still runs; the queued
message's own answer comes with the next turn ([U1, U2, A1, A2]). The poll
stops on the latter only.

The check is the ``poll-done`` block of ``widget.js``, run here by Node
exactly as the browser runs it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

WIDGET_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "widget.js"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        if os.environ.get("CI"):
            pytest.fail("node is not installed on the CI runner; the Backend job must set it up")
        pytest.skip("node is not installed")
    return node


def _poll_block() -> str:
    source = WIDGET_JS.read_text(encoding="utf-8")
    start = source.index("// poll-done:start")
    end = source.index("// poll-done:end")
    return source[start:end]


def _answered(polls: list[dict], baseline: int) -> list[bool]:
    program = (
        f"{_poll_block()}\n"
        f"const polls = {json.dumps(polls)};\n"
        f"console.log(JSON.stringify(polls.map((p) => pollAnswered(p, {baseline}))));\n"
    )
    result = subprocess.run([_node(), "-e", program], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _msgs(*roles: str) -> list[dict]:
    return [{"role": role, "content": f"{role} {i}"} for i, role in enumerate(roles)]


def test_queued_message_waits_past_the_in_flight_answer() -> None:
    # The widget holds [U1, U2] when it starts polling for U2's answer.
    polls = [
        {"messages": _msgs("user", "user"), "is_processing": True},
        {"messages": _msgs("user", "user", "assistant"), "is_processing": True},
        {"messages": _msgs("user", "user", "assistant", "assistant"), "is_processing": False},
    ]
    assert _answered(polls, baseline=2) == [False, False, True]


def test_reload_mid_turn_stops_on_the_answer() -> None:
    # A reload mid-turn restores [U1] and polls until the turn's answer is in.
    polls = [
        {"messages": _msgs("user"), "is_processing": True},
        {"messages": _msgs("user", "assistant"), "is_processing": False},
    ]
    assert _answered(polls, baseline=1) == [False, True]


def test_no_new_message_is_not_an_answer() -> None:
    polls = [{"messages": _msgs("user", "assistant"), "is_processing": False}, {}]
    assert _answered(polls, baseline=2) == [False, False]
