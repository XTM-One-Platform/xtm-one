"""Account writes commit before they answer.

``get_db`` commits on dependency teardown, which FastAPI runs AFTER the
response is sent. A caller that acts on an account write with its very next
request - signing in as the user it just created, re-creating the address it
just deleted, signing in with the password it just set, presenting the token
``/register`` just handed back, acting under a role it was just given - can
otherwise arrive before the transaction lands. The A2A e2e suite did exactly
that: its login ran 100 ms before the ``POST /users`` commit and was refused
with 401 "Invalid credentials".

Each route that changes whether an account exists, how it signs in, what it
may do or which credentials act for it is driven through a session stub that
records its calls in order. The commit has to be the last of them - the
response is built after it, and the write and its audit row belong to the
transaction it lands - unless what follows is a side effect that must not run
before the write is durable (a cache drop, a Redis cleanup), which then comes
right after it: in the route itself, or through the route's ``AfterCommit``,
which runs once the route has returned and before the response is sent. The
two-factor routes, the API keys and the global MCP key are pinned the same
way, row lock included, in
``test_mfa_and_credential_writes_commit_before_response.py``.

The tripwires at the bottom keep it that way: a new write route on the account
routers, or a route anywhere under ``app/api`` that mints or replaces a
credential, commits before it answers or states why it does not need to.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import textwrap
import uuid
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute

from app.api.deps import AfterCommit, run_after_commit
from app.api.v1 import api_keys as api_keys_module
from app.api.v1 import auth as auth_module
from app.api.v1 import groups as groups_module
from app.api.v1 import invitations as invitations_module
from app.api.v1 import platform as platform_module
from app.api.v1 import settings as settings_module
from app.api.v1 import users as users_module
from tests.memory_redis_double import MemoryRedis, summarizer, use_memory_redis

_PASSWORD = "Sup3rSecret"


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value or []


def _apply_insert_defaults(obj) -> None:
    """What the INSERT fills in: the id and every Python-side column default."""
    table = getattr(obj, "__table__", None)
    if table is None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        return
    for column in table.columns:
        if column.default is None or getattr(obj, column.key, None) is not None:
            continue
        default = column.default
        if default.is_callable:
            # ``arg`` takes the execution context, as the INSERT calls it:
            # SQLAlchemy wraps a zero-argument default (``uuid.uuid4``) that way.
            setattr(obj, column.key, default.arg(None))
        elif default.is_scalar:
            setattr(obj, column.key, default.arg)


class _RecordingSession:
    """Just enough ``AsyncSession`` for the account routes, in call order."""

    def __init__(self, *results):
        self.calls: list[str] = []
        self._results = list(results)
        self._pending: list[object] = []

    def _next(self):
        return self._results.pop(0) if self._results else None

    async def execute(self, _stmt):
        self.calls.append("execute")
        return _Result(self._next())

    async def get(self, _model, _pk):
        self.calls.append("get")
        return self._next()

    async def scalar(self, _stmt):
        self.calls.append("scalar")
        return 1

    def add(self, obj):
        self.calls.append("add")
        self._pending.append(obj)

    async def flush(self):
        self.calls.append("flush")
        # The id and defaults the INSERT assigns, read by the audit row and the
        # response.
        for obj in self._pending:
            _apply_insert_defaults(obj)
        self._pending.clear()

    async def delete(self, _obj):
        self.calls.append("delete")

    async def commit(self):
        self.calls.append("commit")

    async def rollback(self):
        self.calls.append("rollback")


async def _record_audit(db, **_kw) -> None:
    db.calls.append("audit")


async def _fast_hash(password: str) -> str:
    return f"hashed:{password}"


async def _noop(*_a, **_kw) -> None:
    return None


def _recorder(session: _RecordingSession, label: str):
    async def _record(*_a, **_kw) -> None:
        session.calls.append(label)

    return _record


def _assert_committed_last(session: _RecordingSession, *writes: str) -> None:
    calls = session.calls
    assert calls.count("commit") == 1, f"expected exactly one commit: {calls}"
    assert calls[-1] == "commit", (
        "the commit must be the route's last session call, so the response "
        f"only ever describes committed state: {calls}"
    )
    for write in writes:
        assert write in calls, f"expected {write!r} inside the transaction: {calls}"


def _assert_committed_then(
    session: _RecordingSession, *after: str, before: tuple[str, ...] = ()
) -> None:
    """The commit lands the writes, and only *after* follows it, in order."""
    calls = session.calls
    assert calls.count("commit") == 1, f"expected exactly one commit: {calls}"
    at = calls.index("commit")
    for write in before:
        assert write in calls[:at], f"expected {write!r} before the commit: {calls}"
    assert calls[at + 1 :] == list(after), (
        f"only {list(after)} may follow the commit, in that order: {calls}"
    )


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})


def _group(**overrides):
    fields = {
        "id": uuid.uuid4(),
        "name": "Analysts",
        "description": None,
        "created_by": None,
        "quota_config": None,
        "kind": "auto",
        "auto_join": False,
        "created_at": None,
        "updated_at": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def admin():
    return SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)


@pytest.fixture
def target():
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="target@example.com",
        full_name="Target User",
        role="user",
        is_active=True,
        sso_provider=None,
        hashed_password="old-hash",
        password_changed_at=None,
    )


# -- users.py (admin) ---------------------------------------------------------


async def test_create_user_commits_before_responding(monkeypatch, admin):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    monkeypatch.setattr(users_module, "hash_password_async", _fast_hash)
    session = _RecordingSession()

    response = await users_module.create_user(
        body=users_module.CreateUserRequest(
            email="New.User@Example.com",
            full_name="New User",
            password=_PASSWORD,
            role="user",
            account_type="local",
        ),
        request=_request(),
        admin=admin,
        db=session,
    )

    _assert_committed_last(session, "add", "flush", "audit")
    assert response.email == "new.user@example.com"


def _record_membership_drops(monkeypatch, session: _RecordingSession) -> list[list]:
    """Record ``invalidate_membership_quota_cache`` in call order, with its ids."""
    import app.services.quota as quota_module

    dropped: list[list] = []

    async def _drop(user_ids):
        session.calls.append("invalidate_membership_quota_cache")
        dropped.append(list(user_ids))

    monkeypatch.setattr(quota_module, "invalidate_membership_quota_cache", _drop)
    return dropped


async def test_delete_user_commits_before_responding(monkeypatch, admin, target):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    monkeypatch.setattr(users_module, "reassign_company_managed_entities", _noop)
    monkeypatch.setattr(users_module, "prune_user_grants", _noop)
    monkeypatch.setattr(users_module, "_clear_deleted_user_memory", _noop)
    # The user lookup, the lock on their A2A tasks, then the (empty) list of
    # their agents.
    session = _RecordingSession(target, None, [])
    dropped = _record_membership_drops(monkeypatch, session)

    await users_module.delete_user(
        user_id=str(target.id),
        request=_request(),
        admin=admin,
        db=session,
    )

    # The cascade removed the account's group memberships: its cached quota
    # resolution goes once that has committed.
    _assert_committed_then(
        session, "invalidate_membership_quota_cache", before=("audit", "delete")
    )
    assert dropped == [[target.id]]


@pytest.mark.parametrize("private_agents", [1, 0])
async def test_delete_user_clears_its_memory_only_once_the_account_is_gone(
    monkeypatch, admin, target, private_agents
):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    monkeypatch.setattr(users_module, "reassign_company_managed_entities", _noop)
    monkeypatch.setattr(users_module, "prune_user_grants", _noop)
    agent_ids = [uuid.uuid4() for _ in range(private_agents)]
    session = _RecordingSession(target, None, [(agent_id,) for agent_id in agent_ids])
    _record_membership_drops(monkeypatch, session)
    cleared: list[tuple[str, list[str]]] = []

    async def _clear(user_id, agents, *, db=None):
        session.calls.append("clear_memory")
        cleared.append((user_id, agents))
        # The purge re-reads the account on this session.
        assert db is session

    monkeypatch.setattr(users_module, "_clear_deleted_user_memory", _clear)

    await users_module.delete_user(
        user_id=str(target.id),
        request=_request(),
        admin=admin,
        db=session,
    )

    # The Redis deletes cannot be rolled back: a failed commit must not leave
    # the account in place with its memory already gone. An account with no
    # private agent still has a profile and lanes with the agents it used.
    _assert_committed_then(
        session,
        "invalidate_membership_quota_cache",
        "clear_memory",
        before=("audit", "delete"),
    )
    assert cleared == [(str(target.id), [str(agent_id) for agent_id in agent_ids])]


@pytest.mark.parametrize("owned_an_agent", [True, False])
async def test_deleted_user_memory_clears_every_key_the_account_left(
    monkeypatch, owned_an_agent
):
    from app.services import agent_memory, user_memory

    uid, other = str(uuid.uuid4()), str(uuid.uuid4())
    private, surviving = str(uuid.uuid4()), str(uuid.uuid4())
    # Spelled with the writers' own key builders, so the cleanup is checked
    # against the keys that are really there.
    private_agent = {
        # A private agent the cascade deleted: its own memory, every lane.
        agent_memory._short_term_key(private),
        agent_memory._medium_term_key(private),
        agent_memory._counter_key(private),
        agent_memory._medium_term_key(private, scope_id=other),
        f"chat:active:{private}",
    }
    account = {
        # The user's lane with an agent that survives, and their profile.
        agent_memory._short_term_key(surviving, scope_id=uid),
        agent_memory._medium_term_key(surviving, scope_id=uid),
        user_memory._short_term_key(uid),
        user_memory._recent_key(uid),
        user_memory._medium_term_key(uid),
        user_memory._entries_key(uid),
        user_memory._counter_key(uid),
    }
    kept = {
        agent_memory._medium_term_key(surviving),
        agent_memory._medium_term_key(surviving, scope_id=other),
        user_memory._medium_term_key(other),
    }
    redis = MemoryRedis(private_agent | account | kept)
    use_memory_redis(monkeypatch, redis)

    await users_module._clear_deleted_user_memory(
        uid, [private] if owned_an_agent else []
    )

    # An account that owned no agent still leaves a profile and lanes behind;
    # an agent it did not own keeps its memory. The purge generation is moved,
    # not deleted: it holds a counter, no memory.
    generation = user_memory._generation_key(uid)
    assert redis.data[generation] == "1"
    assert redis.memory() - {generation} == (kept if owned_an_agent else kept | private_agent)
    assert redis.tombstones() == {
        f"memory_tombstone:account:{uid}",
        *([f"memory_tombstone:agent:{private}"] if owned_an_agent else []),
    }


async def test_every_key_the_memory_writers_produce_is_purged(monkeypatch):
    """The writers and the purge share their key builders: a writer that
    starts using a key the purge does not know leaves it behind here."""
    from app.services import agent_memory, user_memory

    uid, other = str(uuid.uuid4()), str(uuid.uuid4())
    private, surviving = str(uuid.uuid4()), str(uuid.uuid4())
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    llm = summarizer()
    # (agent, scope) lanes the deleted account leaves: its private agent's own
    # and another user's lane on it, and its own lane with a surviving agent.
    lanes = [(private, None), (private, other), (surviving, uid)]

    for agent_id, scope_id in lanes:
        await agent_memory.store_short_term_memory(
            agent_id, source="chat", summary="did a thing", scope_id=scope_id
        )
        await agent_memory.consolidate_memory(
            agent_id, llm, force=True, scope_id=scope_id, scope_is_user_id=False
        )
    await redis.set(
        agent_memory._medium_term_key(private),
        "x" * (agent_memory._MAX_MEDIUM_TERM_PROMPT_CHARS + 1),
    )
    assert await agent_memory.compress_medium_term_memory(private, llm)
    await user_memory.store_user_memory(uid, agent_name="Analyst", summary="asked")
    await user_memory.consolidate_user_memory(
        uid, summarizer(json.dumps({"profile": ["durable summary"], "notes": []})), force=True
    )

    families = set(user_memory.profile_keys(uid)).union(
        *(agent_memory.lane_keys(agent_id, scope_id) for agent_id, scope_id in lanes)
    )
    # A lease is released by its holder and expires on its own: no memory.
    assert not any(key.startswith("lease:") for key in redis.memory())
    written = {key for key in redis.written if not key.startswith("lease:")}
    assert written <= families, written - families
    assert redis.written >= {
        agent_memory._medium_term_key(surviving, scope_id=uid),
        user_memory._medium_term_key(uid),
    }

    await users_module._clear_deleted_user_memory(uid, [private])

    # The deletion's own purge generation stays (a counter, no memory).
    assert not redis.written & redis.memory() - {user_memory._generation_key(uid)}


async def test_a_consolidation_in_flight_cannot_recreate_a_deleted_account(
    monkeypatch,
):
    """Consolidations are detached from the chat turn and write after their
    LLM call: one waiting while the account is deleted must not bring its
    memory back once it answers."""
    import asyncio

    from app.services import agent_memory, user_memory

    uid, surviving, other = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    for identity in (uid, other):
        await user_memory.store_user_memory(identity, agent_name="A", summary="s")
        await agent_memory.store_short_term_memory(
            surviving, source="chat", summary="s", scope_id=identity
        )

    answered = asyncio.Event()
    waiting: list[str] = []

    async def _slow_chat(**_kwargs):
        waiting.append("llm")
        await answered.wait()
        return SimpleNamespace(
            content=json.dumps({"profile": ["recreated summary"], "notes": []}), stop_reason="end_turn"
        )

    llm = SimpleNamespace(chat=_slow_chat)
    in_flight = [
        asyncio.create_task(user_memory.consolidate_user_memory(uid, llm, force=True)),
        asyncio.create_task(
            agent_memory.consolidate_memory(
                surviving, llm, force=True, scope_id=uid, scope_is_user_id=False
            )
        ),
    ]
    while len(waiting) < len(in_flight):
        await asyncio.sleep(0)

    await users_module._clear_deleted_user_memory(uid, [])
    answered.set()
    await asyncio.gather(*in_flight)

    account_keys = set(user_memory.profile_keys(uid)) | set(
        agent_memory.lane_keys(surviving, uid)
    )
    assert not account_keys & redis.memory()
    # A late turn for the deleted account writes nothing either, while other
    # users keep writing as before.
    assert await user_memory.store_user_memory(uid, agent_name="A", summary="s") == 0
    assert not account_keys & redis.memory()
    assert await user_memory.store_user_memory(other, agent_name="A", summary="s") == 2
    assert {
        agent_memory._short_term_key(surviving, other),
        agent_memory._counter_key(surviving, other),
    } <= redis.memory()


async def test_deleted_user_memory_cleanup_never_fails_the_deletion(monkeypatch):
    import app.core.redis as redis_module
    from app.services import agent_memory, user_memory

    async def _unreachable():
        raise ConnectionError("redis is down")

    for module in (redis_module, agent_memory, user_memory):
        monkeypatch.setattr(module, "redis_client", _unreachable)

    # It runs once the deletion committed: an outage is logged, never answered
    # with a 500 for an account that is already gone.
    await users_module._clear_deleted_user_memory(str(uuid.uuid4()), [])


async def test_set_user_password_commits_before_responding(
    monkeypatch, admin, target
):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    monkeypatch.setattr(users_module, "hash_password_async", _fast_hash)
    session = _RecordingSession(target)

    await users_module.set_user_password(
        user_id=str(target.id),
        body=users_module.SetPasswordRequest(new_password=_PASSWORD),
        request=_request(),
        admin=admin,
        db=session,
    )

    _assert_committed_last(session, "flush", "audit")
    assert target.hashed_password == f"hashed:{_PASSWORD}"


async def test_update_user_role_commits_before_responding(
    monkeypatch, admin, target
):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    session = _RecordingSession(target)

    response = await users_module.update_user_role(
        user_id=str(target.id),
        body=users_module.UpdateRoleRequest(role="admin"),
        request=_request(),
        admin=admin,
        db=session,
    )

    _assert_committed_last(session, "flush", "audit")
    assert response["role"] == "admin"
    assert target.role == "admin"


async def test_reset_user_mfa_commits_before_responding(monkeypatch, admin, target):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    target.mfa_enabled = True
    target.mfa_secret = "encrypted-secret"
    session = _RecordingSession(target)

    await users_module.reset_user_mfa(
        user_id=target.id,
        request=_request(),
        admin=admin,
        db=session,
    )

    _assert_committed_last(session, "flush", "audit")
    assert target.mfa_enabled is False
    assert target.mfa_secret is None


async def test_delete_user_integration_commits_before_responding(
    monkeypatch, admin, target
):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    integration = SimpleNamespace(id=uuid.uuid4(), name="Slack", provider="slack")
    session = _RecordingSession(integration)

    await users_module.delete_user_integration(
        user_id=str(target.id),
        integration_id=str(integration.id),
        request=_request(),
        admin=admin,
        db=session,
    )

    _assert_committed_last(session, "audit", "delete")


async def test_delete_user_mcp_server_commits_before_responding(
    monkeypatch, admin, target
):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    server = SimpleNamespace(id=uuid.uuid4(), name="GitHub MCP")
    session = _RecordingSession(server)

    await users_module.delete_user_mcp_server(
        user_id=str(target.id),
        server_id=str(server.id),
        request=_request(),
        admin=admin,
        db=session,
    )

    _assert_committed_last(session, "audit", "delete")


async def test_add_channel_identity_commits_before_responding(
    monkeypatch, admin, target
):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    target.channel_identities = None
    # The user lookup, then nobody else holding the identity.
    session = _RecordingSession(target, None)

    await users_module.add_channel_identity(
        user_id=str(target.id),
        body=users_module.LinkChannelIdentityRequest(
            channel_type="slack", platform_id="U123"
        ),
        request=_request(),
        admin=admin,
        db=session,
    )

    _assert_committed_last(session, "flush", "audit")
    assert target.channel_identities == {"slack:U123": True}


async def test_remove_channel_identity_commits_before_responding(
    monkeypatch, admin, target
):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    target.channel_identities = {"slack:U123": True}
    session = _RecordingSession(target)

    await users_module.remove_channel_identity(
        user_id=str(target.id),
        channel_type="slack",
        platform_id="U123",
        request=_request(),
        admin=admin,
        db=session,
    )

    _assert_committed_last(session, "flush", "audit")
    assert target.channel_identities == {}


async def test_impersonation_is_audited_before_the_token_is_handed_out(
    monkeypatch, admin, target
):
    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    session = _RecordingSession(target)

    response = await users_module.impersonate_user(
        user_id=str(target.id),
        request=_request(),
        admin=admin,
        db=session,
    )

    _assert_committed_last(session, "audit")
    assert response.access_token


async def test_user_quota_cache_is_dropped_after_the_override_commits(
    monkeypatch, admin, target
):
    import app.services.quota as quota_module

    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    target.quota_config = {"agentic": {"period": "monthly"}}
    session = _RecordingSession(target)
    monkeypatch.setattr(
        quota_module,
        "invalidate_quota_cache",
        _recorder(session, "invalidate_quota_cache"),
    )
    after_commit = AfterCommit()

    response = await users_module.update_user_quotas(
        user_id=target.id,
        body=users_module.UpdateUserQuotaRequest(quota_config=None),
        request=_request(),
        admin=admin,
        db=session,
        after_commit=after_commit,
    )
    # What ``run_after_commit`` does once the route has returned, before the
    # response is sent (pinned below with a real application).
    await after_commit.run()

    # Dropped before the commit, a quota check landing in between re-caches
    # the old override for the cache's whole TTL.
    _assert_committed_then(session, "invalidate_quota_cache", before=("flush", "audit"))
    assert response == {"quota_config": None}


async def test_quota_cache_invalidation_never_fails_a_committed_change(monkeypatch):
    import app.services.quota as quota_module

    async def _unreachable():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(quota_module, "redis_client", _unreachable)

    # It runs after the change committed: an outage leaves the old resolution
    # cached until its TTL, and never answers the landed change with a 500.
    await quota_module.invalidate_quota_cache(uuid.uuid4())
    await quota_module.invalidate_quota_cache()


async def test_reset_user_quotas_commits_the_reset_with_its_audit_row(
    monkeypatch, admin, target
):
    import app.services.quota as quota_module

    monkeypatch.setattr(users_module, "log_audit", _record_audit)
    session = _RecordingSession(target)

    async def _reset(db, _user_id):
        # As the real helper does: clear the counters, then commit.
        db.calls.append("reset_usage")
        await db.commit()

    monkeypatch.setattr(quota_module, "reset_user_quota_usage", _reset)

    await users_module.reset_user_quotas(
        user_id=target.id,
        request=_request(),
        admin=admin,
        db=session,
    )

    # One transaction: a reset can never land without its audit row.
    _assert_committed_last(session, "audit", "reset_usage")
    assert session.calls.index("audit") < session.calls.index("reset_usage")


# -- auth.py (self-service) ---------------------------------------------------


async def test_register_commits_before_handing_back_the_token(monkeypatch):
    import app.core.sso_helpers as sso_module

    async def _open_registration(_db):
        return SimpleNamespace(
            local_auth_enabled=True,
            registration_enabled=True,
            allowed_domain_list=[],
        )

    monkeypatch.setattr(sso_module, "get_sso_config", _open_registration)
    monkeypatch.setattr(auth_module, "log_audit", _record_audit)
    monkeypatch.setattr(auth_module, "hash_password_async", _fast_hash)
    session = _RecordingSession()

    response = await auth_module.register(
        body=auth_module.RegisterRequest(
            email="self.service@example.com",
            password=_PASSWORD,
            full_name="Self Service",
        ),
        request=_request(),
        db=session,
    )

    _assert_committed_last(session, "add", "flush", "audit")
    assert response.access_token


async def test_change_password_commits_before_handing_back_the_token(
    monkeypatch, target
):
    async def _current_password_ok(_plain, _hashed):
        return True

    monkeypatch.setattr(auth_module, "verify_password_async", _current_password_ok)
    monkeypatch.setattr(auth_module, "log_audit", _record_audit)
    monkeypatch.setattr(auth_module, "hash_password_async", _fast_hash)
    session = _RecordingSession()

    response = await auth_module.change_password(
        body=auth_module.ChangePasswordRequest(
            current_password="old-password", new_password=_PASSWORD
        ),
        request=_request(),
        user=target,
        db=session,
    )

    _assert_committed_last(session, "flush", "audit")
    assert response["access_token"]
    assert target.hashed_password == f"hashed:{_PASSWORD}"


async def test_reset_password_commits_before_handing_back_the_session(
    monkeypatch, target
):
    async def _consume(_token):
        return {"user_id": str(target.id)}

    monkeypatch.setattr(auth_module, "enforce_rate_limit", _noop)
    monkeypatch.setattr(auth_module.token_store, "consume_password_reset", _consume)
    monkeypatch.setattr(auth_module, "log_audit", _record_audit)
    monkeypatch.setattr(auth_module, "hash_password_async", _fast_hash)
    session = _RecordingSession(target)

    response = await auth_module.reset_password(
        body=auth_module.ResetPasswordRequest(token="raw-token", new_password=_PASSWORD),
        request=_request(),
        db=session,
    )

    _assert_committed_last(session, "flush", "audit")
    assert response.access_token
    assert target.hashed_password == f"hashed:{_PASSWORD}"


# -- groups.py (authorization) ------------------------------------------------


async def test_create_group_commits_before_responding(monkeypatch, admin):
    monkeypatch.setattr(groups_module, "log_audit", _record_audit)
    session = _RecordingSession()

    response = await groups_module.create_group(
        body=groups_module.CreateGroupRequest(name="Analysts"),
        request=_request(),
        admin=admin,
        db=session,
    )

    # The New Group page opens ``/groups/{id}`` on its very next request.
    _assert_committed_last(session, "add", "flush", "audit")
    assert response.name == "Analysts"


async def test_update_group_commits_then_drops_the_quota_cache(monkeypatch, admin):
    import app.services.quota as quota_module

    monkeypatch.setattr(groups_module, "log_audit", _record_audit)
    group = _group()
    # The group lookup, then its member count.
    session = _RecordingSession(group, 3)
    monkeypatch.setattr(
        quota_module,
        "invalidate_quota_cache",
        _recorder(session, "invalidate_quota_cache"),
    )
    after_commit = AfterCommit()

    response = await groups_module.update_group(
        group_id=str(group.id),
        body=groups_module.UpdateGroupRequest(
            quota_config={"agentic": {"period": "monthly"}}
        ),
        request=_request(),
        admin=admin,
        db=session,
        after_commit=after_commit,
    )
    await after_commit.run()

    # The member count is read before the commit: nothing that can still fail
    # runs between the commit and the answer.
    _assert_committed_then(session, "invalidate_quota_cache", before=("flush", "audit"))
    assert response.member_count == 3


class _GroupDeleteSession(_RecordingSession):
    """The group, its members' ids, and nothing for the share cleanup."""

    def __init__(self, group, member_ids):
        super().__init__()
        self._group = group
        self._member_ids = member_ids

    async def execute(self, stmt):
        self.calls.append("execute")
        rendered = str(stmt)
        if "FROM group_members" in rendered:
            return _Result(list(self._member_ids))
        if "FROM groups" in rendered:
            return _Result(self._group)
        return _Result(None)


