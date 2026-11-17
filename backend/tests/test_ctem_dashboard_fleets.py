"""The fleets each person's command center shows (``users.ctem_dashboard_fleet_ids``, #11464).

Written whole through ``PUT /settings/preferences``: in order, deduplicated,
only the fleets the person may read kept; an explicit null restores the
built-in CTEM fleets; omitting the field leaves the choice alone.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.api.v1.settings as settings_api
from app.api.v1.schemas.settings import MAX_DASHBOARD_FLEETS, UserPreferencesRequest

MINE = str(uuid.uuid4())
BUILTIN = str(uuid.uuid4())
OTHERS = str(uuid.uuid4())


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """Answers the visibility read with the fleets the person may read."""

    def __init__(self, visible):
        self._visible = visible
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows([(uuid.UUID(v),) for v in self._visible])

    async def flush(self):
        return None

    async def commit(self):
        return None


def _user(**overrides):
    base = dict(
        id=uuid.uuid4(),
        email="u@example.com",
        full_name="Test User",
        title=None,
        preferred_llm_model=None,
        ctem_vertical="cyber",
        ctem_dashboard_layouts=None,
        ctem_dashboard_fleet_ids=None,
        is_admin=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _update(monkeypatch, user, body, visible=(MINE, BUILTIN)):
    async def _no_audit(*_args, **_kwargs):
        return None

    async def _groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    monkeypatch.setattr(settings_api, "get_user_group_ids", _groups)
    return await settings_api.update_preferences(
        body=body,
        request=SimpleNamespace(headers={}, client=None),
        user=user,
        db=_FakeDB(list(visible)),
    )


@pytest.mark.asyncio
async def test_the_choice_is_kept_in_order_without_duplicates(monkeypatch):
    user = _user()
    await _update(monkeypatch, user, UserPreferencesRequest(ctem_dashboard_fleet_ids=[MINE, BUILTIN, MINE]))
    assert user.ctem_dashboard_fleet_ids == [MINE, BUILTIN]


@pytest.mark.asyncio
async def test_a_fleet_the_person_cannot_read_is_dropped(monkeypatch):
    user = _user()
    await _update(monkeypatch, user, UserPreferencesRequest(ctem_dashboard_fleet_ids=[OTHERS, MINE, "not-a-uuid"]))
    assert user.ctem_dashboard_fleet_ids == [MINE]


@pytest.mark.asyncio
async def test_an_empty_list_hides_every_fleet_and_null_restores_the_builtins(monkeypatch):
    user = _user()
    await _update(monkeypatch, user, UserPreferencesRequest(ctem_dashboard_fleet_ids=[]))
    assert user.ctem_dashboard_fleet_ids == []
    await _update(monkeypatch, user, UserPreferencesRequest(ctem_dashboard_fleet_ids=None))
    assert user.ctem_dashboard_fleet_ids is None


@pytest.mark.asyncio
async def test_omitting_the_field_leaves_the_choice_alone(monkeypatch):
    user = _user(ctem_dashboard_fleet_ids=[MINE])
    await _update(monkeypatch, user, UserPreferencesRequest(ctem_vertical="cyber"))
    assert user.ctem_dashboard_fleet_ids == [MINE]


def test_the_list_is_bounded():
    with pytest.raises(ValidationError):
        UserPreferencesRequest(ctem_dashboard_fleet_ids=[str(uuid.uuid4()) for _ in range(MAX_DASHBOARD_FLEETS + 1)])
    with pytest.raises(ValidationError):
        UserPreferencesRequest(ctem_dashboard_fleet_ids=[""])
