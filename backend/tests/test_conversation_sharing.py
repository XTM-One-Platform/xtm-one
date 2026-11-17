"""Tests for conversation sharing.

The feature keeps RBAC and widens it deliberately: an owner may grant *read*
to named users, to groups, or to "anyone on the platform with the link" — and
to nobody else. What is worth pinning here is not that the happy path works
but that the refusals hold, because a regression in this file does not break a
page, it hands one user's conversation to another account.

The access matrix under test:

===========================  =========================================
Requester                    Outcome
===========================  =========================================
Owner                        ``owner``
Named user grant             ``read``
Member of a granted group    ``read``
``link_access="org_read"``   ``read`` (any authenticated user)
Stranger, private            ``None`` -> 404, never 403
Admin, no grant              ``None`` — no ambient admin read
===========================  =========================================

No database is required: the pop-style fake session mirrors each handler's
query order, the same shape ``test_conversation_files_panel.py`` uses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.chat import (
    CONVERSATION_ACCESS_OWNER,
    CONVERSATION_ACCESS_READ,
    LINK_ACCESS_ORG_READ,
    LINK_ACCESS_PRIVATE,
    SHARE_TARGET_MIN_QUERY_LENGTH,
    ConversationShareItem,
    CreateConversationShareRequest,
    UpdateLinkAccessRequest,
    _resolve_owned_conversation,
    conversation_access,
    create_conversation_share,
    delete_conversation_share,
    list_conversation_shares,
    resolve_conversation_for_read,
    set_conversation_link_access,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, *, scalar=None, rows=None, mappings=None):
        self._scalar = scalar
        self._rows = rows or []
        self._mappings = mappings or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)

    def __iter__(self):
        return iter(self._mappings)


class _FakeDB:
    """Pop-style fake session: each ``execute`` returns the next queued result."""

    def __init__(self, results):
        self._queue = list(results)
        self.added: list[object] = []
        self.deleted: list[object] = []

    async def execute(self, _statement, *_args, **_kwargs):
        if not self._queue:
            raise AssertionError("unexpected extra DB query")
        return self._queue.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None


def _user(uid=None, role="user", name="Ada Lovelace", email="ada@example.test"):
    return SimpleNamespace(
        id=uid or uuid.uuid4(),
        role=role,
        full_name=name,
        email=email,
        is_active=True,
    )


def _conversation(owner_id, *, title="Investigation", link_access=LINK_ACCESS_PRIVATE):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        title=title,
        link_access=link_access,
    )


def _share_row(*, user_id=None, group_id=None, conversation_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation_id or uuid.uuid4(),
        shared_with_user_id=user_id,
        shared_with_group_id=group_id,
        permission="read",
        created_by=uuid.uuid4(),
        created_at=datetime(2026, 7, 30, 9, 0, tzinfo=UTC),
    )


@pytest.fixture(autouse=True)
def _silence_audit(monkeypatch):
    """Audit writes are asserted explicitly where they matter, stubbed elsewhere."""
    import app.services.audit as audit_mod

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(audit_mod, "log_audit", _noop)


# ---------------------------------------------------------------------------
# conversation_access — the access matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_gets_owner_access_without_touching_the_share_table():
    """The owner check is a field comparison — it must not cost a query.

    This runs on every read of every conversation, so a stray share lookup on
    the hot path would be a real regression, not a style point.
    """
    owner = _user()
    conv = _conversation(owner.id)
    db = _FakeDB([])  # any query at all raises

    assert await conversation_access(db, owner, conv) == CONVERSATION_ACCESS_OWNER


@pytest.mark.asyncio
async def test_named_user_grant_gives_read():
    recipient = _user()
    conv = _conversation(uuid.uuid4())
    db = _FakeDB([_Result(scalar=uuid.uuid4())])  # share probe hits

    assert await conversation_access(db, recipient, conv) == CONVERSATION_ACCESS_READ


@pytest.mark.asyncio
async def test_org_read_link_gives_read_to_any_authenticated_user():
    """The tier that makes the URL pasteable into Teams.

    Still authenticated, still revocable — the reader is a named account, not
    an anonymous holder of a high-entropy id.
    """
    stranger = _user()
    conv = _conversation(uuid.uuid4(), link_access=LINK_ACCESS_ORG_READ)
    db = _FakeDB([_Result(scalar=None)])  # no named grant

    assert await conversation_access(db, stranger, conv) == CONVERSATION_ACCESS_READ


@pytest.mark.asyncio
async def test_stranger_on_a_private_conversation_gets_nothing():
    stranger = _user()
    conv = _conversation(uuid.uuid4())
    db = _FakeDB([_Result(scalar=None)])

    assert await conversation_access(db, stranger, conv) is None


@pytest.mark.asyncio
async def test_admin_gets_no_ambient_read():
    """Admins are not on the access list, deliberately.

    Ambient admin read would silently defeat the audited break-glass gate
    put in front of another user's conversation files: an admin could
    read the transcript with no record, then list its files with one. The
    admin path stays break-glass, which records who looked.
    """
    admin = _user(role="admin")
    conv = _conversation(uuid.uuid4())
    db = _FakeDB([_Result(scalar=None)])

    assert await conversation_access(db, admin, conv) is None


@pytest.mark.asyncio
async def test_revoked_share_stops_granting_immediately():
    """No token, no lease, no cache — revocation is the absence of a row."""
    recipient = _user()
    conv = _conversation(uuid.uuid4())

    granted = _FakeDB([_Result(scalar=uuid.uuid4())])
    assert await conversation_access(granted, recipient, conv) == (
        CONVERSATION_ACCESS_READ
    )

    revoked = _FakeDB([_Result(scalar=None)])
    assert await conversation_access(revoked, recipient, conv) is None


# ---------------------------------------------------------------------------
# resolve_conversation_for_read — the 404 contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refused_read_is_404_not_403():
    """A 403 would confirm the id exists.

    That turns any read endpoint into an oracle for enumerating other
    people's conversations by walking uuids, which is exactly what the
    indistinguishable-404 convention exists to prevent.
    """
    stranger = _user()
    conv = _conversation(uuid.uuid4())
    db = _FakeDB([_Result(scalar=conv), _Result(scalar=None)])

    with pytest.raises(HTTPException) as exc:
        await resolve_conversation_for_read(str(conv.id), stranger, db)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Conversation not found"


@pytest.mark.asyncio
async def test_missing_conversation_is_the_same_404():
    stranger = _user()
    db = _FakeDB([_Result(scalar=None)])

    with pytest.raises(HTTPException) as exc:
        await resolve_conversation_for_read(str(uuid.uuid4()), stranger, db)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Conversation not found"


@pytest.mark.asyncio
async def test_read_resolver_reports_the_level_it_granted():
    """Callers vary the *response* on this; they must not re-derive it."""
    recipient = _user()
    conv = _conversation(uuid.uuid4())
    db = _FakeDB([_Result(scalar=conv), _Result(scalar=uuid.uuid4())])

    _resolved, access = await resolve_conversation_for_read(str(conv.id), recipient, db)
    assert access == CONVERSATION_ACCESS_READ


# ---------------------------------------------------------------------------
# Management endpoints — owner-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_share_management_is_owner_only_even_for_admins():
    """Managing who can read is not a shareable or delegable capability.

    A recipient who could re-share would make the owner's revocation
    meaningless; an admin editing someone's share list is a support action
    nobody asked for, and the break-glass path already covers legitimate
    admin need.
    """
    admin = _user(role="admin")
    conv = _conversation(uuid.uuid4())
    # The owner-scoped SELECT finds nothing for a non-owner.
    db = _FakeDB([_Result(scalar=None)])

    with pytest.raises(HTTPException) as exc:
        await _resolve_owned_conversation(str(conv.id), admin, db)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_sharing_with_yourself_is_rejected():
    owner = _user()
    conv = _conversation(owner.id)
    db = _FakeDB([_Result(scalar=conv)])

    with pytest.raises(HTTPException) as exc:
        await create_conversation_share(
            conversation_id=str(conv.id),
            body=CreateConversationShareRequest(user_id=str(owner.id)),
            request=None,
            user=owner,
            db=db,
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_sharing_with_an_inactive_user_is_rejected():
    """A grant to a deactivated account grants nothing today, but would come
    back to life on reactivation — which is not what the owner consented to."""
    owner = _user()
    conv = _conversation(owner.id)
    # conversation lookup, then the active-user lookup finding nothing
    db = _FakeDB([_Result(scalar=conv), _Result(scalar=None)])

    with pytest.raises(HTTPException) as exc:
        await create_conversation_share(
            conversation_id=str(conv.id),
            body=CreateConversationShareRequest(user_id=str(uuid.uuid4())),
            request=None,
            user=owner,
            db=db,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "User not found"


@pytest.mark.asyncio
async def test_resharing_an_existing_grant_is_idempotent():
    """A double-click must not 409 — the owner's intent is already satisfied."""
    owner = _user()
    conv = _conversation(owner.id)
    target_id = uuid.uuid4()
    db = _FakeDB(
        [
            _Result(scalar=conv),  # owned conversation
            _Result(scalar="Grace Hopper"),  # target user is active
            _Result(scalar=uuid.uuid4()),  # a grant already exists
            _Result(rows=[]),  # _serialize_shares
        ]
    )

    resp = await create_conversation_share(
        conversation_id=str(conv.id),
        body=CreateConversationShareRequest(user_id=str(target_id)),
        request=None,
        user=owner,
        db=db,
    )

    assert db.added == [], "a duplicate grant must not be inserted"
    assert resp.link_access == LINK_ACCESS_PRIVATE


