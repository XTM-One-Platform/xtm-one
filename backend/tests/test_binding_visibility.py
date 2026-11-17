"""Coverage for ``shadow_invisible_bindings``.

A shared agent must never run a fixed integration binding using another
user's personal credentials. ``shadow_invisible_bindings`` drops bindings the
acting user cannot see (own + group-shared + company-managed) so the caller
can fall back to per-user resolution. These tests pin that behaviour with a
stubbed session — the query is constructed for real, the visible row set is
controlled.

Raw ``user_visible_filter`` visibility (own + group-shared +
company-managed) is not sufficient for OAuth integrations, because
group-sharing a *personal* OAuth token integration made it "visible" to
every group member without making its token usable by anyone but its
owner. ``shadow_invisible_bindings`` now applies an extra ownership check
for ``auth_type == "oauth"`` rows on top of raw visibility.
"""

from __future__ import annotations

import uuid
from typing import NamedTuple

import pytest

from app.services.integration_tools import shadow_invisible_bindings


class _Row(NamedTuple):
    """Shape of the selected-columns row (index AND attribute access, like a SQLAlchemy ``Row``)."""

    id: uuid.UUID
    auth_type: str
    user_id: uuid.UUID | None
    oauth_share_acknowledged: bool


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """Minimal AsyncSession stub returning the configured visible rows.

    ``visible_ids`` are rows the (stubbed) ``user_visible_filter`` query
    would return — own + group-shared + company-managed. ``auth_types`` and
    ``owners`` let a test mark specific ids as OAuth-owned-by-someone-else,
    to exercise the extra ownership check ``shadow_invisible_bindings``
    applies on top of raw visibility. ``acknowledged`` marks ids
    whose owner explicitly opted in to keep a shared OAuth token usable by
    others (``oauth_share_acknowledged`` — set via
    ``keep_oauth_credentials_on_share`` in ``app.api.v1.integrations``); ids
    not listed default to ``api_key`` / no owner / not acknowledged, so
    existing tests are unaffected.
    """

    def __init__(self, visible_ids, auth_types=None, owners=None, acknowledged=None):
        auth_types = auth_types or {}
        owners = owners or {}
        acknowledged = acknowledged or {}
        self._rows = [
            _Row(
                uuid.UUID(i),
                auth_types.get(i, "api_key"),
                uuid.UUID(owners[i]) if owners.get(i) else None,
                acknowledged.get(i, False),
            )
            for i in visible_ids
        ]
        self.executed = False

    async def execute(self, *_args, **_kwargs):
        self.executed = True
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_drops_invisible_binding_and_returns_provider():
    """An integration not visible to the acting user is removed and its
    provider returned for per-user fallback; visible ones stay."""
    visible_id = str(uuid.uuid4())  # e.g. a company-managed integration
    hidden_id = str(uuid.uuid4())  # the owner's private integration
    effective = {"opencti": visible_id, "github": hidden_id}
    origin = {
        "opencti": "direct_agent_binding",
        "github": "direct_agent_binding",
    }
    db = _FakeDB(visible_ids=[visible_id])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
        user_group_ids=[],
    )

    assert shadowed == ["github"]
    assert effective == {"opencti": visible_id}
    assert origin == {"opencti": "direct_agent_binding"}


@pytest.mark.asyncio
async def test_strips_hash_suffix_to_bare_provider():
    """Flattened multi-instance keys (``github#2``) shadow back to the bare
    provider so per-user resolution can re-resolve it."""
    visible_id = str(uuid.uuid4())
    hidden_id = str(uuid.uuid4())
    effective = {"github": visible_id, "github#2": hidden_id}
    origin = {
        "github": "direct_agent_binding",
        "github#2": "direct_agent_binding",
    }
    db = _FakeDB(visible_ids=[visible_id])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
    )

    assert shadowed == ["github"]
    assert effective == {"github": visible_id}