@pytest.mark.parametrize("had_quota", [True, False])
async def test_delete_group_commits_then_drops_the_quota_cache(
    monkeypatch, admin, had_quota
):
    import app.services.quota as quota_module

    monkeypatch.setattr(groups_module, "log_audit", _record_audit)
    group = _group(quota_config={"agentic": {"period": "monthly"}} if had_quota else None)
    members = [uuid.uuid4(), uuid.uuid4()]
    session = _GroupDeleteSession(group, members)
    monkeypatch.setattr(
        quota_module,
        "invalidate_quota_cache",
        _recorder(session, "invalidate_quota_cache"),
    )
    dropped = _record_membership_drops(monkeypatch, session)
    after_commit = AfterCommit()

    await groups_module.delete_group(
        group_id=str(group.id),
        request=_request(),
        admin=admin,
        db=session,
        after_commit=after_commit,
    )
    await after_commit.run()

    # The cascade ends every membership: with a quota, every cached resolution
    # goes (a member added meanwhile included); without one, the members'.
    if had_quota:
        _assert_committed_then(
            session, "invalidate_quota_cache", before=("audit", "delete")
        )
        assert dropped == []
    else:
        _assert_committed_then(
            session, "invalidate_membership_quota_cache", before=("audit", "delete")
        )
        assert dropped == [members]


