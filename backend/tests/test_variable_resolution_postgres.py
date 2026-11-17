"""Which ``{{var_id.<uuid>}}`` a run resolves, against real PostgreSQL.

A run resolves exactly the variables its identity may see - own, shared with
it directly (``shared_user_ids``) or through one of its groups, company-managed
unless narrowed by ``access_restricted`` - and never another user's private
variable: the value is substituted into the prompt the model reads, a secret
included. ``load_variables`` used to rebuild that visibility by hand and forgot
the per-user share, so a variable shared with the run's user reached the
model as a literal placeholder, while a narrowed company-managed variable
resolved for everyone.

``integration``-marked and skipped when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import agents as agents_api
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.group import Group, GroupMember
from app.models.user import User
from app.models.variable import Variable
from app.services.variable_resolver import load_variables, missing_variable_ids
from tests.scratch_database import scratch_database
from tests.test_catalog_clone_visibility_postgres import _temp_database as _full_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "varres",
        tables=[
            User.__table__,
            Group.__table__,
            GroupMember.__table__,
            Variable.__table__,
        ],
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


def _user(name: str) -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=name.title(),
        role="user",
    )


async def _seed(session: async_sessionmaker) -> SimpleNamespace:
    async with session() as db:
        owner, runner, stranger = _user("owner"), _user("runner"), _user("stranger")
        db.add_all([owner, runner, stranger])
        await db.flush()
        team = Group(name=f"qa-{uuid.uuid4().hex[:8]}", created_by=owner.id)
        db.add(team)
        await db.flush()
        db.add(GroupMember(group_id=team.id, user_id=runner.id))

        def var(name: str, **fields) -> Variable:
            row = Variable(name=name, value=f"{name}-value", **fields)
            db.add(row)
            return row

        rows = SimpleNamespace(
            user_shared_secret=Variable(
                name="QA_PASSWORD",
                is_secret=True,
                value_enc=encrypt_value("s3cret"),
                user_id=owner.id,
                shared_user_ids=[str(runner.id)],
                shared_user_permissions={str(runner.id): "read_only"},
            ),
            group_shared=var(
                "QA_URL",
                user_id=owner.id,
                shared_group_ids=[str(team.id)],
                shared_group_permissions={str(team.id): "read_only"},
            ),
            own=var("RUNNER_OWN", user_id=runner.id),
            company=var("COMPANY", user_id=owner.id, is_company_managed=True),
            restricted=var(
                "RESTRICTED",
                user_id=owner.id,
                is_company_managed=True,
                access_restricted=True,
            ),
            restricted_granted=var(
                "RESTRICTED_GRANTED",
                user_id=owner.id,
                is_company_managed=True,
                access_restricted=True,
                shared_group_ids=[str(team.id)],
                shared_group_permissions={str(team.id): "read_only"},
            ),
            owner_private=var("OWNER_PRIVATE", user_id=owner.id),
            stranger_private=var("STRANGER_PRIVATE", user_id=stranger.id),
        )
        db.add(rows.user_shared_secret)
        await db.commit()
        return SimpleNamespace(
            owner_id=owner.id,
            runner_id=runner.id,
            team_id=str(team.id),
            ids={key: str(row.id) for key, row in vars(rows).items()},
        )


@pytest.mark.asyncio
async def test_a_run_resolves_every_variable_its_identity_may_see_and_nothing_else():
    async with _temp_database() as session:
        seed = await _seed(session)
        ids = seed.ids
        async with session() as db:
            resolved = await load_variables(db, str(seed.runner_id), [seed.team_id])

        assert resolved[ids["user_shared_secret"]] == "s3cret"
        assert resolved[ids["group_shared"]] == "QA_URL-value"
        assert resolved[ids["own"]] == "RUNNER_OWN-value"
        assert resolved[ids["company"]] == "COMPANY-value"
        assert resolved[ids["restricted_granted"]] == "RESTRICTED_GRANTED-value"
        assert set(resolved) == {
            ids["user_shared_secret"],
            ids["group_shared"],
            ids["own"],
            ids["company"],
            ids["restricted_granted"],
        }


@pytest.mark.asyncio
async def test_a_group_share_needs_the_group_ids_of_the_resolving_user():
    async with _temp_database() as session:
        seed = await _seed(session)
        async with session() as db:
            resolved = await load_variables(db, str(seed.runner_id), [])

        assert seed.ids["group_shared"] not in resolved
        assert seed.ids["user_shared_secret"] in resolved


@pytest.mark.asyncio
async def test_the_owner_resolves_what_it_owns_and_not_a_strangers_private_variable():
    async with _temp_database() as session:
        seed = await _seed(session)
        async with session() as db:
            resolved = await load_variables(db, str(seed.owner_id), [])

        assert seed.ids["owner_private"] in resolved
        assert seed.ids["user_shared_secret"] in resolved
        assert seed.ids["restricted"] in resolved
        assert seed.ids["stranger_private"] not in resolved
        assert seed.ids["own"] not in resolved


@pytest.mark.parametrize("no_account", [None, "", "anonymous"])
@pytest.mark.asyncio
async def test_a_run_with_no_account_resolves_the_company_managed_variables(no_account):
    async with _temp_database() as session:
        seed = await _seed(session)
        async with session() as db:
            resolved = await load_variables(db, no_account, [seed.team_id])

        assert set(resolved) == {
            seed.ids["company"],
            seed.ids["restricted"],
            seed.ids["restricted_granted"],
        }


@pytest.mark.asyncio
async def test_missing_variable_ids_names_the_ids_no_variable_carries():
    async with _temp_database() as session:
        seed = await _seed(session)
        deleted = str(uuid.uuid4())
        async with session() as db:
            missing = await missing_variable_ids(
                db, [seed.ids["stranger_private"], deleted.upper(), "not-a-uuid"]
            )

        assert missing == {deleted, "not-a-uuid"}


@pytest.mark.asyncio
async def test_the_agent_notice_lists_a_deleted_variable_by_id_and_never_a_hidden_one():
    """``GET /agents/{id}/missing-variables`` (the agent page's notice): an
    unfilled variable by name, a reference no variable carries by id, and
    nothing about another user's private variable."""
    async with _full_database("varres_notice") as session:
        async with session() as db:
            owner, stranger = _user("owner"), _user("stranger")
            db.add_all([owner, stranger])
            await db.flush()
            unfilled = Variable(name="QA_TOKEN", user_id=owner.id, value=None)
            hidden = Variable(name="STRANGER_TOKEN", user_id=stranger.id, value=None)
            db.add_all([unfilled, hidden])
            await db.flush()
            deleted = str(uuid.uuid4())
            agent = Agent(
                user_id=owner.id,
                name="QA Browser",
                slug=f"qa-browser-{uuid.uuid4().hex[:8]}",
                persona=f"Token {{{{var_id.{unfilled.id}}}}}, {{{{var_id.{hidden.id}}}}}",
            )
            db.add(agent)
            await db.flush()
            db.add(
                Assignment(
                    agent_id=agent.id,
                    user_id=owner.id,
                    name="Launcher TNR",
                    trigger_type="schedule",
                    prompt=f"Open {{{{var_id.{deleted}}}}}",
                )
            )
            await db.commit()
            agent_id, owner_id, unfilled_id = agent.id, owner.id, unfilled.id

        async with session() as db:
            listed = await agents_api.list_agent_missing_variables(
                agent_id=str(agent_id), user=await db.get(User, owner_id), db=db
            )

        assert [(v.id, v.name, v.deleted) for v in listed] == [
            (str(unfilled_id), "QA_TOKEN", False),
            (deleted, None, True),
        ]
