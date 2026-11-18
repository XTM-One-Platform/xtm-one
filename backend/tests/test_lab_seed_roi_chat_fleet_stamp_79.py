"""The lab's ROI chat lines carry their agent's home fleet (#79, dev:harness-gaps lab-seed-roi-chat-records-fleet-stamp)."""

from __future__ import annotations

import ast
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_roi_chat_records.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _script():
    spec = importlib.util.spec_from_file_location("lab_seed_roi_chat_records_79", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_line_of_an_agent_with_a_home_fleet_is_stamped_with_it():
    module = _script()
    ids = {role: str(uuid.uuid4()) for role in module.ROLES}
    fleet = str(uuid.uuid4())
    rows = [r["record"] for r in module.plan(datetime.now(UTC), ids, {ids["fleet_agent"]: fleet})]
    on_fleet = [r for r in rows if r["agent_id"] == ids["fleet_agent"]]
    assert on_fleet and all(r["fleet_id"] == fleet for r in on_fleet)
    assert all(r["fleet_id"] is None for r in rows if r["agent_id"] != ids["fleet_agent"])
    assert all(r["fleet_backfilled"] is False for r in rows)


def test_the_plan_without_fleets_stamps_nothing():
    module = _script()
    ids = {role: str(uuid.uuid4()) for role in module.ROLES}
    assert all(r["record"]["fleet_id"] is None for r in module.plan(datetime.now(UTC), ids))


def test_the_written_columns_exist_on_the_model():
    from app.models.roi import RoiChatRecord

    module = _script()
    ids = {role: str(uuid.uuid4()) for role in module.ROLES}
    columns = set(RoiChatRecord.__table__.columns.keys())
    for row in module.plan(datetime.now(UTC), ids):
        assert set(row["record"]) <= columns


def test_the_seed_resolves_home_fleets_like_the_platform_writer_and_counts_them():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "home_fleet_defaults_of(db, ids.values())" in source
    assert "'fleet_stamped'" in ast.unparse(ast.parse(source))


def test_the_lab_fails_on_no_stamped_line_and_logs_the_count():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    helper = ast.unparse(
        next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "seed_roi_chat_records")
    )
    for part in ("'fleet_stamped'", "if not stamped:", "seeded fleet chat stamped: "):
        assert part in helper, part
