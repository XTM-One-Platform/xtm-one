"""Fleet rules that need no database."""

from __future__ import annotations

import uuid

import pytest

from app.models.fleet import FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.workspace import Workspace
from app.services.fleets import (
    FleetActionRefusedError,
    ensure_workspace_can_host,
    exposed_agent_ids,
    normalize_accepted_handover_types,
)


def test_accepted_handover_types_none_is_all_and_unknown_is_refused() -> None:
    assert normalize_accepted_handover_types(None) is None
    assert normalize_accepted_handover_types(["consult", "consult", "delegate"]) == ["consult", "delegate"]
    with pytest.raises(FleetActionRefusedError) as exc:
        normalize_accepted_handover_types(["consult", "mandatory_transfer"])
    assert exc.value.status_code == 422
    assert "mandatory_transfer" in exc.value.detail


def test_the_orchestrator_is_exposed_first_and_connected_rows_never_are() -> None:
    orchestrator, desk, hidden, connected = (uuid.uuid4() for _ in range(4))
    fleet = Fleet(id=uuid.uuid4(), name="F", slug="f", workspace_id=uuid.uuid4(), orchestrator_agent_id=orchestrator)
    rows = [
        FleetMember(fleet_id=fleet.id, agent_id=hidden, role=FLEET_ROLE_MEMBER, is_exposed=False),
        FleetMember(fleet_id=fleet.id, agent_id=desk, role=FLEET_ROLE_MEMBER, is_exposed=True),
        FleetMember(fleet_id=fleet.id, agent_id=orchestrator, role=FLEET_ROLE_MEMBER, is_exposed=False),
        FleetMember(fleet_id=fleet.id, agent_id=connected, role=FLEET_ROLE_CONNECTED, is_exposed=True),
    ]
    assert exposed_agent_ids(fleet, rows) == [orchestrator, desk]


def test_a_fleet_without_orchestrator_exposes_only_its_flagged_members() -> None:
    desk = uuid.uuid4()
    fleet = Fleet(id=uuid.uuid4(), name="F", slug="f", workspace_id=uuid.uuid4(), orchestrator_agent_id=None)
    rows = [FleetMember(fleet_id=fleet.id, agent_id=desk, role=FLEET_ROLE_MEMBER, is_exposed=True)]
    assert exposed_agent_ids(fleet, rows) == [desk]


def test_a_workspace_hosts_a_fleet_only_if_its_audience_can_use_it() -> None:
    personal = Workspace(name="P", slug="p", is_company_managed=False, is_archived=False)
    company = Workspace(name="C", slug="c", is_company_managed=True, is_archived=False)
    archived = Workspace(name="A", slug="a", is_company_managed=True, is_archived=True)
    ensure_workspace_can_host(False, personal)
    ensure_workspace_can_host(True, company)
    ensure_workspace_can_host(False, company)
    with pytest.raises(FleetActionRefusedError, match="company-managed workspace"):
        ensure_workspace_can_host(True, personal)
    with pytest.raises(FleetActionRefusedError, match="archived"):
        ensure_workspace_can_host(False, archived)
