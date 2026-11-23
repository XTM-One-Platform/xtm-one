"""Tanium, CrowdStrike Falcon and ServiceNow CMDB as asset sources (#1556): mapping, paging, identity."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import organization_asset_identity as identity
from app.services import organization_context_endpoints as endpoints
from app.services.organization_context import AssetEntry
from app.services.organization_context_import import SECTION_SOURCES, SOURCE_FILTERS

# Recorded answers, trimmed to the fields the readers ask for.
TANIUM_NODE: dict[str, Any] = {
    "id": "1042",
    "name": "LAPTOP-042",
    "serialNumber": "PF3ABCD1",
    "domainName": "corp.example",
    "ipAddress": "10.0.0.12",
    "ipAddresses": ["10.0.0.12", "fe80::1"],
    "macAddresses": ["A1-B2-C3-D4-E5-F6"],
    "eidLastSeen": "2026-11-22T10:00:00Z",
    "os": {"name": "Windows 11 Enterprise", "platform": "Windows", "generation": "Windows 11"},
    "installedApplications": [
        {"name": "Google Chrome", "version": "131.0.6778.86"},
        {"name": "Google Chrome", "version": "131.0.6778.86"},
        {"name": "7-Zip", "version": ""},
    ],
}
FALCON_HOST: dict[str, Any] = {
    "device_id": "5f4c0aid",
    "hostname": "LAPTOP-042",
    "machine_domain": "corp.example",
    "platform_name": "Windows",
    "os_product_name": "Windows 11 Enterprise",
    "os_version": "Windows 11",
    "os_build": "22631",
    "product_type_desc": "Workstation",
    "serial_number": "PF3ABCD1",
    "local_ip": "10.0.0.12",
    "external_ip": "203.0.113.7",
    "mac_address": "a1-b2-c3-d4-e5-f6",
    "last_seen": "2026-11-22T11:00:00Z",
    "tags": ["SensorGroupingTags/Finance"],
}
SN_CI: dict[str, Any] = {
    "sys_id": {"value": "9a8b7c6d5e4f", "display_value": "9a8b7c6d5e4f"},
    "name": {"value": "LAPTOP-042", "display_value": "LAPTOP-042"},
    "sys_class_name": {"value": "cmdb_ci_computer", "display_value": "Computer"},
    "serial_number": {"value": "PF3ABCD1", "display_value": "PF3ABCD1"},
    "host_name": {"value": "laptop-042", "display_value": "laptop-042"},
    "fqdn": {"value": "laptop-042.corp.example", "display_value": "laptop-042.corp.example"},
    "ip_address": {"value": "10.0.0.12", "display_value": "10.0.0.12"},
    "mac_address": {"value": "a1:b2:c3:d4:e5:f6", "display_value": "a1:b2:c3:d4:e5:f6"},
    "os": {"value": "Windows 11 Enterprise", "display_value": "Windows 11 Enterprise"},
    "os_version": {"value": "10.0.22631", "display_value": "10.0.22631"},
    "owned_by": {"value": "6816f79c", "display_value": "Jane Doe"},
    "environment": {"value": "production", "display_value": "Production"},
    "last_discovered": {"value": "2026-11-21 09:00:00", "display_value": "2026-11-21 09:00:00"},
}


def test_sources_are_offered_for_assets() -> None:
    for source in endpoints.SOURCES:
        assert source in SECTION_SOURCES["assets"]
        assert SOURCE_FILTERS[("assets", source)] == endpoints.FILTERS[source]
        assert identity.source_class(source) in ("mdm", "edr", "cmdb")


def test_tanium_endpoint_maps_identity_keys_os_and_software() -> None:
    row = endpoints.tanium_endpoint_row(TANIUM_NODE)
    entry = AssetEntry.model_validate(row)
    assert (entry.source, entry.external_id, entry.serial) == ("tanium", "1042", "PF3ABCD1")
    assert (entry.hostname, entry.fqdn) == ("LAPTOP-042", "LAPTOP-042.corp.example")
    assert entry.mac == ["a1:b2:c3:d4:e5:f6"]
    assert entry.ip == ["10.0.0.12", "fe80::1"]
    assert (entry.category, entry.os_family) == ("host", "Windows")
    software = endpoints.tanium_software(TANIUM_NODE["installedApplications"])
    assert {(s["product"], s["version"]) for s in software} == {
        ("Google Chrome", "131.0.6778.86"),
        ("7-Zip", "unknown"),
    }


def test_crowdstrike_host_maps_aid_serial_and_addresses() -> None:
    entry = AssetEntry.model_validate(endpoints.crowdstrike_host_row(FALCON_HOST))
    assert (entry.source, entry.external_id, entry.serial) == ("crowdstrike", "5f4c0aid", "PF3ABCD1")
    assert entry.fqdn == "LAPTOP-042.corp.example"
    assert entry.mac == ["a1:b2:c3:d4:e5:f6"]
    assert entry.ip == ["10.0.0.12", "203.0.113.7"]
    assert (entry.os_family, entry.os_build, entry.last_seen) == ("Windows", "22631", "2026-11-22T11:00:00Z")


def test_servicenow_ci_maps_display_values_owner_and_environment() -> None:
    entry = AssetEntry.model_validate(endpoints.servicenow_ci_row(SN_CI))
    assert (entry.source, entry.external_id, entry.serial) == ("servicenow", "9a8b7c6d5e4f", "PF3ABCD1")
    assert (entry.hostname, entry.fqdn) == ("laptop-042", "laptop-042.corp.example")
    assert (entry.owner, entry.environment) == ("Jane Doe", "production")
    assert "ci_class:cmdb_ci_computer" in entry.tags


def test_servicenow_network_class_is_a_network_device() -> None:
    ci = {"sys_id": "n1", "name": "core-sw-01", "sys_class_name": "cmdb_ci_netgear"}
    assert endpoints.servicenow_ci_row(ci, ci_class="cmdb_ci_netgear")["category"] == "network_device"


def test_filters_build_the_source_queries() -> None:
    assert endpoints.falcon_filter({"platform": ["Windows", "Mac"]}, since_iso="2026-11-01T00:00:00Z") == (
        "platform_name:['Windows','Mac']+last_seen:>='2026-11-01T00:00:00Z'"
    )
    assert endpoints.servicenow_query({"operational_status": ["1", "x"]}) == (
        "install_status!=7^operational_statusIN1^ORDERBYsys_id"
    )
    assert endpoints.servicenow_class({"ci_class": "sys_user"}) == "cmdb_ci_computer"
    assert endpoints.servicenow_class({"ci_class": "cmdb_ci_server"}) == "cmdb_ci_server"


def test_one_device_from_three_sources_merges_into_one_asset() -> None:
    records = [
        identity.section_record("assets", row)
        for row in (
            endpoints.tanium_endpoint_row(TANIUM_NODE),
            endpoints.crowdstrike_host_row(FALCON_HOST),
            endpoints.servicenow_ci_row(SN_CI),
        )
    ]
    canon = identity.match_records(records)
    assert len(canon) == 1
    assert sorted(r.source for r in canon[0].records) == ["crowdstrike", "servicenow", "tanium"]


class _Resp:
    def __init__(self, body: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._body, self.status_code, self.headers = body, status, headers or {}

    def json(self) -> dict[str, Any]:
        return self._body


def _fake_safe_request(monkeypatch: pytest.MonkeyPatch, answers: list[_Resp], seen: list[dict[str, Any]]) -> None:
    async def fake(client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any) -> Any:
        seen.append({"method": method, "url": url, **kwargs})
        resp = answers.pop(0)
        if resp.status_code >= 400:
            return resp, f"HTTP {resp.status_code}"
        return resp, None

    monkeypatch.setattr("app.integrations.http.safe_request", fake)


def test_tanium_reader_pages_with_the_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    second = dict(TANIUM_NODE, id="1043", name="SRV-01", serialNumber="S2")
    answers = [
        _Resp(
            {
                "data": {
                    "endpoints": {
                        "totalRecords": 2,
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                        "edges": [{"node": TANIUM_NODE}],
                    }
                }
            }
        ),
        _Resp({"data": {"endpoints": {"pageInfo": {"hasNextPage": False}, "edges": [{"node": second}]}}}),
    ]
    seen: list[dict[str, Any]] = []
    _fake_safe_request(monkeypatch, answers, seen)
    read = asyncio.run(endpoints.read_tanium("https://tanium.example||tok", {}, limit=100, client=object()))
    assert read.error is None
    assert [r["external_id"] for r in read.rows] == ["1042", "1043"]
    assert read.total == 2 and not read.truncated
    assert seen[1]["json"]["variables"]["after"] == "c1"
    assert read.software_rows == 4


def test_crowdstrike_reader_scrolls_then_reads_details(monkeypatch: pytest.MonkeyPatch) -> None:
    async def token(*_a: Any) -> tuple[str, int]:
        return "bearer", 1799

    monkeypatch.setattr("app.integrations.providers.crowdstrike._request_token_with_ttl", token)
    answers = [
        _Resp({"resources": ["5f4c0aid"], "meta": {"pagination": {"total": 1, "offset": ""}}}),
        _Resp({"resources": [FALCON_HOST]}),
    ]
    seen: list[dict[str, Any]] = []
    _fake_safe_request(monkeypatch, answers, seen)
    read = asyncio.run(
        endpoints.read_crowdstrike(
            "https://api.crowdstrike.com||id||secret", {"platform": "Windows"}, limit=10, client=object()
        )
    )
    assert read.error is None and read.total == 1
    assert read.rows[0]["external_id"] == "5f4c0aid"
    assert seen[0]["params"]["filter"] == "platform_name:['Windows']"
    assert seen[1]["json"] == {"ids": ["5f4c0aid"]}


def test_servicenow_reader_names_the_role_on_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    _fake_safe_request(monkeypatch, [_Resp({}, status=403)], seen)
    read = asyncio.run(
        endpoints.read_servicenow("https://acme.service-now.com||svc||pw", {}, limit=10, client=object())
    )
    assert read.error is not None and "cmdb_read" in read.error
    assert seen[0]["url"].endswith("/api/now/table/cmdb_ci_computer")


def test_servicenow_reader_pages_by_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoints, "_SN_PAGE", 1)
    other = dict(SN_CI, sys_id={"value": "ffff"}, serial_number={"value": "S9"})
    answers = [
        _Resp({"result": [SN_CI]}, headers={"X-Total-Count": "2"}),
        _Resp({"result": [other]}, headers={"X-Total-Count": "2"}),
    ]
    seen: list[dict[str, Any]] = []
    _fake_safe_request(monkeypatch, answers, seen)
    read = asyncio.run(
        endpoints.read_servicenow("https://acme.service-now.com||svc||pw", {}, limit=10, client=object())
    )
    assert [r["external_id"] for r in read.rows] == ["9a8b7c6d5e4f", "ffff"]
    assert [s["params"]["sysparm_offset"] for s in seen] == [0, 1]
    assert read.total == 2 and not read.truncated
