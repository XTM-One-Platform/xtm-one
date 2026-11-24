# SPDX-License-Identifier: Apache-2.0

"""Microsoft Intune, Tanium, CrowdStrike Falcon and ServiceNow CMDB as asset group sources (#1556).

Each source answers its groups and, per group, the ``external_id`` of its member
assets in the same source, so :func:`organization_asset_records.sync_group_members`
binds them to the asset records that source created (whatever source the merged
asset came from first):

- ``intune``: the Entra ID security groups that hold Intune devices (the groups Intune
  assigns its policies and apps to). Device categories were not taken: one category
  per device, set by hand at enrolment, is a coarser grouping than the groups the
  tenant already targets. A group's ``device`` members name their Entra ``deviceId``,
  mapped to the Intune managed device id through ``azureADDeviceId``; a group with no
  device member (a user group) is left out. Graph permissions: ``GroupMember.Read.All``
  (or ``Group.Read.All``), ``Device.Read.All`` and ``DeviceManagementManagedDevices.Read.All``.
- ``tanium``: API Gateway ``computerGroups``, members read with
  ``endpoints(filter: {memberOf: {name}})``: a filter-based group is stored as its
  resolved members at each run.
- ``crowdstrike``: Falcon host groups (``/devices/combined/host-groups/v1``), members from
  ``/devices/queries/host-group-members/v1`` (static and dynamic groups alike).
  Scope: Host groups: Read.
- ``servicenow``: business services (``cmdb_ci_service``), the CMDB's own grouping of the
  hardware that serves a business function; members are the hardware CIs a
  ``cmdb_rel_ci`` relationship names as direct children of the service.

A group whose member list could not be read whole is answered without members: its
stored memberships are left untouched, never cut to a partial read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

INTUNE = "intune"
TANIUM = "tanium"
CROWDSTRIKE = "crowdstrike"
SERVICENOW = "servicenow"
SOURCES: tuple[str, ...] = (INTUNE, TANIUM, CROWDSTRIKE, SERVICENOW)
#: The filters each source supports for asset groups: a name the group's name contains.
FILTERS: dict[str, tuple[str, ...]] = {source: ("search",) for source in SOURCES}
#: The permission each source needs to read groups, named in the error when the API refuses.
REQUIRED_SCOPES: dict[str, str] = {
    INTUNE: "GroupMember.Read.All, Device.Read.All and DeviceManagementManagedDevices.Read.All",
    TANIUM: "an API token with read access to the API Gateway (Computer groups, Endpoints)",
    CROWDSTRIKE: "an API client with the Host groups: Read and Hosts: Read scopes",
    SERVICENOW: "a user with the cmdb_read role (cmdb_ci_service and cmdb_rel_ci)",
}
LABELS: dict[str, str] = {
    INTUNE: "Microsoft Intune",
    TANIUM: "Tanium",
    CROWDSTRIKE: "CrowdStrike Falcon",
    SERVICENOW: "ServiceNow",
}
#: Members read per group at most; a group past it keeps its stored memberships.
MAX_MEMBERS_PER_GROUP = 20000
_GRAPH_PAGE = 999
#: Entra groups read at most to apply a name filter (Graph cannot match a substring).
_GRAPH_GROUPS_SCANNED = 50000
_TANIUM_PAGE = 500
_FALCON_PAGE = 500
_SN_PAGE = 500
_SN_IN_CHUNK = 100


@dataclass
class GroupRead:
    """What a group source answered: group rows, members per group, counts and the error that stopped it."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    #: group external_id -> the external_id of its member assets in the same source.
    members: dict[str, list[str]] = field(default_factory=dict)
    total: int | None = None
    truncated: bool = False
    error: str | None = None
    #: Groups whose member list was cut or failed: answered without members.
    members_unread: set[str] = field(default_factory=set)