@pytest.mark.asyncio
async def test_creating_a_grant_inserts_exactly_one_principal():
    owner = _user()
    conv = _conversation(owner.id)
    target_id = uuid.uuid4()
    db = _FakeDB(
        [
            _Result(scalar=conv),
            _Result(scalar="Grace Hopper"),
            _Result(scalar=None),  # no existing grant
            _Result(rows=[]),
        ]
    )

    await create_conversation_share(
        conversation_id=str(conv.id),
        body=CreateConversationShareRequest(user_id=str(target_id)),
        request=None,
        user=owner,
        db=db,
    )

    assert len(db.added) == 1
    row = db.added[0]
    assert row.shared_with_user_id == target_id
    assert row.shared_with_group_id is None
    assert row.permission == "read"
    assert row.created_by == owner.id


def test_request_schema_rejects_both_or_neither_principal():
    """Mirrors the table's ``num_nonnulls(...) = 1`` CHECK at the edge, so the
    caller gets a 422 they can act on rather than a 500 from the database."""
    with pytest.raises(ValueError):
        CreateConversationShareRequest()
    with pytest.raises(ValueError):
        CreateConversationShareRequest(
            user_id=str(uuid.uuid4()), group_id=str(uuid.uuid4())
        )

    assert CreateConversationShareRequest(user_id=str(uuid.uuid4())).group_id is None
    assert CreateConversationShareRequest(group_id=str(uuid.uuid4())).user_id is None