@pytest.mark.parametrize("quota_config", [{"agentic": {"period": "monthly"}}, None])
async def test_add_members_commits_then_drops_their_quota_cache(
    monkeypatch, admin, target, quota_config
):
    monkeypatch.setattr(groups_module, "log_audit", _record_audit)
    group = _group(quota_config=quota_config)
    # The group, the user, then no existing membership.
    session = _RecordingSession(group, target, None)
    dropped = _record_membership_drops(monkeypatch, session)
    after_commit = AfterCommit()

    response = await groups_module.add_members(
        group_id=str(group.id),
        body=groups_module.AddMembersRequest(user_ids=[str(target.id)]),
        request=_request(),
        admin=admin,
        db=session,
        after_commit=after_commit,
    )
    await after_commit.run()

    # Membership decides what the member may read on their next request, and
    # their groups' quotas their resolved quota: dropped whatever this group's
    # quota read as, since a concurrent quota change can make that read stale.
    _assert_committed_then(
        session, "invalidate_membership_quota_cache", before=("add", "flush", "audit")
    )
    assert dropped == [[str(target.id)]]
    assert [member.user_id for member in response] == [str(target.id)]


async def test_adding_no_new_member_drops_no_quota_cache(monkeypatch, admin, target):
    monkeypatch.setattr(groups_module, "log_audit", _record_audit)
    group = _group(quota_config={"agentic": {"period": "monthly"}})
    # The group, the user, then the membership they already hold.
    session = _RecordingSession(group, target, SimpleNamespace(id=uuid.uuid4()))
    dropped = _record_membership_drops(monkeypatch, session)
    after_commit = AfterCommit()

    response = await groups_module.add_members(
        group_id=str(group.id),
        body=groups_module.AddMembersRequest(user_ids=[str(target.id)]),
        request=_request(),
        admin=admin,
        db=session,
        after_commit=after_commit,
    )
    await after_commit.run()

    assert response == []
    _assert_committed_last(session)
    assert dropped == []


