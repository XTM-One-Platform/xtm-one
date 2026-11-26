"""Intune configuration, script, app and detected-app tools against a fake Graph.

Each tool runs for real (``safe_request``, ``AutoRefreshClient``) against a
fake Microsoft Graph served through ``httpx.MockTransport``, which keeps the
objects it is sent, so a write is checked by what Graph received and by the
read-back the tool answers.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx
import pytest

from app.integrations.providers import intune
from app.integrations.providers.intune_management import SCOPE_SCRIPTS
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

POLICIES = "/beta/deviceManagement/configurationPolicies"
PROFILES = "/v1.0/deviceManagement/deviceConfigurations"
HEALTH = "/beta/deviceManagement/deviceHealthScripts"
SHELL = "/beta/deviceManagement/deviceShellScripts"
APPS = "/v1.0/deviceAppManagement/mobileApps"
DETECTED = "/v1.0/deviceManagement/detectedApps"

EXISTING_TOOLS = {
    "intune_list_devices",
    "intune_get_device",
    "intune_sync_device",
    "intune_reboot_device",
    "intune_lock_device",
    "intune_retire_device",
    "intune_wipe_device",
    "intune_list_compliance_policies",
    "intune_get_compliance_policy",
    "intune_get_compliance_policy_device_statuses",
    "intune_list_config_profiles",
    "intune_get_config_profile",
    "intune_list_detected_apps",
    "intune_list_device_categories",
    "intune_list_role_assignments",
    "intune_compliance_overview",
}

WRITE_TOOLS = {
    "intune_create_settings_catalog_policy",
    "intune_update_settings_catalog_policy",
    "intune_create_config_profile",
    "intune_update_config_profile",
    "intune_create_health_script",
    "intune_update_health_script",
    "intune_create_shell_script",
    "intune_update_shell_script",
    "intune_run_remediation",
    "intune_assign",
    "intune_delete",
}

READ_TOOLS = {
    "intune_list_detected_app_devices",
    "intune_list_settings_catalog_policies",
    "intune_get_settings_catalog_policy",
    "intune_list_health_scripts",
    "intune_get_health_script_device_states",
    "intune_list_mobile_apps",
    "intune_get_app_install_status",
    "intune_get_app_assignments",
}

INSTALL_REPORT = "/beta/deviceManagement/reports/getDeviceInstallStatusReport"
APP_OVERVIEW_REPORT = "/beta/deviceManagement/reports/getAppStatusOverviewReport"

#: A getDeviceInstallStatusReport answer as Graph serves it (Schema + Values, enum
#: columns doubled by their ``_loc`` text), recorded shape, data invented.
INSTALL_REPORT_ANSWER: dict[str, Any] = {
    "TotalRowCount": 3,
    "Schema": [
        {"Column": "DeviceName", "PropertyType": "String"},
        {"Column": "UserPrincipalName", "PropertyType": "String"},
        {"Column": "Platform", "PropertyType": "String"},
        {"Column": "AppVersion", "PropertyType": "String"},
        {"Column": "InstallState", "PropertyType": "Int32"},
        {"Column": "InstallState_loc", "PropertyType": "String"},
        {"Column": "InstallStateDetail", "PropertyType": "Int32"},
        {"Column": "InstallStateDetail_loc", "PropertyType": "String"},
        {"Column": "ErrorCode", "PropertyType": "Int32"},
        {"Column": "HexErrorCode", "PropertyType": "String"},
        {"Column": "LastModifiedDateTime", "PropertyType": "DateTime"},
        {"Column": "DeviceId", "PropertyType": "String"},
    ],
    "Values": [
        [
            "LAPT-0001",
            "a@example.com",
            "macOS",
            "1.2",
            1,
            "Installed",
            0,
            "None",
            0,
            "0",
            "2026-11-26T10:00:00Z",
            "d-1",
        ],
        [
            "LAPT-0002",
            "b@example.com",
            "Windows",
            "1.2",
            2,
            "Failed",
            2016330908,
            "Install failed",
            -2016330908,
            "0x87D1041C",
            "2026-11-26T11:00:00Z",
            "d-2",
        ],
    ],
}

APP_OVERVIEW_ANSWER: dict[str, Any] = {
    "TotalRowCount": 1,
    "Schema": [
        {"Column": "ApplicationId", "PropertyType": "String"},
        {"Column": "InstalledDeviceCount", "PropertyType": "Int32"},
        {"Column": "FailedDeviceCount", "PropertyType": "Int32"},
        {"Column": "PendingInstallDeviceCount", "PropertyType": "Int32"},
        {"Column": "NotInstalledDeviceCount", "PropertyType": "Int32"},
    ],
    "Values": [["app-1", 1, 1, 0, 1]],
}


class FakeGraph:
    """A small in-memory Graph: collections of objects, their assignments and actions."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, dict[str, Any]]] = {
            POLICIES: {},
            PROFILES: {},
            HEALTH: {},
            SHELL: {},
            APPS: {
                "app-1": {"id": "app-1", "displayName": "Company Portal", "@odata.type": "#microsoft.graph.macOSLobApp"}
            },
        }
        self.assignments: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.bodies: list[tuple[str, str, Any]] = []
        self.forbidden: set[str] = set()
        self.refuse_filter: set[str] = set()
        self.no_assignments_route: set[str] = set()
        self.install_report: dict[str, Any] = INSTALL_REPORT_ANSWER
        self.overview_status = 200
        self.install_report_status = 200
        self.detected: list[dict[str, Any]] = [
            {"id": "da-1", "displayName": "Google Chrome", "version": "130.0", "deviceCount": 2},
            {"id": "da-2", "displayName": "Google Chrome", "version": "129.0", "deviceCount": 1},
            {"id": "da-3", "displayName": "Mozilla Firefox", "version": "132.0", "deviceCount": 1},
        ]
        self.detected_devices: dict[str, list[dict[str, Any]]] = {
            "da-1": [
                {
                    "id": "d-1",
                    "deviceName": "LAPT-0001",
                    "operatingSystem": "macOS",
                    "userPrincipalName": "a@example.com",
                },
                {
                    "id": "d-2",
                    "deviceName": "LAPT-0002",
                    "operatingSystem": "Windows",
                    "userPrincipalName": "b@example.com",
                },
            ],
            "da-2": [
                {
                    "id": "d-3",
                    "deviceName": "LAPT-0003",
                    "operatingSystem": "Windows",
                    "userPrincipalName": "c@example.com",
                }
            ],
        }
        self.run_states = [
            {
                "id": "hs-1:d-1",
                "detectionState": "success",
                "remediationState": "remediationSuccess",
                "lastStateUpdateDateTime": "2026-11-11T09:00:00Z",
                "managedDevice": {"id": "d-1", "deviceName": "LAPT-0001"},
            },
            {
                "id": "hs-1:d-2",
                "detectionState": "fail",
                "remediationState": "skipped",
                "managedDevice": {"id": "d-2", "deviceName": "LAPT-0002"},
            },
        ]
        self._next = 0

    def _new_id(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}-{self._next}"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        body: Any = json.loads(request.content) if request.content else None
        self.bodies.append((method, path, body))
        for prefix in self.forbidden:
            if path.startswith(prefix):
                return httpx.Response(
                    403,
                    json={
                        "error": {
                            "code": "Forbidden",
                            "message": "Application is not authorized to perform this operation.",
                        }
                    },
                )

        if path == DETECTED:
            return self._detected(request)
        m = re.fullmatch(rf"{DETECTED}/([^/]+)(/managedDevices)?", path)
        if m:
            app = next((a for a in self.detected if a["id"] == m.group(1)), None)
            if app is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            if m.group(2):
                return httpx.Response(200, json={"value": self.detected_devices.get(app["id"], [])})
            return httpx.Response(200, json=app)

        m = re.fullmatch(r"/beta/deviceManagement/managedDevices/([^/]+)/initiateOnDemandProactiveRemediation", path)
        if m and method == "POST":
            return httpx.Response(204)

        if re.fullmatch(r"/beta/deviceAppManagement/mobileApps/[^/]+/deviceStatuses", path):
            # What Graph answers on a macOS LOB, an iOS VPP or a Windows MSI app.
            return httpx.Response(
                400,
                json={
                    "error": {"code": "BadRequest", "message": "Resource not found for the segment 'deviceStatuses'."}
                },
            )
        if path == INSTALL_REPORT and method == "POST":
            if self.install_report_status != 200 or not re.fullmatch(
                r"\(ApplicationId eq '[^']+'\)", (body or {}).get("filter", "")
            ):
                return httpx.Response(400, json={"error": {"code": "BadRequest", "message": "Invalid filter clause"}})
            return httpx.Response(200, json=self.install_report)
        if path == APP_OVERVIEW_REPORT and method == "POST":
            if self.overview_status != 200:
                return httpx.Response(self.overview_status, json={"error": {"message": "Report unavailable"}})
            return httpx.Response(200, json=APP_OVERVIEW_ANSWER)

        for collection, store in self.objects.items():
            if path == collection:
                if method == "POST":
                    oid = self._new_id(collection.rsplit("/", 1)[-1][:6])
                    store[oid] = {"id": oid, **(body or {})}
                    return httpx.Response(201, json=store[oid])
                values = list(store.values())
                if "$filter" in request.url.params and collection in self.refuse_filter:
                    return httpx.Response(400, json={"error": {"message": "Invalid filter clause"}})
                return httpx.Response(200, json={"value": values})
            m = re.fullmatch(rf"{re.escape(collection)}/([^/]+)(?:/(.+))?", path)
            if not m:
                continue
            oid, sub = m.group(1), m.group(2)
            if oid not in store:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            if sub is None:
                if method == "GET":
                    obj = dict(store[oid])
                    # Like Graph: assignments only when expanded, and a settings catalog
                    # policy never fills them on the object, only on /assignments.
                    if "assignments" in request.url.params.get("$expand", "") and collection != POLICIES:
                        obj["assignments"] = self.assignments.get(oid, [])
                    elif collection == POLICIES:
                        obj["assignments"] = []
                    return httpx.Response(200, json=obj)
                if method == "PATCH":
                    store[oid].update(body or {})
                    return httpx.Response(204)
                if method == "PUT":
                    store[oid] = {"id": oid, **(body or {})}
                    return httpx.Response(204)
                if method == "DELETE":
                    del store[oid]
                    return httpx.Response(204)
            if sub == "assignments":
                if collection == SHELL or collection in self.no_assignments_route:
                    return httpx.Response(
                        400,
                        json={
                            "error": {
                                "code": "No method match route template",
                                "message": "No OData route exists that match template "
                                "~/singleton/navigation/key/navigation with http verb GET",
                            }
                        },
                    )
                return httpx.Response(200, json={"value": self.assignments.get(oid, [])})
            if sub == "assign" and method == "POST":
                key = next(iter(body))
                self.assignments[oid] = [{"id": f"{oid}-a{i}", **a} for i, a in enumerate(body[key])]
                return httpx.Response(200 if collection == APPS else 204)
            if sub == "deviceRunStates":
                return httpx.Response(200, json={"value": self.run_states})
        return httpx.Response(404, json={"error": {"message": f"no route {method} {path}"}})

    def _detected(self, request: httpx.Request) -> httpx.Response:
        clause = request.url.params.get("$filter")
        items = self.detected
        if clause:
            if DETECTED in self.refuse_filter:
                return httpx.Response(400, json={"error": {"message": "Invalid filter clause"}})
            m = re.fullmatch(r"displayName eq '(.*)'", clause)
            if m:
                items = [a for a in items if a["displayName"].lower() == m.group(1).replace("''", "'").lower()]
            else:
                m = re.fullmatch(r"contains\(displayName,'(.*)'\)", clause)
                assert m, clause
                items = [a for a in items if m.group(1).replace("''", "'").lower() in a["displayName"].lower()]
        top = int(request.url.params.get("$top", "100"))
        return httpx.Response(200, json={"value": items[:top]})


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    fake = FakeGraph()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return fake


