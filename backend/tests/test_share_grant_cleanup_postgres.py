"""Deleting a user or a group drops their grants, on real PostgreSQL.

``shared_user_ids`` / ``shared_group_ids`` are JSON arrays with no foreign key,
so nothing cascades a deleted user or group out of them: ``users.delete_user``
and ``groups.delete_group`` prune every model ``helpers.share_grant_models``
returns (``test_share_grant_cleanup_coverage`` pins that set). The models here
are those the earlier hand-kept lists missed: each is given grants to two users
and two groups, and the real routes delete them one by one. Only the deleted one
goes, the survivors keep their tier and their access as non-admins, and the
last one leaves NULL, not ``[]``. A flow run's copy of its flow's audience is
history and stays as it was.

Then what keeps the cleanup whole under concurrency - a share written while its
grantee is deleted, in either order, and a grant edit committed while the prune
runs - and the revision clearing what earlier deletions left behind.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_args

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import AfterCommit
from app.api.v1 import groups as groups_routes
from app.api.v1 import nova_rules as nova_rules_routes
from app.api.v1 import users as users_routes
from app.api.v1.helpers import (
    CompanyManagedEntity,
    normalize_group_grants,
    normalize_user_grants,
    prune_group_grants,
    prune_user_grants,
    user_visible_filter,
)
from app.api.v1.schemas.nova_rule import NovaRuleUpdate
from app.llm.provider_access import ProviderAccessError, apply_provider_access
from app.models.flow import Flow
from app.models.flow_event import FlowEventType
from app.models.flow_run import FlowRun
from app.models.group import Group
from app.models.llm_config import LLMProviderConfig
from app.models.nova_rule import NovaRule
from app.models.presentation import Presentation
from app.models.relay import RelayGroup
from app.models.runner import Runner
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.models.workspace import Workspace
from app.services.llm_provider_deletion import lock_provider_configs
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

#: How long a transaction has to stay blocked to count as waiting on a lock.
_WAITS = 0.5
#: The models ``user_visible_filter`` applies to.
_VISIBILITY_MODELS = set(get_args(CompanyManagedEntity.__value__))

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "q5257shrgnt1_prune_share_grants_naming_no_one.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("q5257shrgnt1", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()


def _request() -> Request:
    return Request({"type": "http", "method": "DELETE", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})


def _user(email: str, *, role: str = "user") -> User:
    return User(id=uuid.uuid4(), email=email, hashed_password="x", full_name=email, role=role)


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


async def _nothing(*_args, **_kwargs):
    return None


# Each builds the entity whose grants are read back.
_ENTITIES: dict[str, Callable[[User, dict], Any]] = {
    "Flow": lambda owner, grants: Flow(user_id=owner.id, name="Triage", **grants),
    "FlowEventType": lambda owner, grants: FlowEventType(
        user_id=owner.id, name="ticket.opened", fields=[], **grants
    ),
    "LLMProviderConfig": lambda owner, grants: LLMProviderConfig(
        provider="openai", api_key_enc="x", access_restricted=True, **grants
    ),
    "NovaRule": lambda owner, grants: NovaRule(user_id=owner.id, name="No exfiltration", **grants),
    "Workspace": lambda owner, grants: Workspace(user_id=owner.id, name="Responders", slug="responders", **grants),
    "Presentation": lambda owner, grants: Presentation(user_id=owner.id, title="Weekly brief", **grants),
    "RelayGroup": lambda owner, grants: RelayGroup(
        user_id=owner.id, name="Paris office", slug="paris-office", **grants
    ),
    "Runner": lambda owner, grants: Runner(
        user_id=owner.id,
        name="Analyst laptop",
        platform="linux",
        key_prefix=uuid.uuid4().hex[:12],
        key_hash="x",
        **grants,
    ),
    "TemplateContainer": lambda owner, grants: TemplateContainer(user_id=owner.id, name="Brand deck", **grants),
}


def _has_permission_maps(entity) -> bool:
    return "shared_user_permissions" in type(entity).__table__.columns


async def _admin_route_calls(sessions, admin_id: uuid.UUID):
    async def delete_user(user_id: str) -> None:
        async with sessions() as db:
            await users_routes.delete_user(user_id, _request(), await db.get(User, admin_id), db)

    async def delete_group(group_id: str) -> None:
        async with sessions() as db:
            await groups_routes.delete_group(group_id, _request(), await db.get(User, admin_id), db, AfterCommit())

    return delete_user, delete_group


async def _seed(sessions, *, users: tuple[str, ...] = (), groups: tuple[str, ...] = ()) -> dict[str, Any]:
    """Accounts and groups by name, and a NOVA rule granted to *users* and *groups*."""
    async with sessions() as db:
        people = {
            name: _user(f"{name}@x.io", role="admin" if name == "admin" else "user")
            for name in ("admin", "owner", "gone", "kept", "extra")
        }
        teams = {name: Group(id=uuid.uuid4(), name=name) for name in ("gone_group", "kept_group", "extra_group")}
        db.add_all([*people.values(), *teams.values()])
        await db.flush()
        ids: dict[str, Any] = {name: str(row.id) for name, row in {**people, **teams}.items()}
        rule = NovaRule(
            user_id=people["owner"].id,
            name="No exfiltration",
            shared_user_ids=[ids[name] for name in users] or None,
            shared_group_ids=[ids[name] for name in groups] or None,
        )
        db.add(rule)
        await db.commit()
        ids["rule"] = rule.id
    return ids


async def _still_waiting(task: asyncio.Task) -> None:
    """*task* is blocked, on the lock the other transaction holds."""
    done, _ = await asyncio.wait({task}, timeout=_WAITS)
    if done:
        task.result()
    assert not done, "expected to wait for the other transaction to end"


@pytest.fixture
def _no_redis_side_effects(monkeypatch):
    """The routes' Redis side effects, not the cleanup under test."""
    monkeypatch.setattr(users_routes, "_clear_deleted_user_memory", _nothing)
    monkeypatch.setattr("app.services.quota.invalidate_membership_quota_cache", _nothing)
    monkeypatch.setattr("app.services.quota.invalidate_quota_cache", _nothing)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_redis_side_effects")
