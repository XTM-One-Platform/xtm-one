# SPDX-License-Identifier: Apache-2.0

"""Microsoft Intune: configuration policies, scripts, apps and detected apps.

The tools an agent needs to change what Intune enforces, next to the
per-device tools of ``intune``:

- settings catalog policies (``configurationPolicies``, Graph beta) and custom
  configuration profiles (``deviceConfigurations``: a macOS custom plist, a
  Windows OMA-URI profile, ADMX-backed settings included, or any other type
  given whole): create, update, read back, assign, delete;
- device health scripts (remediations, Graph beta ``deviceHealthScripts``) and
  macOS shell scripts (``deviceShellScripts``): create, update, assign, delete,
  run a remediation on demand on one device, read the per-device run states;
- apps: list the mobile apps, assign one to groups, read its assignments and its
  per-device install status (the Intune reports API, which serves every app type);
- detected apps: a name filter (contains or exact) with paging, and the devices
  of one detected app with the version each runs.

Every write is named with a write verb (create, update, assign, run, delete),
so the read/write classifier withholds it on a read-only binding. Every write
is read back. A Graph 403 answers which permission needs an admin consent
(``_consent_error``), never a bare 403.
"""

from __future__ import annotations

import base64
import functools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    AutoRefreshClient,
    clip_text,
    format_entity_list,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Microsoft Intune"
_GRAPH = "https://graph.microsoft.com"
_V1 = f"{_GRAPH}/v1.0"
_BETA = f"{_GRAPH}/beta"

#: Graph permissions the tools need, named in the consent error.
SCOPE_CONFIGURATION = "DeviceManagementConfiguration.ReadWrite.All"
SCOPE_SCRIPTS = "DeviceManagementScripts.ReadWrite.All"
SCOPE_APPS = "DeviceManagementApps.ReadWrite.All"
SCOPE_APPS_READ = "DeviceManagementApps.Read.All"
SCOPE_PRIVILEGED = "DeviceManagementManagedDevices.PrivilegedOperations.All"
SCOPE_DEVICES_READ = "DeviceManagementManagedDevices.Read.All"

#: An Intune or Entra object id: it goes in a URL path, so nothing else passes.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: Pages read when Graph refuses a name ``$filter`` and the match runs here.
_SCAN_PAGES = 20
#: Detected apps whose devices one call lists, and devices per app.
_MAX_APP_VERSIONS = 20
_MAX_DEVICES = 500
#: Characters of a read-back JSON body shown in a tool result.
_READ_BACK_CHARS = 6000

#: Intune reports API: per-device install status and the per-app overview counts.
#: ``mobileApps/{id}/deviceStatuses`` is not served on most app types ("Resource not
#: found for the segment 'deviceStatuses'"), the reports are, for every type.
_INSTALL_REPORT = f"{_BETA}/deviceManagement/reports/getDeviceInstallStatusReport"
_APP_OVERVIEW_REPORT = f"{_BETA}/deviceManagement/reports/getAppStatusOverviewReport"
_INSTALL_COLUMNS = (
    "DeviceName",
    "UserPrincipalName",
    "Platform",
    "AppVersion",
    "InstallState",
    "InstallStateDetail",
    "ErrorCode",
    "HexErrorCode",
    "LastModifiedDateTime",
    "DeviceId",
)
#: Count columns of ``getAppStatusOverviewReport``, as an agent reads them.
_OVERVIEW_WORDS = {
    "InstalledDeviceCount": "installed",
    "FailedDeviceCount": "failed",
    "PendingInstallDeviceCount": "pending",
    "NotInstalledDeviceCount": "not installed",
    "NotApplicableDeviceCount": "not applicable",
}
#: Assignment target types, as an agent reads them.
_TARGET_NAMES = {
    "#microsoft.graph.allLicensedUsersAssignmentTarget": "all users",
    "#microsoft.graph.allDevicesAssignmentTarget": "all devices",
    "#microsoft.graph.groupAssignmentTarget": "group",
    "#microsoft.graph.exclusionGroupAssignmentTarget": "group",
}


@dataclass(frozen=True)
class _Kind:
    """An assignable Intune object: where it lives and how it is assigned."""

    base: str
    path: str
    assign_key: str
    scope: str
    noun: str
    #: Keys of an assignment kept when the existing ones are merged.
    assignment_keys: tuple[str, ...] = ("target",)
    deletable: bool = True
    #: How Graph serves the object's assignments: ``collection`` (GET ``{id}/assignments``)
    #: or ``expand`` (GET ``{id}?$expand=assignments``, the kind having no GET route on
    #: the navigation).
    assignments_read: str = "collection"


_KINDS: dict[str, _Kind] = {
    "settings_catalog": _Kind(
        _BETA, "deviceManagement/configurationPolicies", "assignments", SCOPE_CONFIGURATION, "settings catalog policy"
    ),
    "config_profile": _Kind(
        _V1, "deviceManagement/deviceConfigurations", "assignments", SCOPE_CONFIGURATION, "configuration profile"
    ),
    "health_script": _Kind(
        _BETA,
        "deviceManagement/deviceHealthScripts",
        "deviceHealthScriptAssignments",
        SCOPE_SCRIPTS,
        "device health script",
        ("target", "runRemediationScript", "runSchedule"),
    ),
    "shell_script": _Kind(
        _BETA,
        "deviceManagement/deviceShellScripts",
        "deviceManagementScriptAssignments",
        SCOPE_SCRIPTS,
        "macOS shell script",
        assignments_read="expand",
    ),
    "mobile_app": _Kind(
        _V1,
        "deviceAppManagement/mobileApps",
        "mobileAppAssignments",
        SCOPE_APPS,
        "app",
        ("@odata.type", "intent", "target", "settings"),
        deletable=False,
    ),
}

_DELETABLE_KINDS = [k for k, v in _KINDS.items() if v.deletable]


class _ToolError(Exception):
    """A refusal or a Graph failure, its message starting with ``Error: ``."""


def _check_id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _ID_RE.match(value):
        raise _ToolError(f"Error: {what} must be an Intune or Entra object id (letters, digits, '-' or '_'), got {value!r}.")
    return value


def _odata_quote(value: str) -> str:
    return value.replace("'", "''")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _graph_message(resp: Any) -> str:
    try:
        error = resp.json().get("error")
    except (ValueError, AttributeError):
        return ""
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:300]
    return ""


