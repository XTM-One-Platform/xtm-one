"""#10422: the xtm-one lab seeds blocked work items with no reason.

Each planned row is run through ``blocked_why``, so a change to what counts as a reason that the
seed would no longer produce as ``missing`` fails here, not in a UI review.
"""

from __future__ import annotations

import ast
import importlib.util
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_no_reason_blocked.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_no_reason_blocked", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(fields: dict):
    from app.models.work_item import WorkItem

    item = WorkItem(id=uuid.uuid4(), user_id=uuid.uuid4(), title=fields["title"], source="agent")
    for field, value in fields.items():
        setattr(item, field, value)
    return item


def test_every_planned_row_reads_blocked_with_no_reason():
    from app.services.work_item_blocked_refs import blocked_why, has_why

    module = _module()
    workspace = uuid.uuid4()
    rows = module.plan(workspace)
    assert tuple(rows) == module.SLOTS == ("operations", "fleet")
    for slot, fields in rows.items():
        assert blocked_why(_item(fields)) == {"text": "No reason given", "missing": True}, slot
        assert not has_why(**{k: fields[k] for k in ("blocked_on", "blocked_on_refs", "blocked_reason", "needs_human")})
    assert rows["fleet"]["workspace_id"] == workspace
    assert rows["operations"]["workspace_id"] is None


def test_no_fleet_leaves_the_fleet_row_out():
    assert tuple(_module().plan(None)) == ("operations",)


def test_the_lab_starts_the_seed_and_reads_every_row_back():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_no_reason_blocked"])
    for part in (
        "'seed_no_reason_blocked.py'",
        "sys.executable",
        "env=seed_script_env()",
        "correlation_key_prefix=lab-no-reason-blocked",
        "'blocked_why'",
        "'missing'",
        "&fleet=",
        "'no-reason blocked work items'",
        "/tasks/work-items?vis=all",
        "/operations/work-items'",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    assert _module().KEY_PREFIX == "lab-no-reason-blocked"
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_no_reason_blocked(token, base, seeded.get('locked fleet', {}).get('id'))"
    assert call in seeded and "skipped['no-reason blocked work items']" in seeded
    # After the locked fleet, so its id is known; before the log of the seeded rows.
    assert seeded.index("seed_locked_fleet(") < seeded.index(call) < seeded.index("seeded.json")
