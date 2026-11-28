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
from app.integrations.providers.defender import build_tools as defender_build_tools
from app.services import fleet_capability_slots as slots
from app.services.fleet_capability_slots import IntegrationRef, SlotBindingRefusedError
from app.tools.base import CallableToolExecutor

_CATALOG = {
    "ticketer": {"integration_types": ["ticketing"]},
    "chat": {"integration_types": ["messaging"]},
    "edr_vendor": {"integration_types": ["edr", "soar"]},
    "intel": {"integration_types": ["threat_intel"]},
}


def _ref(iid: str, provider: str, *, company: bool = True, connected: bool = True) -> IntegrationRef:
    return IntegrationRef(
        integration_id=iid, provider=provider, name=f"{provider}-{iid}", company_managed=company, connected=connected
    )


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


def test_jira_fills_the_ticketing_slot_and_is_not_a_code_host() -> None:
    assert "jira" in slots.providers_for("ticketing", PROVIDER_CATALOG)
    assert "repository" not in PROVIDER_CATALOG["jira"]["integration_types"]
    assert "jira" not in slots.providers_for("source_code", PROVIDER_CATALOG)


def test_aikido_is_application_security_only_never_a_vulnerability_scanner() -> None:
    """#1434: Aikido appears once, under Application security."""
    assert "aikido" in slots.providers_for("application_security", PROVIDER_CATALOG)
    assert "aikido" not in slots.providers_for("scanner", PROVIDER_CATALOG)
    # Infrastructure and cloud scanners stay in Vulnerability scanner.
    assert {"tenable", "qualys", "wiz"} <= slots.providers_for("scanner", PROVIDER_CATALOG)


def test_github_and_gitlab_fill_source_code() -> None:
    assert {"github", "gitlab"} <= slots.providers_for("source_code", PROVIDER_CATALOG)
    for key in slots.providers_for("source_code", PROVIDER_CATALOG):
        assert "repository" in PROVIDER_CATALOG[key]["integration_types"], key


def test_excluded_types_keep_a_provider_out_of_a_capability() -> None:
    catalog = {
        "infra": {"integration_types": ["vulnerability_management"]},
        "appsec": {"integration_types": ["vulnerability_management", "application_security"]},
    }
    assert slots.providers_for("scanner", catalog) == frozenset({"infra"})
    assert slots.providers_for("application_security", catalog) == frozenset({"appsec"})


def test_the_ctem_roles_take_application_security_and_source_code() -> None:
    """#1434: code flaws and dependencies against assets (Triage, Relevance), source-code assets (Discovery), CI gates and DAST (Validation)."""
    for role in ("Triage", "Relevance", "Discovery"):
        assert {"application_security", "source_code"} <= set(slots.slots_for_role(role)), role
    assert "application_security" in slots.slots_for_role("Validation")
    assert "source_code" not in slots.slots_for_role("Mobilization")


def test_a_personal_aikido_is_blocked_with_its_reason_for_a_company_managed_member() -> None:
    """#1434 item 6: the not_usable state names why a personal Aikido cannot be taken."""
    personal = IntegrationRef(
        integration_id="a1", provider="aikido", name="Aikido", company_managed=False, connected=True
    )
    states = {
        s.capability: s
        for s in slots.slot_states("Triage", {}, [personal], PROVIDER_CATALOG, company_managed_only=True)
    }
    appsec = states["application_security"]
    assert appsec.candidates == []
    assert [(b.integration_id, b.reason) for b in appsec.blocked] == [("a1", slots.BLOCKED_NOT_COMPANY_MANAGED)]
    member = slots.TypeMember(
        member_id="m1", agent_id="g1", name="Triage", role_label="Triage", can_edit=True, bound=[], custom=False
    )
    by_cap = {t.capability: t for t in slots.type_states([(member, list(states.values()))])}
    assert by_cap["application_security"].state == slots.TYPE_NOT_USABLE
    assert by_cap["application_security"].label == "Application security"


def test_every_slot_names_a_known_capability() -> None:
    for role, caps in slots.ROLE_SLOTS.items():
        assert caps, role
        assert set(caps) <= set(slots.CAPABILITIES), role


