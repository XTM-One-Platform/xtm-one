# SPDX-License-Identifier: Apache-2.0

"""OpenCRQ supporting -> primary asset links (#1591): option, roles, inheritance, manual wins."""

import uuid
from typing import Any

from app.models.organization_asset_record import OrganizationPrimaryAssetLink
from app.services import organization_context_opencrq_links as links

JEWELS = {"pa-1": "Payments", "pa-2": "Customer data"}


def _link(
    link_id: str, primary: str, supporting: str, name: str = "", roles: list[str] | None = None
) -> dict[str, Any]:
    return {
        "id": link_id,
        "primaryAssetId": primary,
        "supportingAssetId": supporting,
        "supportingAssetName": name,
        "roles": roles or [],
    }


def _stored(
    external_id: str, record_id: uuid.UUID, jewel: str, roles: list[str], removed: bool = False
) -> OrganizationPrimaryAssetLink:
    return OrganizationPrimaryAssetLink(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        primary_asset_key=jewel,
        record_id=record_id,
        source="opencrq",
        external_id=external_id,
        roles=roles,
        inherited_needs={},
        removed=removed,
    )


def test_option_is_on_by_default_and_off_only_when_said() -> None:
    assert links.link_option_on(None)
    assert links.link_option_on({})
    assert links.link_option_on({"link_supporting_assets": "true"})
    assert not links.link_option_on({"link_supporting_assets": "false"})
    assert not links.link_option_on({"link_supporting_assets": ["false"]})
    assert not links.link_option_on({"link_supporting_assets": False})


def test_roles_are_canonical_and_unknown_inherits_everything() -> None:
    assert links.canonical_roles(["processes", "stores", "stores", "bogus"]) == ["stores", "processes"]
    assert links.canonical_roles([]) == ["unknown"]
    assert links.inherited_needs(["unknown"]) == dict.fromkeys(links.SECURITY_NEEDS, "full")


def test_inherited_needs_take_the_strongest_role() -> None:
    needs = links.inherited_needs(["reads", "monitors"])
    assert needs == {"availability": "lower", "integrity": "lower", "confidentiality": "full", "traceability": "full"}


def test_links_match_the_opencrq_record_first_then_a_single_name() -> None:
    by_id, by_name = uuid.uuid4(), uuid.uuid4()
    plan = links.plan_links(
        JEWELS,
        [
            _link("l1", "pa-1", "sa-1", "db-01", ["stores"]),
            _link("l2", "pa-2", "sa-9", "Web Front", ["transmits"]),
            _link("l3", "pa-2", "sa-8", "nowhere"),
            _link("l4", "pa-2", "sa-7", "twin"),
            _link("l5", "pa-unknown", "sa-1", "db-01"),
        ],
        {"sa-1": by_id},
        {"web front": [by_name], "twin": [uuid.uuid4(), uuid.uuid4()]},
        [],
    )
    added = {w.external_id: w for w in plan.add}
    assert set(added) == {"l1", "l2"}
    assert added["l1"].record_id == by_id and added["l1"].primary_asset_key == "payments"
    assert added["l2"].record_id == by_name and added["l2"].roles == ["transmits"]
    assert added["l1"].inherited_needs["traceability"] == "lower"
    assert plan.unmatched == [("Customer data", "nowhere")]
    assert plan.ambiguous == [("Customer data", "twin")]


def test_a_link_a_person_removed_is_never_added_back() -> None:
    record = uuid.uuid4()
    plan = links.plan_links(
        JEWELS,
        [_link("l1", "pa-1", "sa-1", "db-01", ["stores"])],
        {"sa-1": record},
        {},
        [_stored("l1", record, "payments", ["stores"], removed=True)],
    )
    assert not plan.add and not plan.update and not plan.delete
    assert plan.kept_removed == [("Payments", "db-01")]


def test_changed_roles_update_and_links_opencrq_dropped_go() -> None:
    record = uuid.uuid4()
    gone = _stored("l9", uuid.uuid4(), "payments", ["stores"])
    tomb = _stored("l8", uuid.uuid4(), "payments", ["stores"], removed=True)
    plan = links.plan_links(
        JEWELS,
        [_link("l1", "pa-1", "sa-1", "db-01", ["reads"])],
        {"sa-1": record},
        {},
        [_stored("l1", record, "payments", ["stores"]), gone, tomb],
    )
    assert [w.external_id for w in plan.update] == ["l1"]
    assert plan.delete == [gone.id]  # the tombstone stays: the person's removal still holds


def test_summary_counts() -> None:
    plan = links.plan_links(JEWELS, [_link("l1", "pa-1", "sa-1", "x")], {}, {}, [])
    assert plan.summary()["added"] == 0
    assert plan.summary()["not_in_inventory"] == [{"primary_asset": "Payments", "supporting_asset": "x"}]
