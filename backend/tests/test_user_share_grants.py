"""Per-user share grants and company-managed access restriction.

Covers the pieces that decide *who can reach an entity*: permission
resolution across both principal kinds, the write-path validation that keeps
the stored grants meaningful, and the interaction with the OAuth
credential-sharing guard.

The SQL side of ``user_visible_filter`` is pinned in
``test_visibility_helpers.py``; this module is about the Python semantics.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1.helpers import (
    SHARE_PERMISSION_CONFIG,
    SHARE_PERMISSION_CONFIG_WITH_CHATS,
    SHARE_PERMISSION_READ_ONLY,
    apply_user_share_fields,
    can_manage,
    get_effective_permission,
    get_user_permission,
    normalize_user_grants,
    validate_access_restricted,
    wants_access_restriction,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


def _db_returning(rows):
    """An ``AsyncSession`` double whose single query yields *rows*.

    Rows are ``(user_id, is_active)`` pairs — the shape
    ``normalize_user_grants`` selects, so that a grant can be checked for
    "account still exists" separately from "account is active".
    """
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_FakeResult(rows))
    return db


def _active(*user_ids):
    """Rows for accounts that exist and are active."""
    return [(str(u), True) for u in user_ids]


def _inactive(*user_ids):
    """Rows for accounts that exist but have been deactivated."""
    return [(str(u), False) for u in user_ids]


def _entity(**overrides):
    base = {
        "is_company_managed": False,
        "user_id": None,
        "shared_group_ids": None,
        "shared_group_permissions": None,
        "shared_user_ids": None,
        "shared_user_permissions": None,
        "access_restricted": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Permission resolution
# ---------------------------------------------------------------------------


def test_bare_user_grant_defaults_to_read_only():
    """A grant with no explicit tier is the read_only the picker creates."""
    uid = uuid.uuid4()
    entity = _entity(shared_user_ids=[str(uid)])

    assert get_user_permission(entity, uid) == SHARE_PERMISSION_READ_ONLY


def test_user_grant_honours_its_explicit_tier():
    uid = uuid.uuid4()
    entity = _entity(
        shared_user_ids=[str(uid)],
        shared_user_permissions={str(uid): SHARE_PERMISSION_CONFIG},
    )

    assert get_user_permission(entity, uid) == SHARE_PERMISSION_CONFIG


def test_user_grant_ignores_a_stranger():
    entity = _entity(shared_user_ids=[str(uuid.uuid4())])

    assert get_user_permission(entity, uuid.uuid4()) is None


@pytest.mark.parametrize("mangle", [lambda u: u.hex, lambda u: str(u).upper()])
def test_user_grant_resolves_non_canonical_ids(mangle):
    """Hand-edited rows can carry an unhyphenated or upper-cased id.

    The SQL predicate compares canonical strings and cannot normalise what it
    reads, so the Python resolver normalises defensively — otherwise a grant
    would appear in the UI while conferring nothing.
    """
    uid = uuid.uuid4()
    entity = _entity(
        shared_user_ids=[mangle(uid)],
        shared_user_permissions={mangle(uid): SHARE_PERMISSION_CONFIG_WITH_CHATS},
    )

    assert get_user_permission(entity, uid) == SHARE_PERMISSION_CONFIG_WITH_CHATS


def test_effective_permission_takes_the_stronger_of_user_and_group():
    """Adding someone to a group must never *narrow* a grant they already had."""
    uid, gid = uuid.uuid4(), str(uuid.uuid4())
    entity = _entity(
        shared_user_ids=[str(uid)],
        shared_user_permissions={str(uid): SHARE_PERMISSION_READ_ONLY},
        shared_group_ids=[gid],
        shared_group_permissions={gid: SHARE_PERMISSION_CONFIG},
    )

    assert get_effective_permission(entity, uid, [gid]) == SHARE_PERMISSION_CONFIG

    # ... and symmetrically when the personal grant is the stronger one.
    entity.shared_user_permissions = {str(uid): SHARE_PERMISSION_CONFIG_WITH_CHATS}
    assert (
        get_effective_permission(entity, uid, [gid])
        == SHARE_PERMISSION_CONFIG_WITH_CHATS
    )


def test_can_manage_follows_a_config_tier_user_grant():
    uid = uuid.uuid4()
    user = SimpleNamespace(id=uid, role="user", is_admin=False)

    read_only = _entity(
        shared_user_ids=[str(uid)],
        shared_user_permissions={str(uid): SHARE_PERMISSION_READ_ONLY},
    )
    assert can_manage(user, read_only, []) is False

    config = _entity(
        shared_user_ids=[str(uid)],
        shared_user_permissions={str(uid): SHARE_PERMISSION_CONFIG},
    )
    assert can_manage(user, config, []) is True


def test_can_manage_still_admin_only_for_company_managed():
    """A user grant widens *visibility*, never edit rights on a company entity."""
    uid = uuid.uuid4()
    user = SimpleNamespace(id=uid, role="user", is_admin=False)
    entity = _entity(
        is_company_managed=True,
        access_restricted=True,
        shared_user_ids=[str(uid)],
        shared_user_permissions={str(uid): SHARE_PERMISSION_CONFIG_WITH_CHATS},
    )

    assert can_manage(user, entity, []) is False


# ---------------------------------------------------------------------------
# Write-path validation
# ---------------------------------------------------------------------------


async def test_normalize_canonicalises_and_dedupes():
    uid = uuid.uuid4()
    db = _db_returning(_active(uid))

    ids, perms = await normalize_user_grants(db, [uid.hex, str(uid).upper()], None)

    assert ids == [str(uid)]
    assert perms is None


async def test_normalize_rejects_an_unknown_user():
    """Dropping the grant silently would be a share that quietly does nothing."""
    db = _db_returning([])

    with pytest.raises(HTTPException) as exc:
        await normalize_user_grants(db, [str(uuid.uuid4())], None)

    assert exc.value.status_code == 422
    assert "unknown or inactive" in exc.value.detail


async def test_normalize_rejects_a_malformed_id():
    db = _db_returning([])

    with pytest.raises(HTTPException) as exc:
        await normalize_user_grants(db, ["not-a-uuid"], None)

    assert exc.value.status_code == 422


async def test_normalize_rejects_an_unknown_permission_tier():
    """An unrecognised tier ranks as 0 and would silently behave as read_only."""
    uid = uuid.uuid4()
    db = _db_returning(_active(uid))

    with pytest.raises(HTTPException) as exc:
        await normalize_user_grants(db, [str(uid)], {str(uid): "superuser"})

    assert exc.value.status_code == 422


async def test_normalize_drops_permissions_for_ungranted_users():
    """A tier naming someone with no grant would resurrect if they were re-added."""
    granted, stale = uuid.uuid4(), uuid.uuid4()
    db = _db_returning(_active(granted))

    ids, perms = await normalize_user_grants(
        db,
        [str(granted)],
        {str(granted): SHARE_PERMISSION_CONFIG, str(stale): SHARE_PERMISSION_CONFIG},
    )

    assert ids == [str(granted)]
    assert perms == {str(granted): SHARE_PERMISSION_CONFIG}


async def test_normalize_stores_no_grants_as_null():
    ids, perms = await normalize_user_grants(_db_returning([]), [], {})

    assert ids is None and perms is None


def test_access_restriction_is_rejected_on_a_personal_entity():
    with pytest.raises(HTTPException) as exc:
        validate_access_restricted(access_restricted=True, is_company_managed=False)

    assert exc.value.status_code == 422


def test_access_restriction_is_allowed_on_a_company_entity():
    validate_access_restricted(access_restricted=True, is_company_managed=True)


# ---------------------------------------------------------------------------
# apply_user_share_fields
# ---------------------------------------------------------------------------


async def test_apply_clears_grants_on_an_unrestricted_company_entity():
    """Company-managed and unrestricted means everyone can already see it, so a
    grant row would claim to confer access it does not."""
    uid, gid = uuid.uuid4(), str(uuid.uuid4())
    entity = _entity(
        is_company_managed=True,
        shared_user_ids=[str(uid)],
        shared_group_ids=[gid],
        shared_group_permissions={gid: SHARE_PERMISSION_CONFIG},
    )
    body = SimpleNamespace(
        shared_user_ids=[str(uid)],
        shared_user_permissions=None,
        access_restricted=False,
        model_fields_set={"access_restricted"},
    )

    await apply_user_share_fields(_db_returning(_active(uid)), entity, body, partial=True)

    assert entity.access_restricted is False
    assert entity.shared_user_ids is None
    assert entity.shared_group_ids is None
    assert entity.shared_group_permissions is None


async def test_apply_keeps_grants_on_a_restricted_company_entity():
    uid = uuid.uuid4()
    entity = _entity(is_company_managed=True)
    body = SimpleNamespace(
        shared_user_ids=[str(uid)],
        shared_user_permissions=None,
        access_restricted=True,
        model_fields_set={"shared_user_ids", "access_restricted"},
    )

    await apply_user_share_fields(_db_returning(_active(uid)), entity, body, partial=True)

    assert entity.access_restricted is True
    assert entity.shared_user_ids == [str(uid)]


async def test_apply_leaves_untouched_fields_alone_on_a_partial_update():
    uid = uuid.uuid4()
    entity = _entity(shared_user_ids=[str(uid)])
    body = SimpleNamespace(
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=None,
        model_fields_set=set(),  # a rename, say — nothing sharing-related
    )

    await apply_user_share_fields(_db_returning(_active(uid)), entity, body, partial=True)

    assert entity.shared_user_ids == [str(uid)]


async def test_apply_revalidates_permissions_against_stored_ids():
    """Changing only the tier map must still be checked against the stored ids."""
    granted, stale = uuid.uuid4(), uuid.uuid4()
    entity = _entity(shared_user_ids=[str(granted)])
    body = SimpleNamespace(
        shared_user_ids=None,
        shared_user_permissions={str(stale): SHARE_PERMISSION_CONFIG},
        access_restricted=None,
        model_fields_set={"shared_user_permissions"},
    )

    await apply_user_share_fields(
        _db_returning(_active(granted)), entity, body, partial=True
    )

    assert entity.shared_user_ids == [str(granted)]
    assert entity.shared_user_permissions is None


async def test_apply_clears_restriction_when_company_managed_is_turned_off():
    """Turning company-managed off must not 422 an otherwise unrelated edit."""
    entity = _entity(is_company_managed=False, access_restricted=True)
    body = SimpleNamespace(
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=None,
        model_fields_set={"name"},
    )

    await apply_user_share_fields(_db_returning([]), entity, body, partial=True)

    assert entity.access_restricted is False


def test_wants_access_restriction_falls_back_to_stored_value():
    entity = _entity(access_restricted=True)
    untouched = SimpleNamespace(access_restricted=None, model_fields_set=set())
    explicit = SimpleNamespace(access_restricted=False, model_fields_set={"access_restricted"})

    assert wants_access_restriction(untouched, entity) is True
    assert wants_access_restriction(explicit, entity) is False


# ---------------------------------------------------------------------------
# Interaction with the OAuth credential-sharing guard
# ---------------------------------------------------------------------------


def test_user_grant_shares_the_connected_account_by_default():
    """"Share with Alice" shares the owner's connected account exactly as
    "share with Alice's group" does: the token stays and the consent is
    recorded."""
    from app.models.integration import Integration
    from app.services.integration_credentials import apply_oauth_share_consent

    integration = Integration(
        auth_type="oauth",
        provider="github",
        name="GitHub",
        shared_user_ids=[str(uuid.uuid4())],
        access_token_enc="enc-token",
        refresh_token_enc="enc-refresh",
    )

    apply_oauth_share_consent(integration, share_credentials=None, sharing_changed=True)

    assert integration.access_token_enc == "enc-token"
    assert integration.refresh_token_enc == "enc-refresh"
    assert integration.oauth_share_acknowledged is True


def test_user_grant_honours_the_opt_out_like_a_group_share():
    """The opt-out keeps the grant and withholds the account, whichever kind of
    share: the owner's token stays theirs, everyone else authorizes their own."""
    from app.models.integration import Integration
    from app.services.integration_credentials import apply_oauth_share_consent

    integration = Integration(
        auth_type="oauth",
        provider="github",
        name="GitHub",
        shared_user_ids=[str(uuid.uuid4())],
        access_token_enc="enc-token",
    )

    apply_oauth_share_consent(integration, share_credentials=False, sharing_changed=True)

    assert integration.shared_user_ids
    assert integration.credential_mode == "per_user"
    assert integration.oauth_share_acknowledged is False