@pytest.mark.parametrize("quota_config", [{"agentic": {"period": "monthly"}}, None])
async def test_remove_member_commits_then_drops_the_quota_cache(
    monkeypatch, admin, target, quota_config
):
    monkeypatch.setattr(groups_module, "log_audit", _record_audit)
    group = _group(quota_config=quota_config)
    member = SimpleNamespace(id=uuid.uuid4(), group_id=group.id, user_id=target.id)
    # The membership, then the group and the user named by the audit row.
    session = _RecordingSession(member, group, target)
    dropped = _record_membership_drops(monkeypatch, session)
    after_commit = AfterCommit()

    await groups_module.remove_member(
        group_id=str(group.id),
        user_id=str(target.id),
        request=_request(),
        admin=admin,
        db=session,
        after_commit=after_commit,
    )
    await after_commit.run()

    # A revoked membership must stop granting access from the 204 on.
    _assert_committed_then(
        session, "invalidate_membership_quota_cache", before=("audit", "delete")
    )
    assert dropped == [[target.id]]


# -- platform quotas and MCP snippet keys -------------------------------------


def _patch_platform_settings(monkeypatch, ps) -> None:
    import app.api.v1.platform_settings_helpers as ps_helpers

    async def _load(_db):
        return ps

    monkeypatch.setattr(ps_helpers, "get_platform_settings", _load)


