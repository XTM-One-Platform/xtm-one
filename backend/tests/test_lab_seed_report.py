"""The lab's sample report seed (``.xtm-one/lab/seed_report.py``).

No API creates a report and a lab's stub model cannot call ``generate_report``, so the lab writes one through
``save_report``. These tests pin that its spec passes the platform's own validation, that it carries every block
kind the renderer knows (a new kind fails here until the sample shows it), and, on PostgreSQL, that a restart
finds the report again instead of writing a second one.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.services.report_spec import REPORT_BLOCKS, validate_spec

SEED = Path(__file__).resolve().parents[1].parent / ".xtm-one" / "lab" / "seed_report.py"

pytestmark = pytest.mark.skipif(not SEED.is_file(), reason="no .xtm-one next to the backend")


def _seed_module():
    spec = importlib.util.spec_from_file_location("lab_seed_report", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_sample_report_passes_the_platform_validation():
    module = _seed_module()
    validated = validate_spec(module.spec())
    assert validated["title"] == module.REPORT_TITLE


def test_the_sample_report_shows_every_block_kind():
    kinds = {block["block"] for block in _seed_module().spec()["blocks"]}
    assert kinds == set(REPORT_BLOCKS)


@pytest.mark.integration
async def test_the_seed_run_twice_writes_one_report():
    from app.models.report import Report
    from app.models.user import User
    from tests.scratch_database import ALL_TABLES, scratch_database

    module = _seed_module()
    async with scratch_database(
        "lab_report", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="lab@example.com", hashed_password="x", full_name="Lab Admin", role="admin")
            db.add(owner)
            await db.commit()
            owner_id = str(owner.id)

        first = await module.seed(owner_id)
        second = await module.seed(owner_id)

        assert first["found_again"] is False and second["found_again"] is True
        assert second["report"] == first["report"]
        async with scratch.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Report))
            report = await db.get(Report, uuid.UUID(first["report"]))
        assert count == 1
        assert report.title == module.REPORT_TITLE and report.block_count == len(REPORT_BLOCKS)
