"""No behaviour change on rollout, and the instructions-governed position (#10168, B2-I of #10000).

Pinned here:
- The member run prompt block of a fleet nobody moved is byte-identical to the
  block without the instructions part (B2-I rule 4).
- An announced position adds exactly one line; a stored but not announced one
  (an initial reading) adds nothing.
- Positions: the seven values, between-step ones included; an unknown value is
  refused; moving toward more autonomy is administrator only.
- The migration restores only the rows p10153fltpin1 moved, created before
  #10164 merged, that nobody moved since (audit row).
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest

from app.services import fleet_autonomy
from app.services.fleet_autonomy import (
    INSTRUCTIONS_POSITION_LABELS,
    INSTRUCTIONS_POSITIONS,
    ensure_instructions_change_allowed,
    fleet_autonomy_block,
    instructions_line,
    normalize_instructions_position,
)
from app.services.fleets import FleetActionRefusedError

_GATE = {
    "key": "ticket.create",
    "action": "Create a ticket",
    "module": "platform",
    "impact": "low",
    "tools": [{"name": "create_jira_issue", "when": {}}],
}


@pytest.mark.parametrize("level", ["full_gates", "medium", "low", "autonomous"])
@pytest.mark.parametrize("gates", [None, [], [_GATE]])
def test_the_block_of_a_fleet_nobody_moved_is_unchanged(level, gates):
    before = fleet_autonomy_block(level, gates)
    assert fleet_autonomy_block(level, gates, None) == before
    assert fleet_autonomy_block(level, gates, instructions_line("medium_low", None)) == before
    assert fleet_autonomy_block(level, gates, instructions_line("medium_low", False)) == before


def test_an_announced_position_adds_one_line():
    before = fleet_autonomy_block("full_gates", None)
    line = instructions_line("medium_low", True)
    after = fleet_autonomy_block("full_gates", None, line)
    assert after == before + "\n" + line
    assert "between Medium and Low" in line
    assert "the platform does not enforce this part" in line


def test_every_position_has_a_label_and_the_scale_has_seven_steps():
    assert len(INSTRUCTIONS_POSITIONS) == 7
    assert set(INSTRUCTIONS_POSITION_LABELS) == set(INSTRUCTIONS_POSITIONS)
    for level in fleet_autonomy.AUTONOMY_LEVELS:
        assert level in INSTRUCTIONS_POSITIONS


def test_an_unknown_position_is_refused_and_none_clears():
    assert normalize_instructions_position(None) is None
    assert normalize_instructions_position("medium_low") == "medium_low"
    with pytest.raises(FleetActionRefusedError) as exc:
        normalize_instructions_position("halfway")
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    ("previous", "new", "raise_"),
    [
        (None, "full_gates", False),
        (None, "medium_low", True),
        ("medium", "medium_low", True),
        ("medium_low", "medium", False),
        ("low", None, False),
        ("autonomous", "low_autonomous", False),
    ],
)
def test_only_an_administrator_moves_toward_more_autonomy(previous, new, raise_):
    ensure_instructions_change_allowed(previous=previous, new=new, is_admin=True)
    if raise_:
        with pytest.raises(FleetActionRefusedError) as exc:
            ensure_instructions_change_allowed(previous=previous, new=new, is_admin=False)
        assert exc.value.status_code == 403
    else:
        ensure_instructions_change_allowed(previous=previous, new=new, is_admin=False)


class _Rows:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Db:
    def __init__(self, row=None, fail=False):
        self.row = row
        self.fail = fail

    def begin_nested(self):
        return _Nested()

    async def execute(self, _statement):
        if self.fail:
            raise RuntimeError("database gone")
        return _Rows(self.row)


async def test_the_run_reads_the_line_only_when_announced():
    fid = uuid.uuid4()
    assert await fleet_autonomy.fleet_instructions_for_run(_Db(("medium_low", True)), fid) == instructions_line(
        "medium_low", True
    )
    assert await fleet_autonomy.fleet_instructions_for_run(_Db(("medium_low", None)), fid) is None
    assert await fleet_autonomy.fleet_instructions_for_run(_Db(None), fid) is None
    assert await fleet_autonomy.fleet_instructions_for_run(_Db(fail=True), fid) is None
    assert await fleet_autonomy.fleet_instructions_for_run(_Db(("low", True)), None) is None


def _migration():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "q10168fltinst1_fleet_instructions_position.py"
    )
    spec = importlib.util.spec_from_file_location("q10168fltinst1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_restores_only_the_rows_p10153_moved():
    module = _migration()
    assert module.down_revision == "p10153fltpin1"
    sql = module._RESTORE_SQL
    assert sql.startswith("UPDATE fleets SET autonomy_level = 'full_gates', approval_pins = NULL ")
    for clause in (
        "is_builtin = false",
        "autonomy_level = 'autonomous'",
        "approval_pins::text = '{}'",
        "created_at < '2026-11-08 20:14:48+00'",
        "'structured' -> 'gates'",
        "a.action = 'autonomy_level_change'",
    ):
        assert clause in sql