async def _call(tool: str, /, **arguments: Any) -> str:
    executor = CallableToolExecutor()
    intune.build_tools("tok", executor)
    result = await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))
    return result.content


def _sent(graph: FakeGraph, method: str, path: str) -> list[Any]:
    return [body for m, p, body in graph.bodies if m == method and p == path]


# ── Surface ──────────────────────────────────────────────────────────────


def test_existing_tools_are_kept_and_new_ones_are_classified() -> None:
    executor = CallableToolExecutor()
    names = [t.name for t in intune.build_tools("tok", executor)]
    assert len(names) == len(set(names))
    assert set(names) >= EXISTING_TOOLS
    assert set(names) == EXISTING_TOOLS | WRITE_TOOLS | READ_TOOLS
    assert set(executor.tool_names) == set(names)
    for name in WRITE_TOOLS:
        assert infer_tool_type(name) == "write", name
    for name in READ_TOOLS | {"intune_list_detected_apps"}:
        assert infer_tool_type(name) == "read", name


def test_scripts_scope_is_in_the_oauth_defaults() -> None:
    assert SCOPE_SCRIPTS in intune.IntuneIntegration().get_oauth_defaults().scopes


# ── Settings catalog ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_catalog_policy_create_assign_read_back_delete(graph: FakeGraph) -> None:
    instance = {
        "@odata.type": "#microsoft.graph.deviceManagementConfigurationChoiceSettingInstance",
        "settingDefinitionId": "device_vendor_msft_policy_config_chrome~policy~googlechrome_relaunchnotification",
        "choiceSettingValue": {
            "value": "device_vendor_msft_policy_config_chrome~policy~googlechrome_relaunchnotification_1"
        },
    }
    out = await _call(
        "intune_create_settings_catalog_policy", name="Browser relaunch", platforms="windows10", settings=[instance]
    )
    assert out.startswith("Created settings catalog policy"), out
    (body,) = _sent(graph, "POST", POLICIES)
    assert body["name"] == "Browser relaunch"
    assert body["technologies"] == "mdm"
    assert body["settings"] == [
        {"@odata.type": "#microsoft.graph.deviceManagementConfigurationSetting", "settingInstance": instance}
    ]
    policy_id = next(iter(graph.objects[POLICIES]))
    assert "relaunchnotification" in out

    out = await _call(
        "intune_assign",
        kind="settings_catalog",
        object_id=policy_id,
        include_group_ids=["g-pilot"],
        exclude_group_ids=["g-vip"],
    )
    assert "assignments now (2)" in out, out
    (assign,) = _sent(graph, "POST", f"{POLICIES}/{policy_id}/assign")
    assert assign == {
        "assignments": [
            {"target": {"@odata.type": "#microsoft.graph.groupAssignmentTarget", "groupId": "g-pilot"}},
            {"target": {"@odata.type": "#microsoft.graph.exclusionGroupAssignmentTarget", "groupId": "g-vip"}},
        ]
    }

    out = await _call("intune_get_settings_catalog_policy", policy_id=policy_id)
    assert "g-pilot" in out and "Browser relaunch" in out

    out = await _call("intune_delete", kind="settings_catalog", object_id=policy_id)
    assert out == f"Deleted settings catalog policy {policy_id} (read back: not found)."
    assert policy_id not in graph.objects[POLICIES]


