"""A conversation's history belongs to the conversation, not to its agent.

The rules of ``services/conversation_continuity`` without a database: who an
assistant message is stamped with, how another agent's turn is read back, the
note of a change of agent, and the trace tools' reading of a deleted agent.
The same flows on PostgreSQL: ``test_conversation_continuity_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

import app.services.builtin_tools.traces as traces_module
from app.api.v1.chat import _agent_switch_payload
from app.channels.base import artifact_markers
from app.services import conversation_continuity as continuity
from app.services.builtin_tools.conversation_history import build_conversation_history_tools
from app.services.mention_dispatch import ANSWERED_BY_KEY, MENTION_FAILED_KEY
from app.services.tracing.read_store import daily_stats_visible_filter, trace_visible_query
from app.tools.base import ToolCall

A, B = uuid.uuid4(), uuid.uuid4()


# ── Who wrote an assistant message ───────────────────────────────────────────


def test_an_assistant_message_is_stamped_with_its_conversations_agent():
    assert json.loads(continuity.stamp_author(None, A)) == {continuity.AUTHOR_AGENT_KEY: str(A)}
    stamped = json.loads(continuity.stamp_author(json.dumps({"tool_names": ["x"]}), A))
    assert stamped == {"tool_names": ["x"], continuity.AUTHOR_AGENT_KEY: str(A)}


def test_the_general_assistant_is_stamped_as_null():
    assert json.loads(continuity.stamp_author(None, None)) == {continuity.AUTHOR_AGENT_KEY: None}


@pytest.mark.parametrize(
    "meta",
    [
        {ANSWERED_BY_KEY: {"agent_id": str(B), "agent_name": "B"}},
        {MENTION_FAILED_KEY: True},
        {continuity.AGENT_SWITCH_KEY: {"to": {}}},
        {continuity.AUTHOR_AGENT_KEY: str(B)},
    ],
    ids=["mention-reply", "mention-note", "switch-note", "already-stamped"],
)
def test_a_message_that_names_its_author_is_left_as_it_is(meta):
    raw = json.dumps(meta)
    assert continuity.stamp_author(raw, A) == raw


@pytest.mark.parametrize("raw", ["[1, 2]", "not json"], ids=["not-an-object", "unreadable"])
def test_metadata_that_is_not_an_object_is_never_rewritten(raw):
    assert continuity.stamp_author(raw, A) == raw


def test_the_author_of_a_message_is_read_from_its_metadata():
    assert continuity.message_author({continuity.AUTHOR_AGENT_KEY: str(A)}) == (True, A)
    assert continuity.message_author({continuity.AUTHOR_AGENT_KEY: None}) == (True, None)
    assert continuity.message_author({ANSWERED_BY_KEY: {"agent_id": str(B)}}) == (True, B)
    assert continuity.message_author({}) == (False, None)


def test_an_author_is_named_safely():
    labels = {A: continuity.AgentLabel(name="Video\n[Bot]  "), B: continuity.AgentLabel(name="Gone", deleted=True)}
    assert continuity.label_name(None, labels) == continuity.GENERAL_ASSISTANT_NAME
    assert "\n" not in continuity.label_name(A, labels)
    assert continuity.label_name(B, labels) == "Gone"
    assert continuity.label_name(uuid.uuid4(), labels) == "an agent since deleted"
    assert continuity.label_name(A, labels, known=False) == "an earlier agent"


# ── How another agent's turn is read back ────────────────────────────────────


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """Answers the name and trace reads of ``history_labels``; counts them."""

    def __init__(self, *, agents=(), traces=()):
        self.agents = list(agents)
        self.traces = list(traces)
        self.statements: list[str] = []

    async def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if "llm_traces" in sql:
            return _Rows(self.traces)
        if "agent_tombstones" in sql:
            return _Rows([])
        return _Rows(self.agents)


def _message(role: str, content: str, meta: dict | None = None, *, trace_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        content=content,
        metadata_json=json.dumps(meta) if meta is not None else None,
        llm_trace_id=trace_id,
    )


async def _labels(session, messages, *, answering, deleted=None):
    metas = {m.id: json.loads(m.metadata_json) for m in messages if m.metadata_json}
    return await continuity.history_labels(
        session, messages, metas, answering_agent_id=answering, deleted_agent_id=deleted
    )


@pytest.mark.asyncio
async def test_a_conversation_that_never_changed_agent_reads_as_before_and_costs_no_query():
    session = _Session()
    messages = [
        _message("user", "make the intro shorter"),
        _message("assistant", "done", {continuity.AUTHOR_AGENT_KEY: str(A)}),
        _message("assistant", "an old reply, stored before authors were"),
    ]
    assert await _labels(session, messages, answering=A) == {}
    assert session.statements == []


@pytest.mark.asyncio
async def test_another_agents_turn_is_read_back_as_that_agents_words():
    session = _Session(agents=[SimpleNamespace(id=A, name="Video Bot", icon=None)])
    mine = _message("assistant", "now me", {continuity.AUTHOR_AGENT_KEY: str(B)})
    theirs = _message("assistant", "I set the music to calm", {continuity.AUTHOR_AGENT_KEY: str(A)})
    general = _message("assistant", "hello", {continuity.AUTHOR_AGENT_KEY: None})
    labels = await _labels(session, [theirs, general, mine], answering=B)
    assert labels == {
        theirs.id: "[Reply from Video Bot]\n\nI set the music to calm",
        general.id: f"[Reply from {continuity.GENERAL_ASSISTANT_NAME}]\n\nhello",
    }


@pytest.mark.asyncio
async def test_a_change_of_agent_note_reads_as_a_note():
    note = _message("assistant", "Continued by B on 2026-10-29.", {continuity.AGENT_SWITCH_KEY: {"to": {}}})
    assert await _labels(_Session(), [note], answering=B) == {note.id: "[Note]\n\nContinued by B on 2026-10-29."}


@pytest.mark.asyncio
async def test_an_old_reply_of_a_conversation_that_changed_agent_is_attributed_by_its_trace():
    trace_id = uuid.uuid4()
    legacy = _message("assistant", "the first cut", trace_id=trace_id)
    session = _Session(
        agents=[SimpleNamespace(id=A, name="Video Bot", icon=None)],
        traces=[SimpleNamespace(id=trace_id, agent_id=A, agent_name_snapshot="Video Bot")],
    )
    labels = await _labels(session, [legacy], answering=B, deleted=A)
    assert labels == {legacy.id: "[Reply from Video Bot]\n\nthe first cut"}


@pytest.mark.asyncio
async def test_an_agent_deleted_before_tombstones_is_named_by_its_trace():
    """No agent row, no tombstone: the name the run's trace recorded."""
    trace_id = uuid.uuid4()
    legacy = _message("assistant", "the first cut", trace_id=trace_id)
    note = _message("assistant", "Continued by B on 2026-10-29.", {continuity.AGENT_SWITCH_KEY: {"to": {}}})
    session = _Session(traces=[SimpleNamespace(id=trace_id, agent_id=A, agent_name_snapshot="Old Bot")])
    labels = await _labels(session, [legacy, note], answering=B)
    assert labels[legacy.id] == "[Reply from Old Bot]\n\nthe first cut"


