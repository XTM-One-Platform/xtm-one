"""The organization profile (#8987): contract, missing fields, the agent tool, its wiring.

The store against a real PostgreSQL is exercised by the migration suites;
here the rules without a database: validation of every field, the missing
field report, what an agent reads (filled fields quarantined, missing ones
named, nothing guessed) and that the tool is core and built for every agent.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import organization_context as oc
from app.services.builtin_tools import (
    ORGANIZATION_CONTEXT_TOOL,
    apply_tool_deny_list,
    build_organization_context_tool,
)
from app.tools.base import ToolCall

APP_DIR = Path(__file__).resolve().parents[1] / "app"

FULL = {
    "name": "Acme Energy",
    "sector": "energy",
    "sub_sector": "electricity distribution",
    "regions": ["fr", "DE", "FR"],
    "size": "1001-5000",
    "crown_jewels": [
        {"name": "SCADA", "business_service": "Grid operations", "owner": "OT", "criticality": "critical"}
    ],
    "tech_stack": [{"product": "Windows Server 2019", "vendor": "Microsoft", "cpe": "cpe:2.3:o:microsoft:windows_server_2019:-:*:*:*:*:*:*:*"}],
    "pir_ids": ["pir-1", "pir-1", " "],
    "risk_appetite": {"annual_loss_threshold": 2_000_000, "approval_threshold": 50_000},
    "loaded_hourly_rate": 95,
    "currency": "eur",
    "ticketing": {"integration": "jira", "project": "SEC"},
}


def _row(profile: dict, version: int = 3, source: str = "form"):
    return SimpleNamespace(
        organization_id=None,  # no asset records read: the session is a stub
        version=version,
        profile=profile,
        source=source,
        author_user_id=uuid.uuid4(),
        created_at=datetime(2026, 11, 5, 18, 0, tzinfo=UTC),
    )


def test_a_full_profile_is_normalized_and_misses_nothing():
    profile = oc.parse_profile(FULL)
    assert profile.regions == ["FR", "DE"]
    assert profile.currency == "EUR"
    assert profile.pir_ids == ["pir-1"]
    assert oc.missing_fields(profile) == []


def test_the_profile_keeps_a_name_only_for_the_pirs_it_follows():
    profile = oc.parse_profile({"pir_ids": ["pir-1", "pir-2"], "pir_names": {"pir-1": " Ransomware ", "pir-2": "", "pir-9": "Gone"}})
    assert profile.pir_names == {"pir-1": "Ransomware"}
    assert "pir_names" not in {path for path, _ in oc.CONTRACT_FIELDS}
    assert oc.parse_profile({}).pir_names == {}


def test_an_empty_profile_misses_every_contract_field_in_order():
    assert oc.missing_fields(oc.parse_profile({})) == [path for path, _ in oc.CONTRACT_FIELDS]


def test_a_half_filled_ticketing_target_is_missing():
    profile = oc.parse_profile({**FULL, "ticketing": {"integration": "jira", "project": ""}})
    assert oc.missing_fields(profile) == ["ticketing"]


def test_a_zero_threshold_is_a_value_not_a_missing_field():
    profile = oc.parse_profile({**FULL, "risk_appetite": {"annual_loss_threshold": 0, "approval_threshold": 0}})
    assert oc.missing_fields(profile) == []


@pytest.mark.parametrize(
    ("patch", "where"),
    [
        ({"regions": ["France"]}, "regions"),
        ({"currency": "EURO"}, "currency"),
        ({"size": "huge"}, "size"),
        ({"loaded_hourly_rate": -1}, "loaded_hourly_rate"),
        ({"crown_jewels": [{"name": "x", "criticality": "extreme"}]}, "crown_jewels"),
        # #1844: no alias; a stored ai_systems key is refused, the migration moved it to the assets.
        ({"ai_systems": [{"name": "x"}]}, "ai_systems"),
        ({"unknown_field": 1}, "unknown_field"),
    ],
)
def test_a_bad_field_is_refused_and_named(patch, where):
    with pytest.raises(ValueError, match=where):
        oc.parse_profile({**FULL, **patch})


def test_an_agent_reads_no_profile_as_every_field_missing_and_no_guess():
    text = oc.render_for_agent(None)
    assert "No organization profile is saved yet" in text
    assert "primary assets" in text and "loaded hourly rate" in text
    assert "Do not guess" in text


def test_an_agent_reads_the_filled_fields_quarantined_and_the_missing_ones_named():
    partial = {k: v for k, v in FULL.items() if k not in ("size", "loaded_hourly_rate")}
    text = oc.render_for_agent(_row(partial))
    assert "version 3" in text
    assert "Fields not filled: organization size, loaded hourly rate." in text
    assert "Cite the fields you used" in text
    assert "UNTRUSTED" in text
    assert "Grid operations" in text
    # An empty field is not shown as data.
    assert '"size"' not in text


def test_a_complete_profile_says_none_missing():
    text = oc.render_for_agent(_row(FULL))
    assert "Fields not filled: none." in text
    assert "Do not guess" not in text


async def test_save_refuses_an_unknown_source():
    with pytest.raises(ValueError, match="source"):
        await oc.save_version(None, oc.parse_profile({}), source="email", author_user_id=None)  # type: ignore[arg-type]


def test_the_tool_is_core_read_only_and_survives_a_deny_list():
    definitions, executor = build_organization_context_tool()
    assert [d.name for d in definitions] == [ORGANIZATION_CONTEXT_TOOL]
    assert definitions[0].core is True
    # Optional arguments only: a tracked organization (#9157), a category by page (#11533); none reads the primary.
    assert set(definitions[0].input_schema["properties"]) == {"organization", "category", "query", "offset", "limit"}
    assert definitions[0].input_schema["required"] == []
    assert set(executor.tool_names) == {ORGANIZATION_CONTEXT_TOOL}
    assert [d.name for d in apply_tool_deny_list(definitions, [ORGANIZATION_CONTEXT_TOOL])] == [
        ORGANIZATION_CONTEXT_TOOL
    ]


async def test_the_tool_reads_the_current_version(monkeypatch):
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _current(_db):
        return _row(FULL, version=7)

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(oc, "current_version", _current)
    _, executor = build_organization_context_tool()
    result = await executor.execute_tool(
        ToolCall(id="1", name=ORGANIZATION_CONTEXT_TOOL, arguments={})
    )
    assert "version 7" in str(result.content)


async def test_a_fleet_member_run_starts_from_the_profile(monkeypatch):
    import app.services.fleet_runs as fleet_runs

    async def _home(_db, _agent_id):
        return fleet_runs.FleetRunDefaults(fleet_id=uuid.uuid4())

    async def _current(_db):
        return _row({k: v for k, v in FULL.items() if k != "loaded_hourly_rate"}, version=4)

    async def _ctem(_db: object, _fleet_id: object) -> bool:
        return True

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _home)
    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(oc, "ctem_fleet", _ctem)
    text = await oc.fleet_run_context(None, uuid.uuid4())  # type: ignore[arg-type]
    assert text.startswith(f"\n\n{oc.FLEET_RUN_CONTEXT_HEADER}\n")
    assert "version 4" in text
    assert "Fields not filled: loaded hourly rate." in text
    assert "UNTRUSTED" in text


async def test_a_member_of_a_fleet_that_is_not_ctem_is_not_told_the_ctem_only_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.fleet_runs as fleet_runs

    async def _home(_db: object, _agent_id: object) -> fleet_runs.FleetRunDefaults:
        return fleet_runs.FleetRunDefaults(fleet_id=uuid.uuid4())

    async def _current(_db: object) -> object:
        return _row({k: v for k, v in FULL.items() if k not in ("loaded_hourly_rate", "size")}, version=4)

    async def _not_ctem(_db: object, _fleet_id: object) -> bool:
        return False

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _home)
    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(oc, "ctem_fleet", _not_ctem)
    text = await oc.fleet_run_context(None, uuid.uuid4())  # type: ignore[arg-type]
    assert "Fields not filled: organization size." in text
    assert "loaded hourly rate" not in text
    assert "Organization context incomplete" not in text
    # The same fleet run of an agent holding CTEM tools keeps the notice (#1384).
    agent = SimpleNamespace(
        id=uuid.uuid4(), slug="ctem-risk-quantifier", is_builtin=False, builtin_tool_ids=["ctem_read"]
    )
    text = await oc.fleet_run_context(None, agent.id, agent=agent)  # type: ignore[arg-type]
    assert "Fields not filled: organization size, loaded hourly rate." in text
    assert "Organization context incomplete: missing organization size, loaded hourly rate." in text


async def test_a_fleet_member_run_without_a_profile_names_every_field(monkeypatch):
    import app.services.fleet_runs as fleet_runs

    async def _home(_db, _agent_id):
        return fleet_runs.FleetRunDefaults(fleet_id=uuid.uuid4())

    async def _current(_db):
        return None

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _home)
    monkeypatch.setattr(oc, "current_version", _current)
    text = await oc.fleet_run_context(None, uuid.uuid4())  # type: ignore[arg-type]
    assert "No organization profile is saved yet" in text


async def test_an_agent_in_no_fleet_gets_no_block(monkeypatch):
    import app.services.fleet_runs as fleet_runs

    async def _home(_db, _agent_id):
        return fleet_runs.NO_FLEET

    async def _current(_db):  # pragma: no cover - must not be read
        raise AssertionError("the profile is read for a fleet member only")

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _home)
    monkeypatch.setattr(oc, "current_version", _current)
    assert await oc.fleet_run_context(None, uuid.uuid4()) == ""  # type: ignore[arg-type]


async def test_an_unreadable_profile_never_fails_the_run(monkeypatch):
    import app.services.fleet_runs as fleet_runs

    async def _home(_db, _agent_id):
        return fleet_runs.FleetRunDefaults(fleet_id=uuid.uuid4())

    async def _current(_db):
        raise RuntimeError("db down")

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _home)
    monkeypatch.setattr(oc, "current_version", _current)
    assert await oc.fleet_run_context(None, uuid.uuid4()) == ""  # type: ignore[arg-type]


def test_every_assignment_run_appends_the_fleet_block():
    # Since #11533 the engine reaches fleet_run_context through the cascade's
    # one entry point, which calls it whenever the cascade resolves on.
    tree = ast.parse((APP_DIR / "services/assignment_engine.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "assignment_run_organization_context"
    ]
    assert calls, "assignment_run_organization_context is not called from services/assignment_engine.py"
    injection = ast.parse((APP_DIR / "services/organization_context_injection.py").read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.Call) and getattr(node.func, "id", None) == "fleet_run_context"
        for node in ast.walk(injection)
    ), "fleet_run_context is not called from services/organization_context_injection.py"


@pytest.mark.parametrize("source", ["services/agent_chat.py", "services/assignment_engine.py"])
def test_every_agent_run_and_turn_builds_the_tool(source):
    tree = ast.parse((APP_DIR / source).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "build_organization_context_tool"
    ]
    assert calls, f"build_organization_context_tool is not called from {source}"
