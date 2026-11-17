"""The chat sidebar's expanded workspace groups (``users.chat_sidebar_expanded_workspaces``, #9179).

A per-user UI preference written whole through ``PUT /settings/preferences``:
duplicates dropped in first-seen order, an empty list stored as an empty list
(every group collapsed by choice), an explicit null forgets the choice, and
omitting the field leaves it alone. The list and each key are bounded so a
client cannot grow the ``users`` row (and every ``GET /auth/me``) without limit.

The route coroutine is driven directly, like ``test_user_favorite_agents.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.api.v1.settings as settings_api
from app.api.v1.auth import UserResponse
from app.api.v1.schemas.settings import (
    MAX_SIDEBAR_EXPANDED_GROUPS,
    MAX_SIDEBAR_GROUP_KEY_LENGTH,
    UserPreferencesRequest,
)
from app.models.user import User

WS_A = "11111111-1111-1111-1111-111111111111"
WS_B = "22222222-2222-2222-2222-222222222222"


class _FakeDB:
    async def execute(self, statement):
        raise AssertionError("the expanded groups are written whole: nothing to read")

    async def flush(self):
        return None

    async def commit(self):
        return None


def _user(**overrides):
    base = dict(
        email="u@example.com",
        full_name="Test User",
        title=None,
        preferred_llm_model=None,
        chat_sidebar_expanded_workspaces=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _update(monkeypatch, user, body: UserPreferencesRequest):
    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    return await settings_api.update_preferences(
        body=body,
        request=SimpleNamespace(headers={}, client=None),
        user=user,
        db=_FakeDB(),
    )


def test_the_column_is_mapped_nullable_with_no_default():
    column = User.__table__.c.chat_sidebar_expanded_workspaces
    assert column.nullable
    assert column.server_default is None


def test_auth_me_carries_the_field():
    assert "chat_sidebar_expanded_workspaces" in UserResponse.model_fields


@pytest.mark.asyncio
async def test_the_groups_are_stored_in_order_without_duplicates(monkeypatch):
    user = _user()
    await _update(
        monkeypatch,
        user,
        UserPreferencesRequest(chat_sidebar_expanded_workspaces=[WS_B, WS_A, WS_B]),
    )
    assert user.chat_sidebar_expanded_workspaces == [WS_B, WS_A]


@pytest.mark.asyncio
async def test_an_empty_list_is_kept_and_null_forgets_the_choice(monkeypatch):
    user = _user(chat_sidebar_expanded_workspaces=[WS_A])
    await _update(monkeypatch, user, UserPreferencesRequest(chat_sidebar_expanded_workspaces=[]))
    assert user.chat_sidebar_expanded_workspaces == []
    await _update(monkeypatch, user, UserPreferencesRequest(chat_sidebar_expanded_workspaces=None))
    assert user.chat_sidebar_expanded_workspaces is None


@pytest.mark.asyncio
async def test_omitting_the_field_leaves_it_alone(monkeypatch):
    user = _user(chat_sidebar_expanded_workspaces=[WS_A])
    await _update(monkeypatch, user, UserPreferencesRequest(full_name="Renamed"))
    assert user.chat_sidebar_expanded_workspaces == [WS_A]


def test_the_list_and_its_keys_are_bounded():
    with pytest.raises(ValidationError):
        UserPreferencesRequest(
            chat_sidebar_expanded_workspaces=[f"ws-{i}" for i in range(MAX_SIDEBAR_EXPANDED_GROUPS + 1)]
        )
    with pytest.raises(ValidationError):
        UserPreferencesRequest(chat_sidebar_expanded_workspaces=["x" * (MAX_SIDEBAR_GROUP_KEY_LENGTH + 1)])
    with pytest.raises(ValidationError):
        UserPreferencesRequest(chat_sidebar_expanded_workspaces=[""])
