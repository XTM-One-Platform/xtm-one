"""Auto-provisioned OpenCTI triggers (services/opencti_trigger_provisioning.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.api.v1.webhooks import _normalize_opencti_payload
from app.services import opencti_trigger_provisioning as prov


class FakeOpenCTI:
    """An in-memory OpenCTI answering the GraphQL calls the provisioning makes."""

    def __init__(
        self,
        labels: dict[str, str] | None = None,
        countries: list[dict] | None = None,
        sectors: list[dict] | None = None,
    ) -> None:
        self.labels = labels or {}
        self.countries = countries or []
        self.sectors = sectors or []
        self.notifiers: dict[str, dict] = {}
        self.triggers: dict[str, dict] = {}
        self.playbooks: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self._next = 0

    def _id(self) -> str:
        self._next += 1
        return f"id-{self._next}"

    async def __call__(self, query: str, variables: dict | None = None):
        v = variables or {}
        op = query.split("(")[0].split("{")[0].split()[-1]
        self.calls.append(op)
        if op == "XtmLabels":
            nodes = [{"id": i, "value": name} for name, i in self.labels.items() if v["search"].lower() in name.lower()]
            return {"labels": {"edges": [{"node": n} for n in nodes]}}, None
        if op in ("XtmCountries", "XtmSectors"):
            root = "countries" if op == "XtmCountries" else "sectors"
            rows = self.countries if root == "countries" else self.sectors
            # OpenCTI's search is fuzzy: answer every row, the caller matches exactly.
            return {root: {"edges": [{"node": n} for n in rows]}}, None
        if op == "XtmMe":
            return {"me": {"id": "user-xtm", "name": "XTM One integration"}}, None
        if op == "XtmPirs":
            return {"pirs": {"edges": [{"node": {"id": "pir-1", "name": "Ransomware on finance"}}]}}, None
        if op == "XtmNodeReplace":
            i = v["input"]
            for node in self.playbooks[v["id"]]["nodes"]:
                if node["id"] == v["nodeId"]:
                    node.update(
                        name=i["name"],
                        component_id=i["component_id"],
                        position=i["position"],
                        configuration=i["configuration"],
                    )
            return {"playbookReplaceNode": v["nodeId"]}, None
        if op == "XtmPlaybook":
            p = self.playbooks.get(v["id"])
            if not p:
                return {"playbook": None}, None
            definition = json.dumps({"nodes": p["nodes"], "links": []})
            return {
                "playbook": {
                    "id": v["id"],
                    "name": p["name"],
                    "playbook_running": p["running"],
                    "playbook_definition": definition,
                }
            }, None
        if op == "XtmPlaybookAdd":
            pid = self._id()
            self.playbooks[pid] = {"name": v["input"]["name"], "nodes": [], "running": False}
            return {"playbookAdd": {"id": pid}}, None
        if op == "XtmNodeAdd":
            nid = self._id()
            i = v["input"]
            self.playbooks[v["id"]]["nodes"].append(
                {
                    "id": nid,
                    "name": i["name"],
                    "position": i["position"],
                    "component_id": i["component_id"],
                    "configuration": i["configuration"],
                    "parent": i.get("parentNodeId"),
                }
            )
            return {"playbookAddNode": nid}, None
        if op == "XtmPlaybookRun":
            self.playbooks[v["id"]]["running"] = v["input"][0]["value"] == ["true"]
            return {"playbookFieldPatch": {"id": v["id"]}}, None
        if op == "XtmFind":
            root = (
                "notifiers" if "notifiers(" in query else "playbooks" if "playbooks(" in query else "triggersKnowledge"
            )
            store = self.notifiers if root == "notifiers" else self.playbooks if root == "playbooks" else self.triggers
            nodes = [{"id": k, "name": o["name"]} for k, o in store.items()]
            return {root: {"edges": [{"node": n} for n in nodes]}}, None
        if op == "XtmNotifier":
            n = self.notifiers.get(v["id"])
            return {"notifier": dict(n, id=v["id"]) if n else None}, None
        if op == "XtmNotifierAdd":
            nid = self._id()
            self.notifiers[nid] = {"name": v["input"]["name"], "notifier_configuration": v["input"]["notifier_configuration"]}
            return {"notifierAdd": {"id": nid}}, None
        if op == "XtmNotifierPatch":
            for e in v["input"]:
                self.notifiers[v["id"]][e["key"]] = e["value"][0]
            return {"notifierFieldPatch": {"id": v["id"]}}, None
        if op == "XtmTrigger":
            t = self.triggers.get(v["id"])
            if not t:
                return {"triggerKnowledge": None}, None
            return {"triggerKnowledge": {"id": v["id"], "name": t["name"], "event_types": t["event_types"],
                                         "filters": t["filters"], "notifiers": [{"id": n} for n in t["notifiers"]]}}, None
        if op == "XtmTriggerAdd":
            tid = self._id()
            i = v["input"]
            self.triggers[tid] = {"name": i["name"], "event_types": i["event_types"], "filters": i["filters"], "notifiers": i["notifiers"]}
            return {"triggerKnowledgeLiveAdd": {"id": tid}}, None
        if op == "XtmTriggerPatch":
            for e in v["input"]:
                self.triggers[v["id"]][e["key"]] = e["value"] if e["key"] == "notifiers" else e["value"][0]
            return {"triggerKnowledgeFieldPatch": {"id": v["id"]}}, None
        if op == "XtmDelete":
            store = (
                self.triggers
                if "triggerKnowledgeDelete" in query
                else self.playbooks
                if "playbookDelete" in query
                else self.notifiers
            )
            if store.pop(v["id"], None) is None:
                return {}, "Object not found"
            return {}, None
        raise AssertionError(f"unexpected query {op}")


SPEC_CONFIG = {
    "provider": "opencti",
    "event": "create",
    "provision": {"fleet": "Threat response", "purpose": "New threats", "entity_types": ["Report", "Intrusion-Set"],
                  "labels": ["ai"], "min_score": 50},
}
URL = "https://xtm.example/api/hooks/integrations/i1/assignments/a1"


def test_spec_is_normalised_and_named():
    spec = prov.read_provision_spec(SPEC_CONFIG)
    assert spec["entity_types"] == ["Intrusion-Set", "Report"]
    assert spec["event_types"] == ["create"]
    assert prov.object_name(spec) == "XTM One - Threat response - New threats"


@pytest.mark.parametrize(
    "provision, message",
    [
        ({"fleet": "F"}, "at least one of"),
        ({"entity_types": ["Report"], "event_types": ["merge"]}, "unknown"),
        ({"entity_types": ["Report"], "min_score": 101}, "min_score"),
        ({"entity_types": "", "labels": []}, "non-empty"),
        ({"cisa_kev": "yes"}, "cisa_kev"),
        ({"min_epss_score": 1.5}, "min_epss_score"),
        ({"min_epss_score": True}, "min_epss_score"),
        ({"min_cvss": 11}, "min_cvss"),
        ({"min_cvss": True}, "min_cvss"),
        ({"to_types": [""]}, "non-empty"),
        ("yes", "object"),
    ],
)
def test_unusable_spec_is_refused(provision, message):
    with pytest.raises(prov.ProvisionSpecError, match=message):
        prov.read_provision_spec({"provider": "opencti", "provision": provision})


def test_no_spec_for_other_providers_or_without_block():
    assert prov.read_provision_spec({"provider": "microsoft", "provision": {"labels": ["x"]}}) is None
    assert prov.read_provision_spec({"provider": "opencti"}) is None


def test_filters_carry_types_labels_and_score():
    spec = prov.read_provision_spec(SPEC_CONFIG)
    filters = json.loads(prov.trigger_filters(spec, ["label-1"]))
    keys = {f["key"][0]: f for f in filters["filters"]}
    assert keys["entity_type"]["values"] == ["Intrusion-Set", "Report"]
    assert keys["objectLabel"]["values"] == ["label-1"]
    assert keys["x_opencti_score"] == {"key": ["x_opencti_score"], "values": ["50"], "operator": "gte", "mode": "or"}


def test_a_single_vulnerability_signal_is_a_top_level_filter():
    spec = prov.read_provision_spec({"provider": "opencti", "provision": {"cisa_kev": True}})
    filters = json.loads(prov.trigger_filters(spec, []))
    assert filters["filters"] == [{"key": ["x_opencti_cisa_kev"], "values": ["true"], "operator": "eq", "mode": "or"}]
    assert filters["filterGroups"] == []


def test_epss_alone_is_a_gte_filter_and_a_usable_spec():
    spec = prov.read_provision_spec({"provider": "opencti", "provision": {"min_epss_score": 0.2}})
    (only,) = json.loads(prov.trigger_filters(spec, []))["filters"]
    assert only == {"key": ["x_opencti_epss_score"], "values": ["0.2"], "operator": "gte", "mode": "or"}


def test_a_spec_without_vulnerability_signals_keeps_its_filters():
    spec = prov.read_provision_spec(SPEC_CONFIG)
    assert spec["cisa_kev"] is None and spec["min_epss_score"] is None and spec["from_types"] == []
    assert spec["to_types"] == [] and spec["relationship_types"] == [] and spec["min_cvss"] is None
    filters = json.loads(prov.trigger_filters(spec, ["label-1"]))
    assert {f["key"][0] for f in filters["filters"]} == {"entity_type", "objectLabel", "x_opencti_score"}


def test_to_types_relationship_types_and_min_cvss_are_filters():
    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {"to_types": ["Vulnerability"], "relationship_types": ["has"], "min_cvss": 7},
        }
    )
    assert spec["to_types"] == ["Vulnerability"] and spec["relationship_types"] == ["has"]
    assert spec["min_cvss"] == 7.0
    keys = {f["key"][0]: f for f in json.loads(prov.trigger_filters(spec, []))["filters"]}
    assert keys["toTypes"] == {"key": ["toTypes"], "values": ["Vulnerability"], "operator": "eq", "mode": "or"}
    assert keys["entity_type"]["values"] == ["has"]
    assert keys["x_opencti_cvss_base_score"] == {
        "key": ["x_opencti_cvss_base_score"],
        "values": ["7.0"],
        "operator": "gte",
        "mode": "or",
    }


def test_each_new_key_alone_is_a_usable_spec():
    for provision in ({"to_types": ["Vulnerability"]}, {"relationship_types": ["has"]}, {"min_cvss": 9}):
        assert prov.read_provision_spec({"provider": "opencti", "provision": provision})


def test_notifier_posts_to_the_receiver_with_the_token():
    config = json.loads(prov.notifier_configuration(URL, "s3cret"))
    assert config["url"] == URL and config["verb"] == "POST"
    assert {"attribute": "X-OpenCTI-Token", "value": "s3cret"} in config["headers"]
    assert "notification_id" in config["template"] and "JSON.stringify(data[0].instance)" in config["template"]


@pytest.mark.asyncio
async def test_reconcile_creates_then_is_idempotent():
    octi = FakeOpenCTI(labels={"ai": "label-ai"})
    spec = prov.read_provision_spec(SPEC_CONFIG)
    state = await prov.reconcile(octi, spec, None, url=URL, secret="s")
    assert state["status"] == "provisioned" and state["error"] is None
    assert len(octi.notifiers) == 1 and len(octi.triggers) == 1
    trigger = octi.triggers[state["trigger_id"]]
    assert trigger["notifiers"] == [state["notifier_id"]]
    assert trigger["name"] == "XTM One - Threat response - New threats"

    octi.calls.clear()
    again = await prov.reconcile(octi, spec, state, url=URL, secret="s")
    assert again["trigger_id"] == state["trigger_id"] and again["notifier_id"] == state["notifier_id"]
    assert not [c for c in octi.calls if c.endswith(("Add", "Patch", "Delete"))]


@pytest.mark.asyncio
async def test_reconcile_repairs_drift_and_recreates_deleted_objects():
    octi = FakeOpenCTI(labels={"ai": "label-ai"})
    spec = prov.read_provision_spec(SPEC_CONFIG)
    state = await prov.reconcile(octi, spec, None, url=URL, secret="s")
    octi.triggers[state["trigger_id"]]["filters"] = '{"mode": "and", "filters": [], "filterGroups": []}'
    octi.notifiers[state["notifier_id"]]["notifier_configuration"] = "{}"
    repaired = await prov.reconcile(octi, spec, state, url=URL, secret="s")
    assert json.loads(octi.triggers[repaired["trigger_id"]]["filters"]) == json.loads(prov.trigger_filters(spec, ["label-ai"]))
    assert json.loads(octi.notifiers[repaired["notifier_id"]]["notifier_configuration"])["url"] == URL

    octi.triggers.clear()
    recreated = await prov.reconcile(octi, spec, repaired, url=URL, secret="s")
    assert recreated["status"] == "provisioned" and recreated["trigger_id"] in octi.triggers


@pytest.mark.asyncio
async def test_lost_state_adopts_existing_objects_by_name():
    octi = FakeOpenCTI(labels={"ai": "label-ai"})
    spec = prov.read_provision_spec(SPEC_CONFIG)
    first = await prov.reconcile(octi, spec, None, url=URL, secret="s")
    second = await prov.reconcile(octi, spec, None, url=URL, secret="s")
    assert second["trigger_id"] == first["trigger_id"]
    assert len(octi.triggers) == 1 and len(octi.notifiers) == 1


@pytest.mark.asyncio
async def test_event_type_change_replaces_the_trigger():
    octi = FakeOpenCTI(labels={"ai": "label-ai"})
    spec = prov.read_provision_spec(SPEC_CONFIG)
    state = await prov.reconcile(octi, spec, None, url=URL, secret="s")
    spec2 = dict(spec, event_types=["create", "update"])
    new = await prov.reconcile(octi, spec2, state, url=URL, secret="s")
    assert new["trigger_id"] != state["trigger_id"]
    assert list(octi.triggers) == [new["trigger_id"]]


@pytest.mark.asyncio
async def test_missing_label_is_an_error_and_nothing_is_created():
    octi = FakeOpenCTI(labels={})
    spec = prov.read_provision_spec(SPEC_CONFIG)
    state = await prov.reconcile(octi, spec, None, url=URL, secret="s")
    assert state["status"] == "error" and "labels not found" in state["error"]
    assert not octi.triggers and not octi.notifiers


@pytest.mark.asyncio
async def test_remove_deletes_both_and_tolerates_already_gone():
    octi = FakeOpenCTI(labels={"ai": "label-ai"})
    spec = prov.read_provision_spec(SPEC_CONFIG)
    state = await prov.reconcile(octi, spec, None, url=URL, secret="s")
    assert await prov.remove(octi, state) is None
    assert not octi.triggers and not octi.notifiers
    assert await prov.remove(octi, state) is None


def test_due_for_check():
    now = datetime(2026, 11, 5, tzinfo=UTC)
    fresh = {"status": "provisioned", "checked_at": (now - timedelta(seconds=60)).isoformat()}
    stale = {"status": "provisioned", "checked_at": (now - timedelta(seconds=prov.RECHECK_SECONDS)).isoformat()}
    assert not prov.due_for_check(fresh, now)
    assert prov.due_for_check(stale, now)
    assert prov.due_for_check({"status": "error", "checked_at": now.isoformat()}, now)
    assert prov.due_for_check(None, now)


def test_binding_prefers_assignment_then_agent():
    class Row:
        def __init__(self, bindings):
            self.integration_bindings = bindings

    assert prov.bound_opencti_integration_id(Row({"opencti": ["i-a"]}), Row({"opencti": ["i-b"]})) == "i-a"
    assert prov.bound_opencti_integration_id(Row(None), Row({"opencti": ["i-b"]})) == "i-b"
    assert prov.bound_opencti_integration_id(Row({}), Row({"opencti": ["i-b"]})) is None
    assert prov.bound_opencti_integration_id(Row(None), Row(None)) is None


def test_delivery_carries_the_entity_not_only_its_id():
    payload = {
        "notification_id": "t:report--1:create:2026",
        "type": "create",
        "message": "admin creates report X",
        "data": {
            "id": "report--1",
            "type": "report",
            "name": "X",
            "labels": ["ai", "apt"],
            "x_opencti_score": 80,
            "modified": "2026-11-05T00:00:00Z",
            "extensions": {"extension-definition--ea279b3e": {"id": "internal-1", "type": "Report"}},
        },
    }
    _normalize_opencti_payload(payload)
    event = payload["_opencti_event"]
    assert event["event_type"] == "create"
    assert event["entity_name"] == "X"
    assert event["stix_id"] == "report--1"
    assert event["score"] == 80
    assert event["labels"] == ["ai", "apt"]


# ── Filters derived from the organization profile (#8987 slice 3) ──

CONTEXT_CONFIG = {
    "provider": "opencti",
    "provision": {"fleet": "Threat response", "purpose": "PIR threats", "entity_types": ["Report"],
                  "organization_context": ["pir_ids"], "pir_min_score": 20},
}


def test_a_context_only_spec_is_accepted_and_starts_without_pirs():
    spec = prov.read_provision_spec(
        {"provider": "opencti", "provision": {"organization_context": ["pir_ids"]}}
    )
    assert spec["organization_context"] == ["pir_ids"]
    assert spec["pir_min_score"] == 1 and spec["pir_ids"] == []


@pytest.mark.parametrize(
    "provision, message",
    [
        ({"organization_context": ["size"]}, "cannot be derived yet"),
        ({"organization_context": ["pir_ids"], "pir_min_score": 101}, "pir_min_score"),
        ({"organization_context": ["pir_ids"], "pir_min_score": True}, "pir_min_score"),
    ],
)
def test_an_unusable_context_spec_is_refused(provision, message):
    with pytest.raises(prov.ProvisionSpecError, match=message):
        prov.read_provision_spec({"provider": "opencti", "provision": provision})


def test_the_profile_pirs_become_opencti_s_pir_score_filter():
    from app.services import organization_context as oc

    spec = prov.read_provision_spec(CONTEXT_CONFIG)
    profile = oc.parse_profile({"pir_ids": ["pir-b", "pir-a"]})
    derived, missing = prov.derive_from_organization_context(spec, profile)
    assert missing == []
    filters = json.loads(prov.trigger_filters(derived, []))
    pir = next(f for f in filters["filters"] if f["key"] == ["pir_score"])
    assert pir["values"] == [
        {"key": "score", "values": ["20"], "operator": "gte"},
        {"key": "pir_ids", "values": ["pir-a", "pir-b"]},
    ]
    # The declared entity types still narrow it.
    assert any(f["key"] == ["entity_type"] for f in filters["filters"])


@pytest.mark.parametrize("profile_raw", [None, {}, {"pir_ids": []}])
def test_a_missing_context_field_is_named_never_guessed(profile_raw):
    from app.services import organization_context as oc

    spec = prov.read_provision_spec(CONTEXT_CONFIG)
    profile = None if profile_raw is None else oc.parse_profile(profile_raw)
    derived, missing = prov.derive_from_organization_context(spec, profile)
    assert missing == ["pir_ids"]
    assert derived["pir_ids"] == []
    assert "pir_score" not in prov.trigger_filters(derived, [])


def test_a_spec_without_context_derives_nothing():
    spec = prov.read_provision_spec(SPEC_CONFIG)
    derived, missing = prov.derive_from_organization_context(spec, None)
    assert derived == spec and missing == []


SCOPE_CONFIG = {
    "provider": "opencti",
    "provision": {"fleet": "Threat response", "purpose": "Our threats", "entity_types": ["Report"],
                  "organization_context": ["regions", "sector"]},
}
COUNTRIES = [
    {"id": "c-fr", "name": "France", "x_opencti_aliases": ["FRA", "Republique francaise"]},
    {"id": "c-fi", "name": "Finland", "x_opencti_aliases": ["FIN"]},
    {"id": "c-de", "name": "Germany", "x_opencti_aliases": ["DE"]},
]
SECTORS = [
    {"id": "s-fin", "name": "Finance", "x_opencti_aliases": ["Financial services"]},
    {"id": "s-fin2", "name": "Finance and insurance", "x_opencti_aliases": []},
]


def test_regions_and_sector_are_derived_from_the_profile():
    from app.services import organization_context as oc

    spec = prov.read_provision_spec(SCOPE_CONFIG)
    derived, missing = prov.derive_from_organization_context(
        spec, oc.parse_profile({"regions": ["fr", "DE"], "sector": "Financial services"})
    )
    assert missing == []
    assert derived["regions"] == ["DE", "FR"] and derived["sectors"] == ["Financial services"]


@pytest.mark.parametrize("profile_raw, expected", [(None, ["regions", "sector"]), ({"sector": "Finance"}, ["regions"])])
def test_missing_regions_or_sector_are_named(profile_raw, expected):
    from app.services import organization_context as oc

    spec = prov.read_provision_spec(SCOPE_CONFIG)
    profile = None if profile_raw is None else oc.parse_profile(profile_raw)
    _derived, missing = prov.derive_from_organization_context(spec, profile)
    assert missing == expected


def test_scope_ids_become_an_objects_or_to_id_group():
    spec = prov.read_provision_spec(SPEC_CONFIG)
    filters = json.loads(prov.trigger_filters(spec, ["label-1"], ["s-1", "c-2", "c-2"]))
    assert filters["mode"] == "and"
    assert filters["filterGroups"] == [
        {
            "mode": "or",
            "filters": [
                {"key": ["objects"], "values": ["c-2", "s-1"], "operator": "eq", "mode": "or"},
                {"key": ["toId"], "values": ["c-2", "s-1"], "operator": "eq", "mode": "or"},
            ],
            "filterGroups": [],
        }
    ]
    # Without scope ids the filter is unchanged.
    assert json.loads(prov.trigger_filters(spec, ["label-1"]))["filterGroups"] == []


@pytest.mark.asyncio
async def test_reconcile_resolves_countries_by_alias_and_sectors_by_name_or_alias():
    octi = FakeOpenCTI(countries=COUNTRIES, sectors=SECTORS)
    spec = dict(prov.read_provision_spec(SCOPE_CONFIG), regions=["DE", "FR"], sectors=["financial services"])
    state = await prov.reconcile(octi, spec, None, url=URL, secret="s")
    assert state["status"] == "provisioned", state
    group = json.loads(octi.triggers[state["trigger_id"]]["filters"])["filterGroups"][0]
    # FR by its alpha-3 alias, DE by its alpha-2 alias, the sector by its alias; Finland never matches FR.
    assert group["filters"][0]["values"] == ["c-de", "c-fr", "s-fin"]


@pytest.mark.asyncio
async def test_an_unresolved_region_or_sector_is_an_error_and_nothing_is_created():
    octi = FakeOpenCTI(countries=COUNTRIES, sectors=SECTORS)
    spec = dict(prov.read_provision_spec(SCOPE_CONFIG), regions=["JP"], sectors=["Energy"])
    state = await prov.reconcile(octi, spec, None, url=URL, secret="s")
    assert state["status"] == "error"
    assert "regions ['JP']" in state["error"] and "sector ['Energy']" in state["error"]
    assert not octi.triggers and not octi.notifiers


PIR_CONFIG: dict[str, Any] = {
    "provider": "opencti",
    "event": "create",
    "provision": {
        "fleet": "Threat response",
        "purpose": "PIR flags",
        "mode": "pir_playbook",
        "organization_context": ["pir_ids"],
        "pir_min_score": 70,
    },
}


def _pir_spec() -> dict[str, Any]:
    spec = prov.read_provision_spec(PIR_CONFIG)
    assert spec is not None
    return dict(spec, pir_ids=["pir-2", "pir-1"])


def test_pir_playbook_mode_needs_the_profile_pirs_and_a_known_mode() -> None:
    assert _pir_spec()["mode"] == "pir_playbook"
    trigger_spec = prov.read_provision_spec(SPEC_CONFIG)
    assert trigger_spec is not None and trigger_spec["mode"] == "trigger"
    provision: dict[str, Any] = dict(PIR_CONFIG["provision"])
    no_pirs = {**PIR_CONFIG, "provision": {**provision, "organization_context": []}}
    with pytest.raises(prov.ProvisionSpecError, match="must name pir_ids"):
        prov.read_provision_spec(no_pirs)
    unknown = {**PIR_CONFIG, "provision": {**provision, "mode": "stream"}}
    with pytest.raises(prov.ProvisionSpecError, match=r"provision\.mode"):
        prov.read_provision_spec(unknown)


def test_pir_playbook_nodes_listen_to_the_profile_pirs_and_notify_the_integration_user() -> None:
    entry, notify = prov.pir_playbook_nodes(
        _pir_spec(), "notifier-1", "user-xtm", "XTM One integration", {"pir-1": "Ransomware on finance"}
    )
    # Items as OpenCTI's PIR listener reads them (pir.value), never bare ids, with the
    # label and type its form draws the chip from (a PIR OpenCTI did not name shows its id).
    assert entry["inPirFilters"] == [
        {"value": "pir-1", "label": "Ransomware on finance", "type": "Pir"},
        {"value": "pir-2", "label": "pir-2", "type": "Pir"},
    ]
    assert entry["create"] is True and entry["update"] is True
    assert entry["delete"] is False and entry["create_rel"] is False
    score = json.loads(entry["filters"])["filters"][0]
    assert score == {"key": ["pir_score"], "values": ["70"], "operator": "gte", "mode": "or"}
    # The Targets chip of the notifier node: the user's name and the user icon (CTEM-05 follow-up).
    assert notify == {
        "notifiers": ["notifier-1"],
        "authorized_members": [{"value": "user-xtm", "label": "XTM One integration", "type": "User"}],
    }
    # No name answered: the id, never an empty chip.
    _, bare = prov.pir_playbook_nodes(_pir_spec(), "notifier-1", "user-xtm")
    assert bare["authorized_members"] == [{"value": "user-xtm", "label": "user-xtm", "type": "User"}]
    # No entity_types: the PIR score is the only filter.
    assert len(json.loads(entry["filters"])["filters"]) == 1


def test_pir_playbook_entry_keeps_the_spec_entity_types() -> None:
    config = {**PIR_CONFIG, "provision": {**PIR_CONFIG["provision"], "entity_types": ["Intrusion-Set", "Malware"]}}
    spec = prov.read_provision_spec(config)
    assert spec is not None
    entry, _ = prov.pir_playbook_nodes(dict(spec, pir_ids=["pir-1"]), "notifier-1", "user-xtm")
    filters = json.loads(entry["filters"])["filters"]
    assert {"key": ["entity_type"], "values": ["Intrusion-Set", "Malware"], "operator": "eq", "mode": "or"} in filters
    assert filters[0]["key"] == ["pir_score"]


def test_chip_items_are_compared_by_value_only() -> None:
    _, notify = prov.pir_playbook_nodes(_pir_spec(), "notifier-1", "user-xtm", "XTM One integration")
    resaved = {**notify, "authorized_members": [{"value": "user-xtm", "label": "Renamed", "type": "User"}]}
    assert prov.same_node_configuration(prov.NOTIFIER_COMPONENT, notify, resaved)
    assert prov.same_node_configuration(
        prov.NOTIFIER_COMPONENT, notify, {**notify, "authorized_members": [{"value": "user-xtm"}]}
    )
    other = {**notify, "authorized_members": [{"value": "someone-else", "label": "XTM One integration"}]}
    assert not prov.same_node_configuration(prov.NOTIFIER_COMPONENT, notify, other)
    assert not prov.same_node_configuration(prov.NOTIFIER_COMPONENT, notify, {**notify, "notifiers": ["n-2"]})
    assert not prov.same_node_configuration(
        prov.NOTIFIER_COMPONENT, notify, {**notify, "authorized_members": ["user-xtm"]}
    )


def test_pir_webhook_template_keys_each_change_on_the_pir_information() -> None:
    template = prov.PIR_WEBHOOK_TEMPLATE
    assert "data[0].notification_id" in template and "notification.id" not in template
    assert "pir_information" in template.split('"type"')[0]
    assert '"xtm_source": "pir_playbook"' in template


@pytest.mark.asyncio
async def test_reconcile_pir_playbook_creates_a_running_playbook_then_is_idempotent() -> None:
    octi = FakeOpenCTI()
    state = await prov.reconcile(octi, _pir_spec(), None, url=URL, secret="s")
    assert state["status"] == "provisioned", (state.get("error"), octi.calls)
    assert state["mode"] == "pir_playbook"
    assert not octi.triggers and len(octi.notifiers) == 1
    playbook = octi.playbooks[state["playbook_id"]]
    assert playbook["running"] and playbook["name"] == "XTM One - Threat response - PIR flags"
    entry, notify = playbook["nodes"]
    assert (entry["component_id"], notify["component_id"]) == (prov.PIR_ENTRY_COMPONENT, prov.NOTIFIER_COMPONENT)
    assert notify["parent"] == entry["id"]
    assert json.loads(notify["configuration"])["notifiers"] == [state["notifier_id"]]
    template = json.loads(octi.notifiers[state["notifier_id"]]["notifier_configuration"])["template"]
    assert template == prov.PIR_WEBHOOK_TEMPLATE

    octi.calls.clear()
    again = await prov.reconcile(octi, _pir_spec(), state, url=URL, secret="s")
    assert again["playbook_id"] == state["playbook_id"]
    assert not [c for c in octi.calls if c.endswith(("Add", "Patch", "Delete", "Run"))]


@pytest.mark.asyncio
async def test_a_playbook_provisioned_without_chip_labels_is_relabelled_in_place() -> None:
    octi = FakeOpenCTI()
    state = await prov.reconcile(octi, _pir_spec(), None, url=URL, secret="s")
    playbook = octi.playbooks[state["playbook_id"]]
    entry_node, notify_node = playbook["nodes"]
    node_ids = (entry_node["id"], notify_node["id"])
    # The form CTEM-05 provisioned before the labels: value alone.
    for node, key in ((entry_node, "inPirFilters"), (notify_node, "authorized_members")):
        config = json.loads(node["configuration"])
        config[key] = [{"value": item["value"]} for item in config[key]]
        node["configuration"] = json.dumps(config)
    octi.calls.clear()
    again = await prov.reconcile(octi, _pir_spec(), state, url=URL, secret="s")
    assert again["playbook_id"] == state["playbook_id"] and list(octi.playbooks) == [state["playbook_id"]]
    assert again["fingerprint"] == state["fingerprint"]
    assert octi.calls.count("XtmNodeReplace") == 2
    assert not [c for c in octi.calls if c.endswith(("Add", "Delete", "Run"))]
    entry_node, notify_node = playbook["nodes"]
    # Patched in place: same nodes, same link.
    assert (entry_node["id"], notify_node["id"]) == node_ids and notify_node["parent"] == entry_node["id"]
    members = json.loads(notify_node["configuration"])["authorized_members"]
    assert members == [{"value": "user-xtm", "label": "XTM One integration", "type": "User"}]
    assert json.loads(entry_node["configuration"])["inPirFilters"][0]["label"] == "Ransomware on finance"

    # A label rewritten by a save in OpenCTI is not a drift: nothing is written.
    config = json.loads(notify_node["configuration"])
    config["authorized_members"][0]["label"] = "Renamed in OpenCTI"
    notify_node["configuration"] = json.dumps(config)
    octi.calls.clear()
    kept = await prov.reconcile(octi, _pir_spec(), again, url=URL, secret="s")
    assert kept["playbook_id"] == state["playbook_id"]
    assert not [c for c in octi.calls if c.endswith(("Add", "Replace", "Patch", "Delete", "Run"))]


@pytest.mark.asyncio
async def test_reconcile_pir_playbook_rebuilds_an_edited_or_stopped_playbook() -> None:
    octi = FakeOpenCTI()
    state = await prov.reconcile(octi, _pir_spec(), None, url=URL, secret="s")
    octi.playbooks[state["playbook_id"]]["running"] = False
    rebuilt = await prov.reconcile(octi, _pir_spec(), state, url=URL, secret="s")
    assert rebuilt["playbook_id"] != state["playbook_id"] and list(octi.playbooks) == [rebuilt["playbook_id"]]
    assert octi.playbooks[rebuilt["playbook_id"]]["running"]

    octi.playbooks[rebuilt["playbook_id"]]["nodes"][0]["configuration"] = "{}"
    again = await prov.reconcile(octi, _pir_spec(), rebuilt, url=URL, secret="s")
    assert list(octi.playbooks) == [again["playbook_id"]] and again["playbook_id"] != rebuilt["playbook_id"]


@pytest.mark.asyncio
async def test_switching_mode_swaps_the_trigger_and_the_playbook_and_remove_deletes_all() -> None:
    octi = FakeOpenCTI()
    trigger_spec = dict(_pir_spec(), mode="trigger")
    as_trigger = await prov.reconcile(octi, trigger_spec, None, url=URL, secret="s")
    assert len(octi.triggers) == 1 and not octi.playbooks
    as_playbook = await prov.reconcile(octi, _pir_spec(), as_trigger, url=URL, secret="s")
    assert not octi.triggers and len(octi.playbooks) == 1
    back = await prov.reconcile(octi, trigger_spec, as_playbook, url=URL, secret="s")
    assert len(octi.triggers) == 1 and not octi.playbooks
    assert await prov.remove(octi, {**back, "playbook_id": as_playbook["playbook_id"]}) is None
    assert not octi.triggers and not octi.playbooks and not octi.notifiers