# ── Mapping (no I/O) ──────────────────────────────────────────────────


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def group_row(
    source: str, external_id: Any, name: Any, description: Any = "", tags: list[str] | None = None
) -> dict[str, Any]:
    """An ``asset_groups`` row of *source* (an :class:`AssetGroupEntry` value), or {} without id or name."""
    gid, label = _text(external_id, 200), _text(name, 200)
    if not gid or not label:
        return {}
    return {
        "name": label,
        "description": _text(description, 1000),
        "source": source,
        "external_id": gid,
        "tags": [t for t in (tags or []) if t][:50],
    }


def intune_group_row(group: dict[str, Any]) -> dict[str, Any]:
    tags = ["entra:dynamic"] if "DynamicMembership" in (group.get("groupTypes") or []) else []
    return group_row(INTUNE, group.get("id"), group.get("displayName"), group.get("description"), tags)


def tanium_group_row(node: dict[str, Any]) -> dict[str, Any]:
    kind = _text(node.get("type"), 40).lower()
    return group_row(
        TANIUM, node.get("id"), node.get("name"), node.get("expression"), [f"tanium:{kind}"] if kind else []
    )


def falcon_group_row(group: dict[str, Any]) -> dict[str, Any]:
    kind = _text(group.get("group_type"), 40).lower()
    return group_row(
        CROWDSTRIKE, group.get("id"), group.get("name"), group.get("description"), [f"falcon:{kind}"] if kind else []
    )


def _sn_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or "").strip()
    return str(value or "").strip()


def _sn_display(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("display_value") or value.get("value") or "").strip()
    return str(value or "").strip()


def servicenow_group_row(service: dict[str, Any]) -> dict[str, Any]:
    return group_row(
        SERVICENOW,
        _sn_value(service.get("sys_id")),
        _sn_display(service.get("name")),
        _sn_display(service.get("short_description")),
        ["servicenow:business_service"],
    )


def intune_members(devices: list[dict[str, Any]], managed: dict[str, str]) -> list[str]:
    """The Intune managed device ids of a group's Entra device members (by ``deviceId``)."""
    out: list[str] = []
    for device in devices:
        entra = str(device.get("deviceId") or "").strip().lower()
        if entra and entra in managed:
            out.append(managed[entra])
    return list(dict.fromkeys(out))


def matches_search(name: str, filters: dict[str, Any]) -> bool:
    text = str(filters.get("search") or "").strip().casefold()
    return not text or text in name.casefold()


# ── Reading (I/O) ─────────────────────────────────────────────────────


def missing_scope_message(source: str, status: int) -> str:
    return f"{LABELS[source]} refused the group read (HTTP {status}): it needs {REQUIRED_SCOPES[source]}."


async def read_groups(db: Any, user_id: str, source: str, filters: dict[str, Any], *, limit: int) -> GroupRead:
    """The groups of *source* and their members, with the administrator's integration (a failure is answered)."""
    try:
        if source == INTUNE:
            from app.services import organization_context_microsoft as microsoft

            token, reason = await microsoft.access_token(db, user_id, INTUNE)
            if not token:
                return GroupRead(error=f"{INTUNE}: {reason}")
            return await read_intune_groups(token, filters, limit=limit)
        from app.services import organization_context_endpoints as endpoints

        token, reason = await endpoints.credential(db, user_id, source)
        if not token:
            return GroupRead(error=f"{source}: {reason}")
        return await READERS[source](token, filters, limit=limit)
    except Exception as exc:  # reported, never guessed
        logger.warning("%s group read failed: %s", source, format_exception_message(exc))
        return GroupRead(error=f"{source}: {type(exc).__name__}")


async def _json(client: Any, method: str, url: str, source: str, **kwargs: Any) -> tuple[Any, str]:
    resp = await client.request(method, url, **kwargs)
    if resp.status_code in (401, 403):
        return None, missing_scope_message(source, resp.status_code)
    if resp.status_code >= 400:
        return None, f"{source}: HTTP {resp.status_code}"
    return resp.json(), ""


class _client:  # noqa: N801 - an async context manager named for its use
    """The egress client of a read, or the one a test passes (left open)."""

    def __init__(self, client: Any) -> None:
        self._given = client
        self._own: Any = None

    async def __aenter__(self) -> Any:
        if self._given is not None:
            return self._given
        from app.core.egress import egress_client

        self._own = egress_client(timeout=60)
        return await self._own.__aenter__()

    async def __aexit__(self, *exc: Any) -> None:
        if self._own is not None:
            await self._own.__aexit__(*exc)


