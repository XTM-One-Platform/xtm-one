"""A message's own tool selection, used without being saved.

A block of the conversation (an investigation's "Generate executive summary"
or "Send to OpenCTI") sends its request with the tools that work needs added
to the turn's selection. That selection is the request's, not the
conversation's: `tool_selection_scope: "turn"` makes the turn use it while
the saved selection stays as it was, so the one-off tools do not stay enabled
for every later turn. An ordinary message saves its selection, in a
conversation with an agent as with the General Assistant, when an agent
answers it: reopened, the conversation shows what its last message used.
"""

from __future__ import annotations

import json
import uuid

import pytest

from .test_chat_web_steering import _patch_general_chat, _send, fake_rl, ws_env  # noqa: F401


def _recording(calls: list[dict]):
    async def _run(_db, _uid, _history, _content, **kwargs):
        calls.append(kwargs)
        return ("done", None)

    return _run


@pytest.mark.asyncio
async def test_a_one_off_selection_is_used_and_not_saved(monkeypatch, ws_env):  # noqa: F811
    calls: list[dict] = []
    _patch_general_chat(monkeypatch, _recording(calls))
    frame = {
        "content": "Make the executive summary",
        "builtin_tool_ids": ["deep_investigation", "generate_presentation"],
        "tool_selection_scope": "turn",
    }
    await _send(ws_env, frame)
    assert [c["session_builtin_tool_ids"] for c in calls] == [["deep_investigation", "generate_presentation"]]
    assert ws_env.conv.tool_overrides_json is None


@pytest.mark.asyncio
async def test_an_ordinary_selection_is_still_saved(monkeypatch, ws_env):  # noqa: F811
    calls: list[dict] = []
    _patch_general_chat(monkeypatch, _recording(calls))
    frame = {"content": "hello", "builtin_tool_ids": ["web_fetch"]}
    await _send(ws_env, frame)
    assert len(calls) == 1
    assert json.loads(ws_env.conv.tool_overrides_json)["builtin_tool_ids"] == ["web_fetch"]


def _as_agent_conversation(env) -> None:
    """The fake session answers every lookup with the conversation, so it stands
    in for the conversation's agent too: an enabled agent of its own."""
    env.conv.agent_id = uuid.uuid4()
    env.conv.enabled = True
    env.conv.name = "Analyst"


def _patch_agent_chat(monkeypatch, env, calls: list[dict]):
    """Record each agent turn's kwargs, and mark on the timeline when the
    selection was saved and when the agent ran."""
    import app.api.v1.chat as chat_module
    import app.services.agent_chat as agent_chat_module

    persist = chat_module.persist_tool_selection

    def _persist(*args, **kwargs):
        saved = persist(*args, **kwargs)
        if saved:
            env.timeline.append(("selection saved",))
        return saved

    async def _run(_db, _uid, _agent, _history, _content, **kwargs):
        env.timeline.append(("agent ran",))
        calls.append(kwargs)
        return ("done", None)

    monkeypatch.setattr(chat_module, "persist_tool_selection", _persist)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _run)


@pytest.mark.asyncio
async def test_an_agent_conversation_saves_its_selection(monkeypatch, ws_env):  # noqa: F811
    _as_agent_conversation(ws_env)
    calls: list[dict] = []
    _patch_agent_chat(monkeypatch, ws_env, calls)
    frame = {"content": "hello", "builtin_tool_ids": [], "default_tool_ids": ["generate_report"]}
    await _send(ws_env, frame)
    # The turn runs with the suite switched off: an empty selection, not the defaults.
    assert [c["session_builtin_tool_ids"] for c in calls] == [[]]
    saved = json.loads(ws_env.conv.tool_overrides_json)
    assert saved["builtin_tool_ids"] == []
    assert saved["default_tool_ids"] == ["generate_report"]
    assert saved["agent_id"] == str(ws_env.conv.agent_id)
    # Committed before the agent runs, not left to the turn's first autoflush.
    timeline = ws_env.timeline
    saved_at = timeline.index(("selection saved",))
    assert ("commit",) in timeline[saved_at:timeline.index(("agent ran",))]


@pytest.mark.asyncio
async def test_an_agent_conversation_keeps_its_selection_from_a_one_off(monkeypatch, ws_env):  # noqa: F811
    _as_agent_conversation(ws_env)
    calls: list[dict] = []
    _patch_agent_chat(monkeypatch, ws_env, calls)
    frame = {
        "content": "Make the executive summary",
        "builtin_tool_ids": ["generate_presentation"],
        "tool_selection_scope": "turn",
    }
    await _send(ws_env, frame)
    assert [c["session_builtin_tool_ids"] for c in calls] == [["generate_presentation"]]
    assert ws_env.conv.tool_overrides_json is None


@pytest.mark.asyncio
async def test_a_turn_no_agent_answers_keeps_the_saved_selection(monkeypatch, ws_env):  # noqa: F811
    _as_agent_conversation(ws_env)
    ws_env.conv.enabled = False
    calls: list[dict] = []
    _patch_agent_chat(monkeypatch, ws_env, calls)
    await _send(ws_env, {"content": "hello", "builtin_tool_ids": ["web_fetch"]})
    assert calls == []
    assert ws_env.conv.tool_overrides_json is None


def test_a_rest_send_folded_into_a_running_turn_keeps_the_saved_selection(monkeypatch):
    """A REST send saves its selection once an agent or the General Assistant
    runs it, as the web turn does: one folded into a turn already running, which
    runs with that turn's tools, leaves the saved selection as it was."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.api.v1 import chat as chat_api
    from app.services import chat_turn_guard

    from .test_malformed_conversation_id import _client, _conversation_url, _FakeDB, _user

    user = _user()
    conversation = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        title="Chat",
        presentation_id=None,
        video_project_id=None,
        custom_dashboard_id=None,
        fleet_id=None,
        agent_id=None,
        tool_overrides_json=None,
    )

    async def _nothing(*_args, **_kwargs):
        return None

    async def _store_message(_db, _conversation_id, **fields):
        return SimpleNamespace(id=uuid.uuid4(), sequence=1, created_at=datetime.now(UTC), **fields)

    async def _fold(*_args, **_kwargs):
        return None, True

    monkeypatch.setattr(chat_api, "enforce_rate_limit", _nothing)
    monkeypatch.setattr(chat_api, "load_studio_context", _nothing)
    monkeypatch.setattr(chat_api, "load_desktop_context", _nothing)
    monkeypatch.setattr(chat_api, "store_message", _store_message)
    monkeypatch.setattr(chat_api.chat_turn_stream, "publish_live", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chat_turn_guard, "acquire_or_fold", _fold)

    client = _client(chat_api.router, "/api/v1/chat", _FakeDB(conversation), user)
    resp = client.post(
        _conversation_url(conversation.id, "/messages"),
        json={"content": "hello", "builtin_tool_ids": ["web_fetch"]},
    )

    assert resp.status_code == 202, resp.text
    assert conversation.tool_overrides_json is None