@pytest.mark.asyncio
async def test_assign_keeps_existing_assignments_unless_replace(graph: FakeGraph) -> None:
    graph.objects[POLICIES]["p-1"] = {"id": "p-1", "name": "P"}
    graph.assignments["p-1"] = [
        {"id": "x", "target": {"@odata.type": "#microsoft.graph.groupAssignmentTarget", "groupId": "g-old"}}
    ]
    await _call("intune_assign", kind="settings_catalog", object_id="p-1", include_group_ids=["g-new"])
    groups = {a["target"]["groupId"] for a in graph.assignments["p-1"]}
    assert groups == {"g-old", "g-new"}

    await _call("intune_assign", kind="settings_catalog", object_id="p-1", include_group_ids=["g-only"], replace=True)
    assert [a["target"]["groupId"] for a in graph.assignments["p-1"]] == ["g-only"]


@pytest.mark.asyncio
async def test_update_settings_replaces_the_policy_whole(graph: FakeGraph) -> None:
    graph.objects[POLICIES]["p-1"] = {"id": "p-1", "name": "P", "platforms": "windows10", "technologies": "mdm"}
    await _call("intune_update_settings_catalog_policy", policy_id="p-1", settings=[{"settingDefinitionId": "x"}])
    (put,) = _sent(graph, "PUT", f"{POLICIES}/p-1")
    assert put["name"] == "P" and put["platforms"] == "windows10"
    assert put["settings"][0]["settingInstance"] == {"settingDefinitionId": "x"}

    await _call("intune_update_settings_catalog_policy", policy_id="p-1", name="Renamed")
    assert _sent(graph, "PATCH", f"{POLICIES}/p-1") == [{"name": "Renamed"}]