def _consent_error(action: str, scope: str, resp: Any) -> str:
    said = _graph_message(resp)
    return (
        f"Error: {_PROVIDER} admin consent required for scope {scope} to {action} (HTTP 403). "
        f"The integration's token does not carry the Microsoft Graph permission {scope}, or the "
        "signed-in account holds no Intune role allowing it. A tenant admin adds the delegated "
        f"permission {scope} to the app registration of this integration (Microsoft Entra admin "
        "center > App registrations > the app > API permissions), clicks 'Grant admin consent', "
        "then the integration is reconnected so its token carries it."
        + (f" Graph said: {said}" if said else "")
    )


async def _graph(
    client: AutoRefreshClient,
    method: str,
    url: str,
    action: str,
    scope: str,
    **kwargs: Any,
) -> Any:
    """One Graph call: its JSON body (``{}`` when empty), else ``_ToolError``.

    Reads retry the transient gateway statuses; writes never do.
    """
    if method == "get":
        kwargs.setdefault("retry_statuses", TRANSIENT_RETRY_STATUSES)
    resp, err = await safe_request(client, method, url, _PROVIDER, action, expected_statuses=set(), **kwargs)
    if err:
        if resp is not None and resp.status_code == 403:
            raise _ToolError(_consent_error(action, scope, resp))
        raise _ToolError(err)
    if resp is None or resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def _same_collection(link: str, collection_url: str) -> bool:
    """Whether *link* (an ``@odata.nextLink``) pages *collection_url*, nothing else."""
    return link == collection_url or link.startswith(f"{collection_url}?")


