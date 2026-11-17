"""Tripwire: deleting a user or a group prunes the grants of every shareable model.

``shared_user_ids`` and ``shared_group_ids`` are JSON arrays with no foreign
key, so the deletion routes clear them themselves. They used to walk explicit
model lists, and several shareable models were on neither: their grants kept
the ids of accounts and groups that no longer existed. The cleanups now derive
the models from the mappers (``helpers.share_grant_models``). Both run here
against a session that records the models they read, compared with every
mapped model carrying the column; the only models left out are the grant
snapshots, which must be declared.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import AfterCommit
from app.api.v1 import groups as groups_module
from app.api.v1.helpers import (
    GRANT_SNAPSHOT_MODELS,
    prune_group_grants,
    prune_user_grants,
    share_grant_models,
)
from app.core.database import Base
from app.models.group import Group, GroupMember
from app.models.llm_config import LLMProviderConfig
from app.models.user import User

_COLUMNS = ("shared_user_ids", "shared_group_ids")


def _models_with(column: str) -> set[type]:
    return {mapper.class_ for mapper in Base.registry.mappers if column in mapper.columns}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _RecordingSession:
    """Answers the deletion routes' own lookups; records every other model read.

    ``locked`` holds the principal each cleanup locked, ``unlocked`` every grant
    read that would be rewritten without a row lock.
    """

    def __init__(self, group=None):
        self.group = group
        self.queried: list[type] = []
        self.locked: list[type] = []
        self.unlocked: list[type] = []

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        for_update = stmt._for_update_arg
        if entity in (User, Group):
            if for_update is not None and not for_update.read:
                self.locked.append(entity)
            return _Result([self.group] if entity is Group and self.group else [])
        if entity is GroupMember:
            return _Result([])
        self.queried.append(entity)
        if for_update is None:
            self.unlocked.append(entity)
        return _Result([])

    async def delete(self, _obj):
        return None

    async def commit(self):
        return None


@pytest.mark.parametrize("column", _COLUMNS)
def test_every_model_with_the_column_is_pruned_but_the_declared_snapshots(column):
    assert set(share_grant_models(column)) == _models_with(column) - set(GRANT_SNAPSHOT_MODELS)


@pytest.mark.parametrize("column", _COLUMNS)
def test_a_declared_snapshot_still_carries_the_column(column):
    """An exemption outliving its reason would hide a model that holds real grants."""
    assert set(GRANT_SNAPSHOT_MODELS) <= _models_with(column)


@pytest.mark.asyncio
async def test_user_deletion_prunes_every_model_with_per_user_grants_under_lock():
    session = _RecordingSession()

    assert await prune_user_grants(session, uuid.uuid4()) == 0

    assert session.locked == [User]
    # Then the LLM provider exclusions naming the user, locked too.
    assert session.queried == [*share_grant_models("shared_user_ids"), LLMProviderConfig]
    assert session.unlocked == []


@pytest.mark.asyncio
async def test_group_deletion_prunes_every_model_with_group_grants_under_lock():
    session = _RecordingSession()

    assert await prune_group_grants(session, uuid.uuid4()) == 0

    assert session.locked == [Group]
    assert session.queried == list(share_grant_models("shared_group_ids"))
    assert session.unlocked == []


@pytest.mark.asyncio
async def test_the_group_route_runs_the_cleanup(monkeypatch):
    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(groups_module, "log_audit", _no_audit)
    group = Group(id=uuid.uuid4(), name="Responders", quota_config=None)
    session = _RecordingSession(group)

    await groups_module.delete_group(
        group_id=str(group.id),
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}),
        admin=SimpleNamespace(id=uuid.uuid4()),
        db=session,
        after_commit=AfterCommit(),
    )

    # The providers are read first, under the group's lock: one that excludes
    # the group refuses the deletion.
    assert session.queried == [LLMProviderConfig, *share_grant_models("shared_group_ids")]
