"""Capability slots of a fleet's roles (#914 slice 2).

Each role label declares the capabilities it can use; a capability is a set of
catalog integration types, so any provider of the catalog typed that way fills
it. A slot reads filled, empty or unavailable, and binding writes the member
agent's integration bindings (the customer layer of a built-in).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import fleet_capability_slots as slots
from app.services.fleet_capability_slots import IntegrationRef, SlotBindingRefusedError

_CATALOG = {
    "ticketer": {"integration_types": ["ticketing"]},
    "chat": {"integration_types": ["messaging"]},
    "edr_vendor": {"integration_types": ["edr", "soar"]},
    "intel": {"integration_types": ["threat_intel"]},
}


def _ref(iid: str, provider: str, *, company: bool = True) -> IntegrationRef:
    return IntegrationRef(integration_id=iid, provider=provider, name=f"{provider}-{iid}", company_managed=company)


def test_every_capability_maps_to_types_the_catalog_uses() -> None:
    catalog: dict[str, Any] = PROVIDER_CATALOG
    used = {t for entry in catalog.values() for t in entry.get("integration_types", [])}
    for key, capability in slots.CAPABILITIES.items():
        assert set(capability["integration_types"]) <= used, key
        assert slots.providers_for(key, PROVIDER_CATALOG), key


def test_every_slot_names_a_known_capability() -> None:
    for role, caps in slots.ROLE_SLOTS.items():
        assert caps, role
        assert set(caps) <= set(slots.CAPABILITIES), role


def test_the_ctem_roles_the_brief_names_have_their_slots() -> None:
    assert slots.slots_for_role("Mobilization") == ("ticketing", "notification")
    assert {"edr_xdr", "scanner", "easm"} <= set(slots.slots_for_role("Validation"))
    assert {"vulnerability_intelligence", "asset_inventory"} <= set(slots.slots_for_role("Triage"))
    assert slots.slots_for_role("Orchestrator") == ()
    assert slots.slots_for_role(None) == ()


def test_seeded_fleets_carry_slotted_roles() -> None:
    labels = {m.get("role_label") for fleet in BUILTIN_FLEETS for m in fleet["members"]}
    assert {"Mobilization", "Validation", "Triage"} <= labels


def test_states_filled_empty_unavailable() -> None:
    bindings = {"ticketer": ["t1"]}
    connected = [_ref("t1", "ticketer"), _ref("c1", "chat")]
    states = {s.capability: s for s in slots.slot_states("Mobilization", bindings, connected, _CATALOG)}
    assert states["ticketing"].state == slots.SLOT_FILLED
    assert [b.integration_id for b in states["ticketing"].bound] == ["t1"]
    assert states["ticketing"].candidates == []
    assert states["notification"].state == slots.SLOT_EMPTY
    assert [c.integration_id for c in states["notification"].candidates] == ["c1"]

    nothing = slots.slot_states("Mobilization", {}, [], _CATALOG)
    assert {s.state for s in nothing} == {slots.SLOT_UNAVAILABLE}


def test_a_company_managed_agent_is_offered_company_managed_integrations_only() -> None:
    connected = [_ref("c1", "chat", company=False)]
    states = slots.slot_states("Mobilization", {}, connected, _CATALOG, company_managed_only=True)
    assert {s.state for s in states} == {slots.SLOT_UNAVAILABLE}
    with pytest.raises(SlotBindingRefusedError) as exc:
        slots.bindings_with("Mobilization", "notification", {}, connected[0], _CATALOG, company_managed_only=True)
    assert exc.value.status_code == 400


def test_binding_adds_to_the_agent_bindings_and_keeps_the_others() -> None:
    out = slots.bindings_with("Validation", "edr_xdr", {"intel": ["i1"]}, _ref("e1", "edr_vendor"), _CATALOG)
    assert out == {"intel": ["i1"], "edr_vendor": ["e1"]}
    again = slots.bindings_with("Validation", "edr_xdr", out, _ref("e1", "edr_vendor"), _CATALOG)
    assert again == out


def test_binding_refuses_a_slot_the_role_lacks_or_a_provider_that_does_not_fit() -> None:
    with pytest.raises(SlotBindingRefusedError) as wrong_role:
        slots.bindings_with("Orchestrator", "ticketing", {}, _ref("t1", "ticketer"), _CATALOG)
    assert wrong_role.value.status_code == 422
    with pytest.raises(SlotBindingRefusedError) as wrong_type:
        slots.bindings_with("Mobilization", "ticketing", {}, _ref("c1", "chat"), _CATALOG)
    assert "does not provide" in wrong_type.value.detail
    with pytest.raises(SlotBindingRefusedError):
        slots.bindings_with("Mobilization", "nope", {}, _ref("t1", "ticketer"), _CATALOG)


def test_unbinding_removes_only_that_integration() -> None:
    bindings = {"ticketer": ["t1", "t2"], "intel": ["i1"]}
    out = slots.bindings_without("Mobilization", "ticketing", bindings, "t1", _CATALOG)
    assert out == {"ticketer": ["t2"], "intel": ["i1"]}
    gone = slots.bindings_without("Mobilization", "ticketing", out, "t2", _CATALOG)
    assert gone == {"intel": ["i1"]}
    with pytest.raises(SlotBindingRefusedError) as missing:
        slots.bindings_without("Mobilization", "ticketing", gone, "t2", _CATALOG)
    assert missing.value.status_code == 404


def test_a_scalar_binding_value_is_read_as_one_id() -> None:
    assert slots.bound_ids({"ticketer": "t1", "chat": None}) == {"ticketer": {"t1"}, "chat": set()}
