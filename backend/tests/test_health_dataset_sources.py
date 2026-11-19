"""Where a Concierge-built dataset entry came from (#8257): one source row per entry.

A source is added once per dataset; its case and source kinds are the known
ones; a deleted entry or dataset takes its source row with it.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select

from app.models.health_dataset import CASE_TO_FIX, SOURCE_FLOW_RUN, HealthDatasetEntrySource

pytestmark = pytest.mark.integration


async def _entry(s, dataset, position):
    from app.models.dataset import DatasetEntry

    entry = DatasetEntry(dataset_id=dataset.id, position=position, input_prompt=f"q{position}", expected_answer="")
    s.add(entry)
    await s.flush()
    return entry


def _source(entry, source_id, **overrides):
    return HealthDatasetEntrySource(
        entry_id=entry.id,
        dataset_id=entry.dataset_id,
        case_kind=overrides.pop("case_kind", CASE_TO_FIX),
        source_kind=overrides.pop("source_kind", SOURCE_FLOW_RUN),
        source_id=source_id,
        recorded_calls=[],
        **overrides,
    )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"case_kind": "maybe"}, "ck_health_entry_sources_case_kind"),
        ({"source_kind": "email"}, "ck_health_entry_sources_source_kind"),
    ],
    ids=["case-kind", "source-kind"],
)
async def test_an_unknown_kind_is_refused(overrides, constraint):
    import app.models  # noqa: F401
    from app.models.dataset import Dataset
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("conciergesrckind", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        dataset = Dataset(name="Concierge", family="PROSE", concierge_built=True)
        s.add(dataset)
        await s.flush()
        s.add(_source(await _entry(s, dataset, 0), uuid.uuid4(), **overrides))
        with pytest.raises(sa.exc.IntegrityError, match=constraint):
            await s.flush()


async def test_a_source_is_added_once_per_dataset_and_goes_with_its_entry():
    import app.models  # noqa: F401
    from app.models.dataset import Dataset, DatasetEntry
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("conciergesrcuniq", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        dataset = Dataset(name="Concierge", family="PROSE", concierge_built=True)
        s.add(dataset)
        await s.flush()
        source_id = uuid.uuid4()
        first = await _entry(s, dataset, 0)
        first_id = first.id
        s.add(_source(first, source_id, source_node_agent_ids=[uuid.uuid4()]))
        await s.flush()
        second = await _entry(s, dataset, 1)
        await s.commit()
        async with s.begin_nested():
            s.add(_source(second, source_id))
            with pytest.raises(sa.exc.IntegrityError, match="uq_health_entry_sources_source"):
                await s.flush()
        await s.execute(sa.delete(DatasetEntry).where(DatasetEntry.id == first_id))
        await s.commit()
        left = await s.scalar(select(func.count()).select_from(HealthDatasetEntrySource))

    assert left == 0
