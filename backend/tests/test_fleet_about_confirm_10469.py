"""A manual prerequisite is confirmable, never a permanent "To confirm" (#10469 S2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import fleet_about
from app.services.fleet_about import (
    CONFIRMATION_DAYS,
    MANUAL_PREFIX,
    MET,
    TO_CONFIRM,
    confirmation_state,
    live_prerequisites,
    manual_key,
    set_prerequisite_confirmation,
)

NOW = datetime(2026, 11, 9, 19, 0, tzinfo=UTC)
LABEL = "Data processing agreement signed"


class _Result:
    def scalars(self):
        return self

    def all(self):
        return []


class _Db:
    def __init__(self) -> None:
        self.flushes = 0

    async def execute(self, _query):
        return _Result()

    async def flush(self) -> None:
        self.flushes += 1


@pytest.fixture
def fleet(monkeypatch):
    lead = uuid.uuid4()
    lead_agent = SimpleNamespace(
        id=lead,
        name="Lead",
        enabled=True,
        integration_bindings={},
        integration_template_bindings=[],
        handover_agent_ids=[],
        knowledge_base_ids=[],
        mcp_server_ids=[],
    )

    async def rows(_db, model, ids):
        from app.models.agent import Agent

        return {lead: lead_agent} if model is Agent and lead in ids else {}

    async def visible(_db, _model, ids, _user, _groups):
        return set(ids)

    async def members(_db, _fleet_id):
        return []

    async def counts(_db, _fleet):
        return SimpleNamespace(total=0, enabled=0)

    monkeypatch.setattr(fleet_about, "_rows", rows)
    monkeypatch.setattr(fleet_about, "_visible_ids", visible)
    monkeypatch.setattr("app.services.fleets.fleet_members", members)
    monkeypatch.setattr("app.services.fleet_views.home_member_ids", lambda _rows, _orch: [lead])
    monkeypatch.setattr("app.services.fleet_views.fleet_automation_counts", counts)
    documented = [{"check": "manual", "label": LABEL, "fix_route": ""}]
    return SimpleNamespace(
        id=uuid.uuid4(),
        orchestrator_agent_id=lead,
        documentation={"structured": {"prerequisites": documented}},
        prerequisite_confirmations=None,
    )


def _user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False, full_name="Ada Lovelace", email="ada@example.com")


def _manual(lines):
    return [line for line in lines if line.kind == "manual"]


def test_the_key_folds_case_and_spacing_and_changes_with_the_label():
    assert manual_key("Data  processing agreement SIGNED") == manual_key(LABEL)
    assert manual_key("Data processing agreement renewed") != manual_key(LABEL)


def test_a_confirmation_older_than_the_window_lapses():
    fresh = {"name": "Ada", "at": (NOW - timedelta(days=CONFIRMATION_DAYS)).isoformat()}
    stale = {"name": "Ada", "at": (NOW - timedelta(days=CONFIRMATION_DAYS + 1)).isoformat()}
    assert confirmation_state(fresh, now=NOW)[0]
    assert confirmation_state(stale, now=NOW) == (False, "Ada", stale["at"])
    assert confirmation_state(None, now=NOW) == (False, "", "")
    assert confirmation_state({"name": "Ada", "at": "not a date"}, now=NOW)[0] is False


async def test_confirm_turns_the_line_met_with_who_and_when_and_undo_restores_it(fleet):
    db, user = _Db(), _user()
    documented = fleet.documentation["structured"]["prerequisites"]
    before = _manual(await live_prerequisites(db, user, fleet, documented, group_ids=[], now=NOW, can_confirm=True))
    assert [(line.check, line.state, line.can_confirm) for line in before] == [
        (f"{MANUAL_PREFIX}{manual_key(LABEL)}", TO_CONFIRM, True)
    ]

    line = await set_prerequisite_confirmation(db, user, fleet, before[0].check, confirmed=True, now=NOW)
    assert (line.state, line.confirmed_by, line.confirmed_at) == (MET, "Ada Lovelace", NOW.isoformat())
    stored = fleet.prerequisite_confirmations[manual_key(LABEL)]
    assert stored["by"] == str(user.id) and stored["label"] == LABEL

    # A regenerate that keeps the label keeps the confirmation: it lives on the fleet, not in the doc.
    fleet.documentation = {"structured": {"prerequisites": [{"check": "manual", "label": LABEL.upper()}]}}
    again = _manual(
        await live_prerequisites(
            db, user, fleet, fleet.documentation["structured"]["prerequisites"], group_ids=[], now=NOW
        )
    )
    assert again[0].state == MET and again[0].confirmed_by == "Ada Lovelace"
    # A reader who does not manage the fleet sees it, without the action.
    assert again[0].can_confirm is False

    undone = await set_prerequisite_confirmation(db, user, fleet, before[0].check, confirmed=False, now=NOW)
    assert (undone.state, undone.confirmed_by) == (TO_CONFIRM, "")
    assert fleet.prerequisite_confirmations is None


async def test_a_confirmation_past_ninety_days_returns_the_line_to_confirm(fleet):
    db, user = _Db(), _user()
    old = NOW - timedelta(days=CONFIRMATION_DAYS + 5)
    await set_prerequisite_confirmation(db, user, fleet, f"{MANUAL_PREFIX}{manual_key(LABEL)}", confirmed=True, now=old)
    line = _manual(
        await live_prerequisites(
            db, user, fleet, fleet.documentation["structured"]["prerequisites"], group_ids=[], now=NOW
        )
    )[0]
    assert line.state == TO_CONFIRM and line.confirmed_by == "Ada Lovelace" and line.confirmed_at == old.isoformat()


async def test_a_check_the_fleet_does_not_show_is_refused(fleet):
    with pytest.raises(ValueError, match="No manual prerequisite"):
        await set_prerequisite_confirmation(_Db(), _user(), fleet, "manual:deadbeef0000", confirmed=True, now=NOW)
    assert fleet.prerequisite_confirmations is None


def test_a_built_in_fleet_takes_a_confirmation_and_nothing_else():
    from app.services.fleets import FleetActionRefusedError, ensure_builtin_fleet_writable

    builtin = SimpleNamespace(is_builtin=True)
    ensure_builtin_fleet_writable(builtin, {"prerequisite_confirmations"})
    with pytest.raises(FleetActionRefusedError):
        ensure_builtin_fleet_writable(builtin, {"prerequisite_confirmations", "name"})


async def test_the_tool_refuses_a_caller_who_does_not_manage_the_fleet(monkeypatch):
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import fleets as fleet_tools

    db = SimpleNamespace(commits=0, rollbacks=0)

    async def _commit():
        db.commits += 1

    async def _rollback():
        db.rollbacks += 1

    db.commit, db.rollback = _commit, _rollback

    @asynccontextmanager
    async def _factory():
        yield db

    user = _user()

    async def _load(_db, _uid):
        return user

    async def _refuse(*_args, **_kwargs):
        return None, "You don't have permission to edit this fleet."

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _refuse)
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id))
    answer = await executor.handler("confirm_fleet_prerequisite")(
        fleet_id=str(uuid.uuid4()), check="manual:abc", confirmed=True
    )
    assert "permission" in answer and db.commits == 0
