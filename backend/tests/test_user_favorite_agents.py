"""Tests for per-user favorite agents (``users.favorite_agent_ids``).

The column is an insertion-ordered set (the UI orders the pinned block by the
active sort, not by this list), so ``PUT /settings/preferences`` must store it
verbatim minus duplicates — a double-click must not leave a second entry the
un-star cannot reach. It must also stay an explicitly-present-key update
(``model_fields_set``) so unstarring the last favorite — an empty list, which is
falsy — is not mistaken for "field omitted" and silently dropped.

Mirrors the repo's fake-session convention: the backend suite has no real-DB
harness, so the endpoint coroutine is driven directly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.settings as settings_api
from app.api.v1.schemas.settings import UserPreferencesRequest


class _AgentIdResult:
    """Stands in for the ``select(Agent.id)`` existence probe."""

    def __init__(self, ids):
        self._ids = ids

    def scalars(self):
        return self

    def all(self):
        return list(self._ids)


class _FakeDB:
    """Reports a fixed set of existing agent ids to the existence filter."""

    def __init__(self, existing_ids=None):
        # ``None`` = every id asked about exists (the common case in these
        # tests); a list pins exactly which ones do.
        self._existing = existing_ids
        self.queried = None

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def execute(self, statement):
        # The only statement the endpoint issues for favorites is the
        # ``Agent.id IN (...)`` probe; read the requested ids back off it so a
        # test can assert what was checked. SQLAlchemy may render the IN as one
        # expanding parameter (a sequence) or as one parameter per value, so
        # flatten both shapes.
        asked = []
        for param in statement.compile().params.values():
            values = param if isinstance(param, (list, tuple)) else [param]
            asked.extend(v for v in values if isinstance(v, uuid.UUID))
        self.queried = asked
        if self._existing is None:
            return _AgentIdResult(asked)
        return _AgentIdResult([a for a in asked if a in self._existing])


def _user(**overrides):
    base = dict(
        email="u@example.com",
        full_name="Test User",
        title=None,
        preferred_llm_model=None,
        favorite_agent_ids=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _update(monkeypatch, user, body: UserPreferencesRequest, db=None):
    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    return await settings_api.update_preferences(
        body=body,
        request=SimpleNamespace(headers={}, client=None),
        user=user,
        db=db or _FakeDB(),
    )


@pytest.mark.asyncio
async def test_favorites_are_stored_verbatim(monkeypatch):
    """Whatever list the client sends is what comes back — no re-sorting."""
    user = _user()
    first, second = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    await _update(
        monkeypatch,
        user,
        UserPreferencesRequest(favorite_agent_ids=[first, second]),
    )
    assert user.favorite_agent_ids == [first, second]


@pytest.mark.asyncio
async def test_favorites_are_deduplicated(monkeypatch):
    """A double-click must not leave a second entry the un-star can't reach."""
    user = _user()
    aid = "11111111-1111-1111-1111-111111111111"
    await _update(monkeypatch, user, UserPreferencesRequest(favorite_agent_ids=[aid, aid]))
    assert user.favorite_agent_ids == [aid]


@pytest.mark.asyncio
async def test_unstarring_the_last_favorite_persists_the_empty_list(monkeypatch):
    user = _user(favorite_agent_ids=["11111111-1111-1111-1111-111111111111"])
    await _update(monkeypatch, user, UserPreferencesRequest(favorite_agent_ids=[]))
    assert user.favorite_agent_ids == []


@pytest.mark.asyncio
async def test_ids_naming_no_agent_are_dropped(monkeypatch):
    """The existence filter is what makes ``purge_agent_references`` stick: a
    client holding a user cached before a delete would otherwise write the
    purged id straight back on the next star click."""
    user = _user()
    alive = uuid.UUID("11111111-1111-1111-1111-111111111111")
    deleted = uuid.UUID("22222222-2222-2222-2222-222222222222")
    db = _FakeDB(existing_ids=[alive])
    await _update(
        monkeypatch,
        user,
        UserPreferencesRequest(favorite_agent_ids=[str(deleted), str(alive)]),
        db=db,
    )
    assert user.favorite_agent_ids == [str(alive)]
    # Both ids really went through the probe — proves the assertion above is
    # the filter at work, not an empty query returning nothing.
    assert set(db.queried) == {alive, deleted}


@pytest.mark.asyncio
async def test_selection_above_the_cap_is_rejected(monkeypatch):
    """Unbounded growth would bloat the users row and every /auth/me payload."""
    user = _user()
    too_many = [str(uuid.uuid4()) for _ in range(settings_api.MAX_FAVORITE_AGENTS + 1)]
    with pytest.raises(HTTPException) as excinfo:
        await _update(monkeypatch, user, UserPreferencesRequest(favorite_agent_ids=too_many))
    assert excinfo.value.status_code == 422
    assert user.favorite_agent_ids is None  # nothing written


@pytest.mark.asyncio
async def test_non_uuid_id_is_rejected(monkeypatch):
    """Only agent ids belong here — and a junk string cannot be compared
    against a UUID column anyway."""
    user = _user()
    with pytest.raises(HTTPException) as excinfo:
        await _update(
            monkeypatch,
            user,
            UserPreferencesRequest(favorite_agent_ids=["not-a-uuid"]),
        )
    assert excinfo.value.status_code == 422
    assert user.favorite_agent_ids is None


@pytest.mark.asyncio
async def test_omitting_the_field_leaves_favorites_untouched(monkeypatch):
    """Every other preference update (theme, language, …) must not wipe them."""
    kept = ["11111111-1111-1111-1111-111111111111"]
    user = _user(favorite_agent_ids=list(kept))
    await _update(monkeypatch, user, UserPreferencesRequest(title="CTO"))
    assert user.favorite_agent_ids == kept


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
