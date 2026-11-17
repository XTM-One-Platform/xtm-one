"""Provisioning and AI-state resolution against real PostgreSQL.

The structural tests assert that boot *delegates* and that the ordering is
preserved. These run the thing: provisioning actually creates content, is safe
to repeat, and the tri-state column resolves the way FR-003 says.

Skips rather than fails when PostgreSQL is unreachable, matching the other
``integration``-marked tests here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.platform_settings import PlatformSettings
from app.services import ai_state, provisioning
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with the full schema, dropped on exit."""
    async with scratch_database(
        prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.sessions


# --- FR-003: the tri-state, against a real column -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "stored", "expected"),
    [
        ("xtm_one", None, True),
        ("ai_disabled", None, False),
        ("ai_disabled", False, True),
        ("xtm_one", True, False),
    ],
)
async def test_resolution_against_a_real_column(monkeypatch, mode, stored, expected):
    """The unit tests use a fake session. This proves the column actually
    round-trips NULL as distinct from false — the distinction FR-003 rests on,
    and the one a non-nullable column would have destroyed silently.
    """
    async with _temp_database("ai_state") as session, session() as db:
        db.add(PlatformSettings(ai_disabled=stored))
        await db.commit()

        monkeypatch.setattr(ai_state.settings, "platform_mode", mode)
        assert await ai_state.is_ai_enabled(db) is expected


@pytest.mark.asyncio
async def test_null_is_stored_and_read_back_as_null():
    """A default on the column would make this pass while breaking FR-003."""
    async with _temp_database("ai_state") as session, session() as db:
        db.add(PlatformSettings())
        await db.commit()
        stored = await db.scalar(select(PlatformSettings.ai_disabled))
        assert stored is None, "the column must arrive NULL, not defaulted"


# --- FR-012: provisioning runs, and runs twice ----------------------------


@pytest.mark.asyncio
async def test_provisioning_creates_content_and_is_idempotent(monkeypatch):
    """The ordering moved out of boot with no test behind it. This is that test:
    it runs the real sequence against real PostgreSQL, then runs it again.

    Idempotence is what lets the runtime toggle reuse the boot path (FR-012);
    without it, turning AI on twice would duplicate the built-in fleet.
    """
    async with _temp_database("provision") as session:
        # ``ai_disabled``, not ``xtm_one``. The first version of this test
        # patched the mode to xtm_one — the one input that cannot occur on the
        # path the test exists to cover — and so passed while a runtime enable
        # would have seeded nothing, because every seeder gates on
        # ``platform_mode in ("xtm_one", "dev")``.
        monkeypatch.setattr(ai_state.settings, "platform_mode", "ai_disabled")

        await provisioning.provision_builtin_content()
        async with session() as db:
            first = await db.scalar(select(func.count()).select_from(Agent))

        assert first > 0, "provisioning created no agents at all"

        await provisioning.provision_builtin_content()
        async with session() as db:
            second = await db.scalar(select(func.count()).select_from(Agent))

        assert second == first, (
            f"re-running provisioning duplicated agents: {first} -> {second}"
        )


@pytest.mark.asyncio
async def test_ai_disabled_seeds_the_xtm_one_content_set(monkeypatch):
    """FR-012: turning AI on must create what boot would have.

    Asserted at the boundary rather than through the whole sequence: the mode
    the seeders receive is the entire difference between provisioning working
    and silently doing nothing.
    """
    monkeypatch.setattr(ai_state.settings, "platform_mode", "ai_disabled")
    assert ai_state.content_mode() == "xtm_one"

    monkeypatch.setattr(ai_state.settings, "platform_mode", "copilot")
    assert ai_state.content_mode() == "copilot", "other modes are unchanged"


# --- The General Assistant link an administrator chose is kept -------


def _agent(slug: str) -> Agent:
    return Agent(
        name=slug, slug=slug, enabled=True, user_id=None, is_builtin=True,
        is_company_managed=True, integration_bindings={}, channel_config=None,
        mcp_server_ids=[], custom_tool_ids=[], knowledge_base_ids=[],
        builtin_tool_ids=[], handover_agent_ids=[], output_format="markdown",
        memory_short_term_enabled=False, memory_medium_term_enabled=False,
        memory_persistent_enabled=False, memory_user_enabled=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "stored", "expected"),
    [
        # Never configured: boot and the AI toggle fill in the CTEM Assistant.
        (False, None, "ctem"),
        (False, "other", "ctem"),
        # An administrator chose "no agent", or another agent: kept.
        (True, None, None),
        (True, "other", "other"),
    ],
)
async def test_wiring_links_the_ctem_assistant_only_where_no_admin_chose(
    configured, stored, expected
):
    """Boot and the AI toggle run ``wire_default_agents``, which linked the CTEM
    Assistant over whatever an administrator had saved. Run twice: a
    second boot must not undo the first one's answer either."""
    async with _temp_database("ga_link") as session:
        async with session() as db:
            ctem, other = _agent("ctem-assistant"), _agent("other-assistant")
            db.add_all([ctem, other])
            await db.flush()
            agents = {"ctem": ctem.id, "other": other.id, None: None}
            db.add(
                GeneralAssistantConfig(
                    linked_agent_id=agents[stored],
                    linked_agent_configured=configured,
                )
            )
            await db.commit()

        await provisioning.wire_default_agents()
        await provisioning.wire_default_agents()

        async with session() as db:
            row = (await db.execute(select(GeneralAssistantConfig))).scalar_one()
            assert row.linked_agent_id == agents[expected]
            assert row.linked_agent_configured is configured


@pytest.mark.asyncio
async def test_a_new_general_assistant_row_is_not_configured():
    """A row the ORM did not write (the column's server default, which the
    migration's ``DEFAULT false`` mirrors) is not an administrator's choice."""
    async with _temp_database("ga_link") as session, session() as db:
        await db.execute(
            text(
                "INSERT INTO general_assistant_config "
                "(id, system_prompt, handover_prompt, created_at, updated_at) "
                "VALUES (gen_random_uuid(), '', '', now(), now())"
            )
        )
        await db.commit()
        stored = await db.scalar(select(GeneralAssistantConfig.linked_agent_configured))
        assert stored is False
