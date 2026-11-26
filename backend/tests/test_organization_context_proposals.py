"""Changes of the organization profile proposed from chat (#9484, S-a2; dev-docs/ctem-fleets.md section 6.1).

The gates of S-a2: the tool is offered to the CTEM Assistant and the
Concierge for an administrator only and refuses anyone else again; it
stores a proposal and never writes a profile; every value is validated
against the organization's contract before it is stored; a value already
set is a conflict, applied only when named; an empty accept rejects; new
tracked organizations are checked (relationship, criticality). No database:
the service's pure functions and the tool's guards.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.services import organization_context_proposals as proposals
from app.services.builtin_tools import (
    ORGANIZATION_PROPOSAL_TOOL,
    build_organization_proposal_tool,
    offers_organization_proposals,
)
from app.tools.base import ToolCall

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PRIMARY = SimpleNamespace(role="primary")
TRACKED = SimpleNamespace(role="tracked")


def test_a_field_change_is_validated_and_normalised():
    kept = proposals.validate_field_changes(
        PRIMARY,
        {},
        [{"field": "regions", "value": ["fr", "DE", "fr"], "basis": "we operate in France and Germany"}],
    )
    assert kept == [
        {
            "field": "regions",
            "value": ["FR", "DE"],
            "basis": "we operate in France and Germany",
            "current": [],
            "conflict": False,
        }
    ]


def test_a_value_already_set_is_a_conflict():
    kept = proposals.validate_field_changes(PRIMARY, {"sector": "Retail"}, [{"field": "sector", "value": "Finance"}])
    assert kept[0]["conflict"] is True
    assert kept[0]["current"] == "Retail"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ([], "at least one"),
        ([{"field": "favourite_colour", "value": "blue"}], "not a field"),
        ([{"field": "currency", "value": "EURO"}], "currency"),
        ([{"field": "sector", "value": "A"}, {"field": "sector", "value": "B"}], "twice"),
        ([{"field": "sector", "value": ""}], "empty"),
        ([{"field": "sector", "value": "Retail"}], "already"),
    ],
)
def test_a_bad_change_is_refused_with_its_reason(changes, message):
    with pytest.raises(proposals.ProposalError, match=message):
        proposals.validate_field_changes(PRIMARY, {"sector": "Retail"}, changes)


def test_too_many_changes_are_refused():
    changes = [{"field": "sector", "value": str(i)} for i in range(proposals.MAX_CHANGES + 1)]
    with pytest.raises(proposals.ProposalError, match="At most"):
        proposals.validate_field_changes(PRIMARY, {}, changes)


def test_a_tracked_organization_has_its_own_contract():
    kept = proposals.validate_field_changes(TRACKED, {}, [{"field": "sector", "value": "Cloud"}])
    assert kept[0]["value"] == "Cloud"
    with pytest.raises(proposals.ProposalError, match="not a field"):
        proposals.validate_field_changes(TRACKED, {}, [{"field": "software", "value": []}])


def test_new_tracked_organizations_are_checked():
    kept = proposals.validate_new_tracked([{"name": " Acme ", "relationship": "provider", "basis": "acquired"}])
    assert kept == [{"name": "Acme", "relationship": "provider", "criticality": "medium", "basis": "acquired"}]
    with pytest.raises(proposals.ProposalError, match="relationship"):
        proposals.validate_new_tracked([{"name": "Acme", "relationship": "friend"}])
    with pytest.raises(proposals.ProposalError, match="criticality"):
        proposals.validate_new_tracked([{"name": "Acme", "relationship": "customer", "criticality": "extreme"}])
    with pytest.raises(proposals.ProposalError, match="name"):
        proposals.validate_new_tracked([{"relationship": "customer"}])


def test_the_outside_lens_settings_are_fields_of_the_primary_profile_only():
    """CTEM-26: an agent can scope and enable the outside lens (owned domains, ranges, the pass)."""
    kept = proposals.validate_field_changes(
        PRIMARY,
        {},
        [
            {"field": "owned_domains", "value": ["example.com"]},
            {"field": "address_ranges", "value": ["203.0.113.7", "198.51.100.0/24"]},
            {"field": "attack_surface", "value": {"enabled": True, "integration": "shodan-1"}},
        ],
    )
    values = {entry["field"]: entry["value"] for entry in kept}
    assert values["owned_domains"] == ["example.com"]
    assert values["address_ranges"] == ["203.0.113.7/32", "198.51.100.0/24"]
    assert values["attack_surface"]["enabled"] is True
    assert values["attack_surface"]["integration"] == "shodan-1"
    assert values["attack_surface"]["cadence_hours"] == 24
    for path in proposals.PROPOSABLE_EXPOSURE_PATHS:
        assert path not in proposals._contract_paths("tracked")


def test_an_agent_can_set_the_evidence_health_pass_of_the_primary_profile() -> None:
    """#2196: the OpenCRQ evidence health opt-in is writable by an agent, validated by its model."""
    kept = proposals.validate_field_changes(
        PRIMARY, {}, [{"field": "evidence_health", "value": {"enabled": True, "max_age_days": 60}}]
    )
    value = kept[0]["value"]
    assert value["enabled"] is True
    assert value["max_age_days"] == 60
    assert value["cadence_hours"] == 24
    assert "evidence_health" not in proposals._contract_paths("tracked")
    with pytest.raises(proposals.ProposalError, match="evidence_health"):
        proposals.validate_field_changes(
            PRIMARY, {}, [{"field": "evidence_health", "value": {"enabled": True, "max_age_days": 0}}]
        )