@pytest.mark.asyncio
async def test_a_mention_reply_keeps_the_label_the_mention_turn_gives_it():
    reply = _message("assistant", "hi", {ANSWERED_BY_KEY: {"agent_id": str(A), "agent_name": "A"}})
    assert await _labels(_Session(), [reply], answering=B, deleted=A) == {}


def test_the_switch_note_says_who_continues_and_when():
    assert continuity.switch_note("Deck Bot", datetime(2026, 10, 29, 9, 30, tzinfo=UTC)) == (
        "Continued by Deck Bot on 2026-10-29."
    )


def test_the_switch_note_reaches_the_transcript_as_its_own_field():
    note = _agent_switch_payload(
        {
            continuity.AGENT_SWITCH_KEY: {
                "from": {"agent_id": str(A), "agent_name": "A", "deleted": True},
                "to": {"general_assistant": True},
                "reason": continuity.SWITCH_BY_DELETION,
            }
        }
    )
    assert note is not None
    assert note.from_agent.agent_name == "A" and note.from_agent.deleted
    assert note.to_agent.general_assistant
    assert note.reason == continuity.SWITCH_BY_DELETION
    assert _agent_switch_payload({continuity.AGENT_SWITCH_KEY: "broken"}) is None
    assert _agent_switch_payload({}) is None


