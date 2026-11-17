"""``get_visible_run`` must enforce every tier the flow's own filter does.

A run snapshots its flow's sharing so run listings need no join
(``app/models/flow_run.py``), and ``get_visible_run`` re-implements
``user_visible_filter`` in Python over that snapshot. A hand-written copy of a
shared rule drifts silently: the snapshot covered only the group tier while
flows gained per-user grants and ``access_restricted``, so a company
flow narrowed to one group still had its run journal readable by every
authenticated user — the copy said "company-managed → visible, full stop".

These tests pin each tier of the copy against the rule it mirrors.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import flow_access


class _FakeResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None


class _FakeDb:
    """Returns the run for the first execute, then the caller's group ids."""

    def __init__(self, run, group_ids=()):
        self._results = [[run], list(group_ids)]

    async def execute(self, _statement):
        return _FakeResult(self._results.pop(0) if self._results else [])


def _run(**overrides):
    run = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        access_restricted=False,
        shared_group_ids=None,
        shared_user_ids=None,
    )
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


def _user(is_admin: bool = False, user_id: uuid.UUID | None = None):
    return SimpleNamespace(id=user_id or uuid.uuid4(), is_admin=is_admin)


async def _visible(run, user, group_ids=()) -> bool:
    try:
        await flow_access.get_visible_run(_FakeDb(run, group_ids), user, str(run.id))
    except HTTPException as exc:
        assert exc.status_code == 404  # never 403 — existence stays hidden
        return False
    return True


@pytest.mark.asyncio
async def test_company_managed_run_is_visible_to_everyone():
    """The unnarrowed company tier is a blanket grant, as on the flow."""
    assert await _visible(_run(is_company_managed=True), _user())


@pytest.mark.asyncio
async def test_access_restricted_company_run_is_hidden_from_a_stranger():
    """``access_restricted`` withdraws the blanket clause — the whole point of
    the flag. Without this the run journal of a company flow published to a
    single group is readable by every authenticated user.
    """
    run = _run(is_company_managed=True, access_restricted=True)

    assert not await _visible(run, _user())


@pytest.mark.asyncio
async def test_access_restricted_company_run_stays_visible_to_its_grantees():
    """Narrowed, not hidden: owner, user grants and group grants still resolve."""
    grantee = _user()
    run = _run(
        is_company_managed=True,
        access_restricted=True,
        shared_user_ids=[str(grantee.id)],
    )
    assert await _visible(run, grantee)

    group_id = str(uuid.uuid4())
    group_run = _run(
        is_company_managed=True, access_restricted=True, shared_group_ids=[group_id]
    )
    assert await _visible(group_run, _user(), group_ids=[group_id])

    owner = _user()
    owned = _run(
        is_company_managed=True, access_restricted=True, user_id=owner.id
    )
    assert await _visible(owned, owner)


@pytest.mark.asyncio
async def test_null_access_restricted_reads_as_unrestricted():
    """A NULL means "never configured" — it must not retroactively hide the
    runs of every company flow on installs where ``_ensure_model_columns``
    created the column bare-nullable. Same ``IS NOT TRUE`` reading as the SQL
    predicate.
    """
    assert await _visible(_run(is_company_managed=True, access_restricted=None), _user())


@pytest.mark.asyncio
async def test_per_user_grant_makes_a_private_run_visible():
    grantee = _user()
    run = _run(shared_user_ids=[str(grantee.id)])

    assert await _visible(run, grantee)


@pytest.mark.asyncio
async def test_per_user_grant_is_matched_on_the_canonical_uuid():
    """Resolved through the shared grant resolver, so a non-canonical stored id
    grants here exactly what it grants on the flow — no more, no less.
    """
    grantee = _user()
    run = _run(shared_user_ids=[str(grantee.id).upper()])

    assert await _visible(run, grantee)


@pytest.mark.asyncio
async def test_a_stranger_sees_nothing_of_a_private_run():
    assert not await _visible(_run(), _user())


@pytest.mark.asyncio
async def test_admin_bypasses_every_tier():
    run = _run(is_company_managed=True, access_restricted=True)

    assert await _visible(run, _user(is_admin=True))