async def _graph_paged(
    client: Any, url: str, headers: dict[str, str], limit: int
) -> tuple[list[dict[str, Any]], bool, str]:
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        data, err = await _json(client, "GET", next_url, INTUNE, headers=headers)
        if err or not isinstance(data, dict):
            return items, False, err or f"{INTUNE}: no answer"
        items.extend(v for v in data.get("value") or [] if isinstance(v, dict))
        if len(items) >= limit:
            return items[:limit], bool(data.get("@odata.nextLink")) or len(items) > limit, ""
        next_url = data.get("@odata.nextLink")
    return items, False, ""


async def read_intune_groups(token: str, filters: dict[str, Any], *, limit: int, client: Any = None) -> GroupRead:
    from urllib.parse import quote

    from app.services.organization_context_microsoft import GRAPH_BASE

    result = GroupRead()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "ConsistencyLevel": "eventual"}
    # Graph $filter has no contains() on displayName (startswith only): the name filter is
    # applied to the groups read, a substring match like the other sources (matches_search).
    odata = "securityEnabled eq true"
    searching = bool(str(filters.get("search") or "").strip())
    async with _client(client) as http:
        managed_rows, _cut, err = await _graph_paged(
            http,
            f"{GRAPH_BASE}/deviceManagement/managedDevices?$select=id,azureADDeviceId&$top={_GRAPH_PAGE}",
            headers,
            10 * MAX_MEMBERS_PER_GROUP,
        )
        if err:
            result.error = err
            return result
        managed = {
            str(d.get("azureADDeviceId") or "").lower(): str(d.get("id") or "")
            for d in managed_rows
            if d.get("azureADDeviceId") and d.get("id")
        }
        groups, cut, err = await _graph_paged(
            http,
            f"{GRAPH_BASE}/groups?$select=id,displayName,description,groupTypes&$top={_GRAPH_PAGE}"
            f"&$filter={quote(odata)}",
            headers,
            _GRAPH_GROUPS_SCANNED if searching else limit,
        )
        if err:
            result.error = err
            return result
        if searching:
            groups = [g for g in groups if matches_search(str(g.get("displayName") or ""), filters)]
            cut = cut or len(groups) > limit
            groups = groups[:limit]
        result.truncated = cut
        for group in groups:
            row = intune_group_row(group)
            if not row:
                continue
            devices, member_cut, err = await _graph_paged(
                http,
                f"{GRAPH_BASE}/groups/{row['external_id']}/members/microsoft.graph.device?$select=deviceId&$top={_GRAPH_PAGE}",
                headers,
                MAX_MEMBERS_PER_GROUP,
            )
            if err or member_cut:
                result.rows.append(row)
                result.members_unread.add(row["external_id"])
                continue
            if not devices:  # a user group: not a group of assets
                continue
            result.rows.append(row)
            result.members[row["external_id"]] = intune_members(devices, managed)
    result.total = len(result.rows)
    return result


TANIUM_GROUPS_QUERY = (
    "query XtmInventoryGroups($first: Int, $after: Cursor) {\n"
    "  computerGroups(first: $first, after: $after) {\n"
    "    pageInfo { hasNextPage endCursor }\n"
    "    edges { node { id name expression type } }\n"
    "  }\n"
    "}"
)
TANIUM_MEMBERS_QUERY = (
    "query XtmInventoryGroupMembers($first: Int, $after: Cursor, $name: String) {\n"
    "  endpoints(first: $first, after: $after, filter: {memberOf: {name: $name}}) {\n"
    "    pageInfo { hasNextPage endCursor }\n"
    "    edges { node { id } }\n"
    "  }\n"
    "}"
)


