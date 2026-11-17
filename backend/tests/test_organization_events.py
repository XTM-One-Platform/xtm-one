"""#9157 slice M4: ``provision.organizations`` and the organization of an OpenCTI event."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import opencti_trigger_provisioning as prov
from app.services.organization_events import (
    PAYLOAD_KEY,
    annotate_payload,
    concerned_organizations,
    organization_ids_of,
)

PROVIDER = uuid.UUID("11111111-1111-4111-8111-111111111111")
CUSTOMER = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _org(oid, name, relationship, *, monitoring="on", identity="octi-org", role="tracked", archived=None):
    return SimpleNamespace(
        id=oid, name=name, role=role, relationship=relationship, monitoring=monitoring,
        opencti_organization_id=identity, archived_at=archived,
    )


def _spec(organizations):
    return prov.read_provision_spec(
        {"provider": "opencti", "provision": {"fleet": "F5", "purpose": "Suppliers", "organizations": organizations}}
    )


def test_the_default_scope_is_primary_and_changes_nothing():
    spec = prov.read_provision_spec(
        {"provider": "opencti", "provision": {"entity_types": ["Report"]}}
    )
    assert spec["organizations"] == "primary"
    assert spec["organization_refs"] == []
    assert json.loads(prov.trigger_filters(spec, [])) == {
        "mode": "and",
        "filters": [{"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"}],
        "filterGroups": [],
    }


def test_a_tracked_scope_alone_is_a_usable_spec():
    assert _spec("tracked")["organizations"] == "tracked"
    assert _spec(["provider", "partner"])["organizations"] == ["partner", "provider"]


@pytest.mark.parametrize(
    ("organizations", "message"),
    [(["vendor"], "provision.organizations"), ([], "provision.organizations"), (3, "list of non-empty")],
)
def test_an_unknown_scope_is_refused(organizations, message):
    with pytest.raises(prov.ProvisionSpecError, match=message):
        _spec(organizations)


def test_the_primary_profile_fields_are_not_combined_with_a_tracked_scope():
    with pytest.raises(prov.ProvisionSpecError, match="not combined"):
        prov.read_provision_spec(
            {"provider": "opencti", "provision": {"organizations": "tracked", "organization_context": ["sector"]}}
        )


def test_tracked_refs_keep_monitored_linked_organizations_of_the_scope():
    rows = [
        _org(PROVIDER, "Acme Hosting", "provider", identity="octi-acme"),
        _org(CUSTOMER, "Beta Bank", "customer", identity="octi-beta"),
        _org(uuid.uuid4(), "Off Ltd", "provider", monitoring="off"),
        _org(uuid.uuid4(), "Unlinked", "provider", identity=None),
        _org(uuid.uuid4(), "Gone", "provider", archived="2026-11-01"),
        _org(uuid.uuid4(), "Primary", None, role="primary"),
    ]
    profiles = {PROVIDER: SimpleNamespace(sector="Cloud", pir_ids=["pir-1"])}
    spec = prov.tracked_refs(_spec(["provider"]), rows, profiles)
    assert spec["organization_refs"] == [
        {"id": str(PROVIDER), "name": "Acme Hosting", "identity_id": "octi-acme", "sectors": ["Cloud"]}
    ]
    assert spec["organization_pir_ids"] == ["pir-1"]
    every = prov.tracked_refs(_spec("tracked"), rows, profiles)
    assert [r["name"] for r in every["organization_refs"]] == ["Acme Hosting", "Beta Bank"]


def test_the_tracked_group_unions_the_ids_and_the_pirs():
    spec = dict(_spec("tracked"), organization_pir_ids=["pir-1"])
    group = json.loads(prov.trigger_filters(spec, [], None, ["octi-b", "octi-a", "octi-a"]))["filterGroups"][0]
    assert group["mode"] == "or"
    keys = [f["key"][0] for f in group["filters"]]
    assert keys == ["objects", "toId", "pir_score"]
    assert group["filters"][0]["values"] == ["octi-a", "octi-b"]


class FakeOpenCTI:
    def __init__(self) -> None:
        self.triggers: dict[str, dict] = {}
        self.notifiers: dict[str, dict] = {}
        self.n = 0

    async def __call__(self, query: str, variables: dict | None = None):
        v = variables or {}
        op = query.split("(")[0].split()[-1]
        if op == "XtmSectors":
            return {"sectors": {"edges": [{"node": {"id": "sector-cloud", "name": "Cloud"}}]}}, None
        if op == "XtmStandardId":
            return {"stixCoreObject": {"id": v["id"], "standard_id": f"identity--{v['id']}"}}, None
        if op == "XtmFind":
            return {("notifiers" if "notifiers(" in query else "triggersKnowledge"): {"edges": []}}, None
        if op == "XtmNotifierAdd":
            self.n += 1
            self.notifiers[f"n{self.n}"] = v["input"]
            return {"notifierAdd": {"id": f"n{self.n}"}}, None
        if op == "XtmTriggerAdd":
            self.n += 1
            self.triggers[f"t{self.n}"] = v["input"]
            return {"triggerKnowledgeLiveAdd": {"id": f"t{self.n}"}}, None
        raise AssertionError(f"unexpected query {op}")


async def test_reconcile_keeps_each_organization_s_ids_for_the_receiver():
    rows = [_org(PROVIDER, "Acme Hosting", "provider", identity="octi-acme")]
    spec = prov.tracked_refs(_spec("tracked"), rows, {PROVIDER: SimpleNamespace(sector="Cloud", pir_ids=[])})
    fake = FakeOpenCTI()
    state = await prov.reconcile(fake, spec, None, url="https://x/hook", secret="s")
    assert state["status"] == "provisioned", state
    assert state["organizations"] == [
        {
            "id": str(PROVIDER),
            "name": "Acme Hosting",
            "identity_ids": ["identity--octi-acme", "octi-acme"],
            "sector_ids": ["identity--sector-cloud", "sector-cloud"],
        }
    ]
    filters = json.loads(next(iter(fake.triggers.values()))["filters"])
    assert filters["filterGroups"][0]["filters"][0]["values"] == ["octi-acme", "sector-cloud"]


STATE = [
    {"id": str(PROVIDER), "name": "Acme", "identity_ids": ["identity--acme"], "sector_ids": ["identity--cloud"]},
    {"id": str(CUSTOMER), "name": "Beta", "identity_ids": ["identity--beta"], "sector_ids": ["identity--cloud"]},
]


def test_a_report_naming_the_provider_concerns_the_provider_only():
    report = {"type": "report", "object_refs": ["identity--acme", "identity--cloud", "malware--x"]}
    assert concerned_organizations(report, STATE) == [str(PROVIDER)]


def test_a_sector_match_concerns_every_organization_of_the_sector():
    rel = {"type": "relationship", "relationship_type": "targets", "target_ref": "identity--cloud"}
    assert concerned_organizations(rel, STATE) == sorted([str(PROVIDER), str(CUSTOMER)])


def test_no_match_no_organization():
    assert concerned_organizations({"object_refs": ["identity--other"]}, STATE) == []
    assert concerned_organizations({"object_refs": ["identity--acme"]}, None) == []


def test_the_receiver_records_the_organization_on_the_payload():
    payload = {"data": {"where_sighted_refs": ["identity--beta"]}}
    config = {"provider": "opencti", prov.STATE_KEY: {"organizations": STATE}}
    assert annotate_payload(payload, config) == [str(CUSTOMER)]
    assert payload[PAYLOAD_KEY] == [str(CUSTOMER)]
    assert organization_ids_of(payload) == [CUSTOMER]
    untouched = {"data": {"object_refs": ["identity--acme"]}}
    assert annotate_payload(untouched, {"provider": "opencti"}) == []
    assert PAYLOAD_KEY not in untouched


def test_organization_ids_of_drops_invalid_values():
    assert organization_ids_of({PAYLOAD_KEY: ["nope", str(PROVIDER)]}) == [PROVIDER]
    assert organization_ids_of(None) == []
