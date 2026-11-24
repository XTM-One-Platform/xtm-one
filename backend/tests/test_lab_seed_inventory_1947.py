"""The CTEM inventory every XTM One lab writes on the real row APIs (#1947, dev:harness-gaps lab-no-inventory-asset-seed)."""

from __future__ import annotations

import pytest

from tests.test_lab_seed_declarations import LAB, _lab_namespace, _lab_source

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")

BASE = "http://localhost:3100"
NAMES = ("LAB_INVENTORY_GROUPS", "LAB_INVENTORY_ASSETS", "seed_inventory")


def _rows() -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    ns = _lab_namespace("LAB_INVENTORY_GROUPS", "LAB_INVENTORY_ASSETS")
    return ns["LAB_INVENTORY_GROUPS"], ns["LAB_INVENTORY_ASSETS"]


def test_every_row_passes_the_backend_validators():
    """The rows the seed posts are the ones the row APIs accept: a refused row would skip the whole seed."""
    from app.services import organization_asset_records as records

    groups, assets = _rows()
    for group in groups:
        records.validate_group({**group, "source": "lab"})
    for asset in assets:
        value = records.validate_asset(asset)
        assert value["source"] == "lab" and value["external_id"].startswith("lab-inventory-")
    assert len({a["external_id"] for a in assets}) == len(assets)


def test_the_rows_cover_the_states_the_inventory_screens_render():
    groups, assets = _rows()
    names = {g["name"] for g in groups}
    assert all(a.get("group") in names for a in assets if a.get("group"))
    members = {name: sum(1 for a in assets if a.get("group") == name) for name in names}
    assert sorted(members.values()) == [0, 2, 2]  # two groups with members, one empty
    assert any(not a.get("group") for a in assets)  # an asset in no group
    assert any(len(a["name"]) > 60 for a in assets) and any(len(g["name"]) > 60 for g in groups)
    assert len({a["criticality"] for a in assets}) >= 4
    assert len({a["category"] for a in assets}) >= 3


def _api(calls: list, stored: list[dict] | None = None, counts: dict | None = None):
    groups = list(stored or [])

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        if path == "/organization-context/asset-groups" and method == "GET":
            return {"items": [{**g, "member_count": (counts or {}).get(g["name"], 0)} for g in groups]}
        if path == "/organization-context/asset-groups" and method == "POST":
            groups.append({"name": body["name"]})
            return {"id": f"g-{len(groups)}", **body}
        if path == "/organization-context/assets/records/batch":
            return {"created": len(body["rows"]), "updated": 0, "unchanged": 0}
        return {}

    return api


def _member_counts() -> dict:
    groups, assets = _rows()
    return {g["name"]: sum(1 for a in assets if a.get("group") == g["name"]) for g in groups}


def test_writes_groups_then_assets_and_reads_the_members_back():
    calls: list = []
    ns = _lab_namespace(*NAMES, api=_api(calls, counts=_member_counts()))
    answer = ns["seed_inventory"]("t", BASE)
    groups, assets = _rows()
    assert answer == {"id": f"{len(assets)} assets, {len(groups)} groups", "url": f"{BASE}/ctem/inventory"}
    writes = [(m, p) for m, p, _b in calls if m == "POST"]
    assert writes == [("POST", "/organization-context/asset-groups")] * len(groups) + [
        ("POST", "/organization-context/assets/records/batch")
    ]
    assert any(line.startswith("seeded inventory: ") for line in ns["logged"])


def test_a_restart_creates_no_group_twice():
    calls: list = []
    groups, _assets = _rows()
    stored = [{"name": g["name"]} for g in groups]
    ns = _lab_namespace(*NAMES, api=_api(calls, stored=stored, counts=_member_counts()))
    ns["seed_inventory"]("t", BASE)
    assert not [p for m, p, _b in calls if m == "POST" and p == "/organization-context/asset-groups"]


def test_raises_when_a_group_reads_back_without_its_members():
    calls: list = []
    ns = _lab_namespace(*NAMES, api=_api(calls, counts={}))
    with pytest.raises(RuntimeError, match="reads back 0 members"):
        ns["seed_inventory"]("t", BASE)


def test_the_seed_runs_in_xtm_one_labs_after_the_profile_stub_and_skips_without_a_profile():
    seed = _lab_source("seed_entities")
    assert "seeded['inventory'] = seed_inventory(token, base)" in seed
    assert "skipped['inventory']" in seed
    assert seed.index("seed_org_ai_draft_stub(token, base)") < seed.index("seed_inventory(token, base)")
    assert "no organization profile is saved in this lab" in seed