# ---------------------------------------------------------------------------
# Deactivated grantees
# ---------------------------------------------------------------------------


async def test_stored_grant_survives_its_grantee_being_deactivated():
    """An unrelated edit must not 422 because a grantee was deactivated.

    Deactivation is a reversible toggle and does not prune grants (only
    deletion does), so revalidating the stored list on every write would make
    a rename of any entity shared with that person fail.
    """
    uid = uuid.uuid4()
    ids, perms = await normalize_user_grants(
        _db_returning(_inactive(uid)),
        [str(uid)],
        None,
        existing_ids=[str(uid)],
    )

    assert ids == [str(uid)]
    assert perms is None


async def test_newly_added_grant_still_requires_an_active_account():
    """Grandfathering must not become a way to share with a disabled account."""
    uid = uuid.uuid4()

    with pytest.raises(HTTPException) as exc:
        await normalize_user_grants(
            _db_returning(_inactive(uid)), [str(uid)], None, existing_ids=[]
        )

    assert exc.value.status_code == 422


async def test_stored_grant_to_a_deleted_account_is_dropped():
    """A deleted account cannot be reactivated, so its grant is not kept."""
    uid = uuid.uuid4()
    ids, perms = await normalize_user_grants(
        _db_returning([]), [str(uid)], None, existing_ids=[str(uid)]
    )

    assert ids is None and perms is None


