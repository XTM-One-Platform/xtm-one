"""Memory import: POST /agents/{id}/memory and ``import_memories``.

Fake-DB style (scripted ``execute`` results, captured adds), no live DB.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1 import agent_memory as api
from app.services import persistent_memory as pm


class _Result:
    def __init__(self, one=None, scalars=None):
        self._one = one
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._one

    def scalars(self):
        items = list(self._scalars)
        return SimpleNamespace(all=lambda: items)


class _DB:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.added = []
        self.statements = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return self.results.pop(0) if self.results else _Result()


def _user(*, role: str = "user"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, is_admin=role == "admin")


def _agent(*, owner_id=None, company_managed=False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        name="Agent",
        is_company_managed=company_managed,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
    )


# ── import_memories ──────────────────────────────────────────────────────────


async def test_import_keeps_dates_and_stores_oldest_first(monkeypatch):
    monkeypatch.setattr(pm, "_lock_subject", AsyncMock())
    supersede = AsyncMock(return_value=[])
    monkeypatch.setattr(pm, "_supersede", supersede)
    db = _DB()
    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = old + timedelta(days=3)
    agent_id = uuid.uuid4()

    created = await pm.import_memories(
        db,
        agent_id=agent_id,
        entries=[
            pm.MemoryImportEntry("doc v2", "state", "doc:1", None, None, new),
            pm.MemoryImportEntry("doc v1", "State", "doc:1", "learning", None, old),
            pm.MemoryImportEntry("a fact"),
        ],
    )

    assert [e.content for e in created] == ["doc v1", "doc v2", "a fact"]
    assert created[0].created_at == old and created[1].created_at == new
    assert created[0].memory_type == "state" and created[0].source == "learning"
    assert created[2].source == pm.MEMORY_IMPORT_SOURCE
    assert all(e.agent_id == agent_id and e.embedding is None for e in created)
    # Each state entry supersedes the earlier one on its subject, in order.
    assert [c.args[1] for c in supersede.await_args_list] == created[:2]


async def test_import_older_state_lands_superseded_by_the_held_one(monkeypatch):
    monkeypatch.setattr(pm, "_lock_subject", AsyncMock())
    supersede = AsyncMock(return_value=[])
    monkeypatch.setattr(pm, "_supersede", supersede)
    held = uuid.uuid4()
    db = _DB([_Result(one=held)])

    (entry,) = await pm.import_memories(
        db,
        agent_id=uuid.uuid4(),
        entries=[
            pm.MemoryImportEntry(
                "old view", "state", "doc:1", created_at=datetime(2025, 5, 1, tzinfo=UTC)
            )
        ],
    )

    assert entry.superseded_by_id == held and entry.superseded_at is not None
    supersede.assert_not_awaited()


async def test_import_naive_and_future_dates_are_clamped():
    db = _DB()
    future = datetime.now(UTC) + timedelta(days=30)
    naive = datetime(2026, 2, 2, 10, 0)
    a, b = await pm.import_memories(
        db,
        agent_id=uuid.uuid4(),
        entries=[
            pm.MemoryImportEntry("x", created_at=naive),
            pm.MemoryImportEntry("y", created_at=future),
        ],
    )
    assert a.created_at == naive.replace(tzinfo=UTC)
    assert b.created_at <= datetime.now(UTC)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (pm.MemoryImportEntry("  "), "content is empty"),
        (pm.MemoryImportEntry("x", "Tradecraft"), "tradecraft"),
        (pm.MemoryImportEntry("x", "t" * 31), "memory_type"),
    ],
)
async def test_import_refuses_bad_entries_before_writing(entry, message):
    db = _DB()
    with pytest.raises(pm.MemoryImportError, match=message) as exc_info:
        await pm.import_memories(
            db, agent_id=uuid.uuid4(), entries=[pm.MemoryImportEntry("ok"), entry]
        )
    assert exc_info.value.index == 1
    assert db.added == []


async def test_import_consolidated_drops_the_subject():
    (entry,) = await pm.import_memories(
        _DB(),
        agent_id=uuid.uuid4(),
        entries=[pm.MemoryImportEntry("summary", "consolidated", "doc:1")],
    )
    assert entry.subject_key is None


# ── request model ────────────────────────────────────────────────────────────


def test_request_takes_200_entries_and_refuses_201():
    api.ImportMemoryRequest(entries=[{"content": "x"}] * 200)
    with pytest.raises(ValidationError):
        api.ImportMemoryRequest(entries=[{"content": "x"}] * 201)
    with pytest.raises(ValidationError):
        api.ImportMemoryRequest(entries=[])


# ── scope rule ───────────────────────────────────────────────────────────────


def test_owner_imports_any_scope():
    user = _user()
    agent = _agent(owner_id=user.id)
    assert api.import_scope_error(agent, user, {uuid.uuid4()}) is None


def test_admin_on_company_managed_imports_any_scope():
    user = _user(role="admin")
    agent = _agent(company_managed=True)
    assert api.import_scope_error(agent, user, {uuid.uuid4()}) is None


def test_non_owner_manager_imports_own_and_agent_level_only():
    user = _user()
    agent = _agent(owner_id=uuid.uuid4())
    assert api.import_scope_error(agent, user, set()) is None
    assert api.import_scope_error(agent, user, {user.id}) is None
    assert api.import_scope_error(agent, user, {uuid.uuid4()}) is not None


# ── route ────────────────────────────────────────────────────────────────────


async def _post(agent, user, body, db, *, manages: bool):
    with (
        patch.object(api, "get_user_agent", new=AsyncMock(return_value=agent)),
        patch.object(api, "get_user_group_ids", new=AsyncMock(return_value=[])),
        patch.object(api, "can_manage", return_value=manages),
        patch.object(api, "log_audit", new=AsyncMock()),
    ):
        return await api.import_agent_memories(
            str(agent.id), body, request=None, user=user, db=db
        )


async def test_route_refuses_non_manager():
    user = _user()
    agent = _agent(owner_id=uuid.uuid4())
    db = _DB()
    with pytest.raises(HTTPException) as exc_info:
        await _post(
            agent, user, api.ImportMemoryRequest(entries=[{"content": "x"}]), db, manages=False
        )
    assert exc_info.value.status_code == 403
    assert db.added == [] and not db.committed


async def test_route_refuses_other_users_scope_for_a_non_owner():
    user = _user()
    agent = _agent(owner_id=uuid.uuid4())
    body = api.ImportMemoryRequest(entries=[{"content": "x", "user_id": str(uuid.uuid4())}])
    with pytest.raises(HTTPException) as exc_info:
        await _post(agent, user, body, _DB(), manages=True)
    assert exc_info.value.status_code == 403


async def test_route_refuses_unknown_user():
    user = _user()
    agent = _agent(owner_id=user.id)
    body = api.ImportMemoryRequest(entries=[{"content": "x", "user_id": str(uuid.uuid4())}])
    db = _DB([_Result(scalars=[])])
    with pytest.raises(HTTPException) as exc_info:
        await _post(agent, user, body, db, manages=True)
    assert exc_info.value.status_code == 400
    assert db.added == []


async def test_route_imports_and_commits():
    user = _user()
    agent = _agent(owner_id=user.id)
    body = api.ImportMemoryRequest(
        entries=[
            {"content": "a", "memory_type": "fact", "created_at": "2026-03-01T10:00:00Z"},
            {"content": "b", "memory_type": "preference", "user_id": str(user.id)},
        ]
    )
    db = _DB([_Result(scalars=[user.id])])
    result = await _post(agent, user, body, db, manages=True)
    assert result["imported"] == 2
    assert result["entries"][0]["created_at"].startswith("2026-03-01T10:00:00")
    assert result["entries"][1]["user_id"] == str(user.id)
    assert db.committed


# ── native bundle ────────────────────────────────────────────────────────────────


async def test_export_memories_lists_active_entries_in_import_shape():
    uid = uuid.uuid4()
    row = SimpleNamespace(
        content="c",
        memory_type="state",
        subject_key="doc:1",
        source="chat",
        user_id=uid,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    db = _DB([_Result(scalars=[row])])
    (exported,) = await pm.export_memories(db, agent_id=uuid.uuid4())
    assert exported == {
        "content": "c",
        "memory_type": "state",
        "subject_key": "doc:1",
        "source": "chat",
        "user_id": str(uid),
        "created_at": "2026-04-01T00:00:00+00:00",
    }
    sql = str(db.statements[0])
    assert "superseded_at IS NULL" in sql and "memory_type !=" in sql
    # What the export writes, the import reads back.
    api.ImportMemoryEntry.model_validate(exported)


async def test_bundle_without_memories_writes_nothing():
    from app.api.v1.agent_import import _import_bundle_memories

    db = _DB()
    warnings: list[str] = []
    for entries in (None, [], "x"):
        assert (
            await _import_bundle_memories(
                db, agent_id=uuid.uuid4(), entries=entries, warnings=warnings
            )
            == 0
        )
    assert db.added == [] and db.statements == [] and warnings == []


async def test_bundle_memories_skip_unreadable_and_unknown_users_with_warnings():
    from app.api.v1.agent_import import _import_bundle_memories

    known, missing = uuid.uuid4(), uuid.uuid4()
    db = _DB([_Result(scalars=[known])])
    warnings: list[str] = []
    count = await _import_bundle_memories(
        db,
        agent_id=uuid.uuid4(),
        entries=[
            {"content": "agent level", "memory_type": "fact"},
            {"content": "mine", "user_id": str(known)},
            {"content": "theirs", "user_id": str(missing)},
            {"content": ""},
            {"content": "x", "memory_type": "tradecraft"},
            "not an object",
        ],
        warnings=warnings,
    )
    assert count == 2
    assert sorted(e.content for e in db.added) == ["agent level", "mine"]
    assert len(warnings) == 2
    assert warnings[0].startswith("1 memory entries")
    assert warnings[1].startswith("3 memory entries")


def test_native_bundle_validation_refuses_non_list_memories():
    from app.services.agentschema.detect import AgentSchemaError, validate_native_bundle

    bundle = {"version": 3, "export_type": "agent", "agent": {"name": "A"}}
    validate_native_bundle({**bundle, "memories": []})
    with pytest.raises(AgentSchemaError):
        validate_native_bundle({**bundle, "memories": {"a": 1}})


# ── agent-builder tool ───────────────────────────────────────────────────────────


def _tool(user_id: str):
    from app.services.builtin_tools.agent_builder import build_agent_builder_tool

    definitions, executor = build_agent_builder_tool(SimpleNamespace(), user_id)
    return {d.name: d for d in definitions}, executor


async def _run(executor, **arguments) -> str:
    from app.tools.base import ToolCall

    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="import_agent_memory", arguments=arguments)
    )
    return result.content


def test_tool_is_defined_and_registered():
    definitions, executor = _tool(str(uuid.uuid4()))
    assert "import_agent_memory" in definitions
    assert "import_agent_memory" in executor.tool_names
    schema = definitions["import_agent_memory"].input_schema
    assert schema["properties"]["entries"]["maxItems"] == pm.MEMORY_IMPORT_MAX_ENTRIES


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([], "non-empty list"),
        ("not json", "JSON list"),
        ([{"content": "x"}] * 201, "At most 200"),
        ([{"memory_type": "fact"}], "Invalid memory entries"),
    ],
)
async def test_tool_refuses_bad_input_before_any_db_access(entries, message):
    _, executor = _tool(str(uuid.uuid4()))
    out = await _run(executor, agent_id=str(uuid.uuid4()), entries=entries)
    assert message in out


async def test_tool_refused_for_anonymous():
    _, executor = _tool("anonymous")
    out = await _run(executor, agent_id=str(uuid.uuid4()), entries=[{"content": "x"}])
    assert "anonymous" in out


async def test_route_maps_a_refused_entry_to_400():
    user = _user()
    agent = _agent(owner_id=user.id)
    body = api.ImportMemoryRequest(entries=[{"content": "x", "memory_type": "tradecraft"}])
    with pytest.raises(HTTPException) as exc_info:
        await _post(agent, user, body, _DB(), manages=True)
    assert exc_info.value.status_code == 400
    assert "entries[0]" in exc_info.value.detail
