# SPDX-License-Identifier: Apache-2.0

"""Tanium, CrowdStrike Falcon and ServiceNow CMDB as scheduled asset sources (#1556).

Three sources of the ``assets`` section, read with the administrator's connected
integration of the same provider (the agent integrations of ``app.integrations.providers``):

- ``tanium``: API Gateway GraphQL ``endpoints`` (name, serial, IP and MAC addresses,
  OS, domain, last seen) and their ``installedApplications`` (software with versions);
- ``crowdstrike``: Falcon ``/devices/queries/devices-scroll/v1`` then
  ``/devices/entities/devices/v2`` (hosts, AID, OS, serial, local IP, MAC, last seen);
- ``servicenow``: CMDB Table API on a hardware class (``cmdb_ci_computer`` by default:
  servers, workstations, laptops), with serial, hostname, FQDN, IP, MAC, owner and environment.

The mapping is pure (no I/O) and tested on recorded fixtures. Each row fills the identity
keys of ``organization_asset_identity`` (serial, hostname, FQDN, MAC, IP) so the same device
read from Intune, Defender, Tanium, CrowdStrike, ServiceNow and OpenAEV merges into one asset.
Tanium and CrowdStrike are registered as ``edr`` sources, ServiceNow as ``cmdb``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message
from app.services.organization_context_microsoft import (
    REASON_NO_TOKEN,
    REASON_NOT_CONNECTED,
    EndpointRead,
    _hostname_and_fqdn,
    _mac,
    _seen,
    _text,
    has_credential,
    os_family,
    taxonomy,
)

logger = logging.getLogger(__name__)

TANIUM = "tanium"
CROWDSTRIKE = "crowdstrike"
SERVICENOW = "servicenow"
SOURCES: tuple[str, ...] = (TANIUM, CROWDSTRIKE, SERVICENOW)

LABELS: dict[str, str] = {
    TANIUM: "Tanium",
    CROWDSTRIKE: "CrowdStrike Falcon",
    SERVICENOW: "ServiceNow",
}
#: The permission each source needs, named in the error when the API refuses.
REQUIRED_SCOPES: dict[str, str] = {
    TANIUM: "an API token with read access to the API Gateway (Endpoints)",
    CROWDSTRIKE: "an API client with the Hosts: Read scope",
    SERVICENOW: "a user with the itil or cmdb_read role (read on the CMDB tables)",
}
#: The filters each source supports, in the order the Sources panel shows them.
FILTERS: dict[str, tuple[str, ...]] = {
    TANIUM: ("operating_system", "last_seen_days", "skip_software"),
    CROWDSTRIKE: ("platform", "last_seen_days", "filter_query"),
    SERVICENOW: ("ci_class", "operational_status", "query"),
}
#: The fixed values of those filters (the APIs' own enumerations).
FILTER_VALUES: dict[str, dict[str, tuple[str, ...]]] = {
    TANIUM: {
        "operating_system": ("Windows", "Linux", "Mac"),
        "last_seen_days": ("7", "30", "90"),
        "skip_software": ("true",),
    },
    CROWDSTRIKE: {
        "platform": ("Windows", "Mac", "Linux"),
        "last_seen_days": ("7", "30", "90"),
    },
    SERVICENOW: {
        "ci_class": ("cmdb_ci_computer", "cmdb_ci_server", "cmdb_ci_hardware", "cmdb_ci_netgear"),
        # ServiceNow's operational_status choice values: 1 Operational, 2 Non-Operational ... 6 Retired.
        "operational_status": ("1", "2", "3", "4", "5", "6"),
    },
}
#: The CI classes a ServiceNow read may name (a free table name is refused: the CMDB hardware tree only).
SERVICENOW_CLASSES: frozenset[str] = frozenset(FILTER_VALUES[SERVICENOW]["ci_class"])

#: Page sizes: Tanium Gateway ``first`` (max 500), Falcon scroll ``limit`` (max 5000, details 100 per call),
#: ServiceNow ``sysparm_limit``.
_TANIUM_PAGE = 200
_FALCON_PAGE = 1000
_FALCON_DETAILS = 100
_SN_PAGE = 500
#: Installed applications kept per Tanium endpoint at most.
MAX_APPS_PER_DEVICE = 2000

# ── Mapping (no I/O) ──────────────────────────────────────────────────


def _ips(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        out.extend(_text(v, 64) for v in items if _text(v, 64))
    return list(dict.fromkeys(out))[:10]


def _macs(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            out.extend(_mac(str(item or "").strip().replace("-", ":")))
    return list(dict.fromkeys(m.lower() for m in out))[:10]


def _sys_id(value: Any) -> str:
    """A record's sys_id, read whole or from a ``sysparm_display_value=all`` pair."""
    if isinstance(value, dict):
        return _text(value.get("value"), 64)
    return _text(value, 64)


