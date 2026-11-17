"""A resumed turn runs again a materialize_repository its interrupted attempt had started (issue #10284).

A worker stop cut a checkout mid-call: the workspace held an empty directory and no checkout record, and the
in-flight repeat guard answered the identical call "Not run ... make the same call again and it runs" twice. A
checkout is idempotent (a repeat replaces the tree; add_paths keeps the workspace's files), so the tool is declared
``replay_safe`` and its repeat runs at once. The guard stays on every other write.
"""

from __future__ import annotations

import uuid

from app.core.config import settings
from app.llm.base import ChatMessage
from app.services.turn_checkpoint import ResumedWork, in_flight_calls, in_flight_writes, resume_note
from app.tools.base import ToolCall

ARGS = {"provider": "github", "repository": "owner/name", "path": "x10264", "sub_paths": ["backend"]}


def _cut_round(name: str, arguments: dict) -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="m1", name=name, arguments=arguments)]),
    ]


def _offered(monkeypatch) -> dict:
    from app.services.builtin_tools.media import build_code_interpreter_tool

    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    definitions, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    return {d.name: d for d in definitions}


def test_materialize_repository_is_replay_safe_and_not_cut_safe(monkeypatch):
    offered = _offered(monkeypatch)
    assert offered["materialize_repository"].replay_safe
    # Two consecutive cuts on the same checkout still trip the poison guard: a huge tree may be the cause.
    assert not offered["materialize_repository"].cut_safe


def test_a_cut_materialize_is_not_held_back_on_the_resumed_turn(monkeypatch):
    offered = _offered(monkeypatch)
    note = resume_note(ResumedWork(found=True, in_flight=in_flight_calls(_cut_round("materialize_repository", ARGS))))
    assert note is not None
    replay_safe = frozenset(name for name, d in offered.items() if d.replay_safe)
    assert in_flight_writes([note], replay_safe=replay_safe) == {}


def test_the_guard_stays_on_a_non_idempotent_workspace_write(monkeypatch):
    offered = _offered(monkeypatch)
    edit = {"path": "a.py", "edits": [{"find": "old", "replace": "new"}]}
    note = resume_note(ResumedWork(found=True, in_flight=in_flight_calls(_cut_round("sandbox_edit_file", edit))))
    assert note is not None
    replay_safe = frozenset(name for name, d in offered.items() if d.replay_safe)
    assert not offered["sandbox_edit_file"].replay_safe
    assert [c["name"] for c in in_flight_writes([note], replay_safe=replay_safe).values()] == ["sandbox_edit_file"]
