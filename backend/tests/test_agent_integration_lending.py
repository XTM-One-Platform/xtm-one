"""Integrations lent through an agent (``Agent.integration_lending``, #8824).

The owner of a shared agent can lend their personal integrations through
that agent: a member chatting with it runs the agent's tools on the owner's
account, but nothing is granted outside the agent - the integration is not
the member's, they cannot list it or bind it elsewhere. These tests pin the
three seams: what an agent lends (``lent_integration_ids``), the write rule
(``checked_integration_lending``) and the run-time guard
(``shadow_invisible_bindings`` with ``lent_ids``).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from fastapi import HTTPException

from app.services.integration_credentials import (
    checked_integration_lending,
    lent_integration_ids,
    shared_agent_personal_integrations,
)
from app.services.integration_tools import shadow_invisible_bindings


class _Row(NamedTuple):
    id: uuid.UUID
    auth_type: str
    user_id: uuid.UUID | None
    oauth_share_acknowledged: bool


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return _Scalars(self._rows)


class _SequencedDB:
    """Answers each ``execute`` with the next configured row list."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls = 0

    async def execute(self, *_args, **_kwargs):
        self.calls += 1
        return _Result(self._answers.pop(0) if self._answers else [])


def _agent(owner, bindings, lent):
    return SimpleNamespace(
        user_id=owner,
        integration_bindings=bindings,
        integration_lending={"integration_ids": lent} if lent is not None else None,
    )


class _IntegRow(NamedTuple):
    id: uuid.UUID
    name: str
    provider: str
    shared_group_ids: list[str] | None
    shared_user_ids: list[str] | None = None


@pytest.mark.asyncio
async def test_editor_reads_the_shared_agent_personal_integrations_from_the_agent():
    """An editor cannot read the owner's rows: the agent answers the banner's
    list itself, without the rows already shared with one of its groups."""
    group = str(uuid.uuid4())
    mine, shared = uuid.uuid4(), uuid.uuid4()
    agent = SimpleNamespace(
        user_id=uuid.uuid4(),
        shared_group_ids=[group],
        integration_bindings={"hubspot": [str(mine)], "drata": [str(shared)]},
    )
    db = _SequencedDB(
        [
            _IntegRow(shared, "Drata", "drata", [group]),
            _IntegRow(mine, "HubSpot", "hubspot", None),
        ]
    )
    assert await shared_agent_personal_integrations(db, agent) == [
        {"id": str(mine), "name": "HubSpot", "provider": "hubspot"}
    ]


@pytest.mark.asyncio
async def test_an_agent_shared_with_people_only_lists_what_they_do_not_have():
    """No group: the people the agent is shared with still use the owner's
    rows through it, except a row already shared with one of them."""
    person = str(uuid.uuid4())
    mine, shared = uuid.uuid4(), uuid.uuid4()
    agent = SimpleNamespace(
        user_id=uuid.uuid4(),
        shared_group_ids=None,
        shared_user_ids=[person],
        integration_bindings={"hubspot": [str(mine)], "drata": [str(shared)]},
    )
    db = _SequencedDB(
        [
            _IntegRow(shared, "Drata", "drata", None, [person]),
            _IntegRow(mine, "HubSpot", "hubspot", None),
        ]
    )
    assert await shared_agent_personal_integrations(db, agent) == [
        {"id": str(mine), "name": "HubSpot", "provider": "hubspot"}
    ]


@pytest.mark.asyncio
async def test_an_agent_shared_with_nobody_lists_nothing():
    agent = SimpleNamespace(
        user_id=uuid.uuid4(),
        shared_group_ids=[],
        shared_user_ids=None,
        integration_bindings={"github": [str(uuid.uuid4())]},
    )
    db = _SequencedDB()
    assert await shared_agent_personal_integrations(db, agent) == []
    assert db.calls == 0


def test_lent_ids_are_restricted_to_bound_integrations():
    bound, unbound = str(uuid.uuid4()), str(uuid.uuid4())
    agent = _agent(uuid.uuid4(), {"github": [bound]}, [bound, unbound])
    assert lent_integration_ids(agent) == {bound}


