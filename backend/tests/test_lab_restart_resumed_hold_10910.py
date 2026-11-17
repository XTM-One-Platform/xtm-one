"""The lab's ``lab:restart-mid-turn [hold] [resumed]`` script: a long resumed turn with steers (#10910).

The chat-layout audit of the cut-turn card needs a resumed turn that works for minutes, with a tool call and
steers folded into it, as production resumes do; the stub used to answer the resumed turn at once. lab.py
starts the stores when imported, so its functions are compiled one by one from its syntax tree (as
``test_lab_stream_bursts_10910.py`` does). dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

NAMES = (
    "parse_lab_script",
    "parse_lab_call",
    "parse_lab_stream",
    "parse_lab_echo",
    "message_text",
    "restart_script",
    "restart_step",
    "restart_resumed_step",
    "restart_resumed_call",
    "restart_resumed_pieces",
    "restart_resumed_answer",
    "LAB_SCRIPT_MAX_HOLD_SECONDS",
    "LAB_WAIT_DEFAULT_SECONDS",
    "LAB_WAIT_MAX_SECONDS",
    "LAB_STREAM_DEFAULTS",
    "LAB_STREAM_MAX_BURSTS",
    "LAB_STREAM_MAX_GAP_MS",
    "LAB_ECHO_MAX_CHARS",
    "LAB_RESTART_DEFAULT_HOLD_SECONDS",
    "LAB_RESTART_MAX_HOLD_SECONDS",
    "LAB_RESTART_MAX_RESUMED_SECONDS",
    "LAB_RESTART_RESUMED_CALL_ID",
    "LAB_RESTART_CALL_ID",
    "LAB_RESTART_TOOLS",
    "LAB_RESUMED_NOTE_PREFIX",
    "RESTART_BEFORE_UP_TEXT",
    "restart_refusal",
)


def _lab_namespace(up: bool = True) -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in NAMES)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in NAMES for t in node.targets))
    ]
    restart = {"stopped_at": "05:00:00Z", "back_at": "05:00:30Z", "pending": True}
    lab_up = threading.Event()
    if up:
        lab_up.set()
    namespace: dict = {
        "json": json,
        "_RESTART": restart,
        "LAB_UP": lab_up,
        "log": lambda *_: None,
        "utc_clock": lambda: "05:00:30Z",
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def test_the_script_reads_hold_and_resumed_seconds_capped():
    parse = _lab_namespace()["parse_lab_script"]
    assert parse("lab:restart-mid-turn") == {"script": "restart-mid-turn", "hold": 5, "resumed": 0}
    assert parse("lab:restart-mid-turn 20") == {"script": "restart-mid-turn", "hold": 20, "resumed": 0}
    assert parse("lab:restart-mid-turn 20 90") == {"script": "restart-mid-turn", "hold": 20, "resumed": 90}
    assert parse("lab:restart-mid-turn 999 999") == {"script": "restart-mid-turn", "hold": 60, "resumed": 240}


def test_a_resumed_call_reads_the_steers_sent_during_the_resume():
    ns = _lab_namespace()
    messages = [
        _user("lab:restart-mid-turn 20 60"),
        {"role": "assistant", "content": "Lab script: the answer starts"},
        {"role": "assistant", "content": "[Resumed: this turn was cut by a platform restart]"},
        _user("lab:restart-mid-turn 20 60"),
        _user("first steer"),
        _user("second steer"),
    ]
    script = ns["restart_script"]({"messages": messages}, pending=True)
    assert script["resumed"] == 60
    assert script["steers"] == ["first steer", "second steer"]
    pieces = ns["restart_resumed_pieces"](script)
    assert len(pieces) == 61
    assert '"first steer"; "second steer"' in pieces[0]


def test_a_later_turn_never_reads_an_older_restart_script():
    ns = _lab_namespace()
    messages = [_user("lab:restart-mid-turn 20 60"), {"role": "assistant", "content": "done"}, _user("hello")]
    assert ns["restart_script"]({"messages": messages}, pending=False) is None
    assert ns["restart_script"]({"messages": [_user("lab:echo hi")]}, pending=True) is None


def test_a_restart_before_lab_up_is_refused_and_stops_nothing():
    # A restart sent while the lab still seeded stopped the backend mid-seed and the lab exited 1.
    starting = _lab_namespace(up=False)
    assert starting["restart_refusal"]() == starting["RESTART_BEFORE_UP_TEXT"]
    assert "after lab up" in starting["RESTART_BEFORE_UP_TEXT"]
    assert _lab_namespace(up=True)["restart_refusal"]() is None


def test_the_held_resumed_turn_calls_one_tool_then_answers():
    ns = _lab_namespace()
    first = {"messages": [_user("lab:restart-mid-turn 20 60")]}
    assert ns["restart_resumed_step"](first) == "call"
    after = {"messages": [*first["messages"], {"role": "tool", "tool_call_id": ns["LAB_RESTART_RESUMED_CALL_ID"]}]}
    assert ns["restart_resumed_step"](after) == "answer"
    assert ns["restart_resumed_call"]({"search_knowledge"}) == ("search_knowledge", {"query": "lab resumed turn"})