async def test_platform_quota_cache_is_dropped_after_the_config_commits(
    monkeypatch, admin
):
    import app.services.quota as quota_module

    ps = SimpleNamespace(quota_config=None)
    _patch_platform_settings(monkeypatch, ps)
    monkeypatch.setattr(settings_module, "log_audit", _record_audit)
    session = _RecordingSession()
    monkeypatch.setattr(
        quota_module,
        "invalidate_quota_cache",
        _recorder(session, "invalidate_quota_cache"),
    )
    after_commit = AfterCommit()

    await settings_module.update_platform_quotas(
        body=settings_module.QuotaConfig(),
        request=_request(),
        user=admin,
        db=session,
        _ai_gate=None,
        after_commit=after_commit,
    )
    await after_commit.run()

    _assert_committed_then(session, "invalidate_quota_cache", before=("flush", "audit"))


async def test_snippet_key_commits_before_the_snippet_is_handed_back(
    monkeypatch, target
):
    import app.services.audit as audit_module
    from app.api.v1 import mcp_xtm

    async def _enabled(_db):
        return True

    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _enabled)
    monkeypatch.setattr(audit_module, "log_audit", _record_audit)
    monkeypatch.setattr(api_keys_module, "_hash_key", _fast_hash)
    target.impersonated_by = None
    session = _RecordingSession()

    response = await platform_module.build_xtm_mcp_snippet(
        body=platform_module.XtmMCPSnippetRequest(client="codex", include_key=True),
        request=_request(),
        db=session,
        user=target,
    )

    _assert_committed_last(session, "add", "flush", "audit")
    assert response.api_key_created