# ── Custom configuration profiles ────────────────────────────────────────


@pytest.mark.asyncio
async def test_macos_custom_plist_profile_is_sent_base64(graph: FakeGraph) -> None:
    plist = "<?xml version='1.0'?><plist><dict/></plist>"
    out = await _call(
        "intune_create_config_profile", profile_type="macos_custom_plist", name="Chrome relaunch", plist_xml=plist
    )
    assert out.startswith("Created configuration profile"), out
    (body,) = _sent(graph, "POST", PROFILES)
    assert body["@odata.type"] == "#microsoft.graph.macOSCustomConfiguration"
    assert base64.b64decode(body["payload"]).decode() == plist
    assert body["payloadFileName"] == "Chrome_relaunch.mobileconfig"


@pytest.mark.asyncio
async def test_windows_oma_uri_profile_and_raw_type_check(graph: FakeGraph) -> None:
    oma = [
        {
            "@odata.type": "#microsoft.graph.omaSettingString",
            "omaUri": "./Device/Vendor/MSFT/Policy/Config/X",
            "value": "<enabled/>",
        }
    ]
    await _call(
        "intune_create_config_profile",
        profile_type="windows_custom_oma_uri",
        name="ADMX",
        properties={"omaSettings": oma},
    )
    (body,) = _sent(graph, "POST", PROFILES)
    assert body["@odata.type"] == "#microsoft.graph.windows10CustomConfiguration"
    assert body["omaSettings"] == oma

    out = await _call("intune_create_config_profile", profile_type="raw", name="No type")
    assert out.startswith("Error: a raw profile needs"), out