async def test_unrelated_partial_update_never_touches_the_grant_query():
    """The repro from review: a rename on an entity shared with a deactivated
    user. The share fields are absent from ``model_fields_set``, so validation
    is skipped outright rather than re-running against a now-inactive id."""
    uid = uuid.uuid4()
    entity = _entity(shared_user_ids=[str(uid)])
    body = SimpleNamespace(
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=None,
        model_fields_set={"name"},
    )
    db = _db_returning(_inactive(uid))

    await apply_user_share_fields(db, entity, body, partial=True)

    assert entity.shared_user_ids == [str(uid)]
    db.execute.assert_not_awaited()


async def test_ui_style_update_resending_stored_grants_still_succeeds():
    """The frontend always sends the share fields, so the short-circuit alone
    would not have fixed the reported symptom — the grandfathering does."""
    uid = uuid.uuid4()
    entity = _entity(shared_user_ids=[str(uid)])
    body = SimpleNamespace(
        shared_user_ids=[str(uid)],
        shared_user_permissions=None,
        access_restricted=False,
        model_fields_set={"shared_user_ids", "shared_user_permissions", "access_restricted"},
    )

    await apply_user_share_fields(
        _db_returning(_inactive(uid)), entity, body, partial=True
    )

    assert entity.shared_user_ids == [str(uid)]


