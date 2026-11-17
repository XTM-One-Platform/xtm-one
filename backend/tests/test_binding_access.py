"""Unit tests for the binding additions rule.

The visibility predicate itself is exercised against PostgreSQL in
``test_assignment_tool_sources_access_postgres.py`` and
``test_binding_additions_postgres.py``; these pin what is decided before any
lookup and how a copy falls back.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import binding_access as rule

_NO_SERVER = "00000000-0000-0000-0000-000000000000"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _LookupDB:
    """Answers the id lookup with ``allowed``; counts the lookups."""

    def __init__(self, allowed=()):
        self._allowed = [(uuid.UUID(str(a)),) for a in allowed]
        self.lookups = 0

    async def execute(self, _stmt):
        self.lookups += 1
        return _Result(self._allowed)


def _agent(*bound, company=False):
    return SimpleNamespace(mcp_server_ids=list(bound), is_company_managed=company)


_USER = SimpleNamespace(id=uuid.uuid4(), is_admin=False)


async def _refused(db, agent, value, baseline=None):
    return await rule.refused_tool_source_additions(
        db, user=_USER, agent=agent, value=value, baseline=baseline, user_group_ids=[]
    )


@pytest.mark.asyncio
async def test_entries_already_stored_or_bound_need_no_lookup():
    stale, bound = str(uuid.uuid4()), str(uuid.uuid4())
    db = _LookupDB()

    assert await _refused(db, _agent(bound), [stale.upper(), bound], baseline=[stale]) == []
    assert await _refused(db, _agent(), None, baseline=[stale]) == []
    assert db.lookups == 0


@pytest.mark.asyncio
async def test_an_added_entry_that_is_not_a_server_id_is_refused():
    db = _LookupDB()

    assert await _refused(db, _agent(), ["mcp-0"]) == ["mcp-0"]
    assert await _refused(db, _agent(), ["mcp-0"], baseline=["mcp-0"]) == []
    assert db.lookups == 0


@pytest.mark.asyncio
async def test_an_added_server_the_lookup_does_not_allow_is_refused():
    allowed, other = str(uuid.uuid4()), str(uuid.uuid4())
    db = _LookupDB(allowed=[allowed])

    assert await _refused(db, _agent(), [allowed, other]) == [other]
    assert db.lookups == 1


@pytest.mark.asyncio
async def test_a_refused_write_is_a_400_that_names_nothing():
    other = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await rule.assert_tool_source_additions_allowed(
            _LookupDB(),
            user=_USER,
            agent=_agent(),
            value=[other],
            baseline=None,
            user_group_ids=[],
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == rule.TOOL_SOURCES_REFUSED_DETAIL
    assert other not in exc.value.detail


@pytest.mark.asyncio
async def test_a_copy_drops_what_the_copier_may_not_add_and_never_widens():
    bound, other = str(uuid.uuid4()), str(uuid.uuid4())

    async def _copy(value):
        return await rule.copyable_tool_sources(
            _LookupDB(), user=_USER, agent=_agent(bound), value=value, user_group_ids=[]
        )

    assert await _copy([bound, other]) == [bound]
    assert await _copy([other]) == [_NO_SERVER]
    assert await _copy([_NO_SERVER]) == [_NO_SERVER]
    assert await _copy([]) == []
    assert await _copy(None) is None


@pytest.mark.asyncio
async def test_a_copy_counts_the_servers_it_leaves_out():
    """The duplicate reports what it left out: each server a scope
    names and the copier may not add counts once; the no-server marker names
    no server and is not counted."""
    bound, other, gone = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())

    async def _copy(value):
        return await rule.tool_sources_for_copy(
            _LookupDB(), user=_USER, agent=_agent(bound), value=value, user_group_ids=[]
        )

    assert await _copy([bound, other, other.upper(), gone]) == ([bound], 2)
    assert await _copy([other]) == ([_NO_SERVER], 1)
    assert await _copy([_NO_SERVER]) == ([_NO_SERVER], 0)
    assert await _copy([bound]) == ([bound], 0)
    assert await _copy([]) == ([], 0)
    assert await _copy(None) == (None, 0)


def test_a_non_admin_never_turns_the_whitelist_bypass_on():
    """``bypass_whitelist`` is admin-only: a non-admin may keep one the stored
    entry or the agent's entry for the same channel and bot already carries,
    never turn one on - not even on a bot the row or the agent already names,
    which the channel check skips."""
    bot, other = str(uuid.uuid4()), str(uuid.uuid4())
    on = {"slack": {"channel_bot_id": bot, "bypass_whitelist": True}}
    off = {"slack": {"channel_bot_id": bot}}

    def _check(value, *, user=_USER, baseline=None, agent_config=None):
        rule.assert_whitelist_bypass_not_added(
            value, user=user, baseline=baseline, agent_config=agent_config
        )

    for baseline, agent_config in ((None, None), (off, None), (None, off), (off, off)):
        with pytest.raises(HTTPException) as exc:
            _check(on, baseline=baseline, agent_config=agent_config)
        assert exc.value.status_code == 403
        assert exc.value.detail == rule.WHITELIST_BYPASS_REFUSED_DETAIL
    # Another bot's bypass, or another channel's, is not this entry's.
    with pytest.raises(HTTPException):
        _check(on, baseline={"slack": {"channel_bot_id": other, "bypass_whitelist": True}})
    with pytest.raises(HTTPException):
        _check(on, agent_config={"teams": {"channel_bot_id": bot, "bypass_whitelist": True}})

    # Kept: already on the stored entry or the agent's (any spelling of the id).
    _check(on, baseline={"slack": {"channel_bot_id": bot.upper(), "bypass_whitelist": True}})
    _check(on, agent_config=on)
    # No flag, or an admin: nothing to refuse.
    _check(off)
    _check({"slack": {"channel_bot_id": bot, "bypass_whitelist": False}})
    _check(on, user=SimpleNamespace(id=uuid.uuid4(), is_admin=True))
    _check(None)


def test_only_entries_naming_a_new_bot_are_channel_additions():
    stored, agents_own, new = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    config = {
        "slack": {"channel_bot_id": stored.upper()},
        "teams": {"channel_bot_id": agents_own},
        "discord": {"channel_bot_id": new},
        "telegram": {"receive_enabled": True},  # names no bot: binds nothing
    }

    added = rule.added_channel_entries(
        config,
        baseline={"slack": {"channel_bot_id": stored}},
        agent_bound=[agents_own],
    )

    assert added == {"discord": {"channel_bot_id": new}}


@pytest.mark.asyncio
async def test_integration_additions_skip_the_row_and_the_agent():
    stored, agents_own, new = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    agent = SimpleNamespace(integration_bindings={"github": [agents_own]}, is_company_managed=False)
    db = _LookupDB()

    await rule.assert_integration_additions_allowed(
        db,
        user=_USER,
        agent=agent,
        value={"github": [stored, agents_own]},
        baseline={"github": [stored]},
        user_group_ids=[],
    )
    assert db.lookups == 0

    with pytest.raises(HTTPException) as exc:
        await rule.assert_integration_additions_allowed(
            db,
            user=_USER,
            agent=agent,
            value={"github": [stored, new]},
            baseline={"github": [stored]},
            user_group_ids=[],
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == rule.INTEGRATIONS_REFUSED_DETAIL
    assert new not in exc.value.detail