def _display(value: Any) -> str:
    """A ServiceNow field read with ``sysparm_display_value=all``: its display value, else its value."""
    if isinstance(value, dict):
        return _text(value.get("display_value") or value.get("value"))
    return _text(value)


def tanium_endpoint_row(node: dict[str, Any]) -> dict[str, Any]:
    """A Tanium Gateway endpoint as an asset row with its identity keys and OS (source ``tanium``)."""
    raw_os = node.get("os")
    os_info: dict[str, Any] = raw_os if isinstance(raw_os, dict) else {}
    os_name = _text(os_info.get("name"), 80) or _text(os_info.get("platform"), 80)
    category, subcategory = taxonomy(os_name or _text(os_info.get("platform"), 80))
    name = _text(node.get("name")) or _text(node.get("id"))
    hostname, fqdn = _hostname_and_fqdn(name)
    domain = _text(node.get("domainName"), 200)
    if not fqdn and hostname and domain and "." in domain:
        fqdn = f"{hostname}.{domain}"[:255]
    platform = _text(os_info.get("platform"), 40).lower()
    return {
        "name": name,
        "category": category,
        "subcategory": subcategory,
        "tags": [f"os:{platform}"] if platform else [],
        "source": TANIUM,
        "external_id": _text(node.get("id")),
        "serial": _text(node.get("serialNumber")),
        "hostname": hostname,
        "fqdn": fqdn,
        "mac": _macs(node.get("macAddresses")),
        "ip": _ips(node.get("ipAddress"), node.get("ipAddresses")),
        "os_family": os_family(os_name or platform),
        "os_version": _text(os_info.get("generation"), 80) or _text(os_info.get("name"), 80),
        "last_seen": _seen(node.get("eidLastSeen")),
    }