@pytest.mark.asyncio
async def test_update_profile_keeps_the_derived_type(graph: FakeGraph) -> None:
    graph.objects[PROFILES]["c-1"] = {"id": "c-1", "@odata.type": "#microsoft.graph.macOSCustomConfiguration"}
    await _call("intune_update_config_profile", profile_id="c-1", name="New")
    assert _sent(graph, "PATCH", f"{PROFILES}/c-1") == [
        {"@odata.type": "#microsoft.graph.macOSCustomConfiguration", "displayName": "New"}
    ]


# ── Health scripts and shell scripts ─────────────────────────────────────


@pytest.mark.asyncio
async def test_health_script_create_assign_run_and_read_states(graph: FakeGraph) -> None:
    out = await _call(
        "intune_create_health_script",
        name="Relaunch check",
        detection_script="exit 1",
        remediation_script="Write-Output fixed",
    )
    assert out.startswith("Created device health script"), out
    (body,) = _sent(graph, "POST", HEALTH)
    assert base64.b64decode(body["detectionScriptContent"]).decode() == "exit 1"
    assert base64.b64decode(body["remediationScriptContent"]).decode() == "Write-Output fixed"
    assert body["runAsAccount"] == "system"
    # The read-back never echoes the script bodies.
    assert "exit 1" not in out and "base64 characters" in out
    script_id = next(iter(graph.objects[HEALTH]))

    await _call(
        "intune_assign",
        kind="health_script",
        object_id=script_id,
        include_group_ids=["g-pilot"],
        schedule="daily",
        schedule_time="10:00:00",
    )
    (assign,) = _sent(graph, "POST", f"{HEALTH}/{script_id}/assign")
    (item,) = assign["deviceHealthScriptAssignments"]
    assert item["runRemediationScript"] is True
    assert item["runSchedule"] == {
        "@odata.type": "#microsoft.graph.deviceHealthScriptDailySchedule",
        "interval": 1,
        "useUtc": False,
        "time": "10:00:00",
    }

    out = await _call("intune_run_remediation", device_id="d-1", script_id=script_id)
    assert "requested on device d-1" in out
    assert _sent(graph, "POST", "/beta/deviceManagement/managedDevices/d-1/initiateOnDemandProactiveRemediation") == [
        {"scriptPolicyId": script_id}
    ]

    out = await _call("intune_get_health_script_device_states", script_id=script_id, device_id="d-1")
    assert "Found 1 device run state" in out
    assert "LAPT-0001" in out and "remediationSuccess" in out and "LAPT-0002" not in out


