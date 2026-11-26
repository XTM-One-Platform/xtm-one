"""One real thing across sources (#1178, S1): identity matching, field precedence, software, sections.

Pure: every rule holds on records in memory, without a database.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from app.services import organization_asset_identity as ident
from app.services.organization_asset_identity import Decisions, RecordRef, SourceRecord


def _rec(source: str, ext: str, **ids: object) -> SourceRecord:
    fields = ids.pop("fields", {})  # type: ignore[assignment]
    software = ids.pop("software", [])  # type: ignore[assignment]
    seen = ids.pop("seen", None)
    return SourceRecord(
        source=source,
        external_id=ext,
        identifiers={k: v if isinstance(v, list) else [v] for k, v in ids.items()},  # type: ignore[misc]
        fields=dict(fields),  # type: ignore[arg-type]
        software=list(software),  # type: ignore[arg-type]
        last_seen=seen,  # type: ignore[arg-type]
    )


# ── Normalisation ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        ("serial", " c02-xy 12 ", "C02XY12"),
        ("serial", "To Be Filled By O.E.M.", ""),
        ("serial", "0000000", ""),
        ("mac", "AA-BB-CC-DD-EE-FF", "aa:bb:cc:dd:ee:ff"),
        ("mac", "00:00:00:00:00:00", ""),
        ("mac", "not a mac", ""),
        # Virtual interfaces (Docker bridge, VMware, VirtualBox, Hyper-V, QEMU/KVM) name no machine.
        ("mac", "02:42:ac:11:00:02", ""),
        ("mac", "00-50-56-AB-CD-EF", ""),
        ("mac", "08:00:27:12:34:56", ""),
        ("mac", "00:15:5D:01:02:03", ""),
        ("mac", "52:54:00:12:34:56", ""),
        ("fqdn", "Web01.Corp.Example.", "web01.corp.example"),
        ("fqdn", "web01", ""),
        ("hostname", "CORP\\LAPTOP-ALICE", "laptop-alice"),
        ("hostname", "Laptop-Alice.corp.example", "laptop-alice"),
        ("hostname", "localhost", ""),
        ("hostname", "Amani\u2019s MacBook Pro", "amanis-macbook-pro"),
        ("hostname", "Adam's MacBook Pro", "adams-macbook-pro"),
        ("hostname", "Amanis-MacBook-Pro.local", "amanis-macbook-pro"),
        ("hostname", "build_agent 07", "build-agent-07"),
        ("ip", "10.0.0.5", "10.0.0.5"),
        ("ip", "127.0.0.1", ""),
        ("cloud_resource_id", " ARN:aws:ec2:i-1 ", "arn:aws:ec2:i-1"),
        ("serial", None, ""),
    ],
)
def test_normalize_identifier(kind: str, raw: object, expected: str) -> None:
    assert ident.normalize_identifier(kind, raw) == expected


# ── Matching ──────────────────────────────────────────────────────────


def test_same_device_from_two_sources_is_one_asset_merged_on_serial() -> None:
    intune = _rec("intune", "d-1", serial="C02XY12", hostname="LAPTOP-ALICE")
    openaev = _rec("openaev", "a-9", serial="c02xy12", hostname="laptop-alice.corp.example")
    [asset] = ident.match_records([intune, openaev])
    assert asset.sources == ["intune", "openaev"]
    assert asset.explanation() == "merged on serial"
    assert asset.merged_on == [{"record": "openaev:a-9", "kind": "serial", "value": "C02XY12"}]


def test_hostname_merges_when_no_stronger_key_disagrees() -> None:
    [asset] = ident.match_records([_rec("intune", "d-1", hostname="WS-7"), _rec("openaev", "a-1", hostname="ws-7")])
    assert asset.explanation() == "merged on hostname"


def test_different_serials_never_merge_on_a_shared_hostname() -> None:
    a = _rec("intune", "d-1", serial="AAA111", hostname="ws-7")
    b = _rec("openaev", "a-1", serial="BBB222", hostname="ws-7")
    assert len(ident.match_records([a, b])) == 2


def test_a_machine_named_in_two_cases_by_two_sources_is_one_asset() -> None:
    """An OpenAEV row stored with an empty hostname takes its name as hostname and merges with Intune's."""
    intune = {
        "name": "LAPT-FILI-0002",
        "source": "intune",
        "external_id": "d-1",
        "hostname": "LAPT-FILI-0002",
        "serial": "J1X3CD4",
        "os_family": "Windows",
    }
    openaev = {"name": "lapt-fili-0002", "source": "openaev", "external_id": "a-1", "hostname": "", "mac": []}
    [asset] = ident.dedupe_section("assets", [intune, openaev])
    assert asset.sources == ["intune", "openaev"]
    assert asset.explanation() == "merged on hostname"