@pytest.mark.asyncio
async def test_removes_all_keys_for_same_hidden_integration(caplog):
    """Several flattened keys can reference the SAME hidden integration id.
    All such binding slots must be removed, and the audit log must report the
    actual number of slots dropped (not the count of unique integration ids)."""
    import logging

    hidden_id = str(uuid.uuid4())  # one integration, bound under two keys
    visible_id = str(uuid.uuid4())
    effective = {
        "github": hidden_id,
        "github#2": hidden_id,
        "opencti": visible_id,
    }
    origin = {k: "direct_agent_binding" for k in effective}
    db = _FakeDB(visible_ids=[visible_id])

    with caplog.at_level(logging.INFO):
        shadowed = await shadow_invisible_bindings(
            db,
            effective,
            origin,
            acting_user_id=str(uuid.uuid4()),
        )

    # Both hidden slots gone; the visible one stays.
    assert shadowed == ["github"]
    assert effective == {"opencti": visible_id}
    assert origin == {"opencti": "direct_agent_binding"}
    # The log reports 2 removed binding slots, not 1 unique integration id.
    assert any(
        "dropped 2 fixed integration binding(s)" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_noop_for_anonymous_and_empty():
    """No identity / no bindings → no query, no changes."""
    effective = {"github": str(uuid.uuid4())}
    db = _FakeDB(visible_ids=[])

    assert (
        await shadow_invisible_bindings(
            db, effective, {}, acting_user_id="anonymous"
        )
        == []
    )
    assert db.executed is False

    assert (
        await shadow_invisible_bindings(
            db, {}, {}, acting_user_id=str(uuid.uuid4())
        )
        == []
    )
    assert db.executed is False


@pytest.mark.asyncio
async def test_keeps_all_when_all_visible():
    """When every bound integration is visible, nothing is shadowed."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    effective = {"opencti": a, "github": b}
    origin = {"opencti": "direct_agent_binding", "github": "direct_agent_binding"}
    db = _FakeDB(visible_ids=[a, b])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
        user_group_ids=[],
    )

    assert shadowed == []
    assert effective == {"opencti": a, "github": b}


@pytest.mark.asyncio
async def test_drops_malformed_binding_ids_fail_closed():
    """A binding whose id is not a parseable UUID is unverifiable, so it must
    be dropped fail-closed (and its provider returned for per-user fallback)
    rather than left in ``effective_bindings`` where it would later crash tool
    resolution. No DB query is issued when every id is malformed."""
    effective = {"github": "not-a-uuid", "opencti": "also bad"}
    origin = {"github": "direct_agent_binding", "opencti": "direct_agent_binding"}
    db = _FakeDB(visible_ids=[])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
    )

    assert shadowed == ["github", "opencti"]
    assert effective == {}
    assert origin == {}
    # No candidate UUIDs → no point querying the DB.
    assert db.executed is False


@pytest.mark.asyncio
async def test_drops_only_malformed_binding_keeps_visible():
    """A mix of a malformed id and a visible canonical id: the malformed one is
    dropped fail-closed while the visible one is kept, and the DB is still
    queried for the parseable candidate."""
    visible_id = str(uuid.uuid4())
    effective = {"opencti": visible_id, "github": "garbage"}
    origin = {"opencti": "direct_agent_binding", "github": "direct_agent_binding"}
    db = _FakeDB(visible_ids=[visible_id])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
    )

    assert shadowed == ["github"]
    assert effective == {"opencti": visible_id}
    assert origin == {"opencti": "direct_agent_binding"}
    assert db.executed is True


@pytest.mark.asyncio
async def test_drops_group_shared_oauth_binding_owned_by_someone_else():
    """An OAuth integration group-shared with the acting user's group
    is "visible" per ``user_visible_filter``, but its token belongs to a
    different user — it must still be dropped, not borrowed."""
    owner_id = str(uuid.uuid4())
    acting_id = str(uuid.uuid4())
    shared_oauth_id = str(uuid.uuid4())  # owner's personal GitHub OAuth token
    company_managed_id = str(uuid.uuid4())  # unaffected: never OAuth
    effective = {"github": shared_oauth_id, "opencti": company_managed_id}
    origin = {k: "direct_agent_binding" for k in effective}
    db = _FakeDB(
        visible_ids=[shared_oauth_id, company_managed_id],
        auth_types={shared_oauth_id: "oauth"},
        owners={shared_oauth_id: owner_id},
    )

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=acting_id,
        user_group_ids=["some-shared-group"],
    )

    assert shadowed == ["github"]
    assert effective == {"opencti": company_managed_id}
    assert origin == {"opencti": "direct_agent_binding"}


@pytest.mark.asyncio
async def test_keeps_group_shared_oauth_binding_when_share_acknowledged():
    """When the owner explicitly opted in to keep their token shared
    (``oauth_share_acknowledged``), a group-shared OAuth integration IS
    usable by other group members — the whole point of that opt-in."""
    owner_id = str(uuid.uuid4())
    acting_id = str(uuid.uuid4())
    shared_oauth_id = str(uuid.uuid4())
    effective = {"github": shared_oauth_id}
    origin = {"github": "direct_agent_binding"}
    db = _FakeDB(
        visible_ids=[shared_oauth_id],
        auth_types={shared_oauth_id: "oauth"},
        owners={shared_oauth_id: owner_id},
        acknowledged={shared_oauth_id: True},
    )

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=acting_id,
        user_group_ids=["some-shared-group"],
    )

    assert shadowed == []
    assert effective == {"github": shared_oauth_id}


@pytest.mark.asyncio
async def test_keeps_oauth_binding_owned_by_acting_user():
    """An OAuth integration actually owned by the acting user is kept —
    the extra ownership check must not shadow a user's own token."""
    acting_id = str(uuid.uuid4())
    own_oauth_id = str(uuid.uuid4())
    effective = {"github": own_oauth_id}
    origin = {"github": "direct_agent_binding"}
    db = _FakeDB(
        visible_ids=[own_oauth_id],
        auth_types={own_oauth_id: "oauth"},
        owners={own_oauth_id: acting_id},
    )

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=acting_id,
    )

    assert shadowed == []
    assert effective == {"github": own_oauth_id}


