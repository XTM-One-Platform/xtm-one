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


def test_filigran_ai_is_never_a_candidate_of_any_capability() -> None:
    """An extraction model, not a data source: its threat_intel type stays for the catalog browser only."""
    assert "threat_intel" in PROVIDER_CATALOG["filigran_ai"]["integration_types"]
    for key in slots.CAPABILITIES:
        assert "filigran_ai" not in slots.providers_for(key, PROVIDER_CATALOG), key


def test_an_entry_flagged_out_of_capability_slots_is_never_offered() -> None:
    catalog = {**_CATALOG, "extractor": {"integration_types": ["threat_intel"], "capability_slots": False}}
    assert slots.providers_for("vulnerability_intelligence", catalog) == frozenset({"intel"})
    states = slots.slot_states("Triage", {}, [_ref("x1", "extractor")], catalog)
    assert states[0].capability == "vulnerability_intelligence"
    assert states[0].candidates == []
    assert states[0].state == slots.SLOT_UNAVAILABLE


def test_jira_fills_the_ticketing_slot_and_keeps_its_repository_type() -> None:
    assert "jira" in slots.providers_for("ticketing", PROVIDER_CATALOG)
    assert "repository" in PROVIDER_CATALOG["jira"]["integration_types"]


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
    # In XTM One only is the default notification: the slot is filled, the chat stays offered.
    assert states["notification"].state == slots.SLOT_FILLED
    assert states["notification"].builtin is not None
    assert [c.integration_id for c in states["notification"].candidates] == ["c1"]

    # Notification carries the built-in in-app option: never "None connected".
    nothing = {s.capability: s for s in slots.slot_states("Mobilization", {}, [], _CATALOG)}
    assert nothing["ticketing"].state == slots.SLOT_UNAVAILABLE
    assert nothing["notification"].state == slots.SLOT_FILLED
    assert nothing["notification"].bound == []


def test_a_company_managed_agent_is_offered_company_managed_integrations_only() -> None:
    connected = [_ref("c1", "chat", company=False)]
    states = slots.slot_states("Mobilization", {}, connected, _CATALOG, company_managed_only=True)
    assert all(s.candidates == [] for s in states)
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


def test_notification_offers_email_and_the_platform_email() -> None:
    providers = slots.providers_for("notification", PROVIDER_CATALOG)
    assert {"platform_email", "microsoft", "google", "ses", "slack"} <= providers


def test_asset_inventory_never_offers_the_filigran_modules() -> None:
    """OpenAEV, OpenCRQ and OpenCTI feed the XTM One inventory; they are not alternatives to it."""
    providers = slots.providers_for("asset_inventory", PROVIDER_CATALOG)
    assert not {"openaev", "opencrq", "opencti"} & providers
    builtin = slots.builtin_for("asset_inventory")
    assert builtin is not None and builtin.key == "xtm_one_inventory"


def test_a_builtin_option_is_never_stored_as_a_binding() -> None:
    states = slots.slot_states("Triage", {}, [], _CATALOG)
    inventory = next(s for s in states if s.capability == "asset_inventory")
    assert inventory.state == slots.SLOT_FILLED and inventory.bound == []
    assert slots.bindings_set("Mobilization", "notification", {"intel": ["i1"]}, [], _CATALOG) == {"intel": ["i1"]}


def test_bindings_set_binds_exactly_and_keeps_other_providers() -> None:
    bindings = {"chat": ["c1", "c2"], "intel": ["i1"]}
    out = slots.bindings_set(
        "Mobilization", "notification", bindings, [_ref("c2", "chat"), _ref("c3", "chat")], _CATALOG
    )
    assert out == {"chat": ["c2", "c3"], "intel": ["i1"]}
    with pytest.raises(SlotBindingRefusedError):
        slots.bindings_set("Mobilization", "notification", bindings, [_ref("t1", "ticketer")], _CATALOG)


