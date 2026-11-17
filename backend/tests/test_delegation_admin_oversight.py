"""An admin reads every delegation of an agent, as an audited oversight read.

The agent page's Delegations tab reads ``GET /chat/delegations?agent_id=...``
and ``GET /chat/delegations/{id}``. An admin opening a colleague's agent sees
its delegations whoever started them, but the rows carry conversation
content, so a read serving rows of OTHER users leaves the trail the
runner command history read does: one ``break_glass`` entry per admin, per
agent, per window, committed before the rows are returned.

These tests drive the real endpoints with a scripted session and a fake Redis.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import chat as chat_api
from app.api.v1.runners import RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS
from app.models.agent_delegation import AgentDelegation
from app.models.audit_log import AuditLog

AGENT_NAME = "Support Relay"
OWNER_EMAIL = "owner@example.com"


def _user(*, admin: bool) -> SimpleNamespace:
    uid = uuid.uuid4()
    return SimpleNamespace(
        id=uid,
        role="admin" if admin else "user",
        is_admin=admin,
        email=f"{'admin' if admin else 'member'}-{uid.hex[:6]}@example.com",
    )


def _delegation(owner: uuid.UUID | None, parent: uuid.UUID, target: uuid.UUID) -> AgentDelegation:
    return AgentDelegation(
        id=uuid.uuid4(),
        user_id=owner,
        parent_agent_id=parent,
        target_agent_id=target,
        type="consult",
        description="Summarise the customer escalation",
        status="completed",
        result="The customer asked for a refund.",
        metadata_json={},
        created_at=datetime.now(UTC),
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    """Answers each statement by the table it reads; records adds and commits.

    The delegation read honours the ``user_id`` predicate so a scoped read
    really returns only the caller's rows."""

    def __init__(self, rows: list[AgentDelegation]):
        self.rows = rows
        self.statements: list[str] = []
        self.added: list = []
        self.commits_at: list[int] = []

    async def execute(self, statement, *a, **k):
        sql = re.sub(r"\s+", " ", str(statement))
        self.statements.append(sql)
        if "FROM agent_delegations" in sql:
            params = statement.compile().params
            owner = next((v for k_, v in params.items() if k_.startswith("user_id")), None)
            wanted = params.get("id_1")
            rows = [
                r for r in self.rows if (owner is None or r.user_id == owner) and (wanted is None or r.id == wanted)
            ]
            return _Result(rows)
        if "agents.name" in sql:
            return _Result([AGENT_NAME])
        if "users.email" in sql:
            return _Result([OWNER_EMAIL])
        raise AssertionError(f"unexpected statement: {sql}")

    def add(self, entry):
        self.added.append(entry)

    async def flush(self):
        return None

    async def commit(self):
        self.commits_at.append(len(self.statements))

    @property
    def delegation_where(self) -> str:
        sql = next(s for s in self.statements if "FROM agent_delegations" in s)
        return sql.split(" WHERE ", 1)[1] if " WHERE " in sql else ""

    @property
    def audits(self) -> list[AuditLog]:
        return [e for e in self.added if isinstance(e, AuditLog)]


class _Redis:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.keys: dict[str, dict] = {}
        self.exists_calls: list[str] = []
        self.set_calls: list[tuple[str, dict]] = []

    async def exists(self, key):
        if self.error is not None:
            raise self.error
        self.exists_calls.append(key)
        return 1 if key in self.keys else 0

    async def set(self, key, value, **options):
        if self.error is not None:
            raise self.error
        self.set_calls.append((key, options))
        self.keys[key] = options
        return True


@pytest.fixture
def redis(monkeypatch) -> _Redis:
    fake = _Redis()

    async def _client():
        return fake

    monkeypatch.setattr(chat_api, "redis_client", _client)
    return fake


_REQUEST = SimpleNamespace(headers={}, client=None)


async def _list(user, session, **filters):
    params = {
        "agent_id": None,
        "parent_agent_id": None,
        "target_agent_id": None,
        "delegation_type": None,
        "delegation_status": None,
        "conversation_id": None,
        "limit": 100,
    }
    params.update(filters)
    return await chat_api.list_delegations(_REQUEST, user=user, db=session, **params)


async def _get(user, session, delegation_id):
    return await chat_api.get_delegation(str(delegation_id), _REQUEST, user=user, db=session)


# ── The list ────────────────────────────────────────────────────────────────


async def test_an_admin_reads_every_delegation_of_an_agent_audited_once_per_window(redis):
    admin = _user(admin=True)
    member = _user(admin=False)
    agent, peer = uuid.uuid4(), uuid.uuid4()
    session = _Session([_delegation(member.id, agent, peer), _delegation(admin.id, peer, agent)])

    rows = await _list(admin, session, agent_id=str(agent))

    # Every user's rows, not only the admin's own.
    assert {r["user_id"] for r in rows} == {str(member.id), str(admin.id)}
    assert "agent_delegations.user_id" not in session.delegation_where
    # One entry naming the admin, the agent and the owners concerned - never
    # the admin themself.
    (entry,) = session.audits
    assert entry.action == chat_api.DELEGATION_OVERSIGHT_ACTION == "break_glass"
    assert entry.entity_type == chat_api.DELEGATION_OVERSIGHT_ENTITY_TYPE == "agent_delegations"
    assert entry.entity_id == agent and entry.entity_name == AGENT_NAME
    assert admin.email in entry.summary and OWNER_EMAIL in entry.summary
    assert entry.details["delegation_owner_ids"] == [str(member.id)]
    assert entry.details["window_seconds"] == RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS
    # Committed before the rows are served, then the window is marked.
    assert len(session.commits_at) == 1
    key = chat_api.delegation_oversight_key(admin.id, agent)
    assert redis.set_calls == [(key, {"ex": RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS})]

    # The tab refetches: the second read inside the window writes nothing more.
    await _list(admin, session, agent_id=str(agent))
    assert len(session.audits) == 1 and len(session.commits_at) == 1
    assert redis.exists_calls == [key, key]


