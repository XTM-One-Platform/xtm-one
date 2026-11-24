"""The ``s1495swprofile01`` revision: the profile's tech stack rows become hand-typed software (#1495, slice 3b).

Executed against PostgreSQL: every row of the current profile version survives
as a Software, a hand CPE kept as the override (set on an existing synced
software too), a SaaS row typed ``saas``; the downgrade removes what it added.
The revision is loaded from its file: a migration keeps meaning what it meant
on the day it ran.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.organization import Organization
from app.models.organization_asset import (
    OrganizationAsset,
    OrganizationAssetSoftware,
    OrganizationSoftware,
    OrganizationSoftwareVersion,
)
from app.models.organization_context import OrganizationContextVersion
from app.models.user import User
from tests.scratch_database import scratch_database, with_referenced_tables

_REVISION = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("s1495swprofile01_*.py"))


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("s1495swprofile01", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()

_CPE = "cpe:2.3:a:microsoft:exchange_server:*:*:*:*:*:*:*:*"
OLD_STACK = [{"product": "Retired tool", "vendor": "Nobody"}]
STACK = [
    {"product": "Exchange Server", "vendor": "Microsoft", "cpe": _CPE, "source": "manual"},
    {"product": "1Password", "vendor": "", "kind": "saas"},
    {"product": "nginx", "vendor": "F5", "version": "1.25.3", "source": "openaev", "cpe": "cpe:2.3:a:f5:nginx:1.25.3"},
    {"product": "Chrome", "vendor": "Google", "cpe": "cpe:2.3:a:google:chrome:*", "source": "csv"},
    {"product": "exchange server", "vendor": "microsoft"},
    {"product": "  ", "vendor": "blank"},
    "not a row",
]


def test_the_plan_keeps_one_software_per_vendor_and_product_and_every_hand_cpe() -> None:
    planned = {item["name"]: item for item in revision.plan(STACK)}
    assert sorted(planned) == ["1Password", "F5 nginx", "Google Chrome", "Microsoft Exchange Server"]
    assert planned["Microsoft Exchange Server"]["cpe_override"] == _CPE
    assert planned["1Password"]["software_type"] == "saas"
    assert planned["F5 nginx"]["cpe_override"] == "", "a reported CPE is not the administrator's"
    assert planned["F5 nginx"]["reported_cpe"].startswith("cpe:2.3:a:f5:nginx")
    assert planned["F5 nginx"]["version"] == "1.25.3"
    assert planned["Google Chrome"]["cpe_override"] == "cpe:2.3:a:google:chrome:*", "csv is a hand source"


def test_a_later_duplicate_row_only_adds_the_hand_cpe_the_first_lacked() -> None:
    rows = [{"product": "Jira", "vendor": "Atlassian"}, {"product": "jira", "vendor": "atlassian", "cpe": "cpe:x"}]
    (item,) = revision.plan(rows)
    assert item["product"] == "Jira" and item["cpe_override"] == "cpe:x"


def _run(sync_conn: Any, step: Any) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        step()


async def _migrate(engine: AsyncEngine, step: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_run, step)


async def _software(engine: AsyncEngine, org: uuid.UUID) -> dict[str, dict[str, Any]]:
    soft = OrganizationSoftware.__table__
    async with engine.connect() as conn:
        rows = (await conn.execute(select(soft).where(soft.c.organization_id == org))).mappings().all()
        versions = (
            await conn.execute(
                select(
                    OrganizationSoftwareVersion.__table__.c.software_id, OrganizationSoftwareVersion.__table__.c.version
                )
            )
        ).all()
    by_id = {sid: v for sid, v in versions}
    return {row["name"]: {**dict(row), "version": by_id.get(row["id"])} for row in rows}


@pytest.mark.integration
async def test_every_tech_stack_row_becomes_a_software_and_the_downgrade_removes_only_what_it_added() -> None:
    models: list[Any] = [
        OrganizationContextVersion,
        OrganizationSoftware,
        OrganizationSoftwareVersion,
        OrganizationAsset,
        OrganizationAssetSoftware,
        User,
        Agent,
    ]
    tables = with_referenced_tables([cast(Table, m.__table__) for m in models])
    async with scratch_database("swprofile", tables=tables) as db:
        engine = db.engine
        org, other = uuid.uuid4(), uuid.uuid4()
        synced = uuid.uuid4()
        user_id, agent_id = uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                cast(Table, User.__table__)
                .insert()
                .values(id=user_id, email="s@example.com", full_name="s", hashed_password="x", role="user")
            )
            await conn.execute(
                cast(Table, Agent.__table__)
                .insert()
                .values(
                    id=agent_id,
                    user_id=user_id,
                    name="vuln",
                    organization_context_categories=["tech_stack", "crown_jewels", "software"],
                )
            )
            for oid, name in ((org, "Us"), (other, "Them")):
                await conn.execute(
                    cast(Table, Organization.__table__)
                    .insert()
                    .values(
                        id=oid,
                        name=name,
                        role="primary" if oid == org else "tracked",
                        relationship=None if oid == org else "provider",
                    )
                )
            versions = cast(Table, OrganizationContextVersion.__table__)
            await conn.execute(
                versions.insert(),
                [
                    {"id": uuid.uuid4(), "organization_id": org, "version": 1, "profile": {"tech_stack": OLD_STACK}},
                    {"id": uuid.uuid4(), "organization_id": org, "version": 2, "profile": {"tech_stack": STACK}},
                    {"id": uuid.uuid4(), "organization_id": other, "version": 1, "profile": {"name": "Them"}},
                ],
            )
            # A software a source already reports: the hand CPE becomes its override, no duplicate.
            await conn.execute(
                cast(Table, OrganizationSoftware.__table__)
                .insert()
                .values(
                    id=synced,
                    organization_id=org,
                    vendor="Google",
                    product="Chrome",
                    vendor_key="google",
                    product_key="chrome",
                    name="Google Chrome",
                    name_key="google chrome",
                    software_type="application",
                    cpe_state="unresolved",
                    origin="synced",
                    sources=["intune"],
                )
            )

        await _migrate(engine, revision.upgrade)
        after = await _software(engine, org)
        await _migrate(engine, revision.upgrade)  # idempotent: a second run adds nothing
        again = await _software(engine, org)

        assert sorted(after) == ["1Password", "F5 nginx", "Google Chrome", "Microsoft Exchange Server"]
        assert "Nobody Retired tool" not in after, "only the current profile version is read"
        assert again.keys() == after.keys()
        exchange = after["Microsoft Exchange Server"]
        assert exchange["origin"] == "manual" and exchange["cpe_override"] == _CPE
        assert exchange["cpe_state"] == "override" and exchange["cpe"] == _CPE
        assert after["1Password"]["software_type"] == "saas" and after["1Password"]["origin"] == "manual"
        nginx = after["F5 nginx"]
        assert nginx["cpe_state"] == "reported" and nginx["cpe_override"] == ""
        assert nginx["version"] == "1.25.3" and sorted(nginx["sources"]) == ["manual", "openaev"]
        chrome = after["Google Chrome"]
        assert chrome["id"] == synced and chrome["origin"] == "synced"
        assert chrome["cpe_override"] == "cpe:2.3:a:google:chrome:*"
        assert await _software(engine, other) == {}
        assert await _selection(engine, agent_id) == ["software", "crown_jewels"], "tech_stack reads software"

        await _migrate(engine, revision.downgrade)
        back = await _software(engine, org)
        assert sorted(back) == ["Google Chrome"], "the hand-typed software the revision added are gone"
        assert back["Google Chrome"]["cpe_override"] == "", "the override it set is cleared"
        assert await _selection(engine, agent_id) == ["tech_stack", "crown_jewels"]


async def _selection(engine: AsyncEngine, agent_id: uuid.UUID) -> Any:
    agents = cast(Table, Agent.__table__)
    async with engine.connect() as conn:
        return (
            await conn.execute(select(agents.c.organization_context_categories).where(agents.c.id == agent_id))
        ).scalar_one()