def test_the_ctem_roles_the_brief_names_have_their_slots() -> None:
    assert slots.slots_for_role("Mobilization") == ("ticketing", "notification", "edr_xdr", "siem", "search_engine")
    assert {"edr_xdr", "scanner", "easm"} <= set(slots.slots_for_role("Validation"))
    assert {"vulnerability_intelligence", "asset_inventory"} <= set(slots.slots_for_role("Triage"))
    assert slots.slots_for_role("Orchestrator") == ()
    assert slots.slots_for_role(None) == ()


_WEB_RESEARCH_ROLES = ("Relevance", "Triage", "Validation", "Hunt", "Mobilization")


def test_the_search_engine_capability_takes_the_web_search_providers() -> None:
    """#2728: Brave Search and Google Search fill the Search engine slot."""
    assert slots.CAPABILITIES["search_engine"]["label"] == "Search engine"
    assert {"brave_search", "google_search"} <= slots.providers_for("search_engine", PROVIDER_CATALOG)
    assert slots.builtin_for("search_engine") is None


def test_the_web_research_roles_hold_the_search_engine_slot() -> None:
    """#2728: the roles that gain from open-web research, and only those."""
    for role in _WEB_RESEARCH_ROLES:
        assert "search_engine" in slots.slots_for_role(role), role
    for role in ("Detections", "Gap analysis", "Discovery", "Orchestrator", "Risk"):
        assert "search_engine" not in slots.slots_for_role(role), role


def test_the_ctem_loop_shows_eleven_capability_types() -> None:
    """#2728: CTEM loop > Members > Capabilities reads 11 types with Search engine among them.

    Slots are read from the role label at request time, so a fleet instantiated
    before the upgrade gains the type with no migration and no binding change.
    """
    loop = next(f for f in BUILTIN_FLEETS if f["slug"] == "ctem-loop")
    caps = {c for m in loop["members"] for c in slots.slots_for_role(m.get("role_label"))}
    assert len(caps) == 11
    assert "search_engine" in caps


def test_an_empty_search_engine_slot_is_optional() -> None:
    """#2728: nothing connected reads unavailable / none_connected, never an error."""
    states = {s.capability: s for s in slots.slot_states("Relevance", {}, [], PROVIDER_CATALOG)}
    assert states["search_engine"].state == slots.SLOT_UNAVAILABLE
    member = slots.TypeMember(
        member_id="m1", agent_id="g1", name="Relevance", role_label="Relevance", can_edit=True, bound=[], custom=False
    )
    by_cap = {t.capability: t for t in slots.type_states([(member, list(states.values()))])}
    assert by_cap["search_engine"].state == slots.TYPE_NONE_CONNECTED


def test_binding_brave_fills_the_slot_and_unbinding_clears_it() -> None:
    """#2728: bound, the member holds the Brave integration (its tools); unbound, it does not."""
    brave = _ref("b1", "brave_search")
    bound = slots.bindings_with("Relevance", "search_engine", {"opencti": ["o1"]}, brave, PROVIDER_CATALOG)
    assert bound == {"opencti": ["o1"], "brave_search": ["b1"]}
    states = {s.capability: s for s in slots.slot_states("Relevance", bound, [brave], PROVIDER_CATALOG)}
    assert states["search_engine"].state == slots.SLOT_FILLED
    unbound = slots.bindings_without("Relevance", "search_engine", bound, "b1", PROVIDER_CATALOG)
    assert unbound == {"opencti": ["o1"]}
    with pytest.raises(SlotBindingRefusedError):
        slots.bindings_with("Detections", "search_engine", {}, brave, PROVIDER_CATALOG)


def test_the_web_research_members_match_the_fleet_roles() -> None:
    """#2728: the skill and the built-in search go to exactly the built-in members whose role holds the slot."""
    from app.agents.builtin_agents import CTEM_WEB_RESEARCH_AGENT_SLUGS

    holders = {
        m["agent"]
        for fleet in BUILTIN_FLEETS
        for m in fleet["members"]
        if "search_engine" in slots.slots_for_role(m.get("role_label"))
    }
    assert holders == set(CTEM_WEB_RESEARCH_AGENT_SLUGS)


