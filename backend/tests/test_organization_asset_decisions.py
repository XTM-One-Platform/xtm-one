"""Administrator merge and split of the canonical asset store, without a database (#1178, S3)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import organization_asset_identity as identity
from app.services import organization_asset_store as store
from app.services import organization_context as oc
from app.services import organization_context_rows as rows


def _row(kind: str, *records: tuple[str, str]) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), kind=kind, records=[{"source": s, "external_id": e} for s, e in records])


def _saved(plan: rows.RowPlan) -> oc.OrganizationProfile:
    assert plan.profile is not None
    return plan.profile


LAPTOP_A = {"name": "laptop-01", "category": "host", "source": "intune", "external_id": "d-1", "serial": "SN-1"}
LAPTOP_B = {"name": "laptop-01", "category": "host", "source": "openaev", "external_id": "a-9", "serial": "sn-1"}
OTHER = {"name": "printer", "category": "host", "source": "openaev", "external_id": "a-10"}


def test_stored_rows_become_the_engine_decisions_and_a_malformed_row_is_ignored() -> None:
    decisions = store.decisions_of(
        [
            _row("merge", ("intune", "d-1"), ("openaev", "a-10")),
            _row("split", ("intune", "d-1"), ("openaev", "a-9")),
            _row("split", ("intune", "d-1")),  # one record: ignored
            SimpleNamespace(
                kind="merge", records=[{"source": "", "external_id": "x"}, {"source": "a", "external_id": "b"}]
            ),
        ]
    )
    assert decisions.merges == (
        frozenset({identity.RecordRef("intune", "d-1"), identity.RecordRef("openaev", "a-10")}),
    )
    assert decisions.splits == {frozenset({identity.RecordRef("intune", "d-1"), identity.RecordRef("openaev", "a-9")})}


def test_a_split_keeps_apart_what_the_serial_would_merge_and_a_merge_joins_what_no_key_links() -> None:
    assert len(store.canonical_rows([LAPTOP_A, LAPTOP_B, OTHER])) == 2
    split = store.decisions_of([_row("split", ("intune", "d-1"), ("openaev", "a-9"))])
    assert len(store.canonical_rows([LAPTOP_A, LAPTOP_B, OTHER], decisions=split)) == 3
    merge = store.decisions_of([_row("merge", ("openaev", "a-9"), ("openaev", "a-10"))])
    things = store.canonical_rows([LAPTOP_A, LAPTOP_B, OTHER], decisions=merge)
    assert len(things) == 1
    assert things[0]["explanation"] == "merged on serial and by an administrator"


@pytest.mark.parametrize(
    ("kind", "records", "message"),
    [
        ("join", [("a", "1"), ("b", "2")], "kind must be"),
        ("merge", [("a", "1"), ("a", "1")], "2 to"),
        ("split", [("a", "1"), ("b", "2"), ("c", "3")], "exactly 2"),
        ("split", [("a", ""), ("b", "2")], "source and an external_id"),
        ("merge", [("manual", "row-3"), ("intune", "d-1")], "addressed by their position"),
    ],
)
def test_a_bad_decision_is_refused(kind: str, records: list[tuple[str, str]], message: str) -> None:
    with pytest.raises(store.DecisionError, match=message):
        store.check_decision(kind, [{"source": s, "external_id": e} for s, e in records])


def test_a_hand_typed_row_is_addressed_by_its_position_so_a_decision_on_it_is_refused() -> None:
    record = identity.section_record("assets", {"name": "nas", "source": "manual"}, index=4)
    assert record.external_id == "row-4"
    with pytest.raises(store.DecisionError, match="external_id first: manual:row-4"):
        store.check_decision(
            "split", [{"source": "manual", "external_id": "row-4"}, {"source": "b", "external_id": "2"}]
        )
    assert store.check_decision(
        "split", [{"source": "manual", "external_id": "row-4a"}, {"source": "b", "external_id": "2"}]
    )


def test_a_row_upsert_adds_another_source_record_of_a_named_asset_beside_it() -> None:
    profile = oc.parse_profile({"assets_exposure": [{"name": "crm", "source": "manual", "owner": "Sales"}]})
    plan = rows.plan_upsert(profile, "assets", "intune", [{"name": "CRM", "external_id": "d-7", "category": "host"}])
    assert plan.added == ["CRM"] and not plan.refused
    assert [(a.source, a.name, a.owner) for a in _saved(plan).assets_exposure] == [
        ("manual", "crm", "Sales"),
        ("intune", "CRM", ""),
    ]


def test_a_row_upsert_still_refuses_an_asset_group_named_like_another_source_one() -> None:
    profile = oc.parse_profile({"asset_groups": [{"name": "web", "source": "manual"}]})
    plan = rows.plan_upsert(profile, "asset_groups", "intune", [{"name": "WEB", "external_id": "g-1"}])
    assert plan.refused and "exists and is kept" in plan.refused[0][1]