@pytest.mark.asyncio
async def test_deleting_a_share_is_scoped_to_the_path_conversation():
    """Without the conversation predicate, knowing any share id would let an
    owner revoke a grant on a conversation they do not own."""
    owner = _user()
    conv = _conversation(owner.id)
    db = _FakeDB([_Result(scalar=conv), _Result(scalar=None)])

    with pytest.raises(HTTPException) as exc:
        await delete_conversation_share(
            conversation_id=str(conv.id),
            share_id=str(uuid.uuid4()),
            request=None,
            user=owner,
            db=db,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Share not found"


@pytest.mark.asyncio
async def test_deleting_a_share_removes_the_row():
    owner = _user()
    conv = _conversation(owner.id)
    share = _share_row(user_id=uuid.uuid4(), conversation_id=conv.id)
    db = _FakeDB([_Result(scalar=conv), _Result(scalar=share), _Result(rows=[])])

    await delete_conversation_share(
        conversation_id=str(conv.id),
        share_id=str(share.id),
        request=None,
        user=owner,
        db=db,
    )

    assert db.deleted == [share]


# ---------------------------------------------------------------------------
# Link access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opening_the_link_sets_org_read_and_audits_it(monkeypatch):
    import app.services.audit as audit_mod

    captured: list[dict] = []

    async def _capture(_db, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(audit_mod, "log_audit", _capture)

    owner = _user()
    conv = _conversation(owner.id)
    db = _FakeDB([_Result(scalar=conv), _Result(rows=[])])

    resp = await set_conversation_link_access(
        conversation_id=str(conv.id),
        body=UpdateLinkAccessRequest(link_access=LINK_ACCESS_ORG_READ),
        request=None,
        user=owner,
        db=db,
    )

    assert conv.link_access == LINK_ACCESS_ORG_READ
    assert resp.link_access == LINK_ACCESS_ORG_READ
    assert len(captured) == 1
    assert captured[0]["action"] == "share"
    assert captured[0]["details"]["previous_link_access"] == LINK_ACCESS_PRIVATE


@pytest.mark.asyncio
async def test_restoring_private_audits_as_a_revocation(monkeypatch):
    import app.services.audit as audit_mod

    captured: list[dict] = []

    async def _capture(_db, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(audit_mod, "log_audit", _capture)

    owner = _user()
    conv = _conversation(owner.id, link_access=LINK_ACCESS_ORG_READ)
    db = _FakeDB([_Result(scalar=conv), _Result(rows=[])])

    await set_conversation_link_access(
        conversation_id=str(conv.id),
        body=UpdateLinkAccessRequest(link_access=LINK_ACCESS_PRIVATE),
        request=None,
        user=owner,
        db=db,
    )

    assert conv.link_access == LINK_ACCESS_PRIVATE
    assert captured[0]["action"] == "unshare"


@pytest.mark.asyncio
async def test_reasserting_the_current_link_tier_writes_no_audit_entry(monkeypatch):
    """Otherwise a chatty UI would bury real posture changes in no-op noise."""
    import app.services.audit as audit_mod

    captured: list[dict] = []

    async def _capture(_db, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(audit_mod, "log_audit", _capture)

    owner = _user()
    conv = _conversation(owner.id, link_access=LINK_ACCESS_ORG_READ)
    db = _FakeDB([_Result(scalar=conv), _Result(rows=[])])

    await set_conversation_link_access(
        conversation_id=str(conv.id),
        body=UpdateLinkAccessRequest(link_access=LINK_ACCESS_ORG_READ),
        request=None,
        user=owner,
        db=db,
    )

    assert captured == []


# ---------------------------------------------------------------------------
# Share listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_share_list_resolves_both_principal_kinds():
    owner = _user()
    conv = _conversation(owner.id)
    target_user_id = uuid.uuid4()
    target_group_id = uuid.uuid4()
    shares = [
        _share_row(user_id=target_user_id, conversation_id=conv.id),
        _share_row(group_id=target_group_id, conversation_id=conv.id),
    ]
    db = _FakeDB(
        [
            _Result(scalar=conv),
            _Result(rows=shares),
            _Result(
                mappings=[
                    SimpleNamespace(
                        id=target_user_id,
                        full_name="Grace Hopper",
                        email="grace@example.test",
                    )
                ]
            ),
            _Result(mappings=[SimpleNamespace(id=target_group_id, name="SOC")]),
        ]
    )

    resp = await list_conversation_shares(
        conversation_id=str(conv.id), user=owner, db=db
    )

    assert [s.principal_type for s in resp.shares] == ["user", "group"]
    assert resp.shares[0].principal_name == "Grace Hopper"
    assert resp.shares[0].principal_email == "grace@example.test"
    assert resp.shares[1].principal_name == "SOC"
    assert resp.shares[1].principal_email is None


@pytest.mark.asyncio
async def test_share_list_survives_a_principal_it_cannot_resolve():
    """A row whose principal vanished must not 500 the dialog.

    The FKs cascade, so this should be unreachable — but the dialog is how an
    owner *revokes* access, and it failing closed-but-broken is the worst
    possible moment for a lookup miss to become an exception.
    """
    owner = _user()
    conv = _conversation(owner.id)
    shares = [_share_row(user_id=uuid.uuid4(), conversation_id=conv.id)]
    db = _FakeDB(
        [
            _Result(scalar=conv),
            _Result(rows=shares),
            _Result(mappings=[]),  # user lookup returns nothing
        ]
    )

    resp = await list_conversation_shares(
        conversation_id=str(conv.id), user=owner, db=db
    )

    assert resp.shares[0].principal_name == "Unknown user"


def test_share_item_permission_is_read_only():
    """``read`` is the only tier. Writing into someone else's conversation is
    a different feature (fork), not a permission level."""
    with pytest.raises(ValueError):
        ConversationShareItem(
            id=str(uuid.uuid4()),
            principal_type="user",
            principal_id=str(uuid.uuid4()),
            principal_name="Grace Hopper",
            permission="write",
            created_at="2026-07-30T09:00:00+00:00",
        )


def test_share_target_search_requires_a_real_query():
    """The picker is the only non-admin-readable people directory, so it
    answers "find this colleague", not "enumerate the org"."""
    assert SHARE_TARGET_MIN_QUERY_LENGTH >= 2