def test_openaev_and_intune_merge_on_a_mac_whatever_the_names() -> None:
    intune = {"name": "LAPT-FILI-0002", "source": "intune", "external_id": "d-1", "mac": ["40:d1:33:98:4f:63"]}
    openaev = {"name": "agent-7", "source": "openaev", "external_id": "a-1", "mac": ["40-D1-33-98-4F-63"]}
    [asset] = ident.dedupe_section("assets", [intune, openaev])
    assert asset.explanation() == "merged on MAC address"


def test_a_shared_virtual_mac_never_merges_two_machines() -> None:
    a = {"name": "build-01", "source": "intune", "external_id": "d-1", "mac": ["02:42:ac:11:00:02"]}
    b = {"name": "build-02", "source": "openaev", "external_id": "a-1", "mac": ["02:42:ac:11:00:02"]}
    assert len(ident.dedupe_section("assets", [a, b])) == 2


def test_same_source_with_two_ids_is_two_things() -> None:
    a = _rec("intune", "d-1", hostname="ws-7")
    b = _rec("intune", "d-2", hostname="ws-7")
    assert len(ident.match_records([a, b])) == 2


@pytest.mark.parametrize(
    "value", ["10.0.0.5", "192.168.1.20", "10.0.0.5.", "CORP\\10.0.0.5", "fe80::1", "2001:db8::7", "10.0.0"]
)
def test_ip_shaped_values_are_never_a_hostname_or_fqdn(value: str) -> None:
    assert ident._hostname(value) == ""
    assert ident._fqdn(value) == ""


def test_ip_shaped_hostnames_never_merge_unrelated_assets() -> None:
    a = _rec("intune", "d-1", hostname="10.0.0.5")
    b = _rec("openaev", "a-1", hostname="10.9.9.9")
    assert len(ident.match_records([a, b])) == 2


def test_hostnames_with_digit_labels_still_normalise() -> None:
    assert ident._hostname("ws-7.corp.example.com") == "ws-7"
    assert ident._hostname("1password-host.corp") == "1password-host"
    assert ident._fqdn("host1.10x.example") == "host1.10x.example"


def test_ip_alone_never_merges_and_is_answered_as_a_possible_match() -> None:
    a = _rec("intune", "d-1", ip="10.0.0.5", hostname="alpha")
    b = _rec("openaev", "a-1", ip="10.0.0.5", hostname="beta")
    first, second = ident.match_records([a, b])
    assert first.possible_matches == [{"kind": "ip", "value": "10.0.0.5", "with": second.id}]
    assert second.possible_matches == [{"kind": "ip", "value": "10.0.0.5", "with": first.id}]


def test_transitive_merge_through_different_keys() -> None:
    mdm = _rec("intune", "d-1", serial="S1", mac="aa:bb:cc:dd:ee:01")
    edr = _rec("defender", "m-1", mac="AA-BB-CC-DD-EE-01", fqdn="ws1.corp.example")
    aev = _rec("openaev", "a-1", fqdn="WS1.corp.example")
    [asset] = ident.match_records([mdm, edr, aev])
    assert asset.explanation() == "merged on MAC address and FQDN"


def test_order_of_records_never_changes_the_answer() -> None:
    records = [
        _rec("intune", "d-1", serial="S1", hostname="ws1"),
        _rec("openaev", "a-1", hostname="ws1"),
        _rec("defender", "m-1", serial="S1"),
        _rec("opencrq", "c-1", hostname="ws2"),
        _rec("openaev", "a-2", hostname="ws2", ip="10.1.1.1"),
        _rec("defender", "m-2", ip="10.1.1.1"),
    ]

    def shape(result: list[ident.Canonical]) -> list[tuple[str, tuple[str, ...], str]]:
        return [(c.id, tuple(r.ref.label() for r in c.records), c.explanation()) for c in result]

    expected = shape(ident.match_records(records))
    for perm in itertools.permutations(records):
        assert shape(ident.match_records(list(perm))) == expected


def test_configured_key_order_is_honoured() -> None:
    a = _rec("intune", "d-1", serial="AAA", hostname="ws-7")
    b = _rec("openaev", "a-1", serial="BBB", hostname="ws-7")
    # With hostname ranked above serial, the hostname merges and the serial no longer vetoes.
    [asset] = ident.match_records([a, b], key_order=("hostname", "serial"))
    assert asset.explanation() == "merged on hostname"


