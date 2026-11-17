"""Nova rule sync errors are served per caller, on a real PostgreSQL.

A failed rule sync stores the exception text of the run on the source, for the
people who fix it. The connectivity test of the company-managed Nova connection
repeats that error, and every user can see that connection while only admins
manage it: a caller who cannot manage it is served a fixed sentence instead.
Runs the real routes, as a non-admin user and as an admin, in an isolated
temporary database.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

import app.integrations.providers.nova  # registers the Nova probe
import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import integrations as integrations_mod
from app.api.v1 import nova_rules as nova_rules_mod
from app.models.integration import Integration
from app.models.nova_rule_source import NovaRuleSource
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

# What a failed sync stores: the HTTP client's message, with the request URL.
_HIDDEN = ("payroll-repo", "git.internal.example", "hidden-marker")
_RAW = (
    "Client error '401 Unauthorized' for url 'https://git.internal.example/acme/payroll-repo/contents' (hidden-marker)"
)
_REPO = "Nova-Hunting/nova-rules"


def _user(label: str, *, role: str = "user") -> User:
    return User(
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=label.title(),
        role=role,
    )


async def test_the_nova_sync_error_is_served_per_caller():
    async with (
        scratch_database(
            "novasyncreaders", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
        ) as db,
        db.sessions() as session,
    ):
        member, admin = _user("member"), _user("admin", role="admin")
        nova = Integration(
            id=uuid.uuid4(),
            provider="nova",
            auth_type="none",
            name="Nova",
            status="connected",
            is_company_managed=True,
        )
        session.add_all([member, admin, nova])
        await session.flush()
        session.add(
            NovaRuleSource(
                id=uuid.uuid4(),
                integration_id=nova.id,
                repo=_REPO,
                branch="main",
                sync_interval="daily",
                last_sync_status="error",
                last_sync_error=_RAW,
                sync_failure_count=3,
            )
        )
        await session.commit()
        assert not member.is_admin

        # The admin manages the connection: exactly today's message.
        managed = await integrations_mod.test_integration(str(nova.id), user=admin, db=session)
        assert managed == {
            "status": "error",
            "message": f"Last sync of {_REPO} failed (3 consecutive): {_RAW}",
        }
        [source] = await nova_rules_mod.list_nova_sources(user=admin, db=session)
        assert source["last_sync_error"] == _RAW

        # Any other user sees the connection, and the verdict, not the error.
        read = await integrations_mod.test_integration(str(nova.id), user=member, db=session)
        assert read["status"] == "error"
        assert read["message"].startswith(f"Last sync of {_REPO} failed (3 consecutive): ")
        assert not any(marker in read["message"] for marker in _HIDDEN), read["message"]
        assert "manage this integration" in read["message"]
        # The rule source routes only ever serve people who manage it.
        with pytest.raises(HTTPException) as refused:
            await nova_rules_mod.list_nova_sources(user=member, db=session)
        assert refused.value.status_code == 403
