"""A saved session tool selection is read for the agent it was saved for.

The selection a turn saves on its conversation records that agent (the
General Assistant's has none). A handover, a switch in the agent menu or a
continue-with-agent rebinds the conversation without touching the saved
selection, so every reader goes through ``saved_tool_selection``: one saved
for another agent is no selection at all, and the new agent runs with its
own defaults. What it still lists is no licence either: an id unbound since
the save, which its owner could not bind, is left out where it is read
(``usable_saved_selection``).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import chat_selection
from app.services.chat_saved_tool_selection import (
    persist_tool_selection,
    saved_tool_selection,
    session_tool_kwargs,
)
from app.services.chat_selection import _known_ids, usable_saved_selection

ANALYST = uuid.uuid4()
HANDLER = uuid.uuid4()
KB = "11111111-1111-4111-8111-111111111111"


def _conv(agent_id, saved: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        tool_overrides_json=json.dumps(saved) if saved is not None else None,
    )


def _selection(agent_id=None, **kwargs) -> dict:
    payload = {
        "builtin_tool_ids": ["web_fetch"],
        "custom_tool_ids": [],
        "integration_ids": [],
        "mcp_server_ids": [],
        "knowledge_base_ids": [KB],
        "default_tool_ids": ["web_fetch", KB],
    }
    if agent_id is not ...:
        payload["agent_id"] = str(agent_id) if agent_id else None
    payload.update(kwargs)
    return payload


def test_a_selection_is_read_for_the_agent_it_was_saved_for():
    assert saved_tool_selection(_conv(ANALYST, _selection(ANALYST)))["knowledge_base_ids"] == [KB]


def test_a_selection_saved_for_another_agent_is_none():
    assert saved_tool_selection(_conv(HANDLER, _selection(ANALYST))) is None
    assert saved_tool_selection(_conv(None, _selection(ANALYST))) is None
    assert saved_tool_selection(_conv(ANALYST, _selection(None))) is None


def test_a_selection_saved_before_the_agent_was_recorded_is_the_general_assistants():
    legacy = _selection(...)
    assert saved_tool_selection(_conv(None, legacy)) is not None
    assert saved_tool_selection(_conv(ANALYST, legacy)) is None


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", "3"])
def test_no_readable_selection_is_none(raw):
    assert saved_tool_selection(SimpleNamespace(agent_id=None, tool_overrides_json=raw)) is None


def test_a_save_records_the_agent():
    conv = SimpleNamespace(agent_id=ANALYST, tool_overrides_json=None)
    assert persist_tool_selection(
        conv,
        builtin_tool_ids=[],
        custom_tool_ids=None,
        integration_ids=None,
        mcp_server_ids=None,
        knowledge_base_ids=None,
        default_tool_ids=["generate_report"],
    )
    saved = json.loads(conv.tool_overrides_json)
    assert saved["agent_id"] == str(ANALYST)
    assert saved["builtin_tool_ids"] == []
    assert saved_tool_selection(conv) is not None


def test_a_one_off_selection_saves_nothing():
    conv = SimpleNamespace(agent_id=ANALYST, tool_overrides_json=None)
    assert not persist_tool_selection(
        conv,
        builtin_tool_ids=["generate_presentation"],
        custom_tool_ids=None,
        integration_ids=None,
        mcp_server_ids=None,
        knowledge_base_ids=None,
        default_tool_ids=None,
        scope="turn",
    )
    assert conv.tool_overrides_json is None


def test_a_turn_after_a_handover_runs_without_the_old_agents_selection():
    assert session_tool_kwargs(saved_tool_selection(_conv(HANDLER, _selection(ANALYST)))) == {}
    kwargs = session_tool_kwargs(saved_tool_selection(_conv(ANALYST, _selection(ANALYST))))
    assert kwargs["session_knowledge_base_ids"] == [KB]


def test_a_turn_after_a_handover_runs_with_the_new_agents_defaults():
    agent = SimpleNamespace(
        integration_bindings={},
        builtin_tool_ids=["files"],
        custom_tool_ids=[],
        mcp_server_ids=[],
        knowledge_base_ids=[],
    )
    assert session_tool_kwargs(saved_tool_selection(_conv(HANDLER, _selection(ANALYST))), agent) == {}


class _NoRows:
    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


@pytest.mark.asyncio
async def test_a_saved_selection_makes_no_id_known():
    """What the conversation saved is no licence: the agent's bindings, the
    General Assistant's settings and the profile make an id known, and the
    saved selection only lists what they were."""
    for conv in (_conv(ANALYST, _selection(ANALYST)), _conv(None, _selection(None))):
        known = await _known_ids(_NoRows(), user=None, conversation=conv)
        assert KB not in known["knowledge_base_ids"]


@pytest.mark.asyncio
async def test_an_id_its_owner_may_no_longer_load_is_left_out_of_the_saved_selection(monkeypatch):
    mcp = "22222222-2222-4222-8222-222222222222"
    gone = "33333333-3333-4333-8333-333333333333"
    kept = "44444444-4444-4444-8444-444444444444"
    asked: list[dict] = []

    async def _refused(_db, *, user_id, conversation, requested, **_k):
        asked.append(requested)
        refused = {"knowledge_base_ids": {KB}, "integration_ids": {gone}}
        return {kind: refused.get(kind, set()) for kind in chat_selection.SELECTION_KINDS}

    monkeypatch.setattr(chat_selection, "_refused_ids", _refused)
    conv = _conv(ANALYST, _selection(ANALYST, mcp_server_ids=[mcp], integration_ids=[[gone], kept]))
    usable = await usable_saved_selection(None, user_id=uuid.uuid4(), conversation=conv)
    assert usable["knowledge_base_ids"] == []
    assert usable["integration_ids"] == [[], kept]
    assert usable["mcp_server_ids"] == [mcp]
    assert usable["builtin_tool_ids"] == ["web_fetch"]
    # The baseline is kept: the agent's defaults since the save are still told apart.
    assert usable["default_tool_ids"] == ["web_fetch", KB]
    assert asked[0]["knowledge_base_ids"] == [KB]


@pytest.mark.asyncio
async def test_a_selection_saved_for_another_agent_is_not_checked(monkeypatch):
    async def _refused(*_a, **_k):
        raise AssertionError("nothing to check")

    monkeypatch.setattr(chat_selection, "_refused_ids", _refused)
    conv = _conv(HANDLER, _selection(ANALYST))
    assert await usable_saved_selection(None, user_id=uuid.uuid4(), conversation=conv) is None


class _Db:
    """A session answering every ``select(User)`` with the same owner."""

    def __init__(self):
        self.reads = 0

    async def execute(self, _stmt):
        self.reads += 1
        return SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(id=OWNER, is_admin=False))


OWNER = uuid.uuid4()


def _lookup_spies(monkeypatch, known: dict[str, set[str]]):
    calls = {"known": 0, "groups": 0, "visibility": []}

    async def _known(_db, *, user, conversation):
        calls["known"] += 1
        return {kind: set(known.get(kind, set())) for kind in chat_selection.SELECTION_KINDS}

    async def _groups(_db, _user_id):
        calls["groups"] += 1
        return []

    async def _refused_additions(_db, *, value, **_k):
        calls["visibility"].append(list(value))
        return list(value)

    monkeypatch.setattr(chat_selection, "_known_ids", _known)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    monkeypatch.setattr("app.api.v1.binding_access.refused_additions", _refused_additions)
    return calls


@pytest.mark.asyncio
async def test_a_saved_selection_still_bound_costs_no_group_or_visibility_query(monkeypatch):
    calls = _lookup_spies(monkeypatch, {"knowledge_base_ids": {KB}})
    conv = _conv(ANALYST, _selection(ANALYST))
    assert await usable_saved_selection(_Db(), user_id=OWNER, conversation=conv) == _selection(ANALYST)
    assert calls["groups"] == 0
    assert calls["visibility"] == []


@pytest.mark.asyncio
async def test_a_list_reads_each_owner_agent_and_unbound_id_once(monkeypatch):
    gone = "33333333-3333-4333-8333-333333333333"
    calls = _lookup_spies(monkeypatch, {"knowledge_base_ids": {KB}})
    db = _Db()
    lookups: dict = {}
    convs = [_conv(ANALYST, _selection(ANALYST, mcp_server_ids=[gone])) for _ in range(3)]
    for conv in convs:
        usable = await usable_saved_selection(db, user_id=OWNER, conversation=conv, lookups=lookups)
        assert usable["mcp_server_ids"] == []
        assert usable["knowledge_base_ids"] == [KB]
    assert db.reads == 1
    assert calls["known"] == 1
    assert calls["groups"] == 1
    # Only the id no longer bound is checked against what the owner can see, once.
    assert calls["visibility"] == [[gone]]