def test_enterprise_knowledge_is_a_field_an_agent_can_change_on_the_primary_profile() -> None:
    """#1691: update/propose link the procedure knowledge bases as the Settings form does."""
    link = {"knowledge_base_id": "8a0b3c1e-0000-4000-8000-000000000001", "label": "SOPs", "kind": "sop"}
    kept = proposals.validate_field_changes(PRIMARY, {}, [{"field": "enterprise_knowledge", "value": [link]}])
    assert kept[0]["value"] == [link]
    assert "enterprise_knowledge" not in proposals._contract_paths("tracked")
    with pytest.raises(proposals.ProposalError, match="enterprise_knowledge"):
        proposals.validate_field_changes(
            PRIMARY, {}, [{"field": "enterprise_knowledge", "value": [{**link, "kind": "novel"}]}]
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"field": "address_ranges", "value": ["10.0.0.0/33"]}, "not an IP address or CIDR block"),
        ({"field": "attack_surface", "value": {"enabled": True, "cadence_hours": 0}}, "attack_surface"),
        ({"field": "attack_surface", "value": {"enabled": True, "colour": "red"}}, "attack_surface"),
    ],
)
def test_a_bad_outside_lens_setting_is_refused_with_its_reason(change, message):
    with pytest.raises(proposals.ProposalError, match=message):
        proposals.validate_field_changes(PRIMARY, {}, [change])


async def test_a_proposal_is_changes_or_organizations_never_both():
    with pytest.raises(proposals.ProposalError, match="one of the two"):
        await proposals.propose(None, author_user_id=None)  # type: ignore[arg-type]
    with pytest.raises(proposals.ProposalError, match="one of the two"):
        await proposals.propose(
            None,  # type: ignore[arg-type]
            author_user_id=None,
            changes=[{"field": "sector", "value": "x"}],
            new_tracked=[{"name": "y", "relationship": "partner"}],
        )


class _Db:
    async def flush(self):
        return None


async def test_an_empty_accept_rejects_without_writing():
    row = SimpleNamespace(status="pending", kind="fields", proposals=[], organization_id=None)
    admin = uuid.uuid4()
    outcome = await proposals.decide(_Db(), row, admin_user_id=admin, accept=[])  # type: ignore[arg-type]
    assert outcome == {"status": "rejected", "version": None, "created": []}
    assert row.status == "rejected"
    assert row.decided_by_user_id == admin


async def test_a_decided_proposal_cannot_be_decided_again():
    row = SimpleNamespace(status="accepted", kind="fields", proposals=[])
    with pytest.raises(proposals.ProposalError, match="already accepted"):
        await proposals.decide(_Db(), row, admin_user_id=uuid.uuid4(), accept=["sector"])  # type: ignore[arg-type]


async def test_accepting_an_organization_not_in_the_proposal_is_refused():
    row = SimpleNamespace(
        status="pending",
        kind="new_tracked",
        proposals=[{"name": "Acme", "relationship": "provider", "criticality": "medium", "basis": ""}],
    )
    with pytest.raises(proposals.ProposalError, match="Not in this proposal"):
        await proposals.decide(_Db(), row, admin_user_id=uuid.uuid4(), accept=["Globex"])  # type: ignore[arg-type]
    assert row.status == "pending"


def test_the_answer_names_the_review_page_and_the_conflict():
    row = SimpleNamespace(
        id=uuid.uuid4(),
        kind="fields",
        proposals=[{"field": "sector", "value": "Finance", "conflict": True}],
    )
    text = proposals.render_for_agent(row)  # type: ignore[arg-type]
    assert "Nothing is saved until an administrator accepts it" in text
    assert "named explicitly" in text
    assert f"proposal={row.id}" in text


@pytest.mark.parametrize(
    ("slug", "builtin", "admin", "offered"),
    [
        ("ctem-assistant", True, True, True),
        ("platform-health", True, True, True),
        ("concierge", True, True, False),
        ("ctem-assistant", True, False, False),
        ("ctem-assistant", False, True, False),
        ("opencti-assistant", True, True, False),
        (None, True, True, False),
    ],
)
def test_the_tool_is_offered_to_the_two_agents_for_an_administrator_only(slug, builtin, admin, offered):
    assert offers_organization_proposals(slug, is_builtin=builtin, caller_is_admin=admin) is offered


async def test_the_tool_refuses_a_non_administrator_again():
    definitions, executor = build_organization_proposal_tool(user_id=str(uuid.uuid4()), caller_is_admin=False)
    assert [d.name for d in definitions] == [ORGANIZATION_PROPOSAL_TOOL]
    assert definitions[0].core is False
    result = await executor.execute_tool(
        ToolCall(id="1", name=ORGANIZATION_PROPOSAL_TOOL, arguments={"changes": [{"field": "sector", "value": "x"}]})
    )
    assert str(result.content).startswith("Refused")


def test_the_tool_never_saves_a_version():
    source = (BACKEND / "app/services/builtin_tools/organization_proposals.py").read_text(encoding="utf-8")
    assert "save_version" not in source
    service = ast.parse((BACKEND / "app/services/organization_context_proposals.py").read_text(encoding="utf-8"))
    callers = {
        fn.name
        for fn in ast.walk(service)
        if isinstance(fn, ast.AsyncFunctionDef)
        and any(isinstance(n, ast.Attribute) and n.attr == "save_version" for n in ast.walk(fn))
    }
    # Only an administrator saves a version: their decision on a proposal, or
    # update_organization_context acting for them (apply_changes, #11594).
    assert callers == {"decide", "apply_changes"}


def test_every_chat_turn_wires_the_tool_behind_the_gate():
    tree = ast.parse((BACKEND / "app/services/agent_chat.py").read_text(encoding="utf-8"))
    names = {getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert {"offers_organization_proposals", "build_organization_proposal_tool"} <= names