def test_the_web_research_skill_and_personas_are_seeded() -> None:
    """#2728: the skill is bound to every slot holder, and each persona points to it."""
    from pathlib import Path

    from app.agents.builtin_agents import _BUILTIN_WEB_SEARCH_AGENT_SLUGS, BUILTIN_SKILLS, CTEM_WEB_RESEARCH_AGENT_SLUGS

    skill = next(s for s in BUILTIN_SKILLS if s["slug"] == "ctem-web-research")
    assert set(skill["bind_to_agent_slugs"]) == set(CTEM_WEB_RESEARCH_AGENT_SLUGS)
    assert set(CTEM_WEB_RESEARCH_AGENT_SLUGS) <= set(_BUILTIN_WEB_SEARCH_AGENT_SLUGS)
    assert len(_BUILTIN_WEB_SEARCH_AGENT_SLUGS) == len(set(_BUILTIN_WEB_SEARCH_AGENT_SLUGS))
    seed = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data"
    body = (seed / "skill_content" / "ctem-web-research.md").read_text()
    for phrase in ("untrusted data", "URL", "hostname", "IP address", "primary", "not connected", "KEV"):
        assert phrase in body, phrase
    for slug in CTEM_WEB_RESEARCH_AGENT_SLUGS:
        persona = seed / "agent_persona" / f"{slug}.md"
        if persona.exists():
            text = persona.read_text()
            assert "ctem-web-research" in text, slug
            assert "web search not connected" in text, slug


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


def test_every_capability_binds_several_integrations() -> None:
    """#2424: an agent holds several integrations of one kind (Jira and ServiceNow, two EDRs)."""
    catalog = {**_CATALOG, "snow": {"integration_types": ["itsm"]}, "edr_two": {"integration_types": ["xdr"]}}
    out = slots.bindings_set(
        "Mobilization", "ticketing", {"chat": ["c1"]}, [_ref("t1", "ticketer"), _ref("s1", "snow")], catalog
    )
    assert out == {"chat": ["c1"], "ticketer": ["t1"], "snow": ["s1"]}
    edr = slots.bindings_set(
        "Hunt", "edr_xdr", {}, [_ref("e1", "edr_vendor"), _ref("e2", "edr_vendor"), _ref("x1", "edr_two")], catalog
    )
    assert edr == {"edr_vendor": ["e1", "e2"], "edr_two": ["x1"]}
    states = {s.capability: s for s in slots.slot_states("Hunt", edr, [], catalog)}
    assert states["edr_xdr"].state == slots.SLOT_FILLED
    assert sorted(b.integration_id for b in states["edr_xdr"].bound) == ["e1", "e2", "x1"]


def test_removing_one_of_two_keeps_the_other_bound() -> None:
    """#2424: removing a chip unbinds that integration alone."""
    bindings = {"ticketer": ["t1", "t2"], "chat": ["c1"]}
    assert slots.bindings_set("Mobilization", "ticketing", bindings, [_ref("t2", "ticketer")], _CATALOG) == {
        "ticketer": ["t2"],
        "chat": ["c1"],
    }
    assert slots.bindings_without("Mobilization", "ticketing", bindings, "t1", _CATALOG) == {
        "ticketer": ["t2"],
        "chat": ["c1"],
    }
    # A one-click bind adds to what the slot holds, it never replaces it.
    added = slots.bindings_with("Mobilization", "ticketing", {"ticketer": "t1"}, _ref("t3", "ticketer"), _CATALOG)
    assert added == {"ticketer": ["t1", "t3"]}


def test_a_stored_single_binding_reads_unchanged() -> None:
    """#2424: a slot stored with one integration (a scalar or a one-item list) loads as it was."""
    connected = [_ref("t1", "ticketer")]
    for stored in ({"ticketer": "t1"}, {"ticketer": ["t1"]}):
        states = slots.slot_states("Mobilization", stored, connected, _CATALOG)
        ticketing = next(s for s in states if s.capability == "ticketing")
        assert ticketing.state == slots.SLOT_FILLED
        assert [b.integration_id for b in ticketing.bound] == ["t1"]


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