def test_no_lending_lends_nothing():
    agent = _agent(uuid.uuid4(), {"github": [str(uuid.uuid4())]}, None)
    assert lent_integration_ids(agent) == set()


@pytest.mark.asyncio
async def test_shared_agent_keeps_a_lent_binding_for_a_member():
    """The member cannot see the owner's integration, yet the agent lends it:
    the binding stays for this run, nothing falls back per user."""
    owner = uuid.uuid4()
    lent = str(uuid.uuid4())
    effective = {"github": lent}
    origin = {"github": "direct_agent_binding"}
    # 1st query: nothing visible to the member; 2nd: the lent row is the owner's.
    db = _SequencedDB([], [uuid.UUID(lent)])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
        user_group_ids=[],
        lent_ids={lent},
        lent_owner_id=owner,
    )

    assert shadowed == []
    assert effective == {"github": lent}


@pytest.mark.asyncio
async def test_lent_binding_no_longer_owned_by_the_agent_owner_is_dropped():
    """A lent id whose row is not (or no longer) the owner's is not kept."""
    lent = str(uuid.uuid4())
    effective = {"github": lent}
    origin = {"github": "direct_agent_binding"}
    db = _SequencedDB([], [])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
        lent_ids={lent},
        lent_owner_id=uuid.uuid4(),
    )

    assert shadowed == ["github"]
    assert effective == {}


@pytest.mark.asyncio
async def test_unlent_binding_is_still_dropped():
    """Lending one integration lends that one only."""
    owner = uuid.uuid4()
    lent, other = str(uuid.uuid4()), str(uuid.uuid4())
    effective = {"github": lent, "microsoft": other}
    origin = dict.fromkeys(effective, "direct_agent_binding")
    db = _SequencedDB([], [uuid.UUID(lent)])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
        lent_ids={lent},
        lent_owner_id=owner,
    )

    assert shadowed == ["microsoft"]
    assert effective == {"github": lent}


@pytest.mark.asyncio
async def test_lending_is_written_by_the_owner_for_their_own_bound_integrations():
    owner = uuid.uuid4()
    lent = str(uuid.uuid4())
    db = _SequencedDB([uuid.UUID(lent)])

    value = await checked_integration_lending(
        db,
        actor_id=owner,
        agent_owner_id=owner,
        integration_ids=[lent],
        bindings={"github": [lent]},
        now_iso="2026-11-05T12:00:00+00:00",
    )

    assert value == {
        "integration_ids": [lent],
        "set_by": str(owner),
        "set_at": "2026-11-05T12:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_empty_lending_clears_it():
    value = await checked_integration_lending(
        _SequencedDB(),
        actor_id=uuid.uuid4(),
        agent_owner_id=uuid.uuid4(),
        integration_ids=[],
        bindings={},
        now_iso="2026-11-05T12:00:00+00:00",
    )
    assert value is None


@pytest.mark.asyncio
async def test_lending_an_unbound_integration_is_refused():
    owner = uuid.uuid4()
    with pytest.raises(HTTPException) as exc:
        await checked_integration_lending(
            _SequencedDB(),
            actor_id=owner,
            agent_owner_id=owner,
            integration_ids=[str(uuid.uuid4())],
            bindings={"github": [str(uuid.uuid4())]},
            now_iso="2026-11-05T12:00:00+00:00",
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_only_the_agent_owner_lends():
    lent = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await checked_integration_lending(
            _SequencedDB([uuid.UUID(lent)]),
            actor_id=uuid.uuid4(),
            agent_owner_id=uuid.uuid4(),
            integration_ids=[lent],
            bindings={"github": [lent]},
            now_iso="2026-11-05T12:00:00+00:00",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_lending_someone_elses_integration_is_refused():
    owner = uuid.uuid4()
    lent = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await checked_integration_lending(
            _SequencedDB([]),
            actor_id=owner,
            agent_owner_id=owner,
            integration_ids=[lent],
            bindings={"github": [lent]},
            now_iso="2026-11-05T12:00:00+00:00",
        )
    assert exc.value.status_code == 403