@pytest.mark.asyncio
async def test_non_canonical_uuid_binding_not_shadowed():
    """A binding stored as a non-canonical UUID (no hyphens / uppercase) for a
    VISIBLE integration must NOT be dropped — visibility is compared by UUID
    value, not raw string, so the DB's canonical form still matches."""
    u = uuid.uuid4()
    non_canonical = u.hex.upper()  # 32 hex chars, no hyphens, uppercase
    effective = {"github": non_canonical}
    origin = {"github": "direct_agent_binding"}
    # The DB reports the integration as visible in its canonical form.
    db = _FakeDB(visible_ids=[str(u)])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
    )

    assert shadowed == []
    assert effective == {"github": non_canonical}


@pytest.mark.asyncio
async def test_hidden_openapi_binding_has_no_per_user_fallback():
    """Every OpenAPI integration is a different API: a hidden one is dropped,
    but "the acting user's own openapi integration" is not an equivalent, so
    the provider is not returned for per-user resolution."""
    hidden_github = str(uuid.uuid4())
    hidden_api = str(uuid.uuid4())
    visible_api = str(uuid.uuid4())
    effective = {"github": hidden_github, "openapi": hidden_api, "openapi#2": visible_api}
    origin = dict.fromkeys(effective, "direct_agent_binding")
    db = _FakeDB(visible_ids=[visible_api])

    shadowed = await shadow_invisible_bindings(
        db,
        effective,
        origin,
        acting_user_id=str(uuid.uuid4()),
    )

    assert shadowed == ["github"]
    assert effective == {"openapi#2": visible_api}
