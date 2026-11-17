"""Which bound Slack integrations an assignment run may receive events through.

``slack_events.eligible_integration_ids`` is the event poller's rule: a fixed binding
passes the personal OAuth token gate for the agent's owner
(``oauth_credentials_usable_clause``) whether or not the acting user can see
it (delivered, and logged), and a template binding the per-user resolution
rule the run applies (``template_candidate_filter``: a restricted company-managed
integration for its grantees only, JSONB operators): a mock session cannot evaluate
them, so they run here against PostgreSQL, for a NON-admin acting user. Same
scratch-database recipe as ``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.user import User
from app.services.slack_events import eligible_integration_ids
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "slackscope",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


def _user(name: str) -> User:
    return User(id=uuid.uuid4(), email=f"{name}-{uuid.uuid4().hex[:6]}@example.org", full_name=name, role="user")


def _slack(owner: User | None, name: str, **fields) -> Integration:
    fields.setdefault("auth_type", "oauth")
    return Integration(
        id=uuid.uuid4(),
        user_id=owner.id if owner else None,
        provider="slack",
        name=name,
        status="connected",
        **fields,
    )


async def test_only_integrations_the_acting_user_may_use_are_eligible():
    async with _temp_database() as sessions, sessions() as db:
        me, colleague = _user("me"), _user("colleague")
        team = Group(id=uuid.uuid4(), name=f"team-{uuid.uuid4().hex[:6]}")
        db.add_all([me, colleague, team])
        await db.flush()
        db.add(GroupMember(group_id=team.id, user_id=me.id))
        rows = {
            "own": _slack(me, "own"),
            "colleague_private": _slack(colleague, "colleague private"),
            "group_shared_unacknowledged": _slack(colleague, "group shared", shared_group_ids=[str(team.id)]),
            "group_shared_acknowledged": _slack(
                colleague, "group shared ack", shared_group_ids=[str(team.id)], oauth_share_acknowledged=True
            ),
            "company_unacknowledged": _slack(colleague, "company", is_company_managed=True),
            "company_acknowledged": _slack(
                colleague, "company ack", is_company_managed=True, oauth_share_acknowledged=True
            ),
            "company_restricted": _slack(
                colleague,
                "company restricted",
                is_company_managed=True,
                oauth_share_acknowledged=True,
                access_restricted=True,
            ),
            "company_restricted_granted": _slack(
                colleague,
                "company restricted to my group",
                is_company_managed=True,
                oauth_share_acknowledged=True,
                access_restricted=True,
                shared_group_ids=[str(team.id)],
            ),
            "company_bot_token": _slack(None, "company bot", is_company_managed=True, auth_type="bot_token"),
            "colleague_private_bot_token": _slack(colleague, "colleague bot", auth_type="bot_token"),
        }
        db.add_all(rows.values())
        await db.commit()
        ids = {key: str(row.id) for key, row in rows.items()}

        fixed = await eligible_integration_ids(db, me.id, ids.values(), template=False)
        # The poller's gate: an OAuth token is mine or shared with acknowledgement,
        # a bot token is shared by design - visible to me or not.
        assert fixed == {
            ids["own"],
            ids["group_shared_acknowledged"],
            ids["company_acknowledged"],
            ids["company_restricted"],
            ids["company_restricted_granted"],
            ids["company_bot_token"],
            ids["colleague_private_bot_token"],
        }

        template = await eligible_integration_ids(db, me.id, ids.values(), template=True)
        # A company-managed integration restricted to other grantees is not mine to resolve;
        # one granted to my group is.
        assert template == {
            ids["own"],
            ids["company_acknowledged"],
            ids["company_restricted_granted"],
            ids["company_bot_token"],
        }
        assert ids["colleague_private_bot_token"] not in template
        # Group sharing grants visibility, never permission to run as the owner.
        assert ids["group_shared_acknowledged"] not in template

        nobody = await eligible_integration_ids(db, None, ids.values(), template=False)
        assert ids["own"] not in nobody and ids["colleague_private"] not in nobody
        assert await eligible_integration_ids(db, me.id, ["not-a-uuid"], template=False) == set()