def tanium_software(apps: list[Any]) -> list[dict[str, Any]]:
    """Tanium installed applications as software entries (``version`` ``unknown`` when none)."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for app in apps[:MAX_APPS_PER_DEVICE]:
        if not isinstance(app, dict):
            continue
        product = _text(app.get("name"), 300)
        if not product:
            continue
        version = _text(app.get("version"), 100) or "unknown"
        vendor = _text(app.get("vendor") or app.get("publisher"), 200)
        out.setdefault(
            (vendor.lower(), product.lower(), version.lower()),
            {"vendor": vendor, "product": product, "version": version},
        )
    return list(out.values())


def crowdstrike_host_row(host: dict[str, Any]) -> dict[str, Any]:
    """A Falcon host as an asset row (source ``crowdstrike``), its AID as external id."""
    os_name = _text(host.get("os_product_name"), 80) or _text(host.get("platform_name"), 80)
    product_type = _text(host.get("product_type_desc"), 40)
    category, subcategory = taxonomy(os_name)
    if category == "host" and product_type.lower() == "server":
        subcategory = "server"
    name = _text(host.get("hostname")) or _text(host.get("device_id"))
    hostname, fqdn = _hostname_and_fqdn(name)
    domain = _text(host.get("machine_domain"), 200)
    if not fqdn and hostname and domain and "." in domain:
        fqdn = f"{hostname}.{domain}"[:255]
    tags = [f"os:{_text(host.get('platform_name'), 40).lower()}"] if host.get("platform_name") else []
    for tag in host.get("tags") or []:
        text = _text(tag, 60)
        if text:
            tags.append(text.lower().replace("falconsensor", "falcon"))
    return {
        "name": name,
        "category": category,
        "subcategory": subcategory,
        "tags": list(dict.fromkeys(tags))[:50],
        "source": CROWDSTRIKE,
        "external_id": _text(host.get("device_id")),
        "serial": _text(host.get("serial_number")),
        "hostname": hostname,
        "fqdn": fqdn,
        "mac": _macs(host.get("mac_address")),
        "ip": _ips(host.get("local_ip"), host.get("external_ip")),
        "os_family": os_family(_text(host.get("platform_name"), 80) or os_name),
        "os_version": _text(host.get("os_version"), 80),
        "os_build": _text(host.get("os_build"), 80),
        "last_seen": _seen(host.get("last_seen")),
    }


#: ServiceNow environment choice values -> the profile's environments.
_SN_ENVIRONMENTS: dict[str, str] = {
    "production": "production",
    "prod": "production",
    "staging": "staging",
    "development": "development",
    "dev": "development",
    "test": "test",
    "qa": "test",
    "uat": "test",
}


def servicenow_ci_row(ci: dict[str, Any], *, ci_class: str = "cmdb_ci_computer") -> dict[str, Any]:
    """A CMDB hardware CI as an asset row (source ``servicenow``), its sys_id as external id."""
    os_name = _display(ci.get("os"))
    cls = _sys_id(ci.get("sys_class_name")) or ci_class
    if cls in ("cmdb_ci_netgear", "cmdb_ci_ip_router", "cmdb_ci_ip_switch", "cmdb_ci_ip_firewall"):
        category, subcategory = "network_device", ""
    else:
        category, subcategory = taxonomy(os_name)
        if category == "generic_asset" and cls.endswith("_server"):
            category, subcategory = "host", "server"
    name = _display(ci.get("name")) or _display(ci.get("sys_id"))
    fqdn = _display(ci.get("fqdn")).rstrip(".")[:255]
    host = _display(ci.get("host_name")) or (fqdn.split(".", 1)[0] if fqdn else "")
    hostname, derived = _hostname_and_fqdn(host or name)
    environment = _SN_ENVIRONMENTS.get(_display(ci.get("environment")).lower(), "")
    row: dict[str, Any] = {
        "name": name,
        "category": category,
        "subcategory": subcategory,
        "tags": [f"ci_class:{cls}"] if cls else [],
        "source": SERVICENOW,
        "external_id": _sys_id(ci.get("sys_id")),
        "serial": _display(ci.get("serial_number")),
        "hostname": hostname,
        "fqdn": fqdn or derived,
        "mac": _macs(_display(ci.get("mac_address"))),
        "ip": _ips(_display(ci.get("ip_address"))),
        "os_family": os_family(os_name),
        "os_version": _display(ci.get("os_version"))[:80],
        "last_seen": _seen(_display(ci.get("last_discovered"))),
    }
    owner = _display(ci.get("owned_by")) or _display(ci.get("managed_by"))
    if owner:
        row["owner"] = owner[:200]
    if environment:
        row["environment"] = environment
    return row


def falcon_filter(filters: dict[str, Any], *, since_iso: str = "") -> str:
    """The FQL filter of a Falcon host read (every filter given, ANDed); empty when none."""
    from app.integrations.providers.crowdstrike import _fql_quote

    parts: list[str] = []
    platforms = _values(filters, "platform")
    if platforms:
        parts.append("platform_name:[" + ",".join(f"'{_fql_quote(p)}'" for p in platforms) + "]")
    if since_iso:
        parts.append(f"last_seen:>='{since_iso}'")
    raw = _text(filters.get("filter_query"), 500)
    if raw:
        parts.append(f"({raw})")
    return "+".join(parts)


def servicenow_query(filters: dict[str, Any]) -> str:
    """The ``sysparm_query`` of a CMDB read: install status not retired, the filters given, a stable order."""
    parts = ["install_status!=7"]
    statuses = _values(filters, "operational_status")
    if statuses:
        parts.append("operational_statusIN" + ",".join(s for s in statuses if s.isdigit()))
    raw = _text(filters.get("query"), 500)
    if raw:
        parts.append(raw)
    parts.append("ORDERBYsys_id")
    return "^".join(parts)


def servicenow_class(filters: dict[str, Any]) -> str:
    """The CI class to read: one of :data:`SERVICENOW_CLASSES`, ``cmdb_ci_computer`` otherwise."""
    value = _text(filters.get("ci_class"), 80)
    return value if value in SERVICENOW_CLASSES else "cmdb_ci_computer"


def tanium_os_matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Whether a Tanium row passes the operating system filter (read client-side: the Gateway filters on paths)."""
    wanted = {v.lower() for v in _values(filters, "operating_system")}
    if not wanted:
        return True
    platform = next((t.split(":", 1)[1] for t in row.get("tags") or [] if t.startswith("os:")), "")
    return platform in wanted