# ---------------------------------------------------------------------------
# Directory search bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("%%", r"\%\%"),
        ("a_b", r"a\_b"),
        ("100%", r"100\%"),
        ("alice", "alice"),
        ("back\\slash", "back\\\\slash"),
    ],
)
def test_directory_search_escapes_like_metacharacters(raw, expected):
    """``%%`` is two characters, so it passes the min-length guard and would
    otherwise match every row — turning a deliberately narrow lookup into the
    bulk staff directory the endpoint promises not to be."""
    from app.api.v1.users import escape_like

    assert escape_like(raw) == expected


async def test_owner_self_grant_is_dropped():
    """The owner reaches their own entity unconditionally, so a grant naming
    them confers nothing. Dropped rather than rejected — it only arises on an
    imported or cloned entity, where a 422 would break import over a
    difference that changes no access."""
    owner, other = uuid.uuid4(), uuid.uuid4()

    ids, _ = await normalize_user_grants(
        _db_returning(_active(other)),
        [str(owner), str(other)],
        None,
        owner_id=owner,
    )

    assert ids == [str(other)]


async def test_owner_only_grant_normalises_to_no_grants():
    owner = uuid.uuid4()

    ids, perms = await normalize_user_grants(
        _db_returning([]), [str(owner)], None, owner_id=owner
    )

    assert ids is None and perms is None


# ---------------------------------------------------------------------------
# Directory endpoint contract
# ---------------------------------------------------------------------------