# -- AfterCommit ----------------------------------------------------------------


def _http_scope(path: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }


async def _serve(app: FastAPI, path: str, events: list[str]) -> int:
    """One request through the ASGI app, noting when the response goes out."""
    status_codes: list[int] = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        if message["type"] == "http.response.start":
            events.append("response sent")
            status_codes.append(message["status"])

    await app(_http_scope(path), _receive, _send)
    return status_codes[0]


async def test_after_commit_runs_once_the_route_returned_before_the_response():
    events: list[str] = []
    app = FastAPI()

    async def _drop(label: str) -> None:
        events.append(label)

    async def _broken() -> None:
        raise ConnectionError("redis is down")

    @app.post("/write")
    async def write(after_commit: AfterCommit = Depends(run_after_commit, scope="function")):
        after_commit.add(_drop, "first drop")
        after_commit.add(_broken)
        after_commit.add(_drop, "second drop")
        events.append("route committed and returned")
        return {"ok": True}

    status_code = await _serve(app, "/write", events)

    # After the route's commit, before the caller can act on the answer; one
    # failing action neither fails the landed write nor skips the next one.
    assert status_code == 200
    assert events == [
        "route committed and returned",
        "first drop",
        "second drop",
        "response sent",
    ]


async def test_after_commit_is_skipped_when_the_route_raises():
    events: list[str] = []
    app = FastAPI()

    async def _drop() -> None:
        events.append("drop")

    @app.post("/refused")
    async def refused(after_commit: AfterCommit = Depends(run_after_commit, scope="function")):
        after_commit.add(_drop)
        raise HTTPException(status_code=409, detail="conflict")

    assert await _serve(app, "/refused", events) == 409
    assert events == ["response sent"]