def test_administrator_split_and_merge_win_over_the_keys() -> None:
    a = _rec("intune", "d-1", serial="S1")
    b = _rec("openaev", "a-1", serial="S1")
    c = _rec("opencrq", "c-1", hostname="other")
    split = Decisions(splits=frozenset({frozenset({a.ref, b.ref})}))
    assert len(ident.match_records([a, b, c], decisions=split)) == 3
    merge = Decisions(merges=(frozenset({a.ref, c.ref}),))
    result = ident.match_records([a, b, c], decisions=merge)
    assert len(result) == 1
    assert result[0].explanation() == "merged on serial and by an administrator"
    alone = ident.match_records([a, c], decisions=merge)
    assert alone[0].explanation() == "merged by an administrator"
    assert {m["kind"] for m in result[0].merged_on} == {"manual", "serial"}


def test_removing_a_source_keeps_the_thing_with_the_other() -> None:
    a = _rec("intune", "d-1", serial="S1")
    b = _rec("openaev", "a-1", serial="S1")
    [both] = ident.match_records([a, b])
    [left] = ident.match_records([b])
    assert left.sources == ["openaev"]
    assert both.id == ident.canonical_id([a.ref, b.ref])
    assert left.id == ident.canonical_id([b.ref])


def test_canonical_id_is_stable_for_the_same_records() -> None:
    refs = [RecordRef("openaev", "a-1"), RecordRef("intune", "d-1")]
    assert ident.canonical_id(refs) == ident.canonical_id(list(reversed(refs)))


# ── Fields ────────────────────────────────────────────────────────────


def test_precedence_picks_per_field_and_keeps_every_value() -> None:
    mdm = _rec("intune", "d-1", fields={"os_version": "11 23H2", "criticality": "low"})
    crq = _rec("opencrq", "c-1", fields={"criticality": "very_high", "os_version": "10"})
    aev = _rec("openaev", "a-1", fields={"internet_facing": "yes", "os_version": "11"})
    fields = ident.resolve_fields([mdm, crq, aev], source_classes={"intune": "mdm"})
    assert fields["os_version"].value == "11 23H2" and fields["os_version"].source == "intune"
    assert fields["criticality"].value == "very_high" and fields["criticality"].source == "opencrq"
    assert fields["internet_facing"].source == "openaev"
    assert [c["source"] for c in fields["os_version"].candidates] == ["intune", "openaev", "opencrq"]


def test_unknown_never_wins_over_a_known_value() -> None:
    crq = _rec("opencrq", "c-1", fields={"criticality": "unknown"})
    aev = _rec("openaev", "a-1", fields={"criticality": "high"})
    assert ident.resolve_fields([crq, aev])["criticality"].value == "high"


def test_custom_precedence_overrides_the_default() -> None:
    crq = _rec("opencrq", "c-1", fields={"criticality": "low"})
    hand = _rec("manual", "row-0", fields={"criticality": "high"})
    fields = ident.resolve_fields([crq, hand], precedence={"criticality": ("hand", "opencrq")})
    assert fields["criticality"].source == "manual"


def test_local_fields_come_from_hand_sources_first() -> None:
    sync = _rec("openaev", "a-1", fields={"environment": "test"})
    hand = _rec("manual", "row-0", fields={"environment": "production"})
    assert ident.resolve_fields([sync, hand])["environment"].value == "production"


def test_a_local_field_set_on_a_synced_record_resolves_without_a_hand_record() -> None:
    """#1367: the person's environment and group of a synced asset live on the sync's record and are shown."""
    sync = _rec("openaev", "a-1", fields={"environment": "production", "group": "Servers"})
    fields = ident.resolve_fields([sync])
    assert fields["environment"].value == "production"
    assert fields["group"].value == "Servers"
    # An empty hand value does not hide the synced record's value.
    hand = _rec("manual", "row-0", fields={"environment": ""})
    assert ident.resolve_fields([sync, hand])["environment"].value == "production"


def test_tie_goes_to_the_most_recently_seen_source() -> None:
    old = _rec("intune", "d-1", fields={"os_version": "10"}, seen=datetime(2026, 1, 1, tzinfo=UTC))
    new = _rec("jamf", "j-1", fields={"os_version": "11"}, seen=datetime(2026, 11, 1, tzinfo=UTC))
    classes = {"intune": "mdm", "jamf": "mdm"}
    assert ident.resolve_fields([old, new], source_classes=classes)["os_version"].value == "11"


def test_source_class() -> None:
    assert ident.source_class("csv") == "hand"
    assert ident.source_class("agent:1234") == "agent"
    assert ident.source_class("openaev") == "openaev"
    # The endpoint sources XTM One reads itself are registered (#1251); an unknown one stays other.
    assert ident.source_class("intune") == "mdm"
    assert ident.source_class("defender") == "edr"
    assert ident.source_class("jamf") == "other"
    assert ident.source_class("jamf", {"jamf": "mdm"}) == "mdm"


# ── Software ──────────────────────────────────────────────────────────