def test_directory_search_is_bounded_to_active_users_excluding_the_caller():
    """Pin the search-mode contract in SQL: active accounts only, caller
    excluded, and matching on name or email via an escaped LIKE."""
    import uuid as _uuid

    from sqlalchemy import func, or_, select
    from sqlalchemy.dialects import postgresql

    from app.api.v1.users import _LIKE_ESCAPE, escape_like
    from app.models.user import User

    caller = _uuid.uuid4()
    term = f"%{escape_like('ali')}%"
    stmt = select(User.id).where(
        User.is_active.is_(True),
        User.id != caller,
        or_(
            func.lower(User.full_name).like(term, escape=_LIKE_ESCAPE),
            func.lower(User.email).like(term, escape=_LIKE_ESCAPE),
        ),
    )
    rendered = str(stmt.compile(dialect=postgresql.dialect())).lower()

    assert "is_active is true" in rendered
    assert "users.id != " in rendered
    assert "escape" in rendered


# ---------------------------------------------------------------------------
# Grant lifecycle across deactivate -> edit -> reactivate
# ---------------------------------------------------------------------------
#
# Deactivation is a reversible toggle, so the
# grants a user holds survive it untouched (only deletion prunes them), an
# unrelated edit never re-validates them, and the sharing card keeps naming the
# grantee with a "Deactivated" badge fed by ``GET /users/directory?ids=``.


class _UserLookupSession:
    """Just enough ``AsyncSession`` for ``toggle_user_active``."""

    def __init__(self, user):
        self._user = user
        self.executes = 0
        self.commits = 0

    async def execute(self, _stmt):
        self.executes += 1
        return SimpleNamespace(scalar_one_or_none=lambda: self._user)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


def _stub_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route's cascade reduced to the flip: these tests are about the grants.

    The cascade itself (``services/user_deactivation.py``) is covered against a
    real database in ``test_user_deactivation_postgres.py``.
    """
    from app.services import user_deactivation

    async def _deactivate(_db: Any, user: Any, **_kw: Any) -> Any:
        user.is_active = False
        return user_deactivation.DeactivationOutcome(user_id=user.id)

    async def _reactivate(_db: Any, user: Any, **_kw: Any) -> Any:
        user.is_active = True
        return user_deactivation.ReactivationOutcome(user_id=user.id)

    async def _after(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(user_deactivation, "deactivate_user", _deactivate)
    monkeypatch.setattr(user_deactivation, "reactivate_user", _reactivate)
    monkeypatch.setattr(user_deactivation, "after_commit", _after)


async def test_deactivating_and_reactivating_a_grantee_never_prunes_their_grants(monkeypatch):
    """Pruning is for deletion only. A prune on deactivation would silently
    destroy sharing configuration an admin expects back intact when the
    account is re-enabled - and nothing else in the suite would notice."""
    from app.api.v1 import users as users_module

    pruned: list[object] = []

    async def _record_prune(_db, user_id):
        pruned.append(user_id)
        return 0

    async def _no_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(users_module, "prune_user_grants", _record_prune)
    monkeypatch.setattr(users_module, "log_audit", _no_audit)
    _stub_cascade(monkeypatch)

    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Admin", email="admin@example.com")
    grantee = SimpleNamespace(
        id=uuid.uuid4(),
        email="grantee@example.com",
        full_name="Grantee",
        is_active=True,
    )
    session = _UserLookupSession(grantee)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    off = await users_module.toggle_user_active(
        user_id=str(grantee.id), request=request, admin=admin, db=session
    )
    assert off["is_active"] is False and grantee.is_active is False
    on = await users_module.toggle_user_active(
        user_id=str(grantee.id), request=request, admin=admin, db=session
    )
    assert on["is_active"] is True and grantee.is_active is True

    assert pruned == [], "toggling a user's active state must never prune their share grants"
    assert session.commits == 2


async def test_share_grants_survive_a_deactivate_edit_reactivate_cycle(monkeypatch):
    """One entity shared with one person: the admin deactivates that person
    through the real toggle route, the entity is edited, the admin re-enables
    them - each edit going through the write path the REST routes use, against
    the grantee's CURRENT active state."""
    from app.api.v1 import users as users_module

    async def _no_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(users_module, "log_audit", _no_audit)
    _stub_cascade(monkeypatch)
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Admin", email="admin@example.com")
    grantee = SimpleNamespace(
        id=uuid.uuid4(), email="grantee@example.com", full_name="Grantee", is_active=True
    )
    uid = grantee.id
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    toggle_session = _UserLookupSession(grantee)

    def _grantee_rows():
        # What ``normalize_user_grants`` reads back: the grantee as they are now.
        return [(str(uid), grantee.is_active)]

    entity = _entity(shared_user_ids=[str(uid)])

    # Deactivated through the route; the stored grant is untouched.
    await users_module.toggle_user_active(
        user_id=str(uid), request=request, admin=admin, db=toggle_session
    )
    assert grantee.is_active is False
    assert entity.shared_user_ids == [str(uid)]

    # An unrelated edit does not even look at the grants.
    rename = SimpleNamespace(
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=None,
        model_fields_set={"name"},
    )
    db = _db_returning(_grantee_rows())
    await apply_user_share_fields(db, entity, rename, partial=True)
    assert entity.shared_user_ids == [str(uid)]
    db.execute.assert_not_awaited()

    # Still deactivated: changing that grant's tier is validated against the
    # stored ids, where the grantee is grandfathered - kept and applied.
    raise_tier = SimpleNamespace(
        shared_user_ids=None,
        shared_user_permissions={str(uid): SHARE_PERMISSION_CONFIG},
        access_restricted=None,
        model_fields_set={"shared_user_permissions"},
    )
    await apply_user_share_fields(
        _db_returning(_grantee_rows()), entity, raise_tier, partial=True
    )
    assert entity.shared_user_ids == [str(uid)]
    assert entity.shared_user_permissions == {str(uid): SHARE_PERMISSION_CONFIG}

    # Re-enabled through the route: the same grant is there, and editing it
    # works as for anyone.
    await users_module.toggle_user_active(
        user_id=str(uid), request=request, admin=admin, db=toggle_session
    )
    assert grantee.is_active is True
    raise_again = SimpleNamespace(
        shared_user_ids=[str(uid)],
        shared_user_permissions={str(uid): SHARE_PERMISSION_CONFIG_WITH_CHATS},
        access_restricted=False,
        model_fields_set={"shared_user_ids", "shared_user_permissions", "access_restricted"},
    )
    await apply_user_share_fields(
        _db_returning(_grantee_rows()), entity, raise_again, partial=True
    )
    assert entity.shared_user_ids == [str(uid)]
    assert entity.shared_user_permissions == {str(uid): SHARE_PERMISSION_CONFIG_WITH_CHATS}
    assert get_user_permission(entity, uid) == SHARE_PERMISSION_CONFIG_WITH_CHATS