@pytest.mark.parametrize("filter_name", ["parent_agent_id", "target_agent_id"])
async def test_the_parent_and_target_filters_take_the_same_audited_reach(redis, filter_name):
    admin = _user(admin=True)
    agent = uuid.uuid4()
    session = _Session([_delegation(uuid.uuid4(), agent, agent)])

    rows = await _list(admin, session, **{filter_name: str(agent)})

    assert len(rows) == 1
    (entry,) = session.audits
    assert entry.entity_id == agent
    assert redis.set_calls[0][0] == chat_api.delegation_oversight_key(admin.id, agent)


async def test_an_admin_reading_only_their_own_delegations_leaves_no_trail(redis):
    admin = _user(admin=True)
    agent = uuid.uuid4()
    session = _Session([_delegation(admin.id, agent, uuid.uuid4())])

    rows = await _list(admin, session, agent_id=str(agent))

    assert len(rows) == 1
    assert session.audits == [] and session.commits_at == []
    assert redis.exists_calls == [] and redis.set_calls == []


async def test_an_unfiltered_admin_list_stays_the_admins_own(redis):
    admin = _user(admin=True)
    session = _Session(
        [
            _delegation(admin.id, uuid.uuid4(), uuid.uuid4()),
            _delegation(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()),
        ]
    )

    rows = await _list(admin, session)

    assert [r["user_id"] for r in rows] == [str(admin.id)]
    assert "agent_delegations.user_id" in session.delegation_where
    assert session.audits == [] and redis.exists_calls == []


async def test_a_member_reads_only_their_own_delegations_of_an_agent(redis):
    member = _user(admin=False)
    agent = uuid.uuid4()
    session = _Session([_delegation(member.id, agent, uuid.uuid4()), _delegation(uuid.uuid4(), agent, uuid.uuid4())])

    rows = await _list(member, session, agent_id=str(agent))

    assert [r["user_id"] for r in rows] == [str(member.id)]
    assert "agent_delegations.user_id" in session.delegation_where
    assert session.audits == [] and redis.exists_calls == [] and redis.set_calls == []


async def test_a_failed_audit_write_serves_nothing_and_marks_nothing(redis):
    admin = _user(admin=True)
    agent = uuid.uuid4()
    session = _Session([_delegation(uuid.uuid4(), agent, uuid.uuid4())])

    async def failing_commit():
        raise RuntimeError("database gone")

    session.commit = failing_commit  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await _list(admin, session, agent_id=str(agent))
    assert redis.set_calls == [], "no marker without a committed row"


async def test_a_redis_outage_audits_anyway(monkeypatch):
    fake = _Redis(error=ConnectionError("redis down"))

    async def _client():
        return fake

    monkeypatch.setattr(chat_api, "redis_client", _client)
    admin = _user(admin=True)
    agent = uuid.uuid4()
    session = _Session([_delegation(uuid.uuid4(), agent, uuid.uuid4())])

    await _list(admin, session, agent_id=str(agent))
    await _list(admin, session, agent_id=str(agent))

    assert len(session.audits) == 2 and len(session.commits_at) == 2


# ── The detail ──────────────────────────────────────────────────────────────


async def test_an_admin_reads_a_colleagues_delegation_audited_on_the_delegating_agent(redis):
    admin = _user(admin=True)
    parent, target = uuid.uuid4(), uuid.uuid4()
    row = _delegation(uuid.uuid4(), parent, target)
    session = _Session([row])

    result = await _get(admin, session, row.id)

    assert result["id"] == str(row.id)
    (entry,) = session.audits
    assert entry.entity_id == parent and entry.entity_type == "agent_delegations"
    assert len(session.commits_at) == 1
    assert redis.set_calls[0][0] == chat_api.delegation_oversight_key(admin.id, parent)

    # Same window, same agent: no second entry.
    await _get(admin, session, row.id)
    assert len(session.audits) == 1


async def test_the_detail_of_the_admins_own_delegation_leaves_no_trail(redis):
    admin = _user(admin=True)
    row = _delegation(admin.id, uuid.uuid4(), uuid.uuid4())
    session = _Session([row])

    await _get(admin, session, row.id)

    assert session.audits == [] and redis.exists_calls == []


async def test_a_member_cannot_read_a_colleagues_delegation(redis):
    member = _user(admin=False)
    row = _delegation(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    session = _Session([row])

    with pytest.raises(HTTPException) as excinfo:
        await _get(member, session, row.id)

    assert excinfo.value.status_code == 404
    assert session.audits == [] and redis.exists_calls == []