def _dependants(dependant):
    for sub in dependant.dependencies:
        yield sub
        yield from _dependants(sub)


def test_every_after_commit_runs_before_the_response_is_sent():
    """Request-scoped, ``run_after_commit`` would run after the response went
    out, when the caller may already act on the stale state."""
    from app.main import application

    uses = {
        f"{'/'.join(sorted(route.methods))} {route.path}": sub.scope
        for route in application.routes
        if isinstance(route, APIRoute)
        for sub in _dependants(route.dependant)
        if sub.call is run_after_commit
    }
    # The detection must keep seeing the routes it was written for.
    assert {
        "PUT /api/v1/groups/{group_id}",
        "DELETE /api/v1/groups/{group_id}",
        "POST /api/v1/groups/{group_id}/members",
        "DELETE /api/v1/groups/{group_id}/members/{user_id}",
        "PUT /api/v1/users/{user_id}/quotas",
        "PUT /api/v1/settings/quotas",
    } <= set(uses), sorted(uses)
    request_scoped = sorted(label for label, scope in uses.items() if scope != "function")
    assert not request_scoped, request_scoped


# -- tripwire -----------------------------------------------------------------

#: The account routers: who exists, how they sign in, what they may do, and the
#: credentials that act for them.
_ACCOUNT_ROUTERS = {
    "auth": auth_module.router,
    "users": users_module.router,
    "groups": groups_module.router,
    "api_keys": api_keys_module.router,
    "invitations": invitations_module.router,
}