def test_notification_binds_several_channels() -> None:
    out = slots.bindings_set(
        "Mobilization",
        "notification",
        {},
        [_ref("c1", "chat"), _ref("m1", "mail")],
        {**_CATALOG, "mail": {"integration_types": ["email"]}},
    )
    assert out == {"chat": ["c1"], "mail": ["m1"]}


def _member(mid: str, *, can_edit: bool = True) -> slots.TypeMember:
    return slots.TypeMember(
        member_id=mid,
        agent_id=f"a{mid}",
        name=mid,
        role_label="Mobilization",
        can_edit=can_edit,
        bound=[],
        custom=False,
    )


def test_type_view_groups_members_and_flags_custom() -> None:
    connected = [_ref("t1", "ticketer"), _ref("t2", "ticketer")]
    pairs = [
        (_member("m1"), slots.slot_states("Mobilization", {"ticketer": ["t1"]}, connected, _CATALOG)),
        (_member("m2"), slots.slot_states("Mobilization", {"ticketer": ["t1"]}, connected, _CATALOG)),
        (_member("m3"), slots.slot_states("Mobilization", {"ticketer": ["t2"]}, connected, _CATALOG)),
        (_member("m4"), slots.slot_states("Mobilization", {}, connected, _CATALOG)),
    ]
    types = {t.capability: t for t in slots.type_states(pairs)}
    ticketing = types["ticketing"]
    assert [r.integration_id for r in ticketing.choice] == ["t1"]
    assert ticketing.state == slots.TYPE_PARTIAL
    assert {m.member_id: m.custom for m in ticketing.members} == {"m1": False, "m2": False, "m3": True, "m4": False}
    assert types["notification"].state == slots.TYPE_CONNECTED


def test_type_write_keeps_custom_members_unless_confirmed() -> None:
    connected = [_ref("t1", "ticketer"), _ref("t2", "ticketer")]
    pairs = [
        (_member("m1"), slots.slot_states("Mobilization", {"ticketer": ["t1"]}, connected, _CATALOG)),
        (_member("m2"), slots.slot_states("Mobilization", {"ticketer": ["t2"]}, connected, _CATALOG)),
        (_member("m3", can_edit=False), slots.slot_states("Mobilization", {}, connected, _CATALOG)),
        (_member("m4"), slots.slot_states("Mobilization", {}, connected, _CATALOG)),
    ]
    ticketing = next(t for t in slots.type_states(pairs) if t.capability == "ticketing")
    assert slots.members_to_write(ticketing, override_custom=False) == ["m1", "m4"]
    assert slots.members_to_write(ticketing, override_custom=True) == ["m1", "m2", "m4"]


def test_type_states_none_connected_and_not_bound() -> None:
    none = slots.type_states([(_member("m1"), slots.slot_states("Validation", {}, [], _CATALOG))])
    assert {t.state for t in none} == {slots.TYPE_NONE_CONNECTED}
    offered = slots.type_states(
        [(_member("m1"), slots.slot_states("Validation", {}, [_ref("e1", "edr_vendor")], _CATALOG))]
    )
    assert next(t for t in offered if t.capability == "edr_xdr").state == slots.TYPE_NOT_BOUND


def test_a_scalar_binding_value_is_read_as_one_id() -> None:
    assert slots.bound_ids({"ticketer": "t1", "chat": None}) == {"ticketer": {"t1"}, "chat": set()}


def test_the_mobilization_role_notifies_in_app_when_no_external_channel_is_bound() -> None:
    """#1201 slice 3: the notify step reads the Notification slot; no channel is never a failure."""
    from app.agents.seed import SEED_DATA_DIR

    builtin = slots.builtin_for("notification")
    assert builtin is not None and builtin.always_on
    text = (SEED_DATA_DIR / "agent_persona" / "ctem-remediation-coordinator.md").read_text(encoding="utf-8")
    assert "Notification capability" in text
    assert "notified in XTM One only" in text and "never a failed run or an error" in text