async def _tanium_pages(
    http: Any, url: str, headers: dict[str, str], query: str, key: str, variables: dict[str, Any], limit: int
) -> tuple[list[dict[str, Any]], bool, str]:
    nodes: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        body, err = await _json(
            http,
            "POST",
            url,
            TANIUM,
            headers=headers,
            json={"query": query, "variables": {**variables, "first": _TANIUM_PAGE, "after": after}},
        )
        if err:
            return nodes, False, err
        if not isinstance(body, dict) or body.get("errors"):
            return nodes, False, f"{TANIUM}: {str((body or {}).get('errors'))[:200]}"
        page = (body.get("data") or {}).get(key) or {}
        nodes.extend(e["node"] for e in page.get("edges") or [] if isinstance(e, dict) and e.get("node"))
        info = page.get("pageInfo") or {}
        if len(nodes) >= limit:
            return nodes[:limit], bool(info.get("hasNextPage")) or len(nodes) > limit, ""
        if not info.get("hasNextPage") or not info.get("endCursor"):
            return nodes, False, ""
        after = info["endCursor"]


async def read_tanium_groups(token: str, filters: dict[str, Any], *, limit: int, client: Any = None) -> GroupRead:
    from app.integrations.providers.tanium import _GATEWAY_API, _headers, _parse

    result = GroupRead()
    base_url, api_token = _parse(token)
    if not base_url or not api_token:
        result.error = f"{TANIUM}: no token for the Tanium integration: connect it again"
        return result
    url, headers = f"{base_url}{_GATEWAY_API}", _headers(api_token)
    async with _client(client) as http:
        nodes, cut, err = await _tanium_pages(http, url, headers, TANIUM_GROUPS_QUERY, "computerGroups", {}, limit)
        if err:
            result.error = err
            return result
        result.truncated = cut
        for node in nodes:
            row = tanium_group_row(node)
            if not row or not matches_search(row["name"], filters):
                continue
            result.rows.append(row)
            members, member_cut, err = await _tanium_pages(
                http, url, headers, TANIUM_MEMBERS_QUERY, "endpoints", {"name": row["name"]}, MAX_MEMBERS_PER_GROUP
            )
            if err or member_cut:
                result.members_unread.add(row["external_id"])
                continue
            result.members[row["external_id"]] = list(dict.fromkeys(str(m["id"]) for m in members if m.get("id")))
    result.total = len(result.rows)
    return result


async def read_crowdstrike_groups(token: str, filters: dict[str, Any], *, limit: int, client: Any = None) -> GroupRead:
    from app.integrations.http import parse_compound_token
    from app.integrations.providers.crowdstrike import _DEFAULT_BASE_URL, _headers, _request_token_with_ttl

    result = GroupRead()
    base_url, client_id, client_secret = parse_compound_token(token, 3)
    base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    bearer, _ttl = await _request_token_with_ttl(base_url, client_id, client_secret)
    if not bearer:
        result.error = missing_scope_message(CROWDSTRIKE, 401)
        return result
    headers = _headers(bearer)
    async with _client(client) as http:
        groups: list[dict[str, Any]] = []
        offset = 0
        while len(groups) < limit:
            body, err = await _json(
                http,
                "GET",
                f"{base_url}/devices/combined/host-groups/v1",
                CROWDSTRIKE,
                headers=headers,
                params={"limit": _FALCON_PAGE, "offset": offset},
            )
            if err:
                result.error = err
                return result
            page = [g for g in (body or {}).get("resources") or [] if isinstance(g, dict)]
            groups.extend(page)
            total = (((body or {}).get("meta") or {}).get("pagination") or {}).get("total")
            offset += len(page)
            if not page or (isinstance(total, int) and offset >= total):
                break
        result.truncated = len(groups) > limit
        for group in groups[:limit]:
            row = falcon_group_row(group)
            if not row or not matches_search(row["name"], filters):
                continue
            result.rows.append(row)
            ids, unread = await _falcon_members(http, base_url, headers, row["external_id"])
            if unread:
                result.members_unread.add(row["external_id"])
                continue
            result.members[row["external_id"]] = ids
    result.total = len(result.rows)
    return result