def _values(filters: dict[str, Any], key: str) -> list[str]:
    raw = filters.get(key)
    if raw in (None, ""):
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [str(v).strip() for v in items if str(v).strip()]


def missing_scope_message(source: str, status: int) -> str:
    return (
        f"{LABELS[source]} refused the read (HTTP {status}): the connected integration needs "
        f"{REQUIRED_SCOPES[source]}, then run the source again"
    )


def not_connected_message(source: str) -> str:
    return f"no {LABELS[source]} integration is connected"


def no_token_message(source: str) -> str:
    return f"no credential for the {LABELS[source]} integration: configure it again"


# ── Connection (I/O) ──────────────────────────────────────────────────


async def credential(db: Any, user_id: str, source: str) -> tuple[str | None, str]:
    """The compound token of *source*'s integration (as its agent tools read it), or the reason."""
    from app.services import integration_tools as it
    from app.services.organization_context_microsoft import _integration

    integ = await _integration(db, user_id, source)
    if integ is None:
        return None, not_connected_message(source)
    token = await it._get_token(db, str(integ.id), source, user_id=user_id)
    if not token:
        return None, no_token_message(source)
    return token, ""


async def connection_status(db: Any, user_id: str, source: str) -> tuple[bool, str, str]:
    """Whether *source* has an integration this administrator may read with (no API call)."""
    from app.services.organization_context_microsoft import _integration

    integ = await _integration(db, user_id, source)
    if integ is None:
        return False, not_connected_message(source), REASON_NOT_CONNECTED
    if not has_credential(integ):
        return False, no_token_message(source), REASON_NO_TOKEN
    return True, "", ""


# ── Reading (I/O) ─────────────────────────────────────────────────────


