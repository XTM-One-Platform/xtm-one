"""#2524: migration r2524relcve01 names the relationship signals by their CVE, nothing else moves."""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from types import ModuleType
from typing import Any, cast

import pytest
from sqlalchemy import Table, select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.ctem_signal import CtemSignal
from app.models.organization import Organization
from tests.scratch_database import scratch_database, with_referenced_tables

_MIGRATION = next((pathlib.Path(__file__).resolve().parents[1] / "alembic/versions").glob("r2524relcve01_*.py"))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MIGRATION.stem, _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cve_is_read_from_the_evidence_cve_then_the_message() -> None:
    migration = _load_migration()
    assert migration.cve_of_evidence({"cve": "cve-2026-1234"}) == "CVE-2026-1234"
    assert migration.cve_of_evidence({"cves": ["CVE-2025-0001"]}) == "CVE-2025-0001"
    message = {"message": "[relationship] Veeam Plug-In for AWS has CVE-2026-123634"}
    assert migration.cve_of_evidence(message) == "CVE-2026-123634"
    assert migration.cve_of_evidence('{"message": "has CVE-2026-123634"}') == "CVE-2026-123634"
    assert migration.cve_of_evidence({"message": "no id here"}) == ""
    assert migration.cve_of_evidence(None) == ""


def _signal(org: uuid.UUID, ref: str, evidence: dict[str, Any], **kw: Any) -> CtemSignal:
    return CtemSignal(
        id=uuid.uuid4(),
        organization_id=org,
        trigger_path="new_vulnerability",
        lens="inside",
        source_provider="opencti",
        subject_kind="vulnerability",
        subject_ref=ref,
        evidence=evidence,
        dedupe_key=f"new_vulnerability:opencti:{uuid.uuid4().hex}",
        recorded_via="listener",
        recorded_by="system",
        **kw,
    )


@pytest.mark.integration
async def test_the_migration_renames_only_the_subject_and_the_downgrade_restores_it() -> None:
    migration = _load_migration()
    tables = with_referenced_tables([cast(Table, CtemSignal.__table__)])
    async with scratch_database("ctemrelcve", tables=tables) as db:
        async with db.sessions() as session:
            org = Organization(id=uuid.uuid4(), name="A", role="primary")
            session.add(org)
            await session.flush()
            named = _signal(
                org.id,
                "vulnerability--c0cdd6e3",
                {"stix_type": "relationship:has", "message": "[relationship] Veeam has CVE-2026-123634"},
                state="confirmed",
                decision={"outcome": "in_scope"},
                seen_count=3,
            )
            nameless = _signal(org.id, "vulnerability--4e863e78", {"message": "[relationship] X has a flaw"})
            other = _signal(org.id, "CVE-2017-8570", {"cve": "CVE-2017-8570"}, subject_label="CVE-2017-8570")
            session.add_all([named, nameless, other])
            await session.commit()
            ids = (named.id, nameless.id, other.id)

        engine = db.sessions.kw["bind"]
        async with engine.connect() as conn:
            renamed, left = await conn.run_sync(lambda sync: migration.rename_in_batches(sync, batch_size=1))
            await conn.commit()
        assert (renamed, left) == (1, 1)

        async with db.sessions() as check:
            rows = {r.id: r for r in (await check.execute(select(CtemSignal))).scalars()}
            first = rows[ids[0]]
            assert (first.subject_ref, first.subject_label) == ("CVE-2026-123634", "CVE-2026-123634")
            assert first.evidence["target_ref"] == "vulnerability--c0cdd6e3"
            assert (first.state, first.decision, first.seen_count) == ("confirmed", {"outcome": "in_scope"}, 3)
            assert rows[ids[1]].subject_ref == "vulnerability--4e863e78"
            assert (rows[ids[2]].subject_ref, rows[ids[2]].subject_label) == ("CVE-2017-8570", "CVE-2017-8570")

        async with engine.connect() as conn:
            restored = await conn.run_sync(lambda sync: migration.restore_in_batches(sync, batch_size=1))
            await conn.commit()
        assert restored == 1
        async with db.sessions() as check:
            back = await check.get(CtemSignal, ids[0])
            assert back is not None
            assert (back.subject_ref, back.subject_label) == ("vulnerability--c0cdd6e3", None)
