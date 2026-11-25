"""Seeded and new fleets start at Full human gates (#2279, Sam, 25 Nov 2026; was Low from #10005).

Pinned here without a database: the shipped level, every gate waits on every
built-in fleet, and no gate ever pauses a tool call (#1675: a gate that waits is
one decision item between steps, the fleet's tools stay authorized). The boot
behaviour (fresh seed, person-set level kept, system audit row) is pinned
against PostgreSQL in ``test_builtin_fleets_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
from app.agents.fleet_seed import SEED_AUDIT_ACTOR, SEED_DEFAULT_AUDIT_ACTION, SEEDED_FLEET_AUTONOMY_LEVEL
from app.models.fleet import Fleet
from app.services import fleet_autonomy
from app.services.fleet_autonomy import AUTONOMY_LEVELS, DEFAULT_AUTONOMY_LEVEL, gate_catalog, gated_tool_rules
from app.tools.base import HumanInputRequestedError

_CTEM = sorted(BUILTIN_FLEET_STRUCTURED)

#: Per fleet at Low: the gates that still wait for a person (#1339), for a level a person picks.
_WAITS_AT_LOW = {
    "ctem-loop": {"ctem.validation.non_readonly", "ctem.mobilize"},
    "ctem-vuln-prioritization": {"ctem.validation.non_readonly", "ctem.mobilize"},
    "ctem-ai-security": {"ctem.validation.non_readonly", "ctem.mobilize"},
    "ctem-third-party-risk": {"ctem.mobilize"},
}


def test_seeded_fleets_ship_at_full_human_gates_the_default_of_every_fleet() -> None:
    assert SEEDED_FLEET_AUTONOMY_LEVEL == "full_gates" == DEFAULT_AUTONOMY_LEVEL
    assert AUTONOMY_LEVELS.index(SEEDED_FLEET_AUTONOMY_LEVEL) == 0


def test_custom_fleets_keep_full_human_gates() -> None:
    assert DEFAULT_AUTONOMY_LEVEL == "full_gates"
    assert Fleet.__table__.c.autonomy_level.server_default.arg == "full_gates"
    # Full human gates never means tool approvals: the opt-in stays off (#1467, #1675).
    assert Fleet.__table__.c.enforce_gates_at_tool_level.server_default.arg.text == "false"


def test_the_seed_audit_row_is_not_a_user_change() -> None:
    assert SEED_DEFAULT_AUDIT_ACTION != "autonomy_level_change"
    assert SEED_AUDIT_ACTOR == "fleet-seed"


def test_every_shipped_fleet_is_listed() -> None:
    assert set(_WAITS_AT_LOW) == set(_CTEM)


@pytest.mark.parametrize("slug", _CTEM)
def test_out_of_the_box_every_gate_waits_and_no_tool_call_pauses(slug: str) -> None:
    gates = BUILTIN_FLEET_STRUCTURED[slug].get("gates")
    catalog = gate_catalog(SEEDED_FLEET_AUTONOMY_LEVEL, gates)
    assert all(e["gated"] for e in catalog), slug
    assert gated_tool_rules(SEEDED_FLEET_AUTONOMY_LEVEL, gates) == [], slug


@pytest.mark.parametrize("slug", _CTEM)
def test_at_low_only_destructive_gates_wait(slug: str) -> None:
    catalog = gate_catalog("low", BUILTIN_FLEET_STRUCTURED[slug].get("gates"))
    waiting = {e["key"] for e in catalog if e["gated"]}
    assert waiting == _WAITS_AT_LOW[slug], slug
    assert all(e["impact"] == "destructive" for e in catalog if e["gated"]), slug
    assert all(e["impact"] != "destructive" for e in catalog if not e["gated"]), slug


@pytest.mark.asyncio
async def test_a_fleet_created_without_a_level_gets_full_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2279: no level given = Full human gates, whether or not the fleet declares gates."""
    from app.services import fleets

    created: list[dict[str, object]] = []

    class _Fleet:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

    class _Nested:
        async def __aenter__(self) -> _Nested:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _Session:
        async def flush(self) -> None:
            return None

        def begin_nested(self) -> _Nested:
            return _Nested()

        def add(self, _obj: object) -> None:
            return None

    async def _slug(_db: object, _user_id: object, name: str) -> str:
        return name.lower()

    monkeypatch.setattr(fleets, "Fleet", _Fleet)
    monkeypatch.setattr(fleets, "unique_fleet_slug", _slug)
    gate = {"key": "ticket.create", "action": "Create a ticket", "impact": "low"}
    await fleets.create_fleet(_Session(), user_id=None, name="A", workspace_id=uuid.uuid4())  # type: ignore[arg-type]
    assert created[-1]["autonomy_level"] == "full_gates"
    await fleets.create_fleet(
        _Session(),  # type: ignore[arg-type]
        user_id=None,
        name="B",
        workspace_id=uuid.uuid4(),
        approval_gates=[gate],
    )
    assert created[-1]["autonomy_level"] == "full_gates"
    await fleets.create_fleet(
        _Session(),  # type: ignore[arg-type]
        user_id=None,
        name="C",
        workspace_id=uuid.uuid4(),
        autonomy_level="medium",
    )
    assert created[-1]["autonomy_level"] == "medium"


@pytest.mark.asyncio
async def test_a_new_fleet_at_full_gates_runs_a_gated_tool_with_no_approval_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2279 with #1675: Full human gates by default never turns on tool approval.

    A gate that names a tool waits at Full human gates, yet the tool call runs:
    no administrator opted the fleet in to tool-level pauses, so the gate is a
    decision item between steps, never an approval card inside the run.
    """
    gate: dict[str, object] = {
        "key": "ticket.create",
        "action": "Create a ticket",
        "impact": "low",
        "tools": [{"name": "create_jira_issue"}],
    }
    assert [e["gated"] for e in gate_catalog(DEFAULT_AUTONOMY_LEVEL, [gate])] == [True]

    async def _for_run(_db: object, fleet_id: object) -> tuple[str, list[dict[str, object]]] | None:
        return None if fleet_id is None else (DEFAULT_AUTONOMY_LEVEL, [gate])

    class _Nested:
        async def __aenter__(self) -> _Nested:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _Result:
        def first(self) -> tuple[bool, bool]:
            # The stored opt-in of a new fleet (column default) and not built in.
            return (False, False)

    class _Db:
        def begin_nested(self) -> _Nested:
            return _Nested()

        async def execute(self, _stmt: object) -> _Result:
            return _Result()

    async def _no_kill(_db: object) -> None:
        return None

    import app.services.hitl_whitelist as hitl

    monkeypatch.setattr(fleet_autonomy, "fleet_autonomy_for_run", _for_run)
    monkeypatch.setattr(hitl, "resolve_platform_kill_switch", _no_kill)

    ran: list[str] = []

    def _agent_gate(call: SimpleNamespace) -> None:
        ran.append(call.id)

    class _Composite:
        def __init__(self) -> None:
            self.approval_gate: Callable[[SimpleNamespace], None] = _agent_gate

        def set_approval_gate(self, gate_fn: Callable[[SimpleNamespace], None]) -> None:
            self.approval_gate = gate_fn

    composite = _Composite()
    assert await fleet_autonomy.install_fleet_autonomy_gate(_Db(), composite, "fleet-1") is False
    call = SimpleNamespace(id="c-ticket", name="create_jira_issue", arguments={"summary": "fix"})
    try:
        composite.approval_gate(call)
    except HumanInputRequestedError:  # pragma: no cover - the defect this test pins
        pytest.fail("a fleet at the default level paused a tool call for approval")
    assert ran == ["c-ticket"]