#: Write routes with no commit of their own, and why that is safe: the helper
#: they call commits before they answer, or nothing their caller's next request
#: reads is in this transaction.
_NO_COMMIT_IN_ROUTE = {
    "auth.forgot_password": "writes nothing; a background task mints the reset token in Redis",
    "users.reset_user_quotas": "reset_user_quota_usage commits the reset with the audit row written first",
}

#: Constructing or calling one of these hands a credential to the caller, and
#: assigning one of these attributes replaces or revokes one: a route doing
#: either commits before it answers, whichever module it lives in.
_CREDENTIAL_CALLS = {"UserApiKey", "_mint_package_api_key", "RunnerEnrollmentKey"}
_CREDENTIAL_ATTRIBUTES = {"mcp_global_api_key_hash"}
_WRITE_DECORATORS = {"post", "put", "patch", "delete"}


def _account_write_routes() -> dict[str, object]:
    routes: dict[str, object] = {}
    for module, router in _ACCOUNT_ROUTERS.items():
        for route in router.routes:
            if isinstance(route, APIRoute) and route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
                routes[f"{module}.{route.endpoint.__name__}"] = route.endpoint
    return routes


def _node_commits(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "commit"
        for node in ast.walk(tree)
    )


def _commits(endpoint) -> bool:
    return _node_commits(ast.parse(textwrap.dedent(inspect.getsource(endpoint))))


def _credential_write_routes() -> dict[str, ast.AST]:
    """Every write route under ``app/api`` that mints or replaces a credential."""
    api_root = pathlib.Path(users_module.__file__).resolve().parents[1]
    routes: dict[str, ast.AST] = {}
    for path in sorted(api_root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if not any(
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr in _WRITE_DECORATORS
                for deco in node.decorator_list
            ):
                continue
            calls = {
                inner.func.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            }
            assigned = {
                target.attr
                for inner in ast.walk(node)
                if isinstance(inner, ast.Assign)
                for target in inner.targets
                if isinstance(target, ast.Attribute)
            }
            if calls & _CREDENTIAL_CALLS or assigned & _CREDENTIAL_ATTRIBUTES:
                routes[f"{path.stem}.{node.name}"] = node
    return routes


def test_every_account_write_route_commits_before_answering():
    missing = sorted(
        name
        for name, endpoint in _account_write_routes().items()
        if name not in _NO_COMMIT_IN_ROUTE and not _commits(endpoint)
    )
    assert not missing, (
        "these account write routes leave their commit to the get_db teardown, "
        "which runs after the response is sent - commit before answering (and "
        "pin it above), or add them to _NO_COMMIT_IN_ROUTE with the reason "
        f"their caller's next request cannot depend on the write: {missing}"
    )


def test_no_commit_exemptions_name_live_routes_that_do_not_commit():
    routes = _account_write_routes()
    for name in _NO_COMMIT_IN_ROUTE:
        assert name in routes, f"{name} is no longer an account write route"
        assert not _commits(routes[name]), f"{name} commits now - drop its exemption"


def test_every_route_minting_or_replacing_a_credential_commits_before_answering():
    routes = _credential_write_routes()
    # The detection itself must keep seeing the routes it was written for.
    assert {
        "api_keys.create_api_key",
        "settings.generate_mcp_key",
        "settings.revoke_mcp_key",
        "platform.build_xtm_mcp_snippet",
        "platform.download_xtm_mcp_package",
        "runners.create_enrollment_key",
    } <= set(routes), sorted(routes)
    missing = sorted(name for name, node in routes.items() if not _node_commits(node))
    assert not missing, (
        "these routes hand out (or replace) a credential and leave the commit to "
        "the get_db teardown - the caller would use it before it exists, or a "
        f"revoked one would keep working: {missing}"
    )
