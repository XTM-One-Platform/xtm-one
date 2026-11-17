"""Content-locked knowledge bases leave every agent's write scope.

A read-only KB was already stripped from an agent's write scope before the
write tools were built; a KB installed by a skill package or a built-in
KB was not, so an agent granted write on one was OFFERED ``store_knowledge`` /
``update_knowledge_document`` and then refused at execution. One rule,
``helpers.knowledge_base_content_lock``, now drives the write gate, the chat
and assignment write scopes, the XTM MCP server's scope and the
``content_lock`` the API serves to the agent Knowledge section.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import helpers as api_helpers
from app.api.v1 import mcp_xtm
from app.services import agent_chat, assignment_engine
from app.services.builtin_tools import kb as kb_tools


def _kb(*, read_only=False, builtin=False, owner=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="KB",
        is_read_only=read_only,
        is_builtin=builtin,
        is_company_managed=False,
        user_id=owner,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
    )


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _Db:
    """Answers KB row reads and origin lookups; counts the queries."""

    def __init__(self, kbs=(), *, package_ids=()):
        self.kbs = list(kbs)
        self.package_ids = [uuid.UUID(str(i)) for i in package_ids]
        self.queries: list[str] = []

    async def execute(self, statement):
        sql = str(statement)
        self.queries.append(sql)
        if "agent_skill_package_origin" in sql:
            return _Result(self.package_ids)
        return _Result(self.kbs)


@pytest.mark.parametrize(
    ("kwargs", "package", "lock"),
    [
        ({}, False, None),
        ({"read_only": True}, False, "read_only"),
        ({"builtin": True}, False, "builtin"),
        ({}, True, "package_installed"),
        ({"read_only": True}, True, "package_installed"),
    ],
)
def test_the_content_lock_rule(kwargs, package, lock):
    assert (
        api_helpers.knowledge_base_content_lock(_kb(**kwargs), package_installed=package)
        == lock
    )


@pytest.mark.asyncio
async def test_the_batched_lookup_costs_one_query_for_any_number_of_kbs():
    plain, read_only, builtin, packaged = (
        _kb(),
        _kb(read_only=True),
        _kb(builtin=True),
        _kb(),
    )
    db = _Db(package_ids=[packaged.id])

    locks = await api_helpers.content_locked_knowledge_base_ids(
        db, [plain, read_only, builtin, packaged]
    )

    assert locks == {
        str(read_only.id): "read_only",
        str(builtin.id): "builtin",
        str(packaged.id): "package_installed",
    }
    assert len(db.queries) == 1


@pytest.mark.asyncio
async def test_no_kbs_means_no_query():
    db = _Db()
    assert await api_helpers.content_locked_knowledge_base_ids(db, []) == {}
    assert db.queries == []


@pytest.mark.asyncio
async def test_assignment_scope_loader_strips_every_lock_in_two_queries():
    plain, read_only, packaged = _kb(), _kb(read_only=True), _kb()
    db = _Db([plain, read_only, packaged], package_ids=[packaged.id])

    locked = await kb_tools.load_write_locked_kb_ids(
        db, [str(plain.id), str(read_only.id), str(packaged.id)]
    )

    assert locked == {str(read_only.id), str(packaged.id)}
    assert len(db.queries) == 2


@pytest.mark.asyncio
async def test_assignment_scope_loader_short_circuits_without_ids():
    db = _Db()
    assert await kb_tools.load_write_locked_kb_ids(db, []) == set()
    assert await kb_tools.load_write_locked_kb_ids(db, ["not-a-uuid"]) == set()
    assert db.queries == []


@pytest.mark.asyncio
async def test_the_xtm_mcp_server_never_offers_a_package_installed_kb_for_writing():
    owner = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")
    mine, packaged = _kb(owner=owner.id), _kb(owner=owner.id)
    db = _Db([mine, packaged], package_ids=[packaged.id])

    readable, writable = await mcp_xtm._resolve_knowledge_bases(db, owner, [])

    assert len(readable) == 2
    assert [kb_id for kb_id, _ in writable] == [str(mine.id)]


def test_chat_and_assignment_scopes_read_the_shared_rule():
    """Tripwire: the write scopes must ask the shared lookup, never re-spell a
    lock check (the read-only-only filter is how package KBs slipped through)."""
    chat_src = inspect.getsource(agent_chat)
    assert "content_locked_knowledge_base_ids" in chat_src
    assert "not kb_map[k].is_read_only" not in chat_src
    assert "load_write_locked_kb_ids" in inspect.getsource(assignment_engine)
