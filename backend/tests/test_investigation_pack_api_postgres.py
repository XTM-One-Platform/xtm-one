"""The pack builder routes and built packs in runs, against real PostgreSQL.

Visibility is the point: a built pack is a shareable entity, and it is also
something that queries integrations. Tested as non-admins on the real route
functions and the real resolver: own, not shared, ``read_only``, ``config``,
a group grant then revoked, company-managed; that a colleague's run of a
shared pack uses the colleague's VirusTotal, never the author's; versions
(a changed method only, restore as a new version); the per-owner cap; the
preview; duplicating a built-in pack.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import investigation_packs as routes
from app.api.v1.schemas.investigation_pack import (
    InvestigationPackCreate,
    InvestigationPackUpdate,
    PackPreviewRequest,
)
from app.models.group import Group, GroupMember
from app.models.investigation_pack import InvestigationPack, InvestigationPackVersion
from app.services.investigation_pack_resolver import resolve_packs, resolve_selected

from .test_investigation_pack_resolver_postgres import _temp_database, _user, _virustotal

pytestmark = pytest.mark.integration

METHOD = {
    "steps": [
        {"key": "check", "label": "Check OpenCTI", "sources": [{"kind": "opencti"}]},
        {"key": "map", "label": "Map the hosting", "icon": "network", "sources": [{"kind": "passive_dns"}]},
    ]
}


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})


def _create_body(**overrides) -> InvestigationPackCreate:
    return InvestigationPackCreate.model_validate(
        {"name": "Our method", "subject_types": ["domain", "ip"], "definition": METHOD, **overrides}
    )


def _update_body(**overrides) -> InvestigationPackUpdate:
    return InvestigationPackUpdate.model_validate(
        {"name": "Our method", "subject_types": ["domain", "ip"], "definition": METHOD, **overrides}
    )


async def _create(db, owner, **overrides):
    return await routes.create_investigation_pack(_create_body(**overrides), _request(), owner, db)


async def _people(db, *emails, admin: str | None = None):
    users = [_user(e) for e in emails]
    for u in users:
        if u.email == admin:
            u.role = "admin"
    db.add_all(users)
    await db.commit()
    return users


def _passive(pack) -> set[str]:
    return {s.integration_id for s in pack.sources if s.kind == "passive_dns"}


@pytest.mark.asyncio
async def test_the_owner_sees_edits_and_runs_their_pack_with_their_own_key():
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        (me,) = await _people(db, "me@x.io")
        mine = _virustotal(me, "My VT")
        db.add(mine)
        await db.commit()
        created = await _create(db, me)
        assert (created.version_number, created.can_manage, created.slug) == (1, True, f"pack:{created.id}")

        packs = await resolve_packs(db, me)
        built = packs[created.slug]
        assert _passive(built) == {str(mine.id)}
        assert [p.slug for p in await resolve_selected(db, me, [created.slug])] == [created.slug]


@pytest.mark.asyncio
async def test_a_pack_nobody_shared_is_invisible_to_a_colleague():
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        me, colleague = await _people(db, "me@x.io", "colleague@x.io")
        created = await _create(db, me)

        with pytest.raises(HTTPException) as seen:
            await routes.get_investigation_pack(created.id, colleague, db)
        assert seen.value.status_code == 404
        with pytest.raises(HTTPException) as edited:
            await routes.update_investigation_pack(created.id, _update_body(), _request(), colleague, db)
        assert edited.value.status_code == 404, "never a 403 that would confirm it exists"
        assert created.slug not in await resolve_packs(db, colleague)
        assert await routes.list_investigation_packs("visible", colleague, db) == []


@pytest.mark.asyncio
async def test_a_read_only_colleague_runs_it_with_their_key_never_the_author_s():
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        me, colleague, keyless = await _people(db, "me@x.io", "colleague@x.io", "keyless@x.io")
        mine, theirs = _virustotal(me, "My VT"), _virustotal(colleague, "Their VT")
        db.add_all([mine, theirs])
        await db.commit()
        created = await _create(
            db,
            me,
            shared_user_ids=[str(colleague.id), str(keyless.id)],
            shared_user_permissions={str(colleague.id): "read_only", str(keyless.id): "read_only"},
        )

        seen = await routes.get_investigation_pack(created.id, colleague, db)
        assert seen.can_manage is False
        with pytest.raises(HTTPException) as edited:
            await routes.update_investigation_pack(created.id, _update_body(), _request(), colleague, db)
        assert edited.value.status_code == 403

        assert _passive((await resolve_packs(db, colleague))[created.slug]) == {str(theirs.id)}
        without = (await resolve_packs(db, keyless))[created.slug]
        assert _passive(without) == set()
        assert without.description == (
            "Not available to you: Check OpenCTI: OpenCTI knowledge; Map the hosting: VirusTotal passive DNS."
        )


@pytest.mark.asyncio
async def test_a_config_colleague_edits_it_and_each_changed_method_is_a_version():
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        me, colleague = await _people(db, "me@x.io", "colleague@x.io")
        created = await _create(
            db, me, shared_user_ids=[str(colleague.id)], shared_user_permissions={str(colleague.id): "config"}
        )
        renamed = await routes.update_investigation_pack(
            created.id, _update_body(name="Renamed"), _request(), colleague, db
        )
        assert (renamed.name, renamed.version_number) == ("Renamed", 1), "a name is not a new version"

        longer = {"steps": [*METHOD["steps"], {"key": "web", "label": "Search", "sources": [{"kind": "web_search"}]}]}
        changed = await routes.update_investigation_pack(
            created.id, _update_body(name="Renamed", definition=longer, commit_message="add search"), _request(), colleague, db
        )
        assert changed.version_number == 2
        versions = await routes.list_investigation_pack_versions(created.id, me, db)
        assert [(v.version_number, v.is_current, v.commit_message) for v in versions] == [
            (2, True, "add search"),
            (1, False, "Initial version"),
        ]

        with pytest.raises(HTTPException) as ticked:
            await routes.update_investigation_pack(
                created.id, _update_body(name="Renamed", definition=longer, recommended=True), _request(), colleague, db
            )
        assert ticked.value.status_code == 403, "pre-ticking it for everyone is the owner's choice"
        owner_ticked = await routes.update_investigation_pack(
            created.id, _update_body(name="Renamed", definition=longer, recommended=True), _request(), me, db
        )
        assert (owner_ticked.recommended, owner_ticked.version_number) == (True, 2)
        restored = await routes.restore_investigation_pack_version(created.id, 1, _request(), colleague, db)
        assert restored.version_number == 3
        assert [s["key"] for s in restored.definition["steps"]] == ["check", "map"]
        detail = await routes.get_investigation_pack_version(created.id, 3, me, db)
        assert detail.commit_message == "Restored version 1" and detail.definition == restored.definition


@pytest.mark.asyncio
async def test_a_group_grant_shares_it_and_revoking_it_takes_it_away_at_once():
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        me, member = await _people(db, "me@x.io", "member@x.io")
        group = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        db.add(group)
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=member.id))
        await db.commit()
        created = await _create(db, me, shared_group_ids=[str(group.id)])
        assert created.slug in await resolve_packs(db, member)

        await routes.update_investigation_pack(created.id, _update_body(shared_group_ids=[]), _request(), me, db)
        assert created.slug not in await resolve_packs(db, member)
        with pytest.raises(HTTPException) as gone:
            await routes.get_investigation_pack(created.id, member, db)
        assert gone.value.status_code == 404


@pytest.mark.asyncio
async def test_a_company_pack_is_everyone_s_to_run_and_an_admin_s_to_edit():
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        admin, someone = await _people(db, "admin@x.io", "someone@x.io", admin="admin@x.io")
        with pytest.raises(HTTPException) as refused:
            await _create(db, someone, is_company_managed=True)
        assert refused.value.status_code == 403
        created = await _create(db, admin, is_company_managed=True)
        assert created.user_id is None, "company-managed is ownerless (invariant 9)"

        assert created.slug in await resolve_packs(db, someone)
        assert (await routes.get_investigation_pack(created.id, someone, db)).can_manage is False
        with pytest.raises(HTTPException) as edited:
            await routes.update_investigation_pack(created.id, _update_body(), _request(), someone, db)
        assert edited.value.status_code == 403


@pytest.mark.asyncio
async def test_deleting_a_pack_takes_its_versions_and_only_its_manager_may(monkeypatch):
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        me, colleague = await _people(db, "me@x.io", "colleague@x.io")
        created = await _create(
            db, me, shared_user_ids=[str(colleague.id)], shared_user_permissions={str(colleague.id): "read_only"}
        )
        with pytest.raises(HTTPException) as refused:
            await routes.delete_investigation_pack(created.id, _request(), colleague, db)
        assert refused.value.status_code == 403
        await routes.delete_investigation_pack(created.id, _request(), me, db)
        assert (await db.execute(select(InvestigationPack))).scalars().all() == []
        assert (await db.execute(select(InvestigationPackVersion))).scalars().all() == []


@pytest.mark.asyncio
async def test_an_owner_has_a_cap(monkeypatch):
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        (me,) = await _people(db, "me@x.io")
        monkeypatch.setattr(routes, "MAX_PACKS_PER_OWNER", 1)
        await _create(db, me)
        with pytest.raises(HTTPException) as capped:
            await _create(db, me)
        assert capped.value.status_code == 422


@pytest.mark.asyncio
async def test_the_preview_says_what_this_caller_would_run():
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        me, keyless = await _people(db, "me@x.io", "keyless@x.io")
        db.add(_virustotal(me, "My VT"))
        await db.commit()
        created = await _create(
            db, me, shared_user_ids=[str(keyless.id)], shared_user_permissions={str(keyless.id): "read_only"}
        )

        mine = await routes.preview_investigation_pack(PackPreviewRequest(subject="x.com", pack_id=created.id), me, db)
        theirs = await routes.preview_investigation_pack(
            PackPreviewRequest(subject="x.com", pack_id=created.id), keyless, db
        )
        assert mine.subject_type == "domain"
        assert {a.slug: a.servable for a in mine.actions} == {"step:check": False, "step:map": True, "write_report": True}
        assert {a.slug: a.servable for a in theirs.actions}["step:map"] is False
        assert theirs.reachable is False
        draft = await routes.preview_investigation_pack(
            PackPreviewRequest(subject="1.2.3.4", pack=_create_body()), me, db
        )
        assert [a.slug for a in draft.actions] == ["step:check", "step:map", "write_report"]


@pytest.mark.asyncio
async def test_a_built_in_pack_duplicates_into_a_private_copy():
    async with _temp_database("inv_packs") as sessions, sessions() as db:
        (me,) = await _people(db, "me@x.io")
        copy = await routes.duplicate_builtin_pack("disinfo-infrastructure", _request(), me, db)
        assert copy.name == "Disinformation infrastructure (copy)"
        assert copy.user_id == str(me.id) and copy.is_company_managed is False
        assert [s["key"] for s in copy.definition["steps"]][:3] == ["check_opencti", "qualify_seed", "map_infrastructure"]
        assert copy.slug in await resolve_packs(db, me)
        with pytest.raises(HTTPException) as unknown:
            await routes.duplicate_builtin_pack("gone", _request(), me, db)
        assert unknown.value.status_code == 404


@pytest.mark.asyncio
async def test_deleting_a_user_or_a_group_takes_their_grants_off_a_pack():
    from types import SimpleNamespace

    from app.api.deps import AfterCommit
    from app.api.v1 import groups as groups_module
    from app.api.v1.helpers import prune_user_grants

    async with _temp_database("inv_packs") as sessions, sessions() as db:
        admin, colleague, member = await _people(db, "admin@x.io", "colleague@x.io", "member@x.io", admin="admin@x.io")
        group = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        db.add(group)
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=member.id))
        await db.commit()
        created = await _create(
            db,
            admin,
            shared_user_ids=[str(colleague.id)],
            shared_user_permissions={str(colleague.id): "config"},
            shared_group_ids=[str(group.id)],
            shared_group_permissions={str(group.id): "read_only"},
        )

        assert await prune_user_grants(db, colleague.id) == 1
        await db.commit()
        await groups_module.delete_group(
            group_id=str(group.id),
            request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}),
            admin=admin,
            db=db,
            after_commit=AfterCommit(),
        )
        row = (await db.execute(select(InvestigationPack).where(InvestigationPack.id == uuid.UUID(created.id)))).scalar_one()
        await db.refresh(row)
        assert (row.shared_user_ids, row.shared_user_permissions) == (None, None)
        assert not row.shared_group_ids and not row.shared_group_permissions


HTTP_METHOD = {
    "steps": [
        {
            "key": "check",
            "label": "Check OpenCTI",
            "sources": [
                # A familiar name on a host its author chose: the picker must
                # say where the subject goes, not only what the source is called.
                {"kind": "http", "name": "OpenCTI lookup", "url_template": "https://Collector.Example/{value}"},
                {"kind": "http", "name": "Mirror", "url_template": "https://{value}.mirror.example:8443/x"},
            ],
        }
    ]
}


def _offer(plan, slug):
    return next(p for p in plan.packs if p.slug == slug)


@pytest.mark.asyncio
async def test_the_picker_says_whose_a_pack_is_and_where_it_sends_the_subject():
    from app.api.v1.investigations import plan_source_packs

    async with _temp_database("inv_packs") as sessions, sessions() as db:
        me, reader, editor, member, admin = await _people(
            db, "me@x.io", "reader@x.io", "editor@x.io", "member@x.io", "admin@x.io", admin="admin@x.io"
        )
        group = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        db.add(group)
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=member.id))
        await db.commit()
        shared = await _create(
            db,
            me,
            definition=HTTP_METHOD,
            recommended=True,
            shared_user_ids=[str(reader.id), str(editor.id)],
            shared_user_permissions={str(reader.id): "read_only", str(editor.id): "config"},
            shared_group_ids=[str(group.id)],
        )
        company = await _create(db, admin, definition=HTTP_METHOD, recommended=True, is_company_managed=True)
        # Each host as the run builds it for this subject, `{value}` included.
        hosts = ["collector.example", "example.com.mirror.example:8443"]

        # Read-only, config (may edit it) and a group grant: still a colleague's.
        for colleague in (reader, editor, member):
            theirs = await plan_source_packs(subject="example.com", user=colleague, db=db)
            offered = _offer(theirs, shared.slug)
            assert (offered.origin, offered.owner_name, offered.destinations) == ("shared", "me@x.io", hosts)
            assert offered.recommended is False, "a colleague's recommended pack is offered, never pre-ticked"
        from_company = _offer(theirs, company.slug)
        assert (from_company.origin, from_company.owner_name, from_company.recommended) == ("company", None, True)
        assert from_company.destinations == hosts
        builtin = _offer(theirs, "web-general")
        assert (builtin.origin, builtin.owner_name, builtin.destinations) == ("builtin", None, [])

        mine = _offer(await plan_source_packs(subject="example.com", user=me, db=db), shared.slug)
        assert (mine.origin, mine.owner_name, mine.recommended, mine.destinations) == ("mine", None, True, hosts)

        await routes.update_investigation_pack(
            shared.id,
            _update_body(definition=HTTP_METHOD, shared_user_ids=[], shared_group_ids=[]),
            _request(),
            me,
            db,
        )
        for colleague in (reader, editor, member):
            revoked = await plan_source_packs(subject="example.com", user=colleague, db=db)
            assert shared.slug not in {p.slug for p in revoked.packs}


@pytest.mark.asyncio
async def test_two_saves_and_a_restore_at_once_each_get_their_own_version():
    import asyncio

    longer = {"steps": [*METHOD["steps"], {"key": "again", "label": "Check again", "sources": [{"kind": "opencti"}]}]}
    shorter = {"steps": METHOD["steps"][:1]}
    async with _temp_database("inv_packs") as sessions:
        async with sessions() as db:
            me, editor = await _people(db, "me@x.io", "editor@x.io")
            created = await _create(
                db, me, shared_user_ids=[str(editor.id)], shared_user_permissions={str(editor.id): "config"}
            )

        async def _save(user, definition):
            async with sessions() as db:
                return await routes.update_investigation_pack(
                    created.id, _update_body(definition=definition), _request(), user, db
                )

        async def _restore(user):
            async with sessions() as db:
                return await routes.restore_investigation_pack_version(created.id, 1, _request(), user, db)

        # Each one read version N and inserted N+1 before the row was locked:
        # the second hit the unique (pack, version) and answered a 500.
        done = await asyncio.gather(_save(me, longer), _save(editor, shorter), _restore(me))
        assert sorted(r.version_number for r in done) == [2, 3, 4]

        async with sessions() as db:
            numbers = (
                await db.execute(
                    select(InvestigationPackVersion.version_number).where(
                        InvestigationPackVersion.pack_id == uuid.UUID(created.id)
                    )
                )
            ).scalars().all()
            assert sorted(numbers) == [1, 2, 3, 4]