def test_a_connected_integration_a_company_managed_member_cannot_take_is_named_never_hidden() -> None:
    """#1373: a Jira connected but not company-managed read "None connected" on a built-in fleet."""
    jira = _ref("j1", "ticketer", company=False)
    states = slots.slot_states("Mobilization", {}, [jira], _CATALOG, company_managed_only=True)
    ticketing = next(s for s in states if s.capability == "ticketing")
    assert ticketing.candidates == []
    assert ticketing.state == slots.SLOT_UNAVAILABLE
    assert [(b.integration_id, b.reason) for b in ticketing.blocked] == [("j1", slots.BLOCKED_NOT_COMPANY_MANAGED)]
    types = {t.capability: t for t in slots.type_states([(_member("m1"), states)])}
    assert types["ticketing"].state == slots.TYPE_NOT_USABLE
    assert [b.integration_id for b in types["ticketing"].blocked] == ["j1"]
    # The same integration on an agent that is not company-managed is a plain candidate.
    free = slots.slot_states("Mobilization", {}, [jira], _CATALOG)
    assert [c.integration_id for c in free[0].candidates] == ["j1"] and free[0].blocked == []


def test_an_integration_not_connected_yet_is_blocked_and_never_a_candidate() -> None:
    pending = IntegrationRef(
        integration_id="j2", provider="ticketer", name="Jira", company_managed=True, connected=False
    )
    ticketing = slots.slot_states("Mobilization", {}, [pending], _CATALOG, company_managed_only=True)[0]
    assert ticketing.candidates == []
    assert [(b.integration_id, b.reason) for b in ticketing.blocked] == [("j2", slots.BLOCKED_NOT_CONNECTED)]


def test_a_bound_integration_is_never_blocked() -> None:
    jira = _ref("j1", "ticketer", company=False)
    ticketing = slots.slot_states("Mobilization", {"ticketer": ["j1"]}, [jira], _CATALOG, company_managed_only=True)[0]
    assert ticketing.state == slots.SLOT_FILLED and ticketing.blocked == []


def test_defender_fills_siem_beside_sentinel_and_stays_an_edr_xdr() -> None:
    """#1373: Sentinel and Defender XDR converge; Defender's advanced hunting is a SIEM source."""
    siem = slots.providers_for("siem", PROVIDER_CATALOG)
    assert {"defender", "sentinel"} <= siem
    assert "defender" in slots.providers_for("edr_xdr", PROVIDER_CATALOG)


def test_defender_fills_the_vulnerability_scanner_and_keeps_edr_xdr_and_siem() -> None:
    """Defender Vulnerability Management (TVM) makes Defender XDR a vulnerability scanner too."""
    assert "defender" in slots.providers_for("scanner", PROVIDER_CATALOG)
    assert "defender" in slots.providers_for("edr_xdr", PROVIDER_CATALOG)
    assert "defender" in slots.providers_for("siem", PROVIDER_CATALOG)
    # The TVM reads a scanner consumer needs are Defender tools.
    scanner_reads = {"list_defender_vulnerabilities", "list_defender_machine_vulnerabilities", "list_defender_software"}
    assert scanner_reads <= {tool.name for tool in defender_build_tools("t||c||s", CallableToolExecutor())}


def test_both_reasons_are_listed_and_the_type_reads_not_usable() -> None:
    """#1373: a personal Jira under a company-managed member reads why, not "None connected"."""
    connected = [_ref("t1", "ticketer", company=False), _ref("t2", "ticketer", connected=False)]
    states = slots.slot_states("Mobilization", {}, connected, _CATALOG, company_managed_only=True)
    ticketing = next(s for s in states if s.capability == "ticketing")
    assert ticketing.candidates == []
    assert ticketing.state == slots.SLOT_UNAVAILABLE
    assert [(b.integration_id, b.reason) for b in ticketing.blocked] == [
        ("t1", slots.BLOCKED_NOT_COMPANY_MANAGED),
        ("t2", slots.BLOCKED_NOT_CONNECTED),
    ]
    types = {t.capability: t for t in slots.type_states([(_member("m1"), states)])}
    assert types["ticketing"].state == slots.TYPE_NOT_USABLE
    assert [b.integration_id for b in types["ticketing"].blocked] == ["t1", "t2"]


def test_a_bindable_integration_keeps_the_type_not_bound_beside_a_blocked_one() -> None:
    connected = [_ref("t1", "ticketer"), _ref("t2", "ticketer", company=False)]
    states = slots.slot_states("Mobilization", {}, connected, _CATALOG, company_managed_only=True)
    ticketing = next(t for t in slots.type_states([(_member("m1"), states)]) if t.capability == "ticketing")
    assert ticketing.state == slots.TYPE_NOT_BOUND
    assert [b.integration_id for b in ticketing.blocked] == ["t2"]


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