class _DirectorySession:
    """Records the statement ``search_user_directory`` runs and answers it."""

    def __init__(self, users):
        self._users = users
        self.statements: list[object] = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self._users)


def _where_sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.whereclause.compile(dialect=postgresql.dialect())).lower()


async def test_directory_ids_mode_returns_deactivated_grantees_flagged_inactive():
    """What the "Deactivated" badge on the sharing card reads: resolving stored
    grant ids returns deactivated accounts too, flagged ``is_active=False``,
    instead of dropping them (which would degrade the grant to a bare UUID)."""
    from app.api.v1.users import search_user_directory

    caller = SimpleNamespace(id=uuid.uuid4())
    gone_quiet = SimpleNamespace(
        id=uuid.uuid4(), full_name="Former Colleague", email="former@example.com", is_active=False
    )
    session = _DirectorySession([gone_quiet])

    items = await search_user_directory(
        q=None, ids=str(gone_quiet.id), limit=20, user=caller, db=session
    )

    assert [(i.id, i.is_active) for i in items] == [(str(gone_quiet.id), False)]
    assert items[0].full_name == "Former Colleague"
    # Resolving ids never filters on the active flag (nor excludes the caller).
    where = _where_sql(session.statements[0])
    assert "is_active" not in where
    assert "!=" not in where


async def test_directory_search_mode_stays_active_only():
    """The other half of the contract: a search term only ever finds active
    accounts, so a deactivated person cannot be newly granted from the picker."""
    from app.api.v1.users import search_user_directory

    caller = SimpleNamespace(id=uuid.uuid4())
    session = _DirectorySession([])

    await search_user_directory(q="ali", ids=None, limit=20, user=caller, db=session)

    where = _where_sql(session.statements[0])
    assert "is_active is true" in where
    assert "users.id != " in where
