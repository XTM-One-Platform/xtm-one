"""The fleet autonomy cursor acts on the computed catalog, truthfully (#10153, slice B2 of #10000).

Pinned here:
- C1: a custom fleet with the computed catalog on gates its computed gates at
  its level (tool rules and run prompt); a declared gate with the same key wins;
  a legacy custom fleet (pins NULL) and a built-in fleet ignore the computed ones.
- C2: a new custom fleet starts at ``autonomous`` with the computed catalog on,
  so nothing it computes waits until a person moves the cursor.
- C4: a pin overrides the level for its gate everywhere; only an administrator
  may pin "run" or drop a "wait" pin.
- The migration's data step targets custom fleets with no declared gate only.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_autonomy, fleet_gate_proposals
from app.services.fleet_autonomy import (
    AUTONOMY_LEVELS,
    clean_pins,
    effective_gates,
    fleet_autonomy_block,
    gate_catalog,
    gate_waits,
    gated_tool_rules,
    pins_lower_approval,
)

_HIGH = {
    "key": "github.high-writes",
    "action": "Create or change content in GitHub",
    "module": "platform",
    "provider": "github",
    "impact": "high",
    "tools": [{"name": "create_github_issue", "when": {}}],
}
_DESTRUCTIVE = {
    "key": "microsoft.destructive-writes",
    "action": "Delete, or send in a person's name, in Microsoft",
    "module": "platform",
    "provider": "microsoft",
    "impact": "destructive",
    "tools": [{"name": "send_microsoft_email", "when": {}}],
}
_LOW = {
    "key": "microsoft.low-writes",
    "action": "Mark, label or draft in Microsoft",
    "module": "platform",
    "provider": "microsoft",
    "impact": "low",
    "tools": [{"name": "create_microsoft_draft", "when": {}}],
}
COMPUTED = [_DESTRUCTIVE, _HIGH, _LOW]


def _names(level, gates):
    return {r["name"] for r in gated_tool_rules(level, gates)}


def test_the_level_acts_on_the_computed_catalog_of_a_custom_fleet():
    gates = effective_gates(None, None, COMPUTED, {})
    assert {g["source"] for g in gates} == {"computed"}
    assert _names("full_gates", gates) == {"create_github_issue", "send_microsoft_email", "create_microsoft_draft"}
    assert _names("medium", gates) == {"create_github_issue", "send_microsoft_email"}
    assert _names("low", gates) == {"send_microsoft_email"}
    assert _names("autonomous", gates) == set()


def test_a_declared_gate_wins_over_the_computed_gate_with_the_same_key():
    declared = [dict(_HIGH, impact="destructive", action="Declared")]
    gates = effective_gates(declared, None, COMPUTED, {})
    by_key = {g["key"]: g for g in gates}
    assert by_key["github.high-writes"]["action"] == "Declared"
    assert by_key["github.high-writes"]["source"] == "stored"
    assert len(gates) == 3


def test_documentation_gates_are_read_when_nothing_is_stored():
    documentation = {"structured": {"gates": [_HIGH]}}
    (gate,) = effective_gates(None, documentation, None, None)
    assert gate["source"] == "documentation"


def test_a_pin_overrides_the_level_everywhere():
    gates = effective_gates(None, None, COMPUTED, {"github.high-writes": "run", "microsoft.low-writes": "wait"})
    assert _names("full_gates", gates) == {"send_microsoft_email", "create_microsoft_draft"}
    assert _names("autonomous", gates) == {"create_microsoft_draft"}
    catalog = {e["key"]: e for e in gate_catalog("medium", gates)}
    assert catalog["github.high-writes"]["pin"] == "run"
    assert catalog["github.high-writes"]["by_level"] == {lvl: False for lvl in AUTONOMY_LEVELS}
    assert catalog["microsoft.low-writes"]["by_level"] == {lvl: True for lvl in AUTONOMY_LEVELS}
    assert catalog["microsoft.destructive-writes"]["pin"] is None
    assert catalog["microsoft.destructive-writes"]["tools"] == ["send_microsoft_email"]


def test_invalid_pins_are_dropped_and_an_unpinned_gate_follows_the_level():
    assert clean_pins({"a": "wait", "b": "maybe", "": "run", "c": "run"}) == {"a": "wait", "c": "run"}
    assert clean_pins(None) == {}
    assert gate_waits("low", {"impact": "high"}) is False
    assert gate_waits("low", {"impact": "high", "pin": "bogus"}) is False
    assert gate_waits("bogus", {"impact": "low"}) is True


@pytest.mark.parametrize(
    ("previous", "new", "lowers"),
    [
        ({}, {"k": "wait"}, False),
        ({}, {"k": "run"}, True),
        ({"k": "wait"}, {}, True),
        ({"k": "wait"}, {"k": "run"}, True),
        ({"k": "run"}, {}, False),
        ({"k": "run"}, {"k": "wait"}, False),
        ({"k": "run"}, {"k": "run"}, False),
    ],
)
def test_only_an_administrator_may_let_an_action_run_with_less_approval(previous, new, lowers):
    assert pins_lower_approval(previous, new) is lowers


def test_the_run_prompt_lists_what_waits_and_counts_the_computed_gates_that_run_alone():
    gates = effective_gates(None, None, COMPUTED, {})
    block = fleet_autonomy_block("low", gates)
    assert "Delete, or send in a person's name, in Microsoft" in block
    assert "Create or change content in GitHub" not in block
    assert "2 other write action group(s)" in block
    assert "other write action" not in fleet_autonomy_block("full_gates", gates)


class _Rows:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Db:
    def __init__(self, row: object) -> None:
        self.row = row

    async def execute(self, _statement):
        return _Rows(self.row)


@pytest.fixture
def computed(monkeypatch):
    calls: list[object] = []

    async def _configuration(_db, fleet_id):
        calls.append(fleet_id)
        return SimpleNamespace(proposals=lambda: [dict(g) for g in COMPUTED])

    monkeypatch.setattr(fleet_gate_proposals, "fleet_configuration", _configuration)
    return calls


async def test_a_custom_fleet_with_the_computed_catalog_on_reads_it_at_run_time(computed):
    level, gates = await fleet_gate_proposals.fleet_effective_gates(
        _Db(("medium", None, None, {}, False, None)), uuid.uuid4()
    )
    assert level == "medium"
    assert _names(level, gates) == {"create_github_issue", "send_microsoft_email"}
    assert len(computed) == 1


@pytest.mark.parametrize(("pins", "is_builtin"), [(None, False), ({}, True)])
async def test_a_legacy_custom_fleet_or_a_builtin_fleet_ignores_the_computed_catalog(computed, pins, is_builtin):
    _level, gates = await fleet_gate_proposals.fleet_effective_gates(
        _Db(("full_gates", [_LOW], None, pins, is_builtin, None)), uuid.uuid4()
    )
    assert [g["key"] for g in gates] == ["microsoft.low-writes"]
    assert computed == []


async def test_an_absent_fleet_reads_none(computed):
    assert await fleet_gate_proposals.fleet_effective_gates(_Db(None), uuid.uuid4()) is None


class _ListRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self


class _ListDb:
    """Answers the member read, then the agent read, of ``fleets_gated_action_counts``."""

    def __init__(self, member_rows, agents):
        self.answers = [member_rows, agents]
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return _ListRows(self.answers.pop(0))


async def test_the_list_counts_each_fleets_effective_gates_in_two_reads(monkeypatch):
    """#10457: the Fleets list card's count is the About catalog's length, batched for the list."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id)
    monkeypatch.setattr(fleet_gate_proposals, "_writable_providers", lambda _a: {"github"})
    monkeypatch.setattr(
        fleet_gate_proposals, "propose_gates", lambda agents, _tools, _labels: [dict(_HIGH)] if agents else []
    )
    import app.api.v1.integration_catalog as catalog

    built: list[str] = []
    monkeypatch.setattr(catalog, "build_tools_for_provider", lambda p: built.append(p) or [])
    def _fleet(**fields):
        base = {"id": uuid.uuid4(), "is_builtin": False, "approval_pins": {}, "approval_gates": None, "documentation": None}
        return SimpleNamespace(**{**base, **fields})

    computed, computed_twin, empty = _fleet(), _fleet(), _fleet()
    legacy = _fleet(approval_pins=None)
    builtin = _fleet(is_builtin=True, approval_gates=[_LOW, _HIGH])
    db = _ListDb([(computed.id, agent_id), (computed_twin.id, agent_id)], [agent])
    fleets = [computed, computed_twin, empty, legacy, builtin]
    counts = await fleet_gate_proposals.fleets_gated_action_counts(db, fleets)
    assert counts == {computed.id: 1, computed_twin.id: 1, empty.id: 0, legacy.id: 0, builtin.id: 2}
    assert db.calls == 2
    assert built == ["github"]


