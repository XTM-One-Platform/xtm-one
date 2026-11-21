"""#11252: the xtm-one lab seeds an upcoming and a missed scheduled one-off.

Each planned row is run through ``schedule_state`` and ``scheduled_view``, so a change to what the
views call upcoming or missed that the seed would no longer produce fails here, not in a UI review.
"""

from __future__ import annotations

import ast
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_scheduled_one_offs.py"
CHECKS = ROOT / ".xtm-one" / "checks.toml"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_scheduled_one_offs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(fields: dict):
    from app.models.work_item import WorkItem

    item = WorkItem(id=uuid.uuid4(), user_id=uuid.uuid4(), owner_agent_id=uuid.uuid4(), source="user")
    for field, value in fields.items():
        setattr(item, field, value)
    return item


def test_the_planned_rows_read_upcoming_and_missed():
    from app.services.scheduled_work_items import action_of, schedule_state, scheduled_view

    module = _module()
    now = datetime.now(UTC)
    rows = module.plan(now)
    assert tuple(rows) == module.SLOTS == ("upcoming", "missed")
    upcoming, missed = _item(rows["upcoming"]), _item(rows["missed"])
    assert schedule_state(upcoming, now) == "scheduled"
    assert schedule_state(missed, now) == "missed"
    for item, mode in ((upcoming, "instruction"), (missed, "exact")):
        view = scheduled_view(item, now)
        assert view["mode"] == mode and view["preview"], mode
        assert action_of(item.payload)["mode"] == mode
    assert scheduled_view(missed, now)["tool"] == "store_memory"


def test_the_lab_starts_the_seed_and_reads_every_row_back():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_scheduled_one_offs"])
    for part in (
        "'seed_scheduled_one_offs.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/work-items/scheduled?state=",
        "('upcoming', 'missed')",
        "/tasks/work-items",
        "?view=scheduled",
        "'scheduled one-off'",
        "'scheduled one-off (missed)'",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    assert _module().KEY_PREFIX == "lab-scheduled-one-off"
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_scheduled_one_offs(token, base)"
    assert call in seeded and "skipped['scheduled one-off']" in seeded
    # After the sample fleet (its member owns the rows); before the log of the seeded rows.
    assert seeded.index("seed_locked_fleet(") < seeded.index(call) < seeded.index("seeded.json")


def test_lab_copilot_logs_the_seeded_decks_speaker_view():
    helper = ast.unparse(
        next(
            n
            for n in ast.parse(LAB.read_text(encoding="utf-8")).body
            if isinstance(n, ast.FunctionDef) and n.name == "seed_presentation"
        )
    )
    assert "'deck'" in helper and "/speaker" in helper
    text = CHECKS.read_text(encoding="utf-8")
    assert '"seeded deck: <url>' in text
    assert '"seeded scheduled one-off: <url>"' in text
