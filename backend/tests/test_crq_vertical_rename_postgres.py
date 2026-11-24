"""The ``grc`` -> ``crq`` vertical rename, executed against PostgreSQL.

``c1crq2vertical1`` carries the business vertical and the ``grc.*`` intents of
a database written before the rename to ``crq``. A wrong merge either loses
an administrator's binding (a custom agent bound on ``grc`` with its priority
and lock) or fails the deploy on a unique constraint, which only shows against
a real schema.

The revision is loaded from its file and its names are spelled as literals: a
migration must keep meaning what it meant on the day it ran.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.models  # noqa: F401 - registers every table
from app.models.agent import Agent
from app.models.intent import Intent, IntentBinding, PlatformIntentDeclaration, Vertical
from app.models.platform_registration import PlatformRegistration
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "c1crq2vertical1_rename_grc_vertical_to_crq.py"
)


def _load_revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("c1crq2vertical1", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()


@asynccontextmanager
async def _temp_database() -> AsyncIterator[AsyncEngine]:
    async with scratch_database(
        "crq_vertical_rename", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.engine


async def _run(engine: AsyncEngine, old: str, new: str) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: revision._rename(sync, old, new))


async def _agent(db: AsyncSession, slug: str, **kwargs: Any) -> uuid.UUID:
    agent = Agent(name=slug, slug=slug, **kwargs)
    db.add(agent)
    await db.flush()
    return agent.id


async def _intent(db: AsyncSession, name: str, description: str | None = None) -> uuid.UUID:
    intent = Intent(name=name, description=description)
    db.add(intent)
    await db.flush()
    return intent.id


async def _bindings(engine: AsyncEngine) -> list[Any]:
    async with AsyncSession(engine) as db:
        rows = await db.execute(
            select(
                Intent.name,
                IntentBinding.vertical,
                IntentBinding.agent_id,
                IntentBinding.priority,
                IntentBinding.is_locked,
                IntentBinding.priority_overrides,
            ).join(Intent, Intent.id == IntentBinding.intent_id)
        )
        return sorted(rows.all(), key=lambda r: (r[0], str(r[1]), str(r[2])))


async def _intent_names(engine: AsyncEngine) -> set[str]:
    async with AsyncSession(engine) as db:
        return set((await db.execute(select(Intent.name))).scalars().all())


async def _vertical_names(engine: AsyncEngine) -> set[str]:
    async with AsyncSession(engine) as db:
        return set((await db.execute(select(Vertical.name))).scalars().all())


async def _seed_pre_rename(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """A database as a pre-rename release left it, plus a custom binding."""
    async with AsyncSession(engine) as db:
        builtin = await _agent(db, "opencrq-assistant", is_builtin=True)
        custom = await _agent(
            db,
            "ctem-orchestrator",
            supported_verticals=["grc", "cti"],
            supported_intents=["global.assistant", "grc.control_gap_analysis"],
        )
        db.add(Vertical(name="grc", source="platform", platform_identifier="opencrq"))
        db.add(Vertical(name="cti", source="platform", platform_identifier="opencti"))
        assistant = await _intent(db, "global.assistant", "General-purpose assistant")
        gap = await _intent(db, "grc.control_gap_analysis", "Analyze control gaps")
        await _intent(db, "grc.risk_scenario_generator", "Draft risk scenarios")
        db.add(IntentBinding(intent_id=assistant, agent_id=builtin, vertical="grc", priority=5, is_builtin=True))
        db.add(
            IntentBinding(
                intent_id=assistant,
                agent_id=custom,
                vertical="grc",
                priority=50,
                is_locked=True,
                priority_overrides={"grc": 70, "cti": 3},
            )
        )
        db.add(IntentBinding(intent_id=gap, agent_id=builtin, vertical="grc", priority=0, is_builtin=True))
        db.add(PlatformIntentDeclaration(platform_identifier="opencrq", intent_id=gap))
        db.add(
            PlatformRegistration(
                platform_identifier="opencrq",
                platform_url="https://crq.local",
                platform_title="OpenCRQ",
                platform_version="1.0.0",
                business_vertical="grc",
            )
        )
        await db.commit()
    return {"builtin": builtin, "custom": custom}


async def test_a_pre_rename_database_moves_to_crq_and_keeps_the_custom_binding() -> None:
    async with _temp_database() as engine:
        ids = await _seed_pre_rename(engine)

        await _run(engine, "grc", "crq")

        assert await _vertical_names(engine) == {"crq", "cti"}
        assert await _intent_names(engine) == {
            "global.assistant",
            "crq.control_gap_analysis",
            "crq.risk_scenario_generator",
        }
        bindings = await _bindings(engine)
        assert ("global.assistant", "crq", ids["custom"], 50, True, {"cti": 3, "crq": 70}) in bindings
        assert ("global.assistant", "crq", ids["builtin"], 5, False, None) in [
            (n, v, a, p, lock, None) for n, v, a, p, lock, _ in bindings
        ]
        assert ("crq.control_gap_analysis", "crq", ids["builtin"]) in [(n, v, a) for n, v, a, *_ in bindings]
        assert not any(v == "grc" for _, v, *_ in bindings)
        async with AsyncSession(engine) as db:
            declared = (
                (
                    await db.execute(
                        select(Intent.name)
                        .join(PlatformIntentDeclaration, PlatformIntentDeclaration.intent_id == Intent.id)
                        .where(PlatformIntentDeclaration.platform_identifier == "opencrq")
                    )
                )
                .scalars()
                .all()
            )
            assert declared == ["crq.control_gap_analysis"]
            vertical = (await db.execute(select(PlatformRegistration.business_vertical))).scalar_one()
            assert vertical == "crq"
            agent = await db.get(Agent, ids["custom"])
            assert agent is not None
            assert agent.supported_verticals == ["crq", "cti"]
            assert agent.supported_intents == ["global.assistant", "crq.control_gap_analysis"]


async def test_running_it_twice_changes_nothing_more() -> None:
    async with _temp_database() as engine:
        await _seed_pre_rename(engine)
        await _run(engine, "grc", "crq")
        first = await _bindings(engine)
        await _run(engine, "grc", "crq")
        assert await _bindings(engine) == first
        assert await _vertical_names(engine) == {"crq", "cti"}


async def test_rows_already_seeded_under_crq_merge_without_losing_a_binding() -> None:
    """A boot that seeded ``crq`` before the upgrade ran: merged, the pre-rename rows win."""
    async with _temp_database() as engine:
        ids = await _seed_pre_rename(engine)
        async with AsyncSession(engine) as db:
            db.add(Vertical(name="crq", source="platform", platform_identifier="opencrq"))
            seeded_gap = await _intent(db, "crq.control_gap_analysis", None)
            assistant = (await db.execute(select(Intent.id).where(Intent.name == "global.assistant"))).scalar_one()
            # Duplicates of pre-rename bindings, as a seeder writes them.
            db.add(IntentBinding(intent_id=assistant, agent_id=ids["custom"], vertical="crq", priority=0))
            db.add(IntentBinding(intent_id=seeded_gap, agent_id=ids["builtin"], vertical="crq", is_builtin=True))
            db.add(PlatformIntentDeclaration(platform_identifier="opencrq", intent_id=seeded_gap))
            await db.commit()

        await _run(engine, "grc", "crq")

        assert await _vertical_names(engine) == {"crq", "cti"}
        assert "grc.control_gap_analysis" not in await _intent_names(engine)
        bindings = await _bindings(engine)
        custom = [b for b in bindings if b[0] == "global.assistant" and b[2] == ids["custom"]]
        assert custom == [("global.assistant", "crq", ids["custom"], 50, True, {"cti": 3, "crq": 70})]
        gap = [b for b in bindings if b[0] == "crq.control_gap_analysis"]
        assert len(gap) == 1 and gap[0][1] == "crq"
        async with AsyncSession(engine) as db:
            description = (
                await db.execute(select(Intent.description).where(Intent.name == "crq.control_gap_analysis"))
            ).scalar_one()
            assert description == "Analyze control gaps"
            declarations = (
                await db.execute(
                    text("SELECT count(*) FROM platform_intent_declarations WHERE platform_identifier = 'opencrq'")
                )
            ).scalar_one()
            assert declarations == 1


async def test_downgrade_restores_grc() -> None:
    async with _temp_database() as engine:
        ids = await _seed_pre_rename(engine)
        before = await _bindings(engine)
        await _run(engine, "grc", "crq")
        await _run(engine, "crq", "grc")
        assert await _vertical_names(engine) == {"grc", "cti"}
        assert await _intent_names(engine) == {
            "global.assistant",
            "grc.control_gap_analysis",
            "grc.risk_scenario_generator",
        }
        after = await _bindings(engine)
        assert [b[:5] for b in after] == [b[:5] for b in before]
        custom = [b for b in after if b[2] == ids["custom"]]
        assert custom[0][5] == {"cti": 3, "grc": 70}
