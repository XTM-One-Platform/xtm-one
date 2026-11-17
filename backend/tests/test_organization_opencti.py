"""OpenCTI identity and PIRs of the tracked organizations (#9157, slice M3).

A scripted GraphQL double stands for OpenCTI: the D2 = A rule, the
canonical criteria, the type written only when empty, creation only on
confirmation, a changed PIR replaced and a stale one deleted, the dry run
writing nothing.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import organization_opencti as oo
from app.services import organizations as orgs


class FakeOpenCti:
    def __init__(self, *, pirs: list[dict] | None = None, orgs_: list[dict] | None = None):
        self.pirs = {p["id"]: p for p in (pirs or [])}
        self.orgs = {o["id"]: o for o in (orgs_ or [])}
        self.calls: list[str] = []
        self.sectors = {"cloud": "sector-cloud"}
        self.countries = {"FRA": "country-fr"}
        self._n = 0

    async def __call__(self, query: str, variables: dict | None = None):
        variables = variables or {}
        op = query.split("(")[0].split()[-1]
        self.calls.append(op)
        if op == "XtmPirs":
            return {"pirs": {"edges": [{"node": p} for p in self.pirs.values()]}}, None
        if op == "XtmPirAdd":
            self._n += 1
            pid = f"pir-{self._n}"
            inp = variables["input"]
            self.pirs[pid] = {
                "id": pid,
                "name": inp["name"],
                "pir_criteria": [
                    {"weight": c["weight"], "filters": json.dumps(c["filters"])} for c in inp["pir_criteria"]
                ],
                "input": inp,
            }
            return {"pirAdd": {"id": pid}}, None
        if op == "XtmPirDelete":
            self.pirs.pop(variables["id"], None)
            return {"pirDelete": variables["id"]}, None
        if op == "XtmSectors":
            sid = self.sectors.get(variables["search"].lower())
            nodes = [{"id": sid, "name": variables["search"], "x_opencti_aliases": []}] if sid else []
            return {"sectors": {"edges": [{"node": n} for n in nodes]}}, None
        if op == "XtmCountries":
            cid = self.countries.get(variables["search"].upper())
            nodes = [{"id": cid, "name": "France", "x_opencti_aliases": [variables["search"].upper()]}] if cid else []
            return {"countries": {"edges": [{"node": n} for n in nodes]}}, None
        if op == "XtmOrg":
            return {"organization": self.orgs.get(variables["id"])}, None
        if op == "XtmOrgType":
            self.orgs[variables["id"]]["x_opencti_organization_type"] = variables["input"][0]["value"][0]
            return {"stixDomainObjectEdit": {"fieldPatch": {"id": variables["id"]}}}, None
        if op == "XtmOrgAdd":
            oid = str(uuid.uuid4())
            self.orgs[oid] = {"id": oid, **variables["input"]}
            return {"organizationAdd": {"id": oid}}, None
        if op == "XtmOrgSearch":
            nodes = [o for o in self.orgs.values() if variables["search"].lower() in o["name"].lower()]
            return {"organizations": {"edges": [{"node": n} for n in nodes]}}, None
        raise AssertionError(f"unexpected operation {op}")


def _org(name: str, criticality: str, identity: str | None = None, relationship: str = "provider"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        role="tracked",
        relationship=relationship,
        criticality=criticality,
        opencti_organization_id=identity,
        archived_at=None,
    )


def _target(name: str, criticality: str, identity: str = "11111111-1111-1111-1111-111111111111"):
    return oo.TrackedTarget(
        organization_id=str(uuid.uuid4()),
        name=name,
        criticality=criticality,
        identity_id=identity,
        sector_ids=["22222222-2222-2222-2222-222222222222"],
        region_ids=["33333333-3333-3333-3333-333333333333"],
    )


def test_high_and_critical_get_their_own_pir_the_others_share_one():
    planned = oo.plan_pirs(
        [_target("Acme", "critical"), _target("Beta", "high"), _target("Gamma", "medium"), _target("Delta", "low")]
    )
    names = [p.name for p in planned]
    assert names == ["XTM One - Acme - third-party", "XTM One - Beta - third-party", oo.SHARED_PIR_NAME]
    assert len(planned[-1].organization_ids) == 2


def test_criteria_follow_the_opencti_form_contract():
    from app.integrations.providers.opencti.pir import _normalize_criteria

    pir = oo.plan_pirs([_target("Acme", "critical")])[0]
    assert [c["weight"] for c in pir.criteria] == [3, 1, 1]
    normalized, error = _normalize_criteria(pir.criteria)
    assert error is None and normalized


def test_no_sector_and_no_region_leave_only_the_identity_criterion():
    target = _target("Acme", "high")
    target.sector_ids, target.region_ids = [], []
    assert [c["weight"] for c in oo.plan_pirs([target])[0].criteria] == [3]


@pytest.mark.asyncio
async def test_a_picked_identity_gets_its_type_only_when_empty():
    api = FakeOpenCti(
        orgs_=[{"id": "o1", "name": "Acme"}, {"id": "o2", "name": "Beta", "x_opencti_organization_type": "csirt"}]
    )
    assert await oo.link_identity(api, name="Acme", relationship="provider", identity_id="o1") == ("o1", "vendor")
    assert await oo.link_identity(api, name="Beta", relationship="partner", identity_id="o2") == ("o2", None)
    assert api.orgs["o2"]["x_opencti_organization_type"] == "csirt"


@pytest.mark.asyncio
async def test_an_identity_is_created_only_on_confirmation():
    api = FakeOpenCti()
    with pytest.raises(ValueError, match="confirm"):
        await oo.link_identity(api, name="Acme", relationship="customer")
    assert "XtmOrgAdd" not in api.calls
    created, type_set = await oo.link_identity(api, name="Acme", relationship="customer", create=True)
    assert type_set == "other" and api.orgs[created]["name"] == "Acme"


@pytest.mark.asyncio
async def test_read_identity_names_a_linked_organization_or_answers_none():
    api = FakeOpenCti(orgs_=[{"id": "o1", "name": "Acme", "x_opencti_aliases": ["ACME"]}])
    assert (await oo.read_identity(api, "o1"))["name"] == "Acme"
    assert await oo.read_identity(api, "nope") is None


@pytest.mark.asyncio
async def test_an_unknown_identity_is_not_found():
    with pytest.raises(LookupError):
        await oo.link_identity(FakeOpenCti(), name="Acme", relationship="provider", identity_id="nope")


@pytest.mark.asyncio
async def test_resolve_targets_reports_unlinked_and_unresolved():
    api = FakeOpenCti()
    linked = _org("Acme", "high", identity="11111111-1111-1111-1111-111111111111")
    unlinked = _org("Beta", "low")
    profiles = {
        str(linked.id): orgs.parse_tracked_profile({"sector": "cloud", "regions": ["FR", "JP"]}),
        str(unlinked.id): orgs.parse_tracked_profile({}),
    }
    targets, report = await oo.resolve_targets(api, [linked, unlinked], profiles)
    assert [t.name for t in targets] == ["Acme"]
    assert targets[0].sector_ids == ["sector-cloud"] and targets[0].region_ids == ["country-fr"]
    assert report.not_linked == ["Beta"]
    assert report.unresolved == [{"organization": "Acme", "sectors": [], "regions": ["JP"]}]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing():
    api = FakeOpenCti()
    report = oo.PirSyncReport()
    by_org = await oo.reconcile_pirs(
        api, oo.plan_pirs([_target("Acme", "high")]), report, apply=False, min_confidence=0
    )
    assert by_org == {}
    assert report.created == [{"name": "XTM One - Acme - third-party", "pir_id": None}]
    assert not {"XtmPirAdd", "XtmPirDelete"} & set(api.calls)


@pytest.mark.asyncio
async def test_apply_creates_keeps_replaces_and_deletes_only_its_own_pirs():
    api = FakeOpenCti()
    acme = _target("Acme", "high")
    report = oo.PirSyncReport()
    first = await oo.reconcile_pirs(api, oo.plan_pirs([acme]), report, apply=True, min_confidence=60)
    pir_id = first[acme.organization_id]
    assert api.pirs[pir_id]["input"]["pir_type"] == "THREAT_CUSTOM"
    assert api.pirs[pir_id]["input"]["pir_filters"]["filters"][0]["values"] == ["60"]

    # Same plan again: kept, nothing written.
    api.calls.clear()
    report = oo.PirSyncReport()
    again = await oo.reconcile_pirs(api, oo.plan_pirs([acme]), report, apply=True, min_confidence=60)
    assert again == first and report.unchanged and "XtmPirAdd" not in api.calls

    # A changed region: replaced (OpenCTI cannot patch criteria).
    acme.region_ids = ["44444444-4444-4444-4444-444444444444"]
    report = oo.PirSyncReport()
    replaced = await oo.reconcile_pirs(api, oo.plan_pirs([acme]), report, apply=True, min_confidence=60)
    assert replaced[acme.organization_id] != pir_id and pir_id not in api.pirs
    assert report.replaced[0]["previous_pir_id"] == pir_id

    # A PIR someone else made is never touched; ours that no plan needs is deleted.
    api.pirs["foreign"] = {"id": "foreign", "name": "XTM One threats", "pir_criteria": []}
    report = oo.PirSyncReport()
    await oo.reconcile_pirs(api, [], report, apply=True, min_confidence=0)
    assert set(api.pirs) == {"foreign"}
    assert [d["name"] for d in report.deleted] == ["XTM One - Acme - third-party"]
