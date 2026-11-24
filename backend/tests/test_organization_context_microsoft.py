"""Microsoft Intune and Defender for Endpoint as asset sources (#1251): mapping, paging, identity."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import organization_asset_identity as identity
from app.services import organization_context_microsoft as microsoft
from app.services.organization_context import AssetEntry
from app.services.organization_context_import import SECTION_SOURCES, SOURCE_FILTERS

INTUNE_DEVICE = {
    "id": "8d3b-intune",
    "deviceName": "LAPTOP-042",
    "operatingSystem": "Windows",
    "osVersion": "10.0.22631.4460",
    "serialNumber": "PF3ABCD1",
    "azureADDeviceId": "AAAA1111-2222-3333-4444-555566667777",
    "wiFiMacAddress": "a1b2c3d4e5f6",
    "ethernetMacAddress": "",
    "complianceState": "compliant",
    "managedDeviceOwnerType": "company",
    "chassisType": "laptop",
    "lastSyncDateTime": "2026-11-22T08:00:00Z",
    "userDisplayName": "Jane Doe",
}

DEFENDER_MACHINE = {
    "id": "f3e2-mde",
    "computerDnsName": "laptop-042.corp.example",
    "osPlatform": "Windows11",
    "version": "23H2",
    "osBuild": 22631,
    "aadDeviceId": "aaaa1111-2222-3333-4444-555566667777",
    "lastIpAddress": "10.0.0.12",
    "lastExternalIpAddress": "",
    "exposureLevel": "Medium",
    "healthStatus": "Active",
    "riskScore": "None",
    "lastSeen": "2026-11-22T09:00:00Z",
}


def test_sources_are_offered_for_assets_and_intune_for_asset_groups() -> None:
    assert {"intune", "defender"} <= set(SECTION_SOURCES["assets"])
    # #1556: Intune also feeds asset groups (the Entra device groups); Defender feeds assets only.
    assert "intune" in SECTION_SOURCES["asset_groups"]
    for section, sources in SECTION_SOURCES.items():
        if section not in ("assets", "asset_groups"):
            assert "intune" not in sources
        if section != "assets":
            assert "defender" not in sources
    assert SOURCE_FILTERS[("assets", "intune")][:2] == ("operating_system", "compliance")


def test_not_connected_reason_names_the_source_integration_once() -> None:
    # The import card prefixes "Not available:" and appends where to connect it: the reason says only what is missing.
    assert microsoft.not_connected_message("intune") == "no Microsoft Intune integration is connected"
    assert microsoft.not_connected_message("defender") == "no Microsoft Defender integration is connected"
    assert "not connected" not in microsoft.not_connected_message("intune")


class _Row:
    def __init__(self, status: str = "connected", **fields: Any) -> None:
        self.status = status
        for name in ("access_token_enc", "refresh_token_enc", "api_key_enc", "client_secret_enc", "extra_config_enc"):
            setattr(self, name, fields.get(name))


def test_a_source_is_readable_only_when_its_integration_is_connected_with_a_credential() -> None:
    # UX round 1 B1: an integration created but never authorized, or disconnected, is not "connected and readable".
    assert microsoft.has_credential(_Row(access_token_enc="x", refresh_token_enc="y"))
    assert microsoft.has_credential(_Row(api_key_enc="x"))
    assert not microsoft.has_credential(_Row())
    assert not microsoft.has_credential(_Row(status="disconnected", api_key_enc="x"))
    assert not microsoft.has_credential(_Row(status="pending", access_token_enc="x"))


def _status(monkeypatch: pytest.MonkeyPatch, row: Any) -> tuple[bool, str, str]:
    async def _found(db: Any, user_id: str, provider: str) -> Any:
        return row

    monkeypatch.setattr(microsoft, "_integration", _found)
    return asyncio.run(microsoft.connection_status(None, "u", "defender"))


def test_connection_status_answers_a_reason_code(monkeypatch: pytest.MonkeyPatch) -> None:
    # UX round 1 B3: the dialog words the reason from the code in the user's language.
    assert _status(monkeypatch, None) == (False, microsoft.not_connected_message("defender"), "not_connected")
    assert _status(monkeypatch, _Row(status="disconnected")) == (
        False,
        microsoft.no_token_message("defender"),
        "no_token",
    )
    assert _status(monkeypatch, _Row(api_key_enc="x")) == (True, "", "")


def test_every_import_source_has_a_mapping_in_the_dialog() -> None:
    # UX round 1 B2: the Mapping step of the import stepper lists a row per profile field for every source.
    import pathlib

    dialog = (
        pathlib.Path(__file__).resolve().parents[2] / "frontend/src/pages/settings/OrganizationImportDialog.tsx"
    ).read_text(encoding="utf-8")
    for section, sources in SECTION_SOURCES.items():
        for source in sources:
            assert f'"{section}:{source}"' in dialog, f"no MAPPINGS entry for {section}:{source}"


def test_intune_device_row_carries_identity_and_os() -> None:
    row = microsoft.intune_device_row(INTUNE_DEVICE)
    entry = AssetEntry.model_validate(row)
    assert entry.source == "intune" and entry.external_id == "8d3b-intune"
    assert (entry.category, entry.subcategory) == ("host", "laptop")
    assert entry.serial == "PF3ABCD1"
    assert entry.device_id == "aaaa1111-2222-3333-4444-555566667777"
    assert entry.hostname == "LAPTOP-042"
    assert entry.mac == ["a1:b2:c3:d4:e5:f6"]
    assert (entry.os_family, entry.os_version) == ("Windows", "10.0.22631.4460")
    assert "compliance:compliant" in entry.tags and "ownership:company" in entry.tags
    assert entry.owner == ""  # opt-in only
    assert microsoft.intune_device_row(INTUNE_DEVICE, include_owner=True)["owner"] == "Jane Doe"


def test_intune_device_row_drops_zero_device_id_and_unknown_date() -> None:
    row = microsoft.intune_device_row(
        {
            **INTUNE_DEVICE,
            "azureADDeviceId": "00000000-0000-0000-0000-000000000000",
            "lastSyncDateTime": "0001-01-01T00:00:00Z",
        }
    )
    assert row["device_id"] == "" and row["last_seen"] == ""


def test_defender_machine_row() -> None:
    entry = AssetEntry.model_validate(microsoft.defender_machine_row(DEFENDER_MACHINE))
    assert entry.source == "defender"
    assert (entry.hostname, entry.fqdn) == ("laptop-042", "laptop-042.corp.example")
    assert (entry.os_family, entry.os_version, entry.os_build) == ("Windows", "23H2", "22631")
    assert entry.ip == ["10.0.0.12"]
    assert "exposure:medium" in entry.tags and not any(t.startswith("risk:") for t in entry.tags)
    server = microsoft.defender_machine_row({**DEFENDER_MACHINE, "osPlatform": "WindowsServer2022"})
    assert (server["category"], server["subcategory"]) == ("host", "server")


def test_software_mapping_keeps_versions_and_marks_unknown() -> None:
    apps = [
        {"displayName": "Google Chrome", "version": "131.0.6778.86", "publisher": "Google LLC"},
        {"displayName": "Google Chrome", "version": "131.0.6778.86", "publisher": "Google LLC"},
        {"displayName": "7-Zip", "version": ""},
        {"displayName": ""},
    ]
    software = microsoft.intune_software(apps)
    assert software == [
        {"vendor": "Google LLC", "product": "Google Chrome", "version": "131.0.6778.86"},
        {"vendor": "", "product": "7-Zip", "version": "unknown"},
    ]
    by_machine = microsoft.defender_software_by_machine(
        [
            {"deviceId": "f3e2-mde", "softwareVendor": "nginx", "softwareName": "nginx", "softwareVersion": "1.25.3"},
            {"deviceId": "f3e2-mde", "softwareVendor": "openssl", "softwareName": "openssl", "softwareVersion": ""},
            {"deviceId": "", "softwareName": "orphan"},
        ]
    )
    assert by_machine == {
        "f3e2-mde": [
            {"vendor": "nginx", "product": "nginx", "version": "1.25.3"},
            {"vendor": "openssl", "product": "openssl", "version": "unknown"},
        ]
    }


def test_odata_filters() -> None:
    assert microsoft.intune_odata_filter({}) == ""
    text = microsoft.intune_odata_filter(
        {"operating_system": ["Windows", "macOS"], "compliance": "noncompliant", "last_seen_days": 30},
        now_iso="2026-10-23T00:00:00Z",
    )
    assert text == (
        "(operatingSystem eq 'Windows' or operatingSystem eq 'macOS') and (complianceState eq 'noncompliant') "
        "and lastSyncDateTime ge 2026-10-23T00:00:00Z"
    )
    assert microsoft.defender_odata_filter({"exposure_level": "High"}) == "(exposureLevel eq 'High')"
    assert "''" in microsoft.intune_odata_filter({"operating_system": "it's"})


def test_source_classes_rank_endpoint_sources_first_for_os_and_software() -> None:
    assert identity.source_class("intune") == "mdm"
    assert identity.source_class("defender") == "edr"
    assert identity.source_class("intune", {"intune": "other"}) == "other"  # the organization's setting wins


def test_intune_device_and_openaev_endpoint_merge_into_one_asset_with_os_and_software() -> None:
    intune = identity.section_record("assets", microsoft.intune_device_row(INTUNE_DEVICE))
    intune.software = microsoft.intune_software(
        [{"displayName": "Google Chrome", "version": "131.0.6778.86", "publisher": "Google LLC"}]
    )
    openaev = identity.section_record(
        "assets",
        {
            "name": "laptop-042",
            "source": "openaev",
            "external_id": "oaev-1",
            "serial": "pf3abcd1",
            "os_family": "Windows",
            "os_version": "10.0.19045",
        },
    )
    canon = identity.match_records([intune, openaev])
    assert len(canon) == 1
    assert "serial" in " ".join(str(m) for m in canon[0].merged_on)
    software = identity.merge_software(canon[0].records)
    assert [(s["product"], s["version"]) for s in software] == [("Google Chrome", "131.0.6778.86")]


class _Resp:
    def __init__(self, status: int, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    def __init__(self, routes: dict[str, list[_Resp]]):
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, headers: dict[str, str]) -> _Resp:
        self.calls.append(url)
        for prefix, answers in self.routes.items():
            if url.startswith(prefix):
                return answers.pop(0) if len(answers) > 1 else answers[0]
        return _Resp(404)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_s: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def test_read_intune_pages_waits_out_throttling_and_reads_apps() -> None:
    base = f"{microsoft.GRAPH_BASE}/deviceManagement/managedDevices"
    apps = f"{microsoft.GRAPH_BETA}/deviceManagement/managedDevices"
    second = {**INTUNE_DEVICE, "id": "dev-2", "deviceName": "MAC-7", "operatingSystem": "macOS", "serialNumber": "C02X"}
    client = _Client(
        {
            f"{apps}/8d3b-intune/detectedApps": [
                _Resp(200, {"value": [{"displayName": "Slack", "version": "4.41.97", "publisher": "Slack"}]})
            ],
            f"{apps}/dev-2/detectedApps": [_Resp(200, {"value": []})],
            f"{base}?": [
                _Resp(429, headers={"Retry-After": "1"}),
                _Resp(200, {"value": [INTUNE_DEVICE], "@odata.nextLink": f"{base}?page=2"}),
                _Resp(200, {"value": [second]}),
            ],
        }
    )
    read = asyncio.run(microsoft.read_intune("t", {}, limit=1000, client=client))
    assert read.error is None and not read.truncated
    assert [r["external_id"] for r in read.rows] == ["8d3b-intune", "dev-2"]
    assert read.software["8d3b-intune"] == [{"vendor": "Slack", "product": "Slack", "version": "4.41.97"}]
    assert read.software_rows == 1
    assert read.software_error == ""


def test_read_intune_reads_detected_apps_on_the_beta_endpoint() -> None:
    """v1.0 has no managedDevices/{id}/detectedApps: every per-device read answered HTTP 400 in production."""
    base = f"{microsoft.GRAPH_BASE}/deviceManagement/managedDevices"
    client = _Client(
        {
            f"{base}/8d3b-intune/detectedApps": [
                _Resp(400, {"error": {"message": "Resource not found for the segment 'detectedApps'."}})
            ],
            f"{microsoft.GRAPH_BETA}/deviceManagement/managedDevices/8d3b-intune/detectedApps": [
                _Resp(200, {"value": [{"displayName": "Slack", "version": "4"}]})
            ],
            f"{base}?": [_Resp(200, {"value": [INTUNE_DEVICE]})],
        }
    )
    read = asyncio.run(microsoft.read_intune("t", {}, limit=10, client=client))
    assert read.software["8d3b-intune"][0]["product"] == "Slack"
    assert not any("/v1.0/" in url and "detectedApps" in url for url in client.calls)


def test_a_software_read_that_fails_says_why_instead_of_a_silent_zero() -> None:
    base = f"{microsoft.GRAPH_BASE}/deviceManagement/managedDevices"
    client = _Client(
        {
            f"{microsoft.GRAPH_BETA}/deviceManagement/managedDevices/8d3b-intune/detectedApps": [
                _Resp(400, {"error": {"message": "Bad request"}})
            ],
            f"{base}?": [_Resp(200, {"value": [INTUNE_DEVICE]})],
        }
    )
    read = asyncio.run(microsoft.read_intune("t", {}, limit=10, client=client))
    assert read.error is None and len(read.rows) == 1
    assert read.software_unread == {"8d3b-intune"}
    assert read.software_error == "intune: HTTP 400 (Bad request)"


def test_an_intune_mac_with_an_apostrophe_and_its_openaev_hostname_are_one_asset() -> None:
    """Intune sends the device name, the OpenAEV agent the hostname macOS derives from it."""
    intune = identity.section_record(
        "assets",
        microsoft.intune_device_row(
            {
                **INTUNE_DEVICE,
                "id": "724d3ba3",
                "deviceName": "Amani\u2019s MacBook Pro",
                "operatingSystem": "macOS",
                "serialNumber": "",
                "wiFiMacAddress": "",
            }
        ),
    )
    openaev = identity.section_record(
        "assets", {"name": "Amanis-MacBook-Pro", "source": "openaev", "external_id": "bfdcefda"}
    )
    other = identity.section_record(
        "assets", {"name": "Adams-MacBook-Pro", "source": "openaev", "external_id": "c033be9a"}
    )
    canon = identity.match_records([intune, openaev, other])
    assert sorted(len(c.records) for c in canon) == [1, 2]
    merged = next(c for c in canon if len(c.records) == 2)
    assert {r.source for r in merged.records} == {"intune", "openaev"}
    assert merged.explanation() == "merged on hostname"


def test_an_intune_device_and_an_openaev_endpoint_merge_on_a_mac_address() -> None:
    intune = identity.section_record("assets", microsoft.intune_device_row({**INTUNE_DEVICE, "serialNumber": ""}))
    openaev = identity.section_record(
        "assets",
        {"name": "some-other-name", "source": "openaev", "external_id": "oaev-9", "mac": ["A1-B2-C3-D4-E5-F6"]},
    )
    [asset] = identity.match_records([intune, openaev])
    assert asset.explanation() == "merged on MAC address"


def test_read_intune_names_the_missing_permission() -> None:
    client = _Client({microsoft.GRAPH_BASE: [_Resp(403)]})
    read = asyncio.run(microsoft.read_intune("t", {}, limit=10, client=client))
    assert read.rows == []
    assert "DeviceManagementManagedDevices.Read.All" in (read.error or "")


def test_read_intune_limit_marks_truncated() -> None:
    base = f"{microsoft.GRAPH_BASE}/deviceManagement/managedDevices"
    devices = [{**INTUNE_DEVICE, "id": f"d{i}"} for i in range(3)]
    client = _Client(
        {
            f"{base}/": [_Resp(200, {"value": []})],
            f"{base}?": [_Resp(200, {"value": devices, "@odata.nextLink": f"{base}?page=2"})],
        }
    )
    read = asyncio.run(microsoft.read_intune("t", {}, limit=2, client=client))
    assert len(read.rows) == 2 and read.truncated


def test_read_defender_joins_software_inventory_to_machines() -> None:
    client = _Client(
        {
            f"{microsoft.MDE_BASE}/machines/SoftwareInventoryByMachine": [
                _Resp(
                    200,
                    {
                        "value": [
                            {
                                "deviceId": "f3e2-mde",
                                "softwareVendor": "nginx",
                                "softwareName": "nginx",
                                "softwareVersion": "1.25.3",
                            },
                            {"deviceId": "other", "softwareVendor": "x", "softwareName": "x", "softwareVersion": "1"},
                        ]
                    },
                )
            ],
            f"{microsoft.MDE_BASE}/machines": [_Resp(200, {"value": [DEFENDER_MACHINE]})],
        }
    )
    read = asyncio.run(microsoft.read_defender("t", {}, limit=100, client=client))
    assert read.error is None
    assert list(read.software) == ["f3e2-mde"]
    assert read.software["f3e2-mde"][0]["version"] == "1.25.3"


def test_read_defender_machines_sync_when_software_permission_is_missing() -> None:
    client = _Client(
        {
            f"{microsoft.MDE_BASE}/machines/SoftwareInventoryByMachine": [_Resp(403)],
            f"{microsoft.MDE_BASE}/machines": [_Resp(200, {"value": [DEFENDER_MACHINE]})],
        }
    )
    read = asyncio.run(microsoft.read_defender("t", {}, limit=100, client=client))
    assert read.error is None and len(read.rows) == 1 and read.software == {}
    assert read.software_unread == {"f3e2-mde"}


def test_read_intune_marks_devices_whose_apps_read_failed_as_unread() -> None:
    base = f"{microsoft.GRAPH_BASE}/deviceManagement/managedDevices"
    apps = f"{microsoft.GRAPH_BETA}/deviceManagement/managedDevices"
    second = {**INTUNE_DEVICE, "id": "dev-2", "deviceName": "MAC-7", "serialNumber": "C02X"}
    client = _Client(
        {
            f"{apps}/8d3b-intune/detectedApps": [_Resp(200, {"value": [{"displayName": "Slack", "version": "4"}]})],
            f"{apps}/dev-2/detectedApps": [_Resp(503)],
            f"{base}?": [_Resp(200, {"value": [INTUNE_DEVICE, second]})],
        }
    )
    read = asyncio.run(microsoft.read_intune("t", {}, limit=1000, client=client))
    assert read.error is None and not read.truncated
    assert list(read.software) == ["8d3b-intune"]
    assert read.software_unread == {"dev-2"}


def test_read_defender_partial_inventory_keeps_stored_software() -> None:
    inventory = f"{microsoft.MDE_BASE}/machines/SoftwareInventoryByMachine"
    client = _Client(
        {
            f"{inventory}?pageSize=50000": [
                _Resp(
                    200,
                    {
                        "value": [
                            {
                                "deviceId": "f3e2-mde",
                                "softwareVendor": "nginx",
                                "softwareName": "nginx",
                                "softwareVersion": "1",
                            }
                        ],
                        "@odata.nextLink": f"{inventory}?page=2",
                    },
                )
            ],
            f"{inventory}?page=2": [_Resp(500)],
            f"{microsoft.MDE_BASE}/machines": [_Resp(200, {"value": [DEFENDER_MACHINE]})],
        }
    )
    read = asyncio.run(microsoft.read_defender("t", {}, limit=100, client=client))
    assert read.error is None and len(read.rows) == 1
    assert read.software == {} and read.software_unread == {"f3e2-mde"}