@pytest.mark.parametrize("model_name", sorted(_ENTITIES))
async def test_deleting_a_user_or_a_group_drops_their_grants(model_name):
    async with _scratch("grant_prune") as scratch:
        sessions = scratch.sessions
        async with sessions() as db:
            admin = _user("admin@x.io", role="admin")
            owner, gone, kept = _user("owner@x.io"), _user("gone@x.io"), _user("kept@x.io")
            gone_group = Group(id=uuid.uuid4(), name="gone")
            kept_group = Group(id=uuid.uuid4(), name="kept")
            db.add_all([admin, owner, gone, kept, gone_group, kept_group])
            await db.flush()
            ids = {
                "gone": str(gone.id),
                "kept": str(kept.id),
                "gone_group": str(gone_group.id),
                "kept_group": str(kept_group.id),
            }
            grants = {
                "shared_user_ids": [ids["gone"], ids["kept"]],
                "shared_group_ids": [ids["gone_group"], ids["kept_group"]],
            }
            entity = _ENTITIES[model_name](owner, grants)
            with_maps = _has_permission_maps(entity)
            if with_maps:
                entity.shared_user_permissions = {ids["gone"]: "config", ids["kept"]: "read_only"}
                entity.shared_group_permissions = {ids["gone_group"]: "config", ids["kept_group"]: "read_only"}
            db.add(entity)
            await db.commit()
            model, entity_id, admin_id = type(entity), entity.id, admin.id

        delete_user, delete_group = await _admin_route_calls(sessions, admin_id)

        async def grants_left() -> tuple:
            async with sessions() as db:
                row = await db.get(model, entity_id)
                return (
                    row.shared_user_ids,
                    row.shared_user_permissions if with_maps else None,
                    row.shared_group_ids,
                    row.shared_group_permissions if with_maps else None,
                )

        async def visible_to(user_id: uuid.UUID, group_ids: list[str]) -> bool:
            async with sessions() as db:
                found = await db.execute(
                    select(model.id).where(model.id == entity_id, user_visible_filter(model, user_id, group_ids))
                )
                return found.scalar_one_or_none() == entity_id

        await delete_user(ids["gone"])
        await delete_group(ids["gone_group"])
        assert await grants_left() == (
            [ids["kept"]],
            {ids["kept"]: "read_only"} if with_maps else None,
            [ids["kept_group"]],
            {ids["kept_group"]: "read_only"} if with_maps else None,
        )
        if model in _VISIBILITY_MODELS:
            # Rule 19: the surviving grants still reach a non-admin, directly and through the group.
            assert await visible_to(kept.id, [])
            assert await visible_to(uuid.uuid4(), [ids["kept_group"]])

        await delete_user(ids["kept"])
        await delete_group(ids["kept_group"])
        assert await grants_left() == (None, None, None, None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_redis_side_effects")
async def test_a_flow_run_keeps_the_audience_it_recorded():
    """A run's grants are its flow's at trigger time, history like its frozen graph.

    The flow is pruned and the run left as it was: the deletion never scans a
    table that grows with every trigger.
    """
    async with _scratch("grant_prune_runs") as scratch:
        sessions = scratch.sessions
        async with sessions() as db:
            admin, owner, gone = _user("admin@x.io", role="admin"), _user("owner@x.io"), _user("gone@x.io")
            gone_group = Group(id=uuid.uuid4(), name="gone")
            db.add_all([admin, owner, gone, gone_group])
            await db.flush()
            grants = {"shared_user_ids": [str(gone.id)], "shared_group_ids": [str(gone_group.id)]}
            flow = Flow(id=uuid.uuid4(), user_id=owner.id, name="Triage", **grants)
            run = FlowRun(id=uuid.uuid4(), flow_id=flow.id, user_id=owner.id, trigger_type="manual", **grants)
            db.add_all([flow, run])
            await db.commit()
            flow_id, run_id, admin_id = flow.id, run.id, admin.id

        delete_user, delete_group = await _admin_route_calls(sessions, admin_id)
        await delete_user(grants["shared_user_ids"][0])
        await delete_group(grants["shared_group_ids"][0])

        async with sessions() as db:
            flow, run = await db.get(Flow, flow_id), await db.get(FlowRun, run_id)
            assert (flow.shared_user_ids, flow.shared_group_ids) == (None, None)
            assert (run.shared_user_ids, run.shared_group_ids) == (
                grants["shared_user_ids"],
                grants["shared_group_ids"],
            )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_redis_side_effects")
async def test_a_share_written_while_its_grantee_is_deleted_is_pruned():
    """The share commits first: the deletion waits on the account it locks, then prunes it."""
    async with _scratch("grant_race_share_first") as scratch:
        sessions = scratch.sessions
        ids = await _seed(sessions)
        delete_user, _ = await _admin_route_calls(sessions, uuid.UUID(ids["admin"]))

        async with sessions() as writer:
            rule = await writer.get(NovaRule, ids["rule"])
            rule.shared_user_ids, rule.shared_user_permissions = await normalize_user_grants(
                writer, [ids["gone"], ids["kept"]], None, owner_id=rule.user_id
            )
            await writer.flush()
            deletion = asyncio.create_task(delete_user(ids["gone"]))
            await _still_waiting(deletion)
            await writer.commit()
        await asyncio.wait_for(deletion, 30)

        async with sessions() as db:
            assert (await db.get(NovaRule, ids["rule"])).shared_user_ids == [ids["kept"]]


@pytest.mark.asyncio
async def test_a_share_written_once_the_deletion_locked_its_grantee_is_refused():
    """The deletion locks the account first: the share waits, then finds no one to grant."""
    async with _scratch("grant_race_deletion_first") as scratch:
        sessions = scratch.sessions
        ids = await _seed(sessions)

        async with sessions() as deleter:
            await prune_user_grants(deleter, uuid.UUID(ids["gone"]))
            async with sessions() as writer:
                write = asyncio.create_task(normalize_user_grants(writer, [ids["gone"]], None))
                await _still_waiting(write)
                await deleter.execute(text("DELETE FROM users WHERE id = :id"), {"id": uuid.UUID(ids["gone"])})
                await deleter.commit()
                with pytest.raises(HTTPException) as refused:
                    await asyncio.wait_for(write, 30)
        assert refused.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_redis_side_effects")
async def test_a_group_share_written_while_the_group_is_deleted_is_pruned():
    """The group share commits first: the deletion waits on the group it locks, then prunes it."""
    async with _scratch("group_grant_race_share_first") as scratch:
        sessions = scratch.sessions
        ids = await _seed(sessions)
        _, delete_group = await _admin_route_calls(sessions, uuid.UUID(ids["admin"]))

        async with sessions() as writer:
            rule = await writer.get(NovaRule, ids["rule"])
            rule.shared_group_ids, rule.shared_group_permissions = await normalize_group_grants(
                writer, [ids["gone_group"], ids["kept_group"]], {ids["gone_group"]: "config"}
            )
            await writer.flush()
            deletion = asyncio.create_task(delete_group(ids["gone_group"]))
            await _still_waiting(deletion)
            await writer.commit()
        await asyncio.wait_for(deletion, 30)

        async with sessions() as db:
            rule = await db.get(NovaRule, ids["rule"])
            assert (rule.shared_group_ids, rule.shared_group_permissions) == ([ids["kept_group"]], None)


@pytest.mark.asyncio
async def test_a_group_share_written_once_the_deletion_locked_the_group_is_refused():
    """The deletion locks the group first: the share waits, then finds no group to grant."""
    async with _scratch("group_grant_race_deletion_first") as scratch:
        sessions = scratch.sessions
        ids = await _seed(sessions)

        async with sessions() as deleter:
            await prune_group_grants(deleter, uuid.UUID(ids["gone_group"]))
            async with sessions() as writer:
                write = asyncio.create_task(normalize_group_grants(writer, [ids["gone_group"]], None))
                await _still_waiting(write)
                await deleter.execute(text("DELETE FROM groups WHERE id = :id"), {"id": uuid.UUID(ids["gone_group"])})
                await deleter.commit()
                with pytest.raises(HTTPException) as refused:
                    await asyncio.wait_for(write, 30)
        assert refused.value.status_code == 422


@pytest.mark.asyncio
async def test_the_update_route_stores_only_existing_groups_canonically():
    """``PUT /nova-rules/{id}`` as the rule's non-admin owner: a stale
    form re-sending a group deleted since saves without it, an unknown or
    malformed group is refused and leaves the grants as they were, and the ids
    and tiers are stored canonical."""
    async with _scratch("group_grant_route") as scratch:
        sessions = scratch.sessions
        ids = await _seed(sessions, groups=("gone_group", "kept_group"))
        # A leftover from before the deletion paths pruned every model.
        async with sessions() as db:
            await db.execute(text("DELETE FROM groups WHERE id = :id"), {"id": uuid.UUID(ids["gone_group"])})
            await db.commit()

        async def update(**grants) -> None:
            async with sessions() as db:
                owner = await db.get(User, uuid.UUID(ids["owner"]))
                await nova_rules_routes.update_nova_rule(
                    str(ids["rule"]), NovaRuleUpdate(**grants), _request(), owner, db
                )

        async def stored() -> tuple:
            async with sessions() as db:
                rule = await db.get(NovaRule, ids["rule"])
                return rule.shared_group_ids, rule.shared_group_permissions

        kept = ids["kept_group"]
        await update(
            shared_group_ids=[ids["gone_group"], kept.upper(), kept],
            shared_group_permissions={ids["gone_group"]: "config", kept.upper(): "config"},
        )
        assert await stored() == ([kept], {kept: "config"})

        for refused_grants, detail in (
            ({"shared_group_ids": [kept, ids["gone_group"]]}, f"Cannot share with unknown group(s): {ids['gone_group']}"),
            ({"shared_group_ids": ["not-a-group"]}, "Invalid group id in share grants."),
            ({"shared_group_permissions": {kept: "owner"}}, None),
        ):
            with pytest.raises(HTTPException) as refused:
                await update(**refused_grants)
            assert refused.value.status_code == 422
            if detail is not None:
                assert refused.value.detail == detail
            assert await stored() == ([kept], {kept: "config"})

        await update(shared_group_ids=[])
        assert await stored() == (None, None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_redis_side_effects")
async def test_a_grant_edit_committed_while_the_prune_runs_is_kept():
    """The prune reads the grants under a row lock, so it rewrites the committed list, never the one it saw first.

    The editor drops the deleted principal itself and adds another one, so it
    locks no account or group, only the row.
    """
    async with _scratch("grant_race_lost_update") as scratch:
        sessions = scratch.sessions
        ids = await _seed(sessions, users=("gone", "kept"), groups=("gone_group", "kept_group"))
        delete_user, delete_group = await _admin_route_calls(sessions, uuid.UUID(ids["admin"]))

        for deletion, column, edited in (
            (lambda: delete_user(ids["gone"]), "shared_user_ids", [ids["kept"], ids["extra"]]),
            (lambda: delete_group(ids["gone_group"]), "shared_group_ids", [ids["kept_group"], ids["extra_group"]]),
        ):
            async with sessions() as editor:
                await editor.execute(update(NovaRule).where(NovaRule.id == ids["rule"]).values({column: edited}))
                pending = asyncio.create_task(deletion())
                await _still_waiting(pending)
                await editor.commit()
            await asyncio.wait_for(pending, 30)

            async with sessions() as db:
                assert getattr(await db.get(NovaRule, ids["rule"]), column) == edited


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_redis_side_effects")
async def test_a_provider_restricted_to_a_group_being_deleted_loses_it_in_either_order():
    """A provider access write locks the groups it adds, and reads the provider under the configs lock."""
    async with _scratch("grant_race_provider") as scratch:
        sessions = scratch.sessions
        ids = await _seed(sessions)
        async with sessions() as db:
            config = LLMProviderConfig(provider="openai", api_key_enc="x")
            db.add(config)
            await db.commit()
            config_id = config.id
        _, delete_group = await _admin_route_calls(sessions, uuid.UUID(ids["admin"]))

        async def restrict(writer, group_names: tuple[str, ...]) -> None:
            # What PUT /settings/llm-providers/{id} does before it writes the access.
            await lock_provider_configs(writer)
            config = await writer.get(LLMProviderConfig, config_id)
            await apply_provider_access(
                writer, config, access_restricted=True, group_ids=[ids[name] for name in group_names], user_ids=None
            )

        # The write first: the deletion waits on the group it locks, then prunes it.
        async with sessions() as writer:
            await restrict(writer, ("gone_group", "kept_group"))
            await writer.flush()
            deletion = asyncio.create_task(delete_group(ids["gone_group"]))
            await _still_waiting(deletion)
            await writer.commit()
        await asyncio.wait_for(deletion, 30)
        async with sessions() as db:
            assert (await db.get(LLMProviderConfig, config_id)).shared_group_ids == [ids["kept_group"]]

        # The deletion first: the write waits on the provider row the prune locked, then
        # reads the pruned list, so the group is one it adds, and no longer exists.
        async with sessions() as deleter:
            await prune_group_grants(deleter, uuid.UUID(ids["kept_group"]))
            await deleter.flush()
            async with sessions() as writer:
                write = asyncio.create_task(restrict(writer, ("extra_group", "kept_group")))
                await _still_waiting(write)
                await deleter.execute(text("DELETE FROM groups WHERE id = :id"), {"id": uuid.UUID(ids["kept_group"])})
                await deleter.commit()
                with pytest.raises(ProviderAccessError, match="Unknown group"):
                    await asyncio.wait_for(write, 30)


async def _run_revision(engine: AsyncEngine, step) -> None:
    """Run *step* as ``env.py`` runs a revision: the migration context owns the
    transaction, which the revision's ``autocommit_block`` commits and reopens."""

    def _call(sync_conn) -> None:
        context = MigrationContext.configure(sync_conn)
        with Operations.context(context), context.begin_transaction():
            step()

    async with engine.connect() as conn:
        await conn.run_sync(_call)


@pytest.mark.asyncio
async def test_the_revision_prunes_the_grants_earlier_deletions_left(monkeypatch):
    # One row per batch, so the rows are read across several batches.
    monkeypatch.setattr(revision, "_BATCH_SIZE", 1)
    async with _scratch("grant_backfill") as scratch:
        sessions = scratch.sessions
        ids = await _seed(sessions, users=("kept",), groups=("kept_group",))
        vanished_user, vanished_group = str(uuid.uuid4()), str(uuid.uuid4())
        kept_as_typed = ids["kept"].upper()
        async with sessions() as db:
            owner_id = uuid.UUID(ids["owner"])
            stale = NovaRule(
                user_id=owner_id,
                name="Stale",
                shared_user_ids=[vanished_user, kept_as_typed, "not-a-uuid"],
                shared_user_permissions={vanished_user: "config", kept_as_typed: "read_only"},
                shared_group_ids=[ids["kept_group"], vanished_group, ids["kept_group"].upper()],
                shared_group_permissions={vanished_group: "config", ids["kept_group"]: "read_only"},
            )
            emptied = NovaRule(
                user_id=owner_id,
                name="Emptied",
                shared_user_ids=[vanished_user],
                shared_user_permissions={vanished_user: "config"},
                shared_group_ids=[vanished_group],
            )
            provider = LLMProviderConfig(
                provider="openai",
                api_key_enc="x",
                access_restricted=True,
                shared_user_ids=[vanished_user, ids["kept"]],
                shared_group_ids=[vanished_group],
            )
            flow = Flow(id=uuid.uuid4(), user_id=owner_id, name="Triage")
            run = FlowRun(
                flow_id=flow.id,
                user_id=owner_id,
                trigger_type="manual",
                shared_user_ids=[vanished_user],
                shared_group_ids=[vanished_group],
            )
            db.add_all([stale, emptied, provider, flow, run])
            await db.commit()
            rows = {"stale": stale.id, "emptied": emptied.id, "provider": provider.id, "run": run.id}

        async def stored() -> dict[str, tuple]:
            async with sessions() as db:
                grants = {}
                for name, model in (("stale", NovaRule), ("emptied", NovaRule), ("clean", NovaRule)):
                    row = await db.get(model, rows.get(name, ids["rule"]))
                    grants[name] = (
                        row.shared_user_ids,
                        row.shared_user_permissions,
                        row.shared_group_ids,
                        row.shared_group_permissions,
                    )
                provider_row = await db.get(LLMProviderConfig, rows["provider"])
                run_row = await db.get(FlowRun, rows["run"])
                grants["provider"] = (provider_row.shared_user_ids, provider_row.shared_group_ids)
                grants["run"] = (run_row.shared_user_ids, run_row.shared_group_ids)
                return grants

        expected = {
            # A user id names an account in any spelling; a group id only exactly.
            "stale": ([kept_as_typed], {kept_as_typed: "read_only"}, [ids["kept_group"]], {ids["kept_group"]: "read_only"}),
            "emptied": (None, None, None, None),
            "clean": ([ids["kept"]], None, [ids["kept_group"]], None),
            "provider": ([ids["kept"]], None),
            "run": ([vanished_user], [vanished_group]),
        }
        await _run_revision(scratch.engine, revision.upgrade)
        assert await stored() == expected

        await _run_revision(scratch.engine, revision.upgrade)
        await _run_revision(scratch.engine, revision.downgrade)
        assert await stored() == expected
