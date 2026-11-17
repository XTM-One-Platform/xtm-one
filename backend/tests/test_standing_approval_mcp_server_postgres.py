"""The server an MCP approval was granted on, through real PostgreSQL.

The unit tests pin the decision; this pins the round trip it depends on: a
"Yes, always" stores the approved tool's ``tool_entity_id``, re-granting the name
on another server moves it, and ``list_active_exceptions`` hands it back to the
gate - which then covers the tool of that server and no other.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.tool_approval import ToolApprovalPreference
from app.models.user import User
from app.services.tool_approval_service import grant_exception, list_active_exceptions
from app.tools.approval import evaluate_gate
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_TABLES = [User.__table__, ToolApprovalPreference.__table__, AuditLog.__table__]

TOOL = "search_wiki"
FP = "a" * 64


async def _seed_user(db) -> uuid.UUID:
    email = f"u-{uuid.uuid4().hex[:8]}@example.com"
    user = User(id=uuid.uuid4(), email=email, hashed_password="x", full_name=email)
    db.add(user)
    await db.commit()
    return user.id


def _decide(exceptions, sources, entity_ids, *, server):
    return evaluate_gate(
        TOOL,
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions=exceptions,
        current_fingerprint=FP,
        tool_source="mcp:Wiki",
        tool_entity_id=server,
        exception_sources=sources,
        exception_entity_ids=entity_ids,
    ).action


@pytest.mark.asyncio
async def test_an_mcp_approval_covers_the_server_it_was_last_granted_on():
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    async with scratch_database("hitl_mcp", tables=_TABLES) as scratch, scratch.sessions() as db:
        user_id = await _seed_user(db)

        await grant_exception(
            db,
            user_id=user_id,
            tool_key=TOOL,
            tool_source="mcp:Wiki",
            schema_fingerprint=FP,
            tool_entity_id=first,
        )
        await db.commit()

        sources: dict[str, str] = {}
        entity_ids: dict[str, str] = {}
        exceptions = await list_active_exceptions(
            db, user_id, sources=sources, entity_ids=entity_ids
        )
        assert entity_ids == {TOOL: first}
        assert _decide(exceptions, sources, entity_ids, server=first) == "execute"
        assert _decide(exceptions, sources, entity_ids, server=second) == "pause"

        # "Yes, always" on the other server's tool of that name.
        await grant_exception(
            db,
            user_id=user_id,
            tool_key=TOOL,
            tool_source="mcp:Wiki",
            schema_fingerprint=FP,
            tool_entity_id=second,
        )
        await db.commit()

        rows = (
            await db.scalars(
                select(ToolApprovalPreference).where(
                    ToolApprovalPreference.user_id == user_id,
                    ToolApprovalPreference.revoked_at.is_(None),
                )
            )
        ).all()
        assert [r.tool_entity_id for r in rows] == [second]

        sources, entity_ids = {}, {}
        exceptions = await list_active_exceptions(
            db, user_id, sources=sources, entity_ids=entity_ids
        )
        assert _decide(exceptions, sources, entity_ids, server=second) == "execute"
        assert _decide(exceptions, sources, entity_ids, server=first) == "pause"


@pytest.mark.asyncio
async def test_an_approval_stored_without_a_server_covers_no_mcp_tool():
    """What every MCP approval granted before the server id was recorded looks like after the upgrade."""
    server = str(uuid.uuid4())
    async with scratch_database("hitl_mcp", tables=_TABLES) as scratch, scratch.sessions() as db:
        user_id = await _seed_user(db)
        db.add(
            ToolApprovalPreference(
                user_id=user_id,
                tool_key=TOOL,
                tool_source="mcp:Wiki",
                schema_fingerprint=FP,
            )
        )
        await db.commit()

        sources: dict[str, str] = {}
        entity_ids: dict[str, str] = {}
        exceptions = await list_active_exceptions(
            db, user_id, sources=sources, entity_ids=entity_ids
        )

        assert exceptions == {TOOL: FP}
        assert entity_ids == {}
        assert _decide(exceptions, sources, entity_ids, server=server) == "pause"