@pytest.mark.asyncio
async def test_shell_script_create_and_assign(graph: FakeGraph) -> None:
    out = await _call("intune_create_shell_script", name="Set pref", script="defaults write x y z")
    assert out.startswith("Created macOS shell script"), out
    (body,) = _sent(graph, "POST", SHELL)
    assert body["fileName"] == "Set_pref.sh"
    assert base64.b64decode(body["scriptContent"]).decode() == "defaults write x y z"
    script_id = next(iter(graph.objects[SHELL]))
    out = await _call("intune_assign", kind="shell_script", object_id=script_id, all_devices=True)
    (assign,) = _sent(graph, "POST", f"{SHELL}/{script_id}/assign")
    assert assign == {
        "deviceManagementScriptAssignments": [
            {"target": {"@odata.type": "#microsoft.graph.allDevicesAssignmentTarget"}}
        ]
    }
    # Graph has no GET route on deviceShellScripts/{id}/assignments: read through $expand.
    assert out.startswith("Assigned macOS shell script"), out
    assert "assignments now (1)" in out and "allDevicesAssignmentTarget" in out, out
    assert not [r for r in graph.requests if r.method == "GET" and r.url.path.endswith("/assignments")]


# ── Assignment read-back, every kind ────────────────────────────────────────────────────