async def _list_by_name(
    client: AutoRefreshClient,
    url: str,
    action: str,
    scope: str,
    *,
    field: str,
    name: str,
    exact: bool,
    top: int,
    next_link: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """Items of *url* whose *field* matches *name*, and the next page link.

    The match goes to Graph as ``$filter`` (``eq`` or ``contains``); when Graph
    refuses that filter (HTTP 400, several Intune collections do), the pages are
    read here and matched case-insensitively, at most ``_SCAN_PAGES`` of them.
    """
    top = max(1, min(top, 100))
    if next_link:
        if not _same_collection(next_link, url):
            raise _ToolError(f"Error: next_link must be a page link of {url}, as a previous call answered it.")
        data = await _graph(client, "get", next_link, action, scope)
        return data.get("value", []), data.get("@odata.nextLink", "")
    if not name:
        data = await _graph(client, "get", url, action, scope, params={"$top": top})
        return data.get("value", []), data.get("@odata.nextLink", "")
    quoted = _odata_quote(name)
    clause = f"{field} eq '{quoted}'" if exact else f"contains({field},'{quoted}')"
    resp, _err = await safe_request(
        client,
        "get",
        url,
        _PROVIDER,
        action,
        expected_statuses={400},
        retry_statuses=TRANSIENT_RETRY_STATUSES,
        params={"$filter": clause, "$top": top},
    )
    if resp is not None and resp.is_success:
        data = resp.json()
        return data.get("value", []), data.get("@odata.nextLink", "")
    if resp is not None and resp.status_code == 403:
        raise _ToolError(_consent_error(action, scope, resp))
    if resp is None or resp.status_code != 400:
        raise _ToolError(_err or f"Error: {_PROVIDER} could not {action}.")
    wanted = name.casefold()
    found: list[dict[str, Any]] = []
    page_url: str = url
    params: dict[str, Any] | None = {"$top": 100}
    for _ in range(_SCAN_PAGES):
        data = await _graph(client, "get", page_url, action, scope, params=params)
        for item in data.get("value", []):
            value = str(item.get(field) or "").casefold()
            if value == wanted if exact else wanted in value:
                found.append(item)
        page_url, params = data.get("@odata.nextLink", ""), None
        if not page_url or len(found) >= top:
            break
    return found[:top], ""


def _targets(
    include_group_ids: list[str] | None,
    exclude_group_ids: list[str] | None,
    all_devices: bool,
    all_users: bool,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for gid in include_group_ids or []:
        targets.append(
            {"@odata.type": "#microsoft.graph.groupAssignmentTarget", "groupId": _check_id(gid, "include_group_ids")}
        )
    for gid in exclude_group_ids or []:
        targets.append(
            {
                "@odata.type": "#microsoft.graph.exclusionGroupAssignmentTarget",
                "groupId": _check_id(gid, "exclude_group_ids"),
            }
        )
    if all_devices:
        targets.append({"@odata.type": "#microsoft.graph.allDevicesAssignmentTarget"})
    if all_users:
        targets.append({"@odata.type": "#microsoft.graph.allLicensedUsersAssignmentTarget"})
    return targets


def _target_key(target: dict[str, Any]) -> tuple[str, str]:
    return (str(target.get("@odata.type", "")), str(target.get("groupId", "")))


def _health_schedule(schedule: str, interval: int, time: str, date: str, use_utc: bool) -> dict[str, Any] | None:
    if not schedule:
        return None
    interval = max(1, interval)
    if schedule == "hourly":
        return {"@odata.type": "#microsoft.graph.deviceHealthScriptHourlySchedule", "interval": interval}
    if schedule == "daily":
        return {
            "@odata.type": "#microsoft.graph.deviceHealthScriptDailySchedule",
            "interval": interval,
            "useUtc": use_utc,
            "time": time or "09:00:00",
        }
    if schedule == "once":
        if not date:
            raise _ToolError("Error: schedule 'once' needs schedule_date (YYYY-MM-DD).")
        return {
            "@odata.type": "#microsoft.graph.deviceHealthScriptRunOnceSchedule",
            "interval": interval,
            "useUtc": use_utc,
            "time": time or "09:00:00",
            "date": date,
        }
    raise _ToolError(f"Error: schedule must be hourly, daily or once, got {schedule!r}.")


def _assignment(kind: str, target: dict[str, Any], *, intent: str, run_remediation: bool, schedule: Any) -> dict[str, Any]:
    if kind == "mobile_app":
        return {"@odata.type": "#microsoft.graph.mobileAppAssignment", "intent": intent, "target": target}
    if kind == "health_script":
        item: dict[str, Any] = {"target": target, "runRemediationScript": run_remediation}
        if schedule is not None:
            item["runSchedule"] = schedule
        return item
    return {"target": target}


def _setting_items(settings: list[Any]) -> list[dict[str, Any]]:
    """Settings catalog items: a bare ``settingInstance`` is wrapped in its setting."""
    items: list[dict[str, Any]] = []
    for entry in settings:
        if not isinstance(entry, dict):
            raise _ToolError("Error: every entry of settings must be a JSON object.")
        if "settingInstance" in entry:
            items.append({"@odata.type": "#microsoft.graph.deviceManagementConfigurationSetting", **entry})
        else:
            items.append(
                {"@odata.type": "#microsoft.graph.deviceManagementConfigurationSetting", "settingInstance": entry}
            )
    return items


def _read_back(title: str, body: dict[str, Any], read_call: str) -> str:
    text = json.dumps(body, indent=2, default=str)
    return f"{title}\n{clip_text(text, _READ_BACK_CHARS, read_call)}"


def _fmt_named(item: dict[str, Any]) -> str:
    name = item.get("displayName") or item.get("name") or "Unnamed"
    extra = item.get("platforms") or item.get("@odata.type", "")
    modified = item.get("lastModifiedDateTime", "?")
    return f"• {name} (ID: {item.get('id', '?')}) — {extra}, modified {modified}".replace(" — , ", " — ")


def _fmt_detected_app(a: dict[str, Any]) -> str:
    name = a.get("displayName", "Unknown")
    version = a.get("version", "?")
    count = a.get("deviceCount", 0)
    return f"• {name} v{version} — {count} device(s) (ID: {a.get('id', '?')})"


def _fmt_run_state(s: dict[str, Any]) -> str:
    device = s.get("managedDevice") or {}
    name = device.get("deviceName", "?")
    did = device.get("id", "?")
    out = s.get("postRemediationDetectionScriptOutput") or s.get("preRemediationDetectionScriptOutput") or ""
    line = (
        f"• {name} (ID: {did}) — detection: {s.get('detectionState', '?')}, "
        f"remediation: {s.get('remediationState', '?')}, updated {s.get('lastStateUpdateDateTime', '?')}"
    )
    return f"{line}\n  Output: {clip_text(str(out), 300)}" if out else line


def _report_rows(report: Any) -> tuple[list[dict[str, Any]], int | None]:
    """The rows of an Intune report (``Schema`` + ``Values``) as dicts, and its total.

    A ``<Column>_loc`` column (the localised text of an enum) wins over the raw value.
    """
    if not isinstance(report, dict):
        return [], None
    columns = [str(c.get("Column", "")) for c in report.get("Schema") or [] if isinstance(c, dict)]
    rows: list[dict[str, Any]] = []
    for values in report.get("Values") or []:
        if not isinstance(values, list):
            continue
        row = dict(zip(columns, values, strict=False))
        for column in columns:
            if column.endswith("_loc") and row.get(column) not in (None, ""):
                row[column.removesuffix("_loc")] = row[column]
        rows.append(row)
    total = report.get("TotalRowCount")
    return rows, total if isinstance(total, int) else None


def _fmt_install_row(r: dict[str, Any]) -> str:
    code = r.get("HexErrorCode") or r.get("ErrorCode")
    error = f" (error {code})" if code not in (None, "", 0, "0", "0x0") else ""
    detail = r.get("InstallStateDetail")
    detail_text = f", {detail}" if detail not in (None, "", 0, "0", "None") else ""
    version = f", version {r['AppVersion']}" if r.get("AppVersion") else ""
    return (
        f"• {r.get('DeviceName') or '?'} (ID: {r.get('DeviceId') or '?'}, {r.get('Platform') or '?'}) — "
        f"{r.get('InstallState') or '?'}{detail_text}{error}{version}, user {r.get('UserPrincipalName') or '?'}, "
        f"last modified {r.get('LastModifiedDateTime') or '?'}"
    )


def _fmt_overview(rows: list[dict[str, Any]]) -> str:
    """The count columns of the app overview report, in its own order."""
    if not rows:
        return ""
    counts = [
        f"{_OVERVIEW_WORDS.get(k, k.removesuffix('Count'))} {v}"
        for k, v in rows[0].items()
        if k.endswith("Count") and v is not None
    ]
    return "Summary: " + ", ".join(counts) if counts else ""


def _fmt_assignment(a: dict[str, Any]) -> str:
    target = a.get("target") or {}
    odata = str(target.get("@odata.type", ""))
    name = _TARGET_NAMES.get(odata, odata.rsplit(".", 1)[-1] or "?")
    if target.get("groupId"):
        mode = "exclude" if "exclusion" in odata.lower() else "include"
        name = f"{name} {target['groupId']} ({mode})"
    line = f"• {name} — intent {a.get('intent') or '?'}"
    if target.get("deviceAndAppManagementAssignmentFilterId"):
        filter_type = target.get("deviceAndAppManagementAssignmentFilterType") or "?"
        line += f", filter {target['deviceAndAppManagementAssignmentFilterId']} ({filter_type})"
    return line


def _with_next(text: str, next_link: str) -> str:
    return f"{text}\nMore results: call again with next_link={next_link}" if next_link else text


def management_tools(
    client_factory: Callable[[], AutoRefreshClient],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Register the configuration, script, app and detected-app tools on *executor*."""

    def _url(kind: str, object_id: str = "", suffix: str = "") -> str:
        spec = _KINDS[kind]
        url = f"{spec.base}/{spec.path}"
        if object_id:
            url = f"{url}/{_check_id(object_id, f'{kind} id')}"
        return f"{url}/{suffix}" if suffix else url

    def _tool(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register *fn* under its name; a ``_ToolError`` becomes its ``Error: `` answer."""

        @functools.wraps(fn)
        async def wrapped(**kwargs: Any) -> str:
            try:
                return str(await fn(**kwargs))
            except _ToolError as exc:
                return str(exc)

        executor.register(fn.__name__, wrapped)
        return fn

    # ── Detected apps ────────────────────────────────────────────────────

    detected_url = f"{_V1}/deviceManagement/detectedApps"

    @_tool
    async def intune_list_detected_apps(top: int = 50, name: str = "", exact: bool = False, next_link: str = "") -> str:
        async with client_factory() as client:
            apps, nxt = await _list_by_name(
                client,
                detected_url,
                "list detected applications",
                SCOPE_DEVICES_READ,
                field="displayName",
                name=name.strip(),
                exact=exact,
                top=top,
                next_link=next_link.strip(),
            )
        return _with_next(format_entity_list(apps, _fmt_detected_app, noun="detected application"), nxt)

    @_tool
    async def intune_list_detected_app_devices(app_id: str = "", name: str = "") -> str:
        async with client_factory() as client:
            if app_id:
                app = await _graph(
                    client,
                    "get",
                    f"{detected_url}/{_check_id(app_id, 'app_id')}",
                    "get a detected app",
                    SCOPE_DEVICES_READ,
                )
                apps = [app]
            elif name.strip():
                apps, _ = await _list_by_name(
                    client,
                    detected_url,
                    "list detected applications",
                    SCOPE_DEVICES_READ,
                    field="displayName",
                    name=name.strip(),
                    exact=True,
                    top=_MAX_APP_VERSIONS,
                )
            else:
                raise _ToolError("Error: give app_id or name.")
            lines: list[str] = []
            for app in apps[:_MAX_APP_VERSIONS]:
                url = f"{detected_url}/{_check_id(str(app.get('id', '')), 'app id')}/managedDevices"
                params: dict[str, Any] | None = {"$select": "id,deviceName,userPrincipalName,operatingSystem"}
                seen = 0
                while url and seen < _MAX_DEVICES:
                    data = await _graph(
                        client, "get", url, "list the devices of a detected app", SCOPE_DEVICES_READ, params=params
                    )
                    for d in data.get("value", []):
                        seen += 1
                        lines.append(
                            f"• {d.get('deviceName', '?')} (ID: {d.get('id', '?')}) — "
                            f"{app.get('displayName', '?')} {app.get('version', '?')}, "
                            f"{d.get('operatingSystem', '?')}, owner {d.get('userPrincipalName', 'unassigned')}"
                        )
                    url, params = data.get("@odata.nextLink", ""), None
        if not apps:
            return f"No detected application named {name!r}."
        if not lines:
            return "No device reports this application."
        return f"Found {len(lines)} device(s) across {len(apps)} version(s):\n" + "\n".join(lines)

    # ── Assignments, read with the route Graph serves for each kind ─────

    async def _read_assignments(
        client: AutoRefreshClient, kind: str, object_id: str, action: str, scope: str = ""
    ) -> list[Any]:
        """The assignments of one object, through the route Graph serves for *kind*.

        ``deviceShellScripts`` has no GET route on ``/assignments`` ("No OData route
        exists"): it is read with ``$expand=assignments`` on the object. The other kinds
        read the ``/assignments`` collection, which Graph always fills, where an
        ``$expand=assignments`` beside another expand comes back empty on a settings
        catalog policy. *scope* is the permission a 403 names (default: the kind's).
        """
        spec = _KINDS[kind]
        scope = scope or spec.scope
        if spec.assignments_read == "expand":
            obj = await _graph(
                client, "get", _url(kind, object_id), action, scope, params={"$expand": "assignments"}
            )
            return list(obj.get("assignments") or [])
        data = await _graph(client, "get", _url(kind, object_id, "assignments"), action, scope)
        return list(data.get("value") or [])

    async def _object_with_assignments(
        client: AutoRefreshClient, kind: str, object_id: str, action: str, expand: str = "", scope: str = ""
    ) -> dict[str, Any]:
        """One object with its ``assignments`` filled, never an empty list by omission."""
        spec = _KINDS[kind]
        scope = scope or spec.scope
        if spec.assignments_read == "expand":
            expand = ",".join(p for p in (expand, "assignments") if p)
        kwargs: dict[str, Any] = {"params": {"$expand": expand}} if expand else {}
        obj: dict[str, Any] = await _graph(client, "get", _url(kind, object_id), action, scope, **kwargs)
        if spec.assignments_read == "expand":
            obj["assignments"] = list(obj.get("assignments") or [])
        else:
            obj["assignments"] = await _read_assignments(client, kind, object_id, f"{action} assignments", scope)
        return obj

    # ── Settings catalog policies ───────────────────────────────────────

    async def _policy_read_back(client: AutoRefreshClient, policy_id: str, title: str) -> str:
        policy = await _object_with_assignments(
            client, "settings_catalog", policy_id, "read back the settings catalog policy", expand="settings"
        )
        return _read_back(title, policy, f"intune_get_settings_catalog_policy(policy_id='{policy_id}')")

    @_tool
    async def intune_list_settings_catalog_policies(name: str = "", top: int = 50, next_link: str = "") -> str:
        async with client_factory() as client:
            items, nxt = await _list_by_name(
                client,
                _url("settings_catalog"),
                "list settings catalog policies",
                SCOPE_CONFIGURATION,
                field="name",
                name=name.strip(),
                exact=False,
                top=top,
                next_link=next_link.strip(),
            )
        return _with_next(format_entity_list(items, _fmt_named, noun="settings catalog policy"), nxt)

    @_tool
    async def intune_get_settings_catalog_policy(policy_id: str) -> str:
        async with client_factory() as client:
            return await _policy_read_back(client, policy_id, "Settings catalog policy:")

    @_tool
    async def intune_create_settings_catalog_policy(
        name: str,
        platforms: str,
        settings: list[Any],
        description: str = "",
        technologies: str = "mdm",
        role_scope_tag_ids: list[str] | None = None,
    ) -> str:
        body = {
            "name": name,
            "description": description,
            "platforms": platforms,
            "technologies": technologies,
            "roleScopeTagIds": role_scope_tag_ids or ["0"],
            "settings": _setting_items(settings),
        }
        async with client_factory() as client:
            created = await _graph(
                client, "post", _url("settings_catalog"), "create a settings catalog policy", SCOPE_CONFIGURATION, json=body
            )
            return await _policy_read_back(client, str(created.get("id", "")), f"Created settings catalog policy {created.get('id')}:")

    @_tool
    async def intune_update_settings_catalog_policy(
        policy_id: str,
        name: str = "",
        description: str | None = None,
        settings: list[Any] | None = None,
    ) -> str:
        async with client_factory() as client:
            url = _url("settings_catalog", policy_id)
            if settings is None:
                patch: dict[str, Any] = {}
                if name:
                    patch["name"] = name
                if description is not None:
                    patch["description"] = description
                if not patch:
                    raise _ToolError("Error: nothing to update: give name, description or settings.")
                await _graph(client, "patch", url, "update the settings catalog policy", SCOPE_CONFIGURATION, json=patch)
            else:
                # Graph replaces a policy's settings only through a PUT of the whole policy.
                current = await _graph(client, "get", url, "read the settings catalog policy", SCOPE_CONFIGURATION)
                body = {
                    "name": name or current.get("name", ""),
                    "description": current.get("description", "") if description is None else description,
                    "platforms": current.get("platforms"),
                    "technologies": current.get("technologies"),
                    "roleScopeTagIds": current.get("roleScopeTagIds") or ["0"],
                    "settings": _setting_items(settings),
                }
                if current.get("templateReference"):
                    body["templateReference"] = current["templateReference"]
                await _graph(client, "put", url, "replace the settings catalog policy", SCOPE_CONFIGURATION, json=body)
            return await _policy_read_back(client, policy_id, f"Updated settings catalog policy {policy_id}:")

    # ── Custom configuration profiles ───────────────────────────────────

    def _profile_body(
        profile_type: str, name: str, description: str, properties: dict[str, Any] | None, plist_xml: str, payload_name: str
    ) -> dict[str, Any]:
        body: dict[str, Any] = dict(properties or {})
        if name:
            body["displayName"] = name
        if description:
            body["description"] = description
        if profile_type == "macos_custom_plist":
            body["@odata.type"] = "#microsoft.graph.macOSCustomConfiguration"
            if plist_xml:
                body["payload"] = _b64(plist_xml)
                body["payloadName"] = payload_name or name
                body.setdefault("payloadFileName", f"{(payload_name or name or 'payload').replace(' ', '_')}.mobileconfig")
        elif profile_type == "windows_custom_oma_uri":
            body["@odata.type"] = "#microsoft.graph.windows10CustomConfiguration"
        elif profile_type != "raw":
            raise _ToolError("Error: profile_type must be macos_custom_plist, windows_custom_oma_uri or raw.")
        if not str(body.get("@odata.type", "")).startswith("#microsoft.graph."):
            raise _ToolError("Error: a raw profile needs properties['@odata.type'], e.g. '#microsoft.graph.iosCustomConfiguration'.")
        return body

    async def _profile_read_back(client: AutoRefreshClient, profile_id: str, title: str) -> str:
        profile = await _object_with_assignments(
            client, "config_profile", profile_id, "read back the configuration profile"
        )
        return _read_back(title, profile, f"intune_get_config_profile(profile_id='{profile_id}')")

    @_tool
    async def intune_create_config_profile(
        profile_type: str,
        name: str,
        description: str = "",
        plist_xml: str = "",
        payload_name: str = "",
        properties: dict[str, Any] | None = None,
    ) -> str:
        if not name:
            raise _ToolError("Error: name is required.")
        body = _profile_body(profile_type, name, description, properties, plist_xml, payload_name)
        if profile_type == "macos_custom_plist" and "payload" not in body:
            raise _ToolError("Error: a macos_custom_plist profile needs plist_xml.")
        async with client_factory() as client:
            created = await _graph(
                client, "post", _url("config_profile"), "create a configuration profile", SCOPE_CONFIGURATION, json=body
            )
            pid = str(created.get("id", ""))
            return await _profile_read_back(client, pid, f"Created configuration profile {pid}:")

    @_tool
    async def intune_update_config_profile(
        profile_id: str,
        name: str = "",
        description: str = "",
        plist_xml: str = "",
        payload_name: str = "",
        properties: dict[str, Any] | None = None,
    ) -> str:
        async with client_factory() as client:
            url = _url("config_profile", profile_id)
            current = await _graph(client, "get", url, "read the configuration profile", SCOPE_CONFIGURATION)
            odata_type = str(current.get("@odata.type", ""))
            body = dict(properties or {})
            body["@odata.type"] = odata_type  # Graph needs the derived type on a PATCH
            if name:
                body["displayName"] = name
            if description:
                body["description"] = description
            if plist_xml:
                body["payload"] = _b64(plist_xml)
                if payload_name:
                    body["payloadName"] = payload_name
            if len(body) == 1:
                raise _ToolError("Error: nothing to update: give name, description, plist_xml or properties.")
            await _graph(client, "patch", url, "update the configuration profile", SCOPE_CONFIGURATION, json=body)
            return await _profile_read_back(client, profile_id, f"Updated configuration profile {profile_id}:")

    # ── Device health scripts (remediations) and macOS shell scripts ────

    async def _script_read_back(client: AutoRefreshClient, kind: str, script_id: str, title: str) -> str:
        script = await _object_with_assignments(client, kind, script_id, f"read back the {_KINDS[kind].noun}")
        for key in ("detectionScriptContent", "remediationScriptContent", "scriptContent"):
            if script.get(key):
                script[key] = f"<{len(script[key])} base64 characters>"
        return _read_back(title, script, "")

    def _health_body(
        name: str,
        description: str,
        publisher: str,
        detection_script: str,
        remediation_script: str,
        run_as_account: str,
        run_as_32_bit: bool | None,
        enforce_signature_check: bool | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"@odata.type": "#microsoft.graph.deviceHealthScript"}
        if name:
            body["displayName"] = name
        if description:
            body["description"] = description
        if publisher:
            body["publisher"] = publisher
        if detection_script:
            body["detectionScriptContent"] = _b64(detection_script)
        if remediation_script:
            body["remediationScriptContent"] = _b64(remediation_script)
        if run_as_account:
            body["runAsAccount"] = run_as_account
        if run_as_32_bit is not None:
            body["runAs32Bit"] = run_as_32_bit
        if enforce_signature_check is not None:
            body["enforceSignatureCheck"] = enforce_signature_check
        return body

    @_tool
    async def intune_list_health_scripts(name: str = "", top: int = 50, next_link: str = "") -> str:
        async with client_factory() as client:
            items, nxt = await _list_by_name(
                client,
                _url("health_script"),
                "list device health scripts",
                SCOPE_SCRIPTS,
                field="displayName",
                name=name.strip(),
                exact=False,
                top=top,
                next_link=next_link.strip(),
            )
        return _with_next(format_entity_list(items, _fmt_named, noun="device health script"), nxt)

    @_tool
    async def intune_create_health_script(
        name: str,
        detection_script: str,
        remediation_script: str = "",
        description: str = "",
        publisher: str = "",
        run_as_account: str = "system",
        run_as_32_bit: bool = False,
        enforce_signature_check: bool = False,
    ) -> str:
        if not name or not detection_script:
            raise _ToolError("Error: name and detection_script are required.")
        body = _health_body(
            name,
            description,
            publisher or "XTM One",
            detection_script,
            remediation_script,
            run_as_account,
            run_as_32_bit,
            enforce_signature_check,
        )
        body["roleScopeTagIds"] = ["0"]
        async with client_factory() as client:
            created = await _graph(client, "post", _url("health_script"), "create a device health script", SCOPE_SCRIPTS, json=body)
            sid = str(created.get("id", ""))
            return await _script_read_back(client, "health_script", sid, f"Created device health script {sid}:")

    @_tool
    async def intune_update_health_script(
        script_id: str,
        name: str = "",
        description: str = "",
        publisher: str = "",
        detection_script: str = "",
        remediation_script: str = "",
        run_as_account: str = "",
        run_as_32_bit: bool | None = None,
        enforce_signature_check: bool | None = None,
    ) -> str:
        body = _health_body(
            name,
            description,
            publisher,
            detection_script,
            remediation_script,
            run_as_account,
            run_as_32_bit,
            enforce_signature_check,
        )
        if len(body) == 1:
            raise _ToolError("Error: nothing to update.")
        async with client_factory() as client:
            await _graph(
                client, "patch", _url("health_script", script_id), "update the device health script", SCOPE_SCRIPTS, json=body
            )
            return await _script_read_back(client, "health_script", script_id, f"Updated device health script {script_id}:")

    def _shell_body(
        name: str, description: str, script: str, run_as_account: str, file_name: str, frequency: str, retry_count: int | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"@odata.type": "#microsoft.graph.deviceShellScript"}
        if name:
            body["displayName"] = name
        if description:
            body["description"] = description
        if script:
            body["scriptContent"] = _b64(script)
        if run_as_account:
            body["runAsAccount"] = run_as_account
        if file_name:
            body["fileName"] = file_name
        if frequency:
            body["executionFrequency"] = frequency
        if retry_count is not None:
            body["retryCount"] = retry_count
        return body

    @_tool
    async def intune_create_shell_script(
        name: str,
        script: str,
        description: str = "",
        run_as_account: str = "system",
        file_name: str = "",
        execution_frequency: str = "",
        retry_count: int = 0,
    ) -> str:
        if not name or not script:
            raise _ToolError("Error: name and script are required.")
        body = _shell_body(
            name,
            description,
            script,
            run_as_account,
            file_name or f"{re.sub(r'[^A-Za-z0-9_-]+', '_', name)}.sh",
            execution_frequency,
            retry_count,
        )
        body["roleScopeTagIds"] = ["0"]
        async with client_factory() as client:
            created = await _graph(client, "post", _url("shell_script"), "create a macOS shell script", SCOPE_SCRIPTS, json=body)
            sid = str(created.get("id", ""))
            return await _script_read_back(client, "shell_script", sid, f"Created macOS shell script {sid}:")

    @_tool
    async def intune_update_shell_script(
        script_id: str,
        name: str = "",
        description: str = "",
        script: str = "",
        run_as_account: str = "",
        file_name: str = "",
        execution_frequency: str = "",
        retry_count: int | None = None,
    ) -> str:
        body = _shell_body(name, description, script, run_as_account, file_name, execution_frequency, retry_count)
        if len(body) == 1:
            raise _ToolError("Error: nothing to update.")
        async with client_factory() as client:
            await _graph(client, "patch", _url("shell_script", script_id), "update the macOS shell script", SCOPE_SCRIPTS, json=body)
            return await _script_read_back(client, "shell_script", script_id, f"Updated macOS shell script {script_id}:")

    @_tool
    async def intune_run_remediation(device_id: str, script_id: str) -> str:
        device = _check_id(device_id, "device_id")
        script = _check_id(script_id, "script_id")
        async with client_factory() as client:
            await _graph(
                client,
                "post",
                f"{_BETA}/deviceManagement/managedDevices/{device}/initiateOnDemandProactiveRemediation",
                "run a remediation on demand",
                SCOPE_PRIVILEGED,
                json={"scriptPolicyId": script},
            )
        return (
            f"On-demand remediation {script} requested on device {device}. The device runs it at its next "
            f"check-in; read the result with intune_get_health_script_device_states(script_id='{script}', device_id='{device}')."
        )

    @_tool
    async def intune_get_health_script_device_states(script_id: str, device_id: str = "", top: int = 50) -> str:
        wanted = _check_id(device_id, "device_id") if device_id else ""
        async with client_factory() as client:
            url = _url("health_script", script_id, "deviceRunStates")
            params: dict[str, Any] | None = {"$expand": "managedDevice($select=id,deviceName)", "$top": 100}
            states: list[dict[str, Any]] = []
            for _ in range(_SCAN_PAGES):
                data = await _graph(client, "get", url, "read the device run states", SCOPE_SCRIPTS, params=params)
                for state in data.get("value", []):
                    if not wanted or (state.get("managedDevice") or {}).get("id") == wanted or str(state.get("id", "")).endswith(wanted):
                        states.append(state)
                url, params = data.get("@odata.nextLink", ""), None
                if not url or len(states) >= max(1, min(top, 500)):
                    break
        return format_entity_list(states[: max(1, min(top, 500))], _fmt_run_state, noun="device run state")

    # ── Apps ─────────────────────────────────────────────────────────────

    @_tool
    async def intune_list_mobile_apps(name: str = "", top: int = 50, next_link: str = "") -> str:
        async with client_factory() as client:
            items, nxt = await _list_by_name(
                client,
                _url("mobile_app"),
                "list apps",
                SCOPE_APPS_READ,
                field="displayName",
                name=name.strip(),
                exact=False,
                top=top,
                next_link=next_link.strip(),
            )
        return _with_next(format_entity_list(items, _fmt_named, noun="app"), nxt)

    @_tool
    async def intune_get_app_install_status(app_id: str, top: int = 100, skip: int = 0) -> str:
        app = _check_id(app_id, "app_id")
        top = max(1, min(top, 500))
        skip = max(0, skip)
        app_filter = f"(ApplicationId eq '{_odata_quote(app)}')"
        async with client_factory() as client:
            report = await _graph(
                client,
                "post",
                _INSTALL_REPORT,
                "read the app install status",
                SCOPE_APPS_READ,
                json={
                    "filter": app_filter,
                    "select": list(_INSTALL_COLUMNS),
                    "orderBy": ["DeviceName"],
                    "top": top,
                    "skip": skip,
                },
            )
            summary = ""
            if skip == 0:
                try:
                    overview = await _graph(
                        client,
                        "post",
                        _APP_OVERVIEW_REPORT,
                        "read the app install overview",
                        SCOPE_APPS_READ,
                        json={"filter": app_filter},
                    )
                    summary = _fmt_overview(_report_rows(overview)[0])
                except _ToolError as exc:
                    # The per-device rows answered: say the summary failed, never hide the rows.
                    summary = f"Summary unavailable: {str(exc).removeprefix('Error: ')}"
        rows, total = _report_rows(report)
        if not rows:
            text = "No device reports an install status for this app." if skip == 0 else "No more devices."
        else:
            shown = f"{skip + 1}-{skip + len(rows)}" + (f" of {total}" if total is not None else "")
            text = f"Device install status (devices {shown}):\n" + "\n".join(_fmt_install_row(r) for r in rows)
            if total is not None and skip + len(rows) < total:
                text += f"\nMore results: call again with skip={skip + len(rows)}"
        return f"{summary}\n{text}" if summary else text

    @_tool
    async def intune_get_app_assignments(app_id: str) -> str:
        async with client_factory() as client:
            app = await _object_with_assignments(client, "mobile_app", app_id, "read the app", scope=SCOPE_APPS_READ)
        kind_name = str(app.get("@odata.type", "")).rsplit(".", 1)[-1] or "app"
        head = f"{app.get('displayName') or 'Unnamed'} (ID: {app.get('id', app_id)}, {kind_name})"
        assignments = app["assignments"]
        if not assignments:
            return f"{head}: not assigned to any group, user or device."
        lines = "\n".join(_fmt_assignment(a) for a in assignments)
        return f"{head}: {len(assignments)} assignment(s):\n{lines}"

    # ── Assign and delete, every kind ───────────────────────────────────

    @_tool
    async def intune_assign(
        kind: str,
        object_id: str,
        include_group_ids: list[str] | None = None,
        exclude_group_ids: list[str] | None = None,
        all_devices: bool = False,
        all_users: bool = False,
        replace: bool = False,
        intent: str = "required",
        run_remediation: bool = True,
        schedule: str = "",
        schedule_interval: int = 1,
        schedule_time: str = "",
        schedule_date: str = "",
        schedule_use_utc: bool = False,
    ) -> str:
        if kind not in _KINDS:
            raise _ToolError(f"Error: kind must be one of {', '.join(_KINDS)}.")
        spec = _KINDS[kind]
        targets = _targets(include_group_ids, exclude_group_ids, all_devices, all_users)
        if not targets:
            raise _ToolError("Error: give include_group_ids, exclude_group_ids, all_devices or all_users.")
        run_schedule = _health_schedule(schedule, schedule_interval, schedule_time, schedule_date, schedule_use_utc)
        new = [
            _assignment(kind, t, intent=intent, run_remediation=run_remediation, schedule=run_schedule) for t in targets
        ]
        async with client_factory() as client:
            merged = list(new)
            if not replace:
                # Graph's assign replaces every assignment: keep the ones the call does not name.
                current = await _read_assignments(client, kind, object_id, f"read the {spec.noun} assignments")
                named = {_target_key(t) for t in targets}
                for existing in current:
                    target = existing.get("target") or {}
                    if _target_key(target) in named:
                        continue
                    merged.append({k: existing[k] for k in spec.assignment_keys if k in existing})
            await _graph(
                client,
                "post",
                _url(kind, object_id, "assign"),
                f"assign the {spec.noun}",
                spec.scope,
                json={spec.assign_key: merged},
            )
            try:
                after = await _read_assignments(client, kind, object_id, f"read back the {spec.noun} assignments")
            except _ToolError as exc:
                # The POST succeeded: say so, never let a failed read hide the write.
                reason = str(exc).removeprefix("Error: ")
                return f"Assigned {spec.noun} {object_id} (POST ok), read-back failed: {reason}"
        lines = []
        for item in after:
            target = item.get("target") or {}
            kind_name = str(target.get("@odata.type", "?")).rsplit(".", 1)[-1]
            extra = f", intent {item['intent']}" if item.get("intent") else ""
            lines.append(f"• {kind_name} {target.get('groupId', '')}{extra}".rstrip())
        return f"Assigned {spec.noun} {object_id}; assignments now ({len(lines)}):\n" + "\n".join(lines)

    @_tool
    async def intune_delete(kind: str, object_id: str) -> str:
        if kind not in _DELETABLE_KINDS:
            raise _ToolError(f"Error: kind must be one of {', '.join(_DELETABLE_KINDS)}.")
        spec = _KINDS[kind]
        url = _url(kind, object_id)
        async with client_factory() as client:
            await _graph(client, "delete", url, f"delete the {spec.noun}", spec.scope)
            resp, _ = await safe_request(
                client, "get", url, _PROVIDER, f"read back the {spec.noun}", expected_statuses={404}
            )
        if resp is not None and resp.status_code == 404:
            return f"Deleted {spec.noun} {object_id} (read back: not found)."
        return f"Delete of {spec.noun} {object_id} accepted; Graph still answers it, read it again shortly."

    return _definitions()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_GROUPS = {"type": "array", "items": {"type": "string"}}
_NEXT_LINK = {"type": "string", "description": "The next_link a previous call answered, to read the next page"}
_TOP = {"type": "integer", "description": "Max results (1-100, default 50)", "default": 50}


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _definitions() -> list[ToolDefinition]:
    s = {"type": "string"}
    return [
        ToolDefinition(
            name="intune_list_detected_apps",
            description=(
                "List applications detected across managed devices (name, version, device count). "
                "Filter by name (contains, or exact=true); one entry per version."
            ),
            input_schema=_obj(
                {
                    "top": _TOP,
                    "name": {"type": "string", "description": "App display name to match"},
                    "exact": {"type": "boolean", "description": "Match the whole name (default: contains)"},
                    "next_link": _NEXT_LINK,
                }
            ),
        ),
        ToolDefinition(
            name="intune_list_detected_app_devices",
            description="List the devices that report one detected app, with the version each runs: by app_id, or every version of an exact app name.",
            input_schema=_obj({"app_id": {**s, "description": "Detected app ID"}, "name": {**s, "description": "Exact app display name"}}),
        ),
        ToolDefinition(
            name="intune_list_settings_catalog_policies",
            description="List settings catalog configuration policies, optionally by name (contains).",
            input_schema=_obj({"name": s, "top": _TOP, "next_link": _NEXT_LINK}),
        ),
        ToolDefinition(
            name="intune_get_settings_catalog_policy",
            description="Get a settings catalog policy with its settings and assignments.",
            input_schema=_obj({"policy_id": s}, ["policy_id"]),
        ),
        ToolDefinition(
            name="intune_create_settings_catalog_policy",
            description=(
                "Create a settings catalog policy and read it back. settings: Graph "
                "deviceManagementConfigurationSetting objects, or their settingInstance alone "
                "(settingDefinitionId plus the value of its type). Assign it with intune_assign."
            ),
            input_schema=_obj(
                {
                    "name": s,
                    "platforms": {**s, "description": "windows10, macOS, iOS, android, linux..."},
                    "settings": {"type": "array", "items": {"type": "object"}},
                    "description": s,
                    "technologies": {**s, "description": "Default mdm"},
                    "role_scope_tag_ids": _GROUPS,
                },
                ["name", "platforms", "settings"],
            ),
        ),
        ToolDefinition(
            name="intune_update_settings_catalog_policy",
            description="Rename or redescribe a settings catalog policy, or replace its settings whole; read back.",
            input_schema=_obj(
                {"policy_id": s, "name": s, "description": s, "settings": {"type": "array", "items": {"type": "object"}}},
                ["policy_id"],
            ),
        ),
        ToolDefinition(
            name="intune_create_config_profile",
            description=(
                "Create a custom configuration profile and read it back: macos_custom_plist (plist_xml), "
                "windows_custom_oma_uri (properties.omaSettings, ADMX-backed settings included), or raw "
                "(properties with its @odata.type). Assign it with intune_assign."
            ),
            input_schema=_obj(
                {
                    "profile_type": {"type": "string", "enum": ["macos_custom_plist", "windows_custom_oma_uri", "raw"]},
                    "name": s,
                    "description": s,
                    "plist_xml": {**s, "description": "The .mobileconfig / plist XML (macOS)"},
                    "payload_name": s,
                    "properties": {"type": "object", "description": "Other deviceConfiguration properties"},
                },
                ["profile_type", "name"],
            ),
        ),
        ToolDefinition(
            name="intune_update_config_profile",
            description="Update a configuration profile (name, description, plist_xml, other properties); read back.",
            input_schema=_obj(
                {
                    "profile_id": s,
                    "name": s,
                    "description": s,
                    "plist_xml": s,
                    "payload_name": s,
                    "properties": {"type": "object"},
                },
                ["profile_id"],
            ),
        ),
        ToolDefinition(
            name="intune_list_health_scripts",
            description="List device health scripts (remediations), optionally by name (contains).",
            input_schema=_obj({"name": s, "top": _TOP, "next_link": _NEXT_LINK}),
        ),
        ToolDefinition(
            name="intune_create_health_script",
            description=(
                "Create a device health script (remediation): PowerShell detection script (exit 1 = issue found) "
                "and optional remediation script; read back. Assign it with intune_assign (kind health_script, schedule)."
            ),
            input_schema=_obj(
                {
                    "name": s,
                    "detection_script": s,
                    "remediation_script": s,
                    "description": s,
                    "publisher": s,
                    "run_as_account": {"type": "string", "enum": ["system", "user"]},
                    "run_as_32_bit": {"type": "boolean"},
                    "enforce_signature_check": {"type": "boolean"},
                },
                ["name", "detection_script"],
            ),
        ),
        ToolDefinition(
            name="intune_update_health_script",
            description="Update a device health script (any field given); read back.",
            input_schema=_obj(
                {
                    "script_id": s,
                    "name": s,
                    "description": s,
                    "publisher": s,
                    "detection_script": s,
                    "remediation_script": s,
                    "run_as_account": {"type": "string", "enum": ["system", "user"]},
                    "run_as_32_bit": {"type": "boolean"},
                    "enforce_signature_check": {"type": "boolean"},
                },
                ["script_id"],
            ),
        ),
        ToolDefinition(
            name="intune_create_shell_script",
            description="Create a macOS shell script; read back. Assign it with intune_assign (kind shell_script).",
            input_schema=_obj(
                {
                    "name": s,
                    "script": s,
                    "description": s,
                    "run_as_account": {"type": "string", "enum": ["system", "user"]},
                    "file_name": s,
                    "execution_frequency": {**s, "description": "ISO 8601 duration, e.g. PT1H; empty: run once"},
                    "retry_count": {"type": "integer"},
                },
                ["name", "script"],
            ),
        ),
        ToolDefinition(
            name="intune_update_shell_script",
            description="Update a macOS shell script (any field given); read back.",
            input_schema=_obj(
                {
                    "script_id": s,
                    "name": s,
                    "description": s,
                    "script": s,
                    "run_as_account": {"type": "string", "enum": ["system", "user"]},
                    "file_name": s,
                    "execution_frequency": s,
                    "retry_count": {"type": "integer"},
                },
                ["script_id"],
            ),
        ),
        ToolDefinition(
            name="intune_run_remediation",
            description="Run a device health script on demand on one managed device (at its next check-in).",
            input_schema=_obj({"device_id": s, "script_id": s}, ["device_id", "script_id"]),
        ),
        ToolDefinition(
            name="intune_get_health_script_device_states",
            description="Read the per-device detection and remediation states of a device health script, optionally for one device.",
            input_schema=_obj({"script_id": s, "device_id": s, "top": _TOP}, ["script_id"]),
        ),
        ToolDefinition(
            name="intune_list_mobile_apps",
            description="List the apps Intune manages (mobileApps), optionally by name (contains).",
            input_schema=_obj({"name": s, "top": _TOP, "next_link": _NEXT_LINK}),
        ),
        ToolDefinition(
            name="intune_get_app_install_status",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description="Read an app's install summary and per-device install status (Intune reports API, every app type).",
            input_schema=_obj(
                {
                    "app_id": s,
                    "top": {"type": "integer", "description": "Devices per page (1-500, default 100)", "default": 100},
                    "skip": {"type": "integer", "description": "Devices to skip, from a previous answer"},
                },
                ["app_id"],
            ),
        ),
        ToolDefinition(
            name="intune_get_app_assignments",
            description="Read an app's assignments: all users, all devices or groups (include / exclude), intent, filter.",
            input_schema=_obj({"app_id": s}, ["app_id"]),
        ),
        ToolDefinition(
            name="intune_assign",
            description=(
                "Assign a policy, profile, script or app to Entra groups (include / exclude), all devices or all users, "
                "and read the assignments back. Existing assignments are kept unless replace=true."
            ),
            input_schema=_obj(
                {
                    "kind": {"type": "string", "enum": list(_KINDS)},
                    "object_id": s,
                    "include_group_ids": _GROUPS,
                    "exclude_group_ids": _GROUPS,
                    "all_devices": {"type": "boolean"},
                    "all_users": {"type": "boolean"},
                    "replace": {"type": "boolean", "description": "Drop the assignments not named (default false)"},
                    "intent": {
                        "type": "string",
                        "enum": ["required", "available", "uninstall", "availableWithoutEnrollment"],
                        "description": "mobile_app only",
                    },
                    "run_remediation": {"type": "boolean", "description": "health_script only (default true)"},
                    "schedule": {"type": "string", "enum": ["hourly", "daily", "once"], "description": "health_script only"},
                    "schedule_interval": {"type": "integer"},
                    "schedule_time": {**s, "description": "HH:MM:SS"},
                    "schedule_date": {**s, "description": "YYYY-MM-DD (once)"},
                    "schedule_use_utc": {"type": "boolean"},
                },
                ["kind", "object_id"],
            ),
        ),
        ToolDefinition(
            name="intune_delete",
            description="Delete a settings catalog policy, configuration profile, device health script or macOS shell script; read back.",
            input_schema=_obj({"kind": {"type": "string", "enum": _DELETABLE_KINDS}, "object_id": s}, ["kind", "object_id"]),
        ),
    ]