def test_markers_past_a_cut_are_still_named():
    video, deck = uuid.uuid4(), uuid.uuid4()
    text = f"see [[VIDEO:{video}]] and [[PRESENTATION:{deck}]] and again [[VIDEO:{video}]]"
    assert artifact_markers(text) == [f"[[VIDEO:{video}]]", f"[[PRESENTATION:{deck}]]"]


# ── The reader suite ─────────────────────────────────────────────────────────


def test_the_reader_comes_with_the_listing_unless_the_agent_builder_offers_it():
    defs, _ = build_conversation_history_tools(str(uuid.uuid4()), conversation_id=None)
    assert [d.name for d in defs] == ["list_conversations", "read_conversation_history"]
    defs, _ = build_conversation_history_tools(str(uuid.uuid4()), conversation_id=None, with_listing=False)
    assert [d.name for d in defs] == ["read_conversation_history"]
    listing = build_conversation_history_tools(str(uuid.uuid4()), conversation_id=None)[0][0]
    assert {"agent", "object_id", "object_type"} <= listing.input_schema["properties"].keys()


@pytest.mark.asyncio
async def test_an_anonymous_caller_reads_nothing():
    _, executor = build_conversation_history_tools("anonymous", conversation_id=str(uuid.uuid4()))
    result = await executor.execute_tool(ToolCall(id="1", name="read_conversation_history", arguments={}))
    assert "not available for anonymous users" in result.content


# ── A deleted agent's traces ─────────────────────────────────────────────────


def _sql(query) -> str:
    return str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})).lower()


def test_a_trace_whose_agent_is_gone_stays_visible_to_its_user_and_the_agents_owner():
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    rendered = _sql(trace_visible_query(user, []))
    assert "left outer join agent_tombstones" in rendered
    assert "agents.id is null" in rendered
    assert "agent_tombstones.user_id" in rendered
    # The rollup agrees with the raw traces (``daily_stats_visible_filter``).
    assert "agents.id is null" in _sql(daily_stats_visible_filter(user, []))


class _DB:
    async def commit(self):
        return None


class _SessionCtx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_exc):
        return False


def _non_admin(monkeypatch) -> uuid.UUID:
    """A non-admin caller of the trace tools; their id."""
    db = _DB()
    caller = uuid.uuid4()
    monkeypatch.setattr(traces_module, "async_session", lambda: _SessionCtx(db))

    async def _context(_db, _uid):
        return SimpleNamespace(id=caller, email="u@test.local", is_admin=False), []

    async def _audit(*_a, **_kw):
        return None

    monkeypatch.setattr(traces_module, "_load_user_context", _context)
    monkeypatch.setattr(traces_module, "_audit_trace_read", _audit)
    return caller


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", [True, False], ids=["deleted-agent", "live-agent"])
async def test_a_deleted_agent_is_a_target_without_a_supervision_grant(monkeypatch, gone):
    _non_admin(monkeypatch)
    target = str(uuid.uuid4())
    seen: dict = {}

    async def _gone(_db, ids):
        return set(ids) if gone else set()

    async def _fetch(_db, **kwargs):
        seen.update(kwargs)
        return '{"count": 0, "traces": []}'

    monkeypatch.setattr(traces_module, "_agents_gone", _gone)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fetch)
    _, executor = traces_module.build_trace_tools(db=object(), user_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(id="1", name="inspect_agent_traces", arguments={"target_agent_id": target})
    )
    if gone:
        assert result.is_error is False
        assert seen["agent_ids"] == [target]
        # Read as the caller may see it, never through an admin bypass.
        assert seen["user"].is_admin is False
    else:
        assert "not allowed to inspect traces" in result.content
        assert seen == {}