def test_software_merges_across_sources_and_keeps_unknown_versions() -> None:
    t1 = datetime(2026, 11, 1, tzinfo=UTC)
    t2 = datetime(2026, 11, 20, tzinfo=UTC)
    mdm = _rec(
        "intune",
        "d-1",
        software=[
            {"vendor": "F5", "product": "nginx", "version": "1.27.1", "first_seen": t1, "last_seen": t1},
            {"vendor": "Google", "product": "Chrome", "version": ""},
        ],
    )
    edr = _rec(
        "defender",
        "m-1",
        software=[
            {
                "vendor": "f5",
                "product": "NGINX",
                "version": "1.27.1",
                "cpe": "cpe:2.3:a:f5:nginx:1.27.1",
                "last_seen": t2,
            }
        ],
    )
    software = ident.merge_software([mdm, edr])
    assert len(software) == 2
    nginx, chrome = software
    assert chrome["version"] == "unknown"
    assert nginx["sources"] == ["defender", "intune"]
    assert nginx["cpe"] == "cpe:2.3:a:f5:nginx:1.27.1"
    assert nginx["first_seen"] == t1 and nginx["last_seen"] == t2


def test_software_without_a_product_is_dropped() -> None:
    assert ident.merge_software([_rec("intune", "d-1", software=[{"vendor": "x"}])]) == []


# ── CPE resolution ─────────────────────────────────────────────────────────


def test_a_cpe_override_wins_over_a_reported_cpe() -> None:
    override = "cpe:2.3:a:google:chrome:*:*:*:*:*:*:*:*"
    resolved = ident.resolve_cpe("Google", "Chrome", "130", reported=["cpe:2.3:a:google:chrome:130"], override=override)
    assert resolved == {"cpe": override, "confidence": "override"}


def test_cpe_from_a_resolver_carries_its_confidence_and_version() -> None:
    def lookup(vendor: str, product: str, version: str) -> tuple[str, str] | None:
        return ("cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*", "high") if product.lower() == "nginx" else None

    nginx = ident.resolve_cpe("F5", "nginx", "1.27.1", resolver=lookup)
    assert nginx == {"cpe": "cpe:2.3:a:f5:nginx:1.27.1:*:*:*:*:*:*:*", "confidence": "high"}
    assert ident.resolve_cpe("x", "y", "1", resolver=lookup) == {"cpe": "", "confidence": "unresolved"}


# ── Sections ──────────────────────────────────────────────────────────


def test_tech_stack_dedup_on_cpe_and_name() -> None:
    rows = [
        {
            "product": "nginx",
            "vendor": "F5",
            "cpe": "cpe:2.3:a:f5:nginx:1.27.1",
            "source": "openaev",
            "external_id": "s1",
        },
        {"product": "NGINX", "vendor": "", "cpe": "", "source": "manual"},
        {"product": "nginx", "vendor": "Other", "cpe": "", "source": "opencti", "external_id": "x9"},
    ]
    groups = ident.dedupe_section("software", rows)
    assert len(groups) == 2  # a different vendor of the same product name stays apart
    merged = next(g for g in groups if len(g.records) == 2)
    assert merged.sources == ["manual", "openaev"]


def test_hand_rows_with_the_same_name_merge() -> None:
    rows = [{"name": "Payments"}, {"name": "payments "}]
    [pa_row] = ident.dedupe_section("primary_assets", rows)
    assert pa_row.explanation() == "merged on name"


def test_primary_asset_from_opencrq_merges_with_the_typed_one() -> None:
    rows = [
        {"name": "Customer data", "source": "manual"},
        {"name": "Customer data", "source": "opencrq", "external_id": "pa-1", "opencrq_asset_id": "pa-1"},
    ]
    [pa_row] = ident.dedupe_section("primary_assets", rows)
    assert pa_row.sources == ["manual", "opencrq"]


def test_asset_groups_dedup_on_external_reference_then_name() -> None:
    rows = [
        {"name": "Prod clusters", "source": "openaev", "external_id": "g1", "external_reference": "REF-1"},
        {"name": "Production clusters", "source": "opencrq", "external_id": "q1", "external_reference": "ref-1"},
        {"name": "Prod clusters", "source": "manual"},
    ]
    [group] = ident.dedupe_section("asset_groups", rows)
    assert group.sources == ["manual", "openaev", "opencrq"]


def test_asset_rows_fall_back_on_the_name_as_hostname() -> None:
    rows = [
        {"name": "LAPTOP-ALICE", "source": "intune", "external_id": "d-1"},
        {"name": "laptop-alice", "source": "openaev", "external_id": "a-1"},
    ]
    [asset] = ident.dedupe_section("assets", rows)
    assert asset.explanation() == "merged on hostname"


def test_unknown_section_is_refused() -> None:
    with pytest.raises(ValueError, match="section must be one of"):
        ident.dedupe_section("nope", [])