_GET_TOOL = {
    "settings_catalog": (POLICIES, "intune_get_settings_catalog_policy", "policy_id"),
    "config_profile": (PROFILES, "intune_update_config_profile", "profile_id"),
    "health_script": (HEALTH, "intune_update_health_script", "script_id"),
    "shell_script": (SHELL, "intune_update_shell_script", "script_id"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [*_GET_TOOL, "mobile_app"])
async def test_assign_then_read_back_returns_the_target_written(graph: FakeGraph, kind: str) -> None:
    collection = _GET_TOOL[kind][0] if kind in _GET_TOOL else APPS
    oid = "app-1" if kind == "mobile_app" else "obj-1"
    graph.objects[collection].setdefault(oid, {"id": oid, "name": "n", "displayName": "n"})
    out = await _call("intune_assign", kind=kind, object_id=oid, include_group_ids=["g-pilot"])
    assert "assignments now (1)" in out and "g-pilot" in out, out
    if kind == "mobile_app":
        return
    _, tool, arg = _GET_TOOL[kind]
    extra = {} if tool == "intune_get_settings_catalog_policy" else {"description": "d"}
    out = await _call(tool, **{arg: oid}, **extra)
    body = json.loads(out.split("\n", 1)[1])
    assert [a["target"]["groupId"] for a in body["assignments"]] == ["g-pilot"], out


@pytest.mark.asyncio
async def test_settings_catalog_policy_reads_its_assignments_non_empty(graph: FakeGraph) -> None:
    graph.objects[POLICIES]["p-1"] = {"id": "p-1", "name": "Baseline"}
    graph.assignments["p-1"] = [
        {"id": "p-1-a0", "target": {"@odata.type": "#microsoft.graph.allDevicesAssignmentTarget"}}
    ]
    out = await _call("intune_get_settings_catalog_policy", policy_id="p-1")
    body = json.loads(out.split("\n", 1)[1])
    assert body["assignments"][0]["target"]["@odata.type"].endswith("allDevicesAssignmentTarget"), out


@pytest.mark.asyncio
async def test_a_read_back_failure_after_the_post_is_reported_as_assigned(graph: FakeGraph) -> None:
    graph.objects[POLICIES]["p-1"] = {"id": "p-1", "name": "Baseline"}
    graph.no_assignments_route.add(POLICIES)
    out = await _call("intune_assign", kind="settings_catalog", object_id="p-1", all_devices=True, replace=True)
    assert out.startswith("Assigned settings catalog policy p-1 (POST ok), read-back failed: "), out
    assert "No OData route" in out
    assert graph.assignments["p-1"][0]["target"]["@odata.type"].endswith("allDevicesAssignmentTarget")


@pytest.mark.asyncio
async def test_schedule_once_needs_a_date(graph: FakeGraph) -> None:
    graph.objects[HEALTH]["h-1"] = {"id": "h-1"}
    out = await _call("intune_assign", kind="health_script", object_id="h-1", all_devices=True, schedule="once")
    assert out.startswith("Error: schedule 'once' needs schedule_date"), out
    assert not _sent(graph, "POST", f"{HEALTH}/h-1/assign")


# ── Apps ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_assign_with_intent_and_install_status(graph: FakeGraph) -> None:
    out = await _call(
        "intune_assign", kind="mobile_app", object_id="app-1", include_group_ids=["g-1"], intent="available"
    )
    assert "intent available" in out, out
    (assign,) = _sent(graph, "POST", f"{APPS}/app-1/assign")
    assert assign["mobileAppAssignments"] == [
        {
            "@odata.type": "#microsoft.graph.mobileAppAssignment",
            "intent": "available",
            "target": {"@odata.type": "#microsoft.graph.groupAssignmentTarget", "groupId": "g-1"},
        }
    ]
    out = await _call("intune_delete", kind="mobile_app", object_id="app-1")
    assert out.startswith("Error: kind must be one of"), out


@pytest.mark.asyncio
async def test_app_install_status_reads_the_reports_api(graph: FakeGraph) -> None:
    out = await _call("intune_get_app_install_status", app_id="app-1", top=2)
    (sent,) = _sent(graph, "POST", INSTALL_REPORT)
    assert sent["filter"] == "(ApplicationId eq 'app-1')"
    assert sent["top"] == 2 and sent["skip"] == 0
    assert {"DeviceName", "UserPrincipalName", "Platform", "InstallState", "ErrorCode"} <= set(sent["select"])
    assert _sent(graph, "POST", APP_OVERVIEW_REPORT) == [{"filter": "(ApplicationId eq 'app-1')"}]
    assert not [r for r in graph.requests if r.url.path.endswith("/deviceStatuses")]
    assert out.startswith("Summary: installed 1, failed 1, pending 0, not installed 1"), out
    assert "Device install status (devices 1-2 of 3):" in out
    assert "• LAPT-0001 (ID: d-1, macOS) — Installed, version 1.2, user a@example.com" in out
    assert "• LAPT-0002 (ID: d-2, Windows) — Failed, Install failed (error 0x87D1041C), version 1.2" in out
    assert out.endswith("More results: call again with skip=2"), out


@pytest.mark.asyncio
async def test_app_install_status_next_page_has_no_summary(graph: FakeGraph) -> None:
    graph.install_report = {**INSTALL_REPORT_ANSWER, "Values": INSTALL_REPORT_ANSWER["Values"][:1]}
    out = await _call("intune_get_app_install_status", app_id="app-1", skip=2)
    (sent,) = _sent(graph, "POST", INSTALL_REPORT)
    assert sent["skip"] == 2
    assert not _sent(graph, "POST", APP_OVERVIEW_REPORT)
    assert out.startswith("Device install status (devices 3-3 of 3):"), out
    assert "More results" not in out


@pytest.mark.asyncio
async def test_app_install_status_without_rows_and_with_a_failed_summary(graph: FakeGraph) -> None:
    graph.install_report = {"TotalRowCount": 0, "Schema": INSTALL_REPORT_ANSWER["Schema"], "Values": []}
    graph.overview_status = 500
    out = await _call("intune_get_app_install_status", app_id="app-1")
    assert out.startswith("Summary unavailable:"), out
    assert out.endswith("No device reports an install status for this app."), out


@pytest.mark.asyncio
async def test_app_install_status_reports_graph_errors(graph: FakeGraph) -> None:
    graph.install_report_status = 400
    out = await _call("intune_get_app_install_status", app_id="app-1")
    assert out.startswith("Error:"), out
    assert "Invalid filter clause" in out, out
    assert not _sent(graph, "POST", APP_OVERVIEW_REPORT)

    graph.install_report_status = 200
    graph.forbidden.add(INSTALL_REPORT)
    out = await _call("intune_get_app_install_status", app_id="app-1")
    assert out.startswith("Error: Microsoft Intune admin consent required for scope DeviceManagementApps.Read.All"), out

    graph.requests.clear()
    out = await _call("intune_get_app_install_status", app_id="it's")
    assert out.startswith("Error: app_id must be an Intune or Entra object id"), out
    assert not graph.requests


@pytest.mark.asyncio
async def test_app_assignments_all_users_required(graph: FakeGraph) -> None:
    graph.assignments["app-1"] = [
        {
            "id": "a-1",
            "intent": "required",
            "target": {"@odata.type": "#microsoft.graph.allLicensedUsersAssignmentTarget"},
        }
    ]
    out = await _call("intune_get_app_assignments", app_id="app-1")
    assert out == "Company Portal (ID: app-1, macOSLobApp): 1 assignment(s):\n• all users — intent required", out
    assert [r.method for r in graph.requests] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_app_assignments_group_include_and_exclude_available(graph: FakeGraph) -> None:
    graph.assignments["app-1"] = [
        {
            "id": "a-1",
            "intent": "available",
            "target": {
                "@odata.type": "#microsoft.graph.groupAssignmentTarget",
                "groupId": "g-pilot",
                "deviceAndAppManagementAssignmentFilterId": "f-1",
                "deviceAndAppManagementAssignmentFilterType": "include",
            },
        },
        {
            "id": "a-2",
            "intent": "available",
            "target": {"@odata.type": "#microsoft.graph.exclusionGroupAssignmentTarget", "groupId": "g-out"},
        },
    ]
    out = await _call("intune_get_app_assignments", app_id="app-1")
    assert "2 assignment(s)" in out, out
    assert "• group g-pilot (include) — intent available, filter f-1 (include)" in out
    assert "• group g-out (exclude) — intent available" in out


@pytest.mark.asyncio
async def test_app_assignments_none_and_forbidden(graph: FakeGraph) -> None:
    out = await _call("intune_get_app_assignments", app_id="app-1")
    assert out.endswith("not assigned to any group, user or device."), out
    graph.forbidden.add(APPS)
    out = await _call("intune_get_app_assignments", app_id="app-1")
    assert out.startswith("Error: Microsoft Intune admin consent required for scope DeviceManagementApps.Read.All"), out


# ── Detected apps ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detected_apps_filter_by_name(graph: FakeGraph) -> None:
    out = await _call("intune_list_detected_apps", name="chrome")
    assert "Found 2 detected applications" in out and "Firefox" not in out
    out = await _call("intune_list_detected_apps", name="Mozilla Firefox", exact=True)
    assert "Found 1 detected application" in out and "Chrome" not in out
    out = await _call("intune_list_detected_apps")
    assert "Found 3 detected applications" in out


@pytest.mark.asyncio
async def test_detected_apps_name_filter_falls_back_when_graph_refuses_it(graph: FakeGraph) -> None:
    graph.refuse_filter.add(DETECTED)
    out = await _call("intune_list_detected_apps", name="chrome")
    assert "Found 2 detected applications" in out and "Firefox" not in out


@pytest.mark.asyncio
async def test_detected_app_devices_with_versions(graph: FakeGraph) -> None:
    out = await _call("intune_list_detected_app_devices", name="Google Chrome")
    assert out.startswith("Found 3 device(s) across 2 version(s)"), out
    assert "LAPT-0001 (ID: d-1) — Google Chrome 130.0" in out
    assert "LAPT-0003 (ID: d-3) — Google Chrome 129.0" in out
    out = await _call("intune_list_detected_app_devices", app_id="da-2")
    assert "LAPT-0003" in out and "LAPT-0001" not in out


@pytest.mark.asyncio
async def test_next_link_must_page_the_same_collection(graph: FakeGraph) -> None:
    out = await _call("intune_list_detected_apps", next_link="https://evil.example.com/x")
    assert out.startswith("Error: next_link must be a page link"), out
    assert not graph.requests


# ── Errors ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_scope_names_the_permission(graph: FakeGraph) -> None:
    graph.forbidden.add(HEALTH)
    out = await _call("intune_create_health_script", name="x", detection_script="exit 0")
    assert out.startswith(f"Error: Microsoft Intune admin consent required for scope {SCOPE_SCRIPTS}"), out
    assert "Grant admin consent" in out


@pytest.mark.asyncio
async def test_an_id_that_is_not_an_id_never_reaches_graph(graph: FakeGraph) -> None:
    out = await _call("intune_assign", kind="settings_catalog", object_id="../../users", all_devices=True)
    assert out.startswith("Error: settings_catalog id must be an Intune or Entra object id"), out
    out = await _call("intune_assign", kind="settings_catalog", object_id="p-1", include_group_ids=["a/b"])
    assert out.startswith("Error: include_group_ids must be"), out
    assert not graph.requests