class _Store:
    def __init__(self, trace):
        self.trace = trace

    async def get_trace_detail(self, _db, **_kw):
        return self.trace, None, None, None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cross_agent", "gone", "own_run", "readable"),
    [(True, True, True, True), (True, True, False, False), (False, True, True, False), (True, False, True, False)],
    ids=["own-run-of-a-deleted-agent", "someone-elses-run", "self-inspection-only", "live-agent"],
)
async def test_a_deleted_agents_payload_is_read_through_the_supervision_surface(
    monkeypatch, cross_agent, gone, own_run, readable
):
    caller = _non_admin(monkeypatch)
    trace = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        # Someone else's run of an agent the caller did not own: the tombstone
        # read (no database here) fails closed.
        user_id=caller if own_run else uuid.uuid4(),
        output={"text": "the answer"},
    )

    async def _gone(_db, ids):
        return set(ids) if gone else set()

    monkeypatch.setattr(traces_module, "_agents_gone", _gone)
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: _Store(trace))
    _, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(caller), agent_id=str(uuid.uuid4()), allow_cross_agent=cross_agent
    )
    result = await executor.execute_tool(
        ToolCall(id="1", name="get_trace_payload", arguments={"trace_id": str(trace.id), "field": "output"})
    )
    assert ("the answer" in result.content) is readable
    if not readable:
        assert "not allowed to read payloads" in result.content


@pytest.mark.asyncio
async def test_telling_deleted_agents_apart_fails_closed():
    class _Broken:
        async def execute(self, _statement):
            raise RuntimeError("database down")

    assert await traces_module._agents_gone(_Broken(), {str(uuid.uuid4())}) == set()


@pytest.mark.asyncio
async def test_the_listed_trace_names_its_agent(monkeypatch):
    rows = [SimpleNamespace(agent_name="Video Bot"), SimpleNamespace(agent_name=None)]
    traces = [{"agent_id": str(A)}, {"agent_id": None}]

    async def _gone(_db, ids):
        return {str(A)} & set(ids)

    monkeypatch.setattr(traces_module, "_agents_gone", _gone)
    await traces_module._annotate_agents(object(), traces, rows)
    assert traces == [{"agent_id": str(A), "agent_name": "Video Bot", "agent_deleted": True}, {"agent_id": None}]


# ── A successor the deletion names ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unusable_successor_is_refused_before_anything_is_deleted(monkeypatch):
    from app.api.v1 import agents as agents_api
    from app.services import agent_deletion

    agent = SimpleNamespace(id=A)
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    deleted: list = []

    async def _groups(_db, _uid):
        return []

    async def _agent(*_a, **_kw):
        return agent

    async def _refusal(_db, **kwargs):
        assert kwargs == {"actor_id": user.id, "agent_id": A, "successor_id": B}
        return "The successor agent **B** is disabled: enable it first, or choose another one."

    async def _delete(*a, **kw):
        deleted.append((a, kw))

    monkeypatch.setattr(agents_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents_api, "get_user_agent", _agent)
    monkeypatch.setattr(agents_api, "assert_writable_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(continuity, "successor_refusal", _refusal)
    monkeypatch.setattr(agent_deletion, "delete_agent", _delete)
    with pytest.raises(HTTPException) as refused:
        await agents_api.delete_agent(
            agent_id=str(A), request=None, user=user, db=object(), _ai_gate=None, successor_agent_id=str(B)
        )
    assert refused.value.status_code == 400
    assert "disabled" in refused.value.detail
    assert deleted == []


@pytest.mark.asyncio
async def test_a_successor_cannot_be_the_agent_itself():
    assert "its own successor" in await continuity.successor_refusal(
        object(), actor_id=uuid.uuid4(), agent_id=A, successor_id=A
    )
