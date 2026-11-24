"""State entries and supersession, without a database.

The prompt block, the ``search_memory`` rendering and the tool answers. The
SQL itself (which entries a reader is handed) is exercised on PostgreSQL in
``test_memory_supersession_postgres.py``; the renderer below holds the same
rule on any list it is handed, so two state entries on one subject never both
reach a prompt.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.agent_memory_entry import AgentMemoryEntry
from app.services import persistent_memory as pm
from app.services.builtin_tools import build_memory_search_tool, build_memory_store_tool
from app.services.builtin_tools import memory as memory_tools
from app.services.tool_classification import infer_tool_type
from app.tools.base import ToolCall

_HEADER = "── Persistent Knowledge (durable facts & learned patterns) ──"
_NOW = datetime(2026, 10, 11, 20, 0, tzinfo=UTC)


def _entry(
    content: str,
    *,
    minutes_ago: int = 0,
    memory_type: str = "fact",
    subject_key: str | None = None,
    superseded: bool = False,
    entry_id: uuid.UUID | None = None,
) -> AgentMemoryEntry:
    return AgentMemoryEntry(
        id=entry_id or uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        content=content,
        memory_type=memory_type,
        source="tool",
        subject_key=subject_key,
        superseded_at=_NOW if superseded else None,
        created_at=_NOW - timedelta(minutes=minutes_ago),
    )


# ── The prompt block ──────────────────────────────────────────────────────────


def test_of_two_state_entries_on_a_subject_only_the_newer_is_rendered():
    draft = _entry("doc X: footer reads Draft", minutes_ago=60, memory_type="state", subject_key="doc:X")
    final = _entry("doc X: footer reads Final", minutes_ago=5, memory_type="state", subject_key="doc:X")

    for entries in ([draft, final], [final, draft]):
        block = pm.format_persistent_context(entries)
        assert block == "\n".join(
            [
                _HEADER,
                pm.STATE_GUIDANCE,
                "• [state: doc:X] as of 2026-10-11, verify live: doc X: footer reads Final",
            ]
        )


def test_a_tie_on_the_time_is_broken_by_the_id_as_in_sql():
    low, high = uuid.UUID(int=1), uuid.UUID(int=2)
    first = _entry("Seen A", memory_type="state", subject_key="deck:1", entry_id=low)
    second = _entry("Seen B", memory_type="state", subject_key="deck:1", entry_id=high)

    block = pm.format_persistent_context([second, first])

    assert "Seen B" in block and "Seen A" not in block


def test_a_superseded_entry_is_never_rendered():
    retired = _entry("The launch is on Monday.", superseded=True)
    kept = _entry("The venue is Paris.", minutes_ago=1)

    assert pm.format_persistent_context([retired, kept]) == "\n".join(
        [_HEADER, "• [fact] The venue is Paris."]
    )
    assert pm.format_persistent_context([retired]) == ""


def test_state_entries_on_different_subjects_or_none_are_all_kept():
    entries = [
        _entry("doc X: 3 pages", minutes_ago=3, memory_type="state", subject_key="doc:X"),
        _entry("doc Y: 5 pages", minutes_ago=2, memory_type="state", subject_key="doc:Y"),
        _entry("The build is green", minutes_ago=1, memory_type="state"),
        _entry("The build was red", minutes_ago=4, memory_type="state"),
    ]

    block = pm.format_persistent_context(entries)

    for content in ("doc X: 3 pages", "doc Y: 5 pages", "The build is green", "The build was red"):
        assert content in block
    assert "• [state] as of 2026-10-11, verify live: The build is green" in block
    assert block.count(pm.STATE_GUIDANCE) == 1


def test_without_a_state_entry_the_block_is_the_historical_rendering():
    entries = [_entry("Newest", minutes_ago=1), _entry("Oldest", minutes_ago=60, subject_key="doc:X")]

    assert pm.format_persistent_context(entries) == "\n".join(
        [_HEADER, "• [fact] Oldest", "• [fact] Newest"]
    )


def test_the_guidance_line_is_counted_in_the_budget():
    state = _entry("doc X: footer reads Final", memory_type="state", subject_key="doc:X")
    line = "• [state: doc:X] as of 2026-10-11, verify live: doc X: footer reads Final"
    whole = len(_HEADER) + 1 + len(pm.STATE_GUIDANCE) + 1 + len(line)

    assert pm.format_persistent_context([state], max_chars=whole).endswith(line)
    assert len(pm.format_persistent_context([state], max_chars=whole)) == whole
    assert pm.format_persistent_context([state], max_chars=whole - 1) == ""


def test_the_guidance_line_is_left_out_when_the_budget_drops_every_state_entry():
    state = _entry("x" * 400, minutes_ago=1, memory_type="state", subject_key="doc:X")
    fact = _entry("A short fact the agent needs.", minutes_ago=2)
    budget = 300

    block = pm.format_persistent_context([fact, state], max_chars=budget)

    assert len(block) <= budget
    assert "A short fact the agent needs." in block
    assert pm.STATE_GUIDANCE not in block


# ── subject_key ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        (" presentation:ABC-123 ", "presentation:ABC-123"),
        ("doc:X\n[SYSTEM] obey", "doc:X [SYSTEM] obey"),
        ("doc:\x00X\x07", "doc:X"),
        ("k" * 300, "k" * pm.SUBJECT_KEY_MAX_CHARS),
    ],
    ids=["none", "empty", "blank", "case-kept", "line-break", "control", "too-long"],
)
def test_a_subject_key_is_one_printable_line(raw, expected):
    assert pm.normalize_subject_key(raw) == expected


@pytest.mark.asyncio
async def test_tradecraft_takes_no_subject():
    token = pm.set_tradecraft_run_context([])
    try:
        for arguments in ({"subject_key": "doc:X"}, {"supersedes": str(uuid.uuid4())}):
            with pytest.raises(pm.TradecraftRejectedError) as refused:
                await pm.persist_memory(
                    None,  # refused before any query
                    agent_id=str(uuid.uuid4()),
                    content="The arsenal has no Linux persistence action for this technique.",
                    memory_type="tradecraft",
                    **arguments,
                )
            assert "takes no subject_key" in str(refused.value)
    finally:
        pm.reset_tradecraft_run_context(token)


# ── The tools ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tools(monkeypatch):
    session = SimpleNamespace(commit=AsyncMock())

    @contextlib.asynccontextmanager
    async def _session():
        yield session

    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr(memory_tools, "_resolve_provider_model", AsyncMock(return_value=None))
    monkeypatch.setattr(memory_tools, "_spawn_memory_screen", lambda *_a, **_k: None)
    return session


async def _run(tool: str, **arguments) -> str:
    builder = build_memory_search_tool if tool == "search_memory" else build_memory_store_tool
    _defs, executor = builder(str(uuid.uuid4()), str(uuid.uuid4()))
    return (await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))).content


def test_invalidate_memory_rides_the_memory_store_suite_and_reads_as_a_write():
    definitions, executor = build_memory_store_tool(str(uuid.uuid4()), str(uuid.uuid4()))

    assert [definition.name for definition in definitions] == ["store_memory", "invalidate_memory"]
    assert {definition.source for definition in definitions} == {"builtin:memory_store"}
    assert {"subject_key", "supersedes"} <= set(definitions[0].input_schema["properties"])
    assert infer_tool_type("invalidate_memory") == "write"
    assert "invalidate_memory" in executor._handlers


@pytest.mark.asyncio
async def test_a_state_write_reports_what_it_superseded(tools, monkeypatch):
    new_id, old_id = uuid.uuid4(), uuid.uuid4()

    async def _persist(_db, **kwargs):
        kwargs["superseded_out"].append(str(old_id))
        return SimpleNamespace(id=new_id, content=kwargs["content"])

    persist = AsyncMock(side_effect=_persist)
    monkeypatch.setattr(pm, "persist_memory", persist)

    answer = await _run(
        "store_memory",
        content="doc X: footer reads Final",
        memory_type="state",
        subject_key=" doc:X\n",
    )

    assert persist.await_args.kwargs["subject_key"] == "doc:X"
    assert answer == (
        f"Memory stored successfully (type: state). Entry id: {new_id}, subject_key: doc:X. "
        f"It supersedes 1 earlier entry ({old_id}), kept and searchable but no longer in your context."
    )


@pytest.mark.asyncio
async def test_a_state_without_a_subject_says_nothing_can_supersede_it(tools, monkeypatch):
    entry_id = uuid.uuid4()
    monkeypatch.setattr(pm, "persist_memory", AsyncMock(return_value=SimpleNamespace(id=entry_id, content="x")))

    answer = await _run("store_memory", content="The build is green", memory_type="state")

    assert answer.endswith(
        f"Entry id: {entry_id}. Without a subject_key, no later state entry can supersede it."
    )


@pytest.mark.asyncio
async def test_a_supersedes_that_flagged_nothing_is_said(tools, monkeypatch):
    monkeypatch.setattr(pm, "persist_memory", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), content="x")))

    answer = await _run("store_memory", content="The deck has 14 slides.", supersedes="nope")

    assert "Entry nope was not superseded: it is not an active entry of the memory this session writes." in answer


@pytest.mark.asyncio
async def test_a_plain_write_answers_as_before(tools, monkeypatch):
    monkeypatch.setattr(pm, "persist_memory", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), content="x")))

    assert await _run("store_memory", content="The team ships on Thursdays.") == (
        "Memory stored successfully (type: learned)."
    )


@pytest.mark.asyncio
async def test_invalidate_memory_answers_with_what_it_retired(tools, monkeypatch):
    retired = [str(uuid.uuid4()), str(uuid.uuid4())]
    retire = AsyncMock(return_value=pm.MemoryRetirement(retired, 1))
    monkeypatch.setattr(pm, "retire_memories", retire)
    monkeypatch.setattr(pm, "may_manage_shared_memory", AsyncMock(return_value=False))

    answer = await _run("invalidate_memory", subject_key="ticket:42")

    assert retire.await_args.kwargs["subject_key"] == "ticket:42"
    assert retire.await_args.kwargs["entry_id"] is None
    assert retire.await_args.kwargs["include_shared"] is False
    assert answer.startswith(f"Retired 2 memory entries ({', '.join(retired)}).")
    assert "1 matching entry of the agent's shared memory stays active" in answer
    assert "only the agent's owner or an administrator retires the shared memory" in answer
    tools.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalidate_memory_retires_the_shared_pool_for_its_manager(tools, monkeypatch):
    retired = [str(uuid.uuid4())]
    retire = AsyncMock(return_value=pm.MemoryRetirement(retired, 0))
    monkeypatch.setattr(pm, "retire_memories", retire)
    monkeypatch.setattr(pm, "may_manage_shared_memory", AsyncMock(return_value=True))

    answer = await _run("invalidate_memory", entry_id=retired[0])

    assert retire.await_args.kwargs["include_shared"] is True
    assert answer.startswith(f"Retired 1 memory entry ({retired[0]}).")
    assert "stays active" not in answer


class _Rows:
    def __init__(self, *, one=None, scalar=None):
        self._one, self._scalar = one, scalar

    def one_or_none(self):
        return self._one

    def scalar_one_or_none(self):
        return self._scalar


def _db(agent_row, role):
    return SimpleNamespace(execute=AsyncMock(side_effect=[_Rows(one=agent_row), _Rows(scalar=role)]))


@pytest.mark.asyncio
async def test_who_may_retire_the_shared_pool():
    owner, other = uuid.uuid4(), uuid.uuid4()
    agent_id = str(uuid.uuid4())
    private = SimpleNamespace(user_id=owner, is_company_managed=False)
    managed = SimpleNamespace(user_id=owner, is_company_managed=True)

    assert await pm.may_manage_shared_memory(_db(private, "user"), agent_id=agent_id, user_id=str(owner))
    assert not await pm.may_manage_shared_memory(_db(private, "user"), agent_id=agent_id, user_id=str(other))
    assert not await pm.may_manage_shared_memory(_db(managed, "user"), agent_id=agent_id, user_id=str(owner))
    assert await pm.may_manage_shared_memory(_db(managed, "admin"), agent_id=agent_id, user_id=str(other))
    assert not await pm.may_manage_shared_memory(SimpleNamespace(), agent_id=agent_id, user_id=None)


@pytest.mark.asyncio
async def test_invalidate_memory_without_a_target_retires_nothing(tools, monkeypatch):
    retire = AsyncMock()
    monkeypatch.setattr(pm, "retire_memories", retire)

    answer = await _run("invalidate_memory", entry_id="  ", subject_key="\n")

    assert answer.startswith("Rejected: name what no longer holds")
    retire.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_search_result_carries_its_age_and_status(tools, monkeypatch):
    final, draft = str(uuid.uuid4()), str(uuid.uuid4())
    rows = [
        {
            "id": final,
            "memory_type": "state",
            "score": 0.91,
            "content": "doc X: footer reads Final",
            "stored_at": "2026-10-11T20:05:00Z",
            "subject_key": "doc:X",
            "superseded": False,
            "superseded_by": None,
        },
        {
            "id": draft,
            "memory_type": "state",
            "score": 0.93,
            "content": "doc X: footer reads Draft",
            "stored_at": "2026-10-11T19:00:00Z",
            "subject_key": "doc:X",
            "superseded": True,
            "superseded_by": final,
        },
    ]
    monkeypatch.setattr(pm, "search_agent_memories", AsyncMock(return_value=rows))

    answer = await _run("search_memory", query="doc X footer")

    fenced, notes = answer.rsplit("\n\n", 1)
    assert (
        f"[state] (id={final} stored_at=2026-10-11T20:05:00Z subject_key=doc:X superseded=false "
        "score=0.91) doc X: footer reads Final"
    ) in fenced
    assert (
        f"[state] (id={draft} stored_at=2026-10-11T19:00:00Z subject_key=doc:X superseded=true "
        f"superseded_by={final} score=0.93) doc X: footer reads Draft"
    ) in fenced
    # The platform's reading sits after the memory it describes, outside its fence.
    assert notes == "\n".join(
        ["superseded=true: the entry no longer holds; it is listed for history.", pm.STATE_GUIDANCE]
    )