@dataclass
class _Http:
    """One client and the source's headers, the refusal worded with the scope to grant."""

    client: Any
    source: str
    headers: dict[str, str] = field(default_factory=dict)

    async def call(self, method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
        from app.integrations.http import safe_request

        resp, err = await safe_request(
            self.client,
            method,
            url,
            LABELS[self.source],
            f"read {self.source} inventory",
            headers=self.headers,
            expected_statuses={401, 403},
            **kwargs,
        )
        if resp is not None and resp.status_code in (401, 403):
            return None, missing_scope_message(self.source, resp.status_code)
        if err or resp is None:
            return None, err or f"{self.source}: no answer"
        return resp, ""


def _since(filters: dict[str, Any]) -> str:
    from datetime import UTC, datetime, timedelta

    try:
        days = int(filters.get("last_seen_days") or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return ""
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


TANIUM_ENDPOINTS_QUERY = (
    "query XtmInventoryEndpoints($first: Int, $after: Cursor, $apps: Boolean!) {\n"
    "  endpoints(first: $first, after: $after) {\n"
    "    totalRecords\n"
    "    pageInfo { hasNextPage endCursor }\n"
    "    edges { node { id name serialNumber domainName ipAddress ipAddresses macAddresses eidLastSeen\n"
    "      os { name platform generation }\n"
    "      installedApplications @include(if: $apps) { name version } } }\n"
    "  }\n"
    "}"
)


async def read_tanium(token: str, filters: dict[str, Any], *, limit: int, client: Any = None) -> EndpointRead:
    """Endpoints and their installed applications through the API Gateway, paged (``limit`` at most)."""
    from app.integrations.providers.tanium import _GATEWAY_API, _headers, _parse

    result = EndpointRead()
    base_url, api_token = _parse(token)
    if not base_url or not api_token:
        result.error = no_token_message(TANIUM)
        return result
    since = _since(filters)
    apps = not bool(filters.get("skip_software"))
    async with _client(client) as http_client:
        http = _Http(http_client, TANIUM, _headers(api_token))
        after: str | None = None
        nodes: list[dict[str, Any]] = []
        try:
            while True:
                resp, err = await http.call(
                    "post",
                    f"{base_url}{_GATEWAY_API}",
                    json={
                        "query": TANIUM_ENDPOINTS_QUERY,
                        "variables": {"first": _TANIUM_PAGE, "after": after, "apps": apps},
                    },
                )
                if err:
                    result.error = err
                    return result
                body = resp.json() or {}
                if body.get("errors"):
                    result.error = f"{TANIUM}: {str(body['errors'])[:200]}"
                    return result
                page = (body.get("data") or {}).get("endpoints") or {}
                if result.total is None and isinstance(page.get("totalRecords"), int):
                    result.total = page["totalRecords"]
                nodes.extend(e["node"] for e in page.get("edges") or [] if isinstance(e, dict) and e.get("node"))
                info = page.get("pageInfo") or {}
                if len(nodes) >= limit:
                    result.truncated = bool(info.get("hasNextPage")) or len(nodes) > limit
                    nodes = nodes[:limit]
                    break
                if not info.get("hasNextPage") or not info.get("endCursor"):
                    break
                after = info["endCursor"]
        except Exception as exc:  # reported, never guessed
            logger.warning("Tanium inventory read failed: %s", format_exception_message(exc))
            result.error = f"{TANIUM}: {type(exc).__name__}"
            return result
    for node in nodes:
        row = tanium_endpoint_row(node)
        if not row["external_id"] or not tanium_os_matches(row, filters):
            continue
        if since and row["last_seen"] and row["last_seen"] < since:
            continue
        result.rows.append(row)
        if apps:
            software = tanium_software(node.get("installedApplications") or [])
            result.software[row["external_id"]] = software
            result.software_rows += len(software)
    if result.total is None:
        result.total = len(nodes)
    return result


async def read_crowdstrike(token: str, filters: dict[str, Any], *, limit: int, client: Any = None) -> EndpointRead:
    """Falcon hosts: the scroll query for their AIDs, then their details 100 at a time (``limit`` at most)."""
    from app.integrations.http import parse_compound_token
    from app.integrations.providers.crowdstrike import _DEFAULT_BASE_URL, _headers, _request_token_with_ttl

    result = EndpointRead()
    base_url, client_id, client_secret = parse_compound_token(token, 3)
    base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    bearer, _ttl = await _request_token_with_ttl(base_url, client_id, client_secret)
    if not bearer:
        result.error = missing_scope_message(CROWDSTRIKE, 401)
        return result
    fql = falcon_filter(filters, since_iso=_since(filters))
    async with _client(client) as http_client:
        http = _Http(http_client, CROWDSTRIKE, _headers(bearer))
        ids: list[str] = []
        offset = ""
        try:
            while True:
                params: dict[str, Any] = {"limit": _FALCON_PAGE, "sort": "device_id.asc"}
                if fql:
                    params["filter"] = fql
                if offset:
                    params["offset"] = offset
                resp, err = await http.call("get", f"{base_url}/devices/queries/devices-scroll/v1", params=params)
                if err:
                    result.error = err
                    return result
                body = resp.json() or {}
                ids.extend(str(i) for i in body.get("resources") or [] if i)
                pagination = (body.get("meta") or {}).get("pagination") or {}
                if result.total is None and isinstance(pagination.get("total"), int):
                    result.total = pagination["total"]
                offset = str(pagination.get("offset") or "")
                if len(ids) >= limit:
                    result.truncated = len(ids) > limit or bool(offset and (result.total or 0) > limit)
                    ids = ids[:limit]
                    break
                if not offset or not body.get("resources"):
                    break
            for start in range(0, len(ids), _FALCON_DETAILS):
                resp, err = await http.call(
                    "post",
                    f"{base_url}/devices/entities/devices/v2",
                    json={"ids": ids[start : start + _FALCON_DETAILS]},
                )
                if err:
                    result.error = err
                    return result
                for host in (resp.json() or {}).get("resources") or []:
                    if isinstance(host, dict) and host.get("device_id"):
                        result.rows.append(crowdstrike_host_row(host))
        except Exception as exc:  # reported, never guessed
            logger.warning("CrowdStrike inventory read failed: %s", format_exception_message(exc))
            result.error = f"{CROWDSTRIKE}: {type(exc).__name__}"
            return result
    if result.total is None:
        result.total = len(ids)
    return result


SERVICENOW_FIELDS = (
    "sys_id,name,sys_class_name,serial_number,host_name,fqdn,ip_address,mac_address,os,os_version,"
    "owned_by,managed_by,environment,last_discovered"
)


async def read_servicenow(token: str, filters: dict[str, Any], *, limit: int, client: Any = None) -> EndpointRead:
    """Hardware CIs of one CMDB class through the Table API, paged by offset (``limit`` at most)."""
    from app.integrations.providers.servicenow._common import (
        build_servicenow_headers,
        parse_servicenow_token,
        table_url,
        total_count,
    )

    result = EndpointRead()
    base_url, username, password = parse_servicenow_token(token)
    if not base_url or not username:
        result.error = no_token_message(SERVICENOW)
        return result
    ci_class = servicenow_class(filters)
    query = servicenow_query(filters)
    async with _client(client) as http_client:
        http = _Http(http_client, SERVICENOW, build_servicenow_headers(username, password))
        offset = 0
        try:
            while offset < limit:
                size = min(_SN_PAGE, limit - offset)
                resp, err = await http.call(
                    "get",
                    table_url(base_url, ci_class),
                    params={
                        "sysparm_query": query,
                        "sysparm_fields": SERVICENOW_FIELDS,
                        "sysparm_display_value": "all",
                        "sysparm_exclude_reference_link": "true",
                        "sysparm_limit": size,
                        "sysparm_offset": offset,
                    },
                )
                if err:
                    result.error = err
                    return result
                if result.total is None:
                    result.total = total_count(resp)
                rows = (resp.json() or {}).get("result") or []
                for ci in rows:
                    if isinstance(ci, dict):
                        row = servicenow_ci_row(ci, ci_class=ci_class)
                        if row["external_id"]:
                            result.rows.append(row)
                offset += len(rows)
                if len(rows) < size or (result.total is not None and offset >= result.total):
                    break
        except Exception as exc:  # reported, never guessed
            logger.warning("ServiceNow inventory read failed: %s", format_exception_message(exc))
            result.error = f"{SERVICENOW}: {type(exc).__name__}"
            return result
    if result.total is None:
        result.total = len(result.rows)
    result.truncated = result.total > len(result.rows) if result.total is not None else False
    return result


READERS = {TANIUM: read_tanium, CROWDSTRIKE: read_crowdstrike, SERVICENOW: read_servicenow}


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