async def test_the_list_count_reads_nothing_when_no_fleet_computes_gates():
    db = _ListDb([], [])
    fleet = SimpleNamespace(id=uuid.uuid4(), is_builtin=True, approval_pins=None, approval_gates=[], documentation=None)
    assert await fleet_gate_proposals.fleets_gated_action_counts(db, [fleet]) == {fleet.id: 0}
    assert db.calls == 0


async def test_a_failed_read_never_fails_the_run(monkeypatch):
    class _Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FailingDb:
        def begin_nested(self):
            return _Nested()

        async def execute(self, _statement):
            raise RuntimeError("database gone")

    assert await fleet_autonomy.fleet_autonomy_for_run(_FailingDb(), uuid.uuid4()) is None


async def test_a_new_custom_fleet_starts_at_full_gates_with_the_computed_catalog_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2279 (Sam, 25 Nov 2026): no level given = Full human gates; it was autonomous from #10153 C2."""
    from app.services import fleets

    created: list[dict] = []

    class _Fleet:
        def __init__(self, **kwargs):
            created.append(kwargs)

    class _Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        async def flush(self):
            return None

        def begin_nested(self):
            return _Nested()

        def add(self, _obj):
            return None

    async def _slug(_db, _user_id, name):
        return name.lower()

    monkeypatch.setattr(fleets, "Fleet", _Fleet)
    monkeypatch.setattr(fleets, "unique_fleet_slug", _slug)
    await fleets.create_fleet(_Session(), user_id=None, name="A", workspace_id=uuid.uuid4())
    assert created[-1]["autonomy_level"] == "full_gates"
    assert created[-1]["approval_pins"] == {}
    await fleets.create_fleet(
        _Session(), user_id=None, name="B", workspace_id=uuid.uuid4(), approval_gates=[_HIGH], approval_pins=None
    )
    assert created[-1]["autonomy_level"] == "full_gates"
    assert created[-1]["approval_pins"] is None


def test_the_migration_moves_only_custom_fleets_that_declare_no_gate():
    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "p10153fltpin1_add_fleet_approval_pins.py"
    spec = importlib.util.spec_from_file_location("p10153fltpin1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    condition = module._GATES_NOTHING
    assert "is_builtin = false" in condition
    assert "approval_gates" in condition and "'structured' -> 'gates'" in condition
    # The static statements carry the same condition (no interpolation in the DDL module).
    for statement in (module._UPGRADE_SQL, module._DOWNGRADE_SQL):
        assert statement.endswith(condition)
    assert module.down_revision == "r10006roibf1"