async def _falcon_members(http: Any, base_url: str, headers: dict[str, str], group_id: str) -> tuple[list[str], bool]:
    ids: list[str] = []
    offset = 0
    while True:
        body, err = await _json(
            http,
            "GET",
            f"{base_url}/devices/queries/host-group-members/v1",
            CROWDSTRIKE,
            headers=headers,
            params={"id": group_id, "limit": _FALCON_PAGE, "offset": offset},
        )
        if err:
            return ids, True
        page = [str(i) for i in (body or {}).get("resources") or [] if i]
        ids.extend(page)
        if len(ids) > MAX_MEMBERS_PER_GROUP:
            return ids, True
        total = (((body or {}).get("meta") or {}).get("pagination") or {}).get("total")
        offset += len(page)
        if not page or (isinstance(total, int) and offset >= total):
            return list(dict.fromkeys(ids)), False


async def read_servicenow_groups(token: str, filters: dict[str, Any], *, limit: int, client: Any = None) -> GroupRead:
    from app.integrations.providers.servicenow._common import (
        build_servicenow_headers,
        parse_servicenow_token,
        table_url,
    )

    result = GroupRead()
    base_url, username, password = parse_servicenow_token(token)
    if not base_url or not username:
        result.error = f"{SERVICENOW}: no token for the ServiceNow integration: connect it again"
        return result
    headers = build_servicenow_headers(username, password)
    search = str(filters.get("search") or "").strip().replace("^", "")[:100]
    query = "operational_status!=6" + (f"^nameLIKE{search}" if search else "")
    async with _client(client) as http:
        services: list[dict[str, Any]] = []
        offset = 0
        while len(services) < limit + 1:
            body, err = await _json(
                http,
                "GET",
                table_url(base_url, "cmdb_ci_service"),
                SERVICENOW,
                headers=headers,
                params={
                    "sysparm_query": query,
                    "sysparm_fields": "sys_id,name,short_description",
                    "sysparm_display_value": "all",
                    "sysparm_exclude_reference_link": "true",
                    "sysparm_limit": _SN_PAGE,
                    "sysparm_offset": offset,
                },
            )
            if err:
                result.error = err
                return result
            page = [s for s in (body or {}).get("result") or [] if isinstance(s, dict)]
            services.extend(page)
            offset += len(page)
            if len(page) < _SN_PAGE:
                break
        result.truncated = len(services) > limit
        rows = [r for r in (servicenow_group_row(s) for s in services[:limit]) if r]
        result.rows = rows
        members: dict[str, list[str]] = {r["external_id"]: [] for r in rows}
        ids = list(members)
        for start in range(0, len(ids), _SN_IN_CHUNK):
            chunk = ids[start : start + _SN_IN_CHUNK]
            rel_offset = 0
            while True:
                body, err = await _json(
                    http,
                    "GET",
                    table_url(base_url, "cmdb_rel_ci"),
                    SERVICENOW,
                    headers=headers,
                    params={
                        "sysparm_query": f"parentIN{','.join(chunk)}^child.sys_class_nameINSTANCEOFcmdb_ci_hardware",
                        "sysparm_fields": "parent,child",
                        "sysparm_exclude_reference_link": "true",
                        "sysparm_limit": _SN_PAGE,
                        "sysparm_offset": rel_offset,
                    },
                )
                if err:
                    result.members_unread.update(chunk)
                    break
                page = [r for r in (body or {}).get("result") or [] if isinstance(r, dict)]
                for rel in page:
                    parent, child = _sn_value(rel.get("parent")), _sn_value(rel.get("child"))
                    if parent in members and child:
                        members[parent].append(child)
                rel_offset += len(page)
                if len(page) < _SN_PAGE:
                    break
        for gid, children in members.items():
            if gid in result.members_unread:
                continue
            kept = list(dict.fromkeys(children))
            if len(kept) > MAX_MEMBERS_PER_GROUP:
                result.members_unread.add(gid)
                continue
            result.members[gid] = kept
    result.total = len(result.rows)
    return result


READERS = {TANIUM: read_tanium_groups, CROWDSTRIKE: read_crowdstrike_groups, SERVICENOW: read_servicenow_groups}
