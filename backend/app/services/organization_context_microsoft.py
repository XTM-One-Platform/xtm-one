# SPDX-License-Identifier: Apache-2.0

"""Microsoft Intune and Defender for Endpoint as scheduled asset sources (#1251).

Two sources of the ``assets`` section, read with the administrator's connected
Microsoft integration:

- ``intune``: Graph ``deviceManagement/managedDevices`` (devices, OS, serial,
  Entra device id) and ``detectedApps`` (software with versions);
- ``defender``: Defender for Endpoint ``/api/machines`` (machines, OS build,
  exposure level, Entra device id) and ``/api/machines/SoftwareInventoryByMachine``
  (software with versions, one export for the whole tenant).

The mapping is pure (no I/O) and tested on fixtures: a device becomes an asset
row carrying its identity keys (serial, Entra device id, hostname, MAC, IP),
its OS and when the source last saw it; its software becomes a software
document anchored on that row (``organization_asset_software_docs``), so the
profile stays a list of devices while the store attaches the software, with
source ``intune`` (class ``mdm``) or ``defender`` (class ``edr``) and the
precedence of ``organization_asset_identity.DEFAULT_PRECEDENCE``.

The readers page through the APIs (``@odata.nextLink``), wait out a 429 with its
``Retry-After`` (bounded), and answer a missing permission with the scope to grant.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

INTUNE = "intune"
DEFENDER = "defender"
SOURCES: tuple[str, ...] = (INTUNE, DEFENDER)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
#: A managed device's ``detectedApps`` exist on the beta endpoint only: v1.0 answers HTTP 400 for
#: ``managedDevices/{id}/detectedApps`` (its managedDevice has no such relationship).
GRAPH_BETA = "https://graph.microsoft.com/beta"
MDE_BASE = "https://api.securitycenter.microsoft.com/api"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
MDE_SCOPE = "https://api.securitycenter.microsoft.com/.default"

#: The permissions each source needs, named in the error when the API refuses.
REQUIRED_SCOPES: dict[str, tuple[str, ...]] = {
    INTUNE: ("DeviceManagementManagedDevices.Read.All",),
    DEFENDER: ("Machine.Read.All", "Software.Read.All"),
}
#: The filters each source supports, in the order the Sources panel shows them.
FILTERS: dict[str, tuple[str, ...]] = {
    INTUNE: ("operating_system", "compliance", "ownership", "last_seen_days"),
    DEFENDER: ("os_platform", "health_status", "exposure_level", "last_seen_days"),
}
#: Graph page size (managedDevices accepts up to 1000).
_PAGE = 999
#: Detected apps read per device at most (a device with more is reported cut).
MAX_APPS_PER_DEVICE = 2000
#: Concurrent ``detectedApps`` reads.
_APP_CONCURRENCY = 8
#: Retries of a throttled (429) or unavailable (503) call; the wait is the server's Retry-After, capped.
_RETRIES = 4
_MAX_WAIT_S = 30.0

# ── Mapping (no I/O) ──────────────────────────────────────────────────

#: Operating system (lower-cased prefix) -> (category, subcategory).
_OS_TAXONOMY: tuple[tuple[str, tuple[str, str]], ...] = (
    ("ipados", ("mobile_device", "tablet")),
    ("ios", ("mobile_device", "smartphone")),
    ("android", ("mobile_device", "smartphone")),
    ("windowsserver", ("host", "server")),
    ("windows server", ("host", "server")),
    ("windows", ("host", "workstation")),
    ("macos", ("host", "workstation")),
    ("mac os", ("host", "workstation")),
    ("linux", ("host", "server")),
    ("ubuntu", ("host", "server")),
    ("redhat", ("host", "server")),
    ("centos", ("host", "server")),
    ("debian", ("host", "server")),
    ("chrome", ("host", "laptop")),
)
#: Normalised OS family of a raw operating system name.
_OS_FAMILY: tuple[tuple[str, str], ...] = (
    ("ipados", "iPadOS"),
    ("ios", "iOS"),
    ("android", "Android"),
    ("windows", "Windows"),
    ("macos", "macOS"),
    ("mac os", "macOS"),
    ("chrome", "ChromeOS"),
    ("linux", "Linux"),
    ("ubuntu", "Linux"),
    ("redhat", "Linux"),
    ("centos", "Linux"),
    ("debian", "Linux"),
)
_UNKNOWN_DATES = ("0001-01-01", "1601-01-01")


def _text(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _seen(value: Any) -> str:
    text = _text(value, 40)
    return "" if not text or text.startswith(_UNKNOWN_DATES) else text


def os_family(name: str) -> str:
    low = (name or "").strip().lower()
    for prefix, family in _OS_FAMILY:
        if low.startswith(prefix):
            return family
    return (name or "").strip()[:80]


def taxonomy(os_name: str, *, chassis: str = "", model: str = "") -> tuple[str, str]:
    low = (os_name or "").strip().lower()
    category, subcategory = "generic_asset", ""
    for prefix, pair in _OS_TAXONOMY:
        if low.startswith(prefix):
            category, subcategory = pair
            break
    if category == "host" and chassis.strip().lower() == "laptop":
        subcategory = "laptop"
    if category == "mobile_device" and "ipad" in model.lower():
        subcategory = "tablet"
    return category, subcategory


def _hostname_and_fqdn(name: str) -> tuple[str, str]:
    name = (name or "").strip().rstrip(".")
    if "." in name:
        return name.split(".", 1)[0][:255], name[:255]
    return name[:255], ""


def _mac(value: Any) -> list[str]:
    text = _text(value, 64)
    if len(text) == 12 and all(c in "0123456789abcdefABCDEF" for c in text):
        text = ":".join(text[i : i + 2] for i in range(0, 12, 2))
    return [text] if text else []


def intune_device_row(device: dict[str, Any], *, include_owner: bool = False) -> dict[str, Any]:
    """An Intune managed device as an asset row with its identity keys and OS (source ``intune``)."""
    os_name = _text(device.get("operatingSystem"), 80)
    category, subcategory = taxonomy(
        os_name, chassis=_text(device.get("chassisType"), 40), model=_text(device.get("model"), 200)
    )
    tags = [f"os:{os_name.lower()}"] if os_name else []
    compliance = _text(device.get("complianceState"), 40).lower()
    if compliance:
        tags.append(f"compliance:{compliance}")
    ownership = _text(device.get("managedDeviceOwnerType"), 40).lower()
    if ownership and ownership != "unknown":
        tags.append(f"ownership:{ownership}")
    name = _text(device.get("deviceName")) or _text(device.get("id"))
    hostname, fqdn = _hostname_and_fqdn(name)
    row: dict[str, Any] = {
        "name": name,
        "category": category,
        "subcategory": subcategory,
        "tags": tags,
        "source": INTUNE,
        "external_id": _text(device.get("id")),
        "serial": _text(device.get("serialNumber")),
        "device_id": _text(device.get("azureADDeviceId")).lower(),
        "hostname": hostname,
        "fqdn": fqdn,
        "mac": _mac(device.get("wiFiMacAddress")) + _mac(device.get("ethernetMacAddress")),
        "os_family": os_family(os_name),
        "os_version": _text(device.get("osVersion"), 80),
        "last_seen": _seen(device.get("lastSyncDateTime")),
    }
    if row["device_id"] == "00000000-0000-0000-0000-000000000000":
        row["device_id"] = ""
    owner = _text(device.get("userDisplayName"))
    if include_owner and owner:
        row["owner"] = owner
    return row


def intune_software(apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Intune detected apps as software entries (``version`` ``unknown`` when Intune gives none)."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for app in apps:
        product = _text(app.get("displayName"), 300)
        if not product:
            continue
        version = _text(app.get("version"), 100) or "unknown"
        vendor = _text(app.get("publisher"), 200)
        out.setdefault(
            (vendor.lower(), product.lower(), version.lower()),
            {
                "vendor": vendor,
                "product": product,
                "version": version,
            },
        )
    return list(out.values())


def defender_machine_row(machine: dict[str, Any]) -> dict[str, Any]:
    """A Defender for Endpoint machine as an asset row (source ``defender``), exposure level as a tag."""
    os_platform = _text(machine.get("osPlatform"), 80)
    category, subcategory = taxonomy(os_platform)
    tags = [f"os:{os_platform.lower()}"] if os_platform else []
    for key, prefix in (("exposureLevel", "exposure"), ("healthStatus", "health"), ("riskScore", "risk")):
        value = _text(machine.get(key), 40).lower()
        if value and value != "none":
            tags.append(f"{prefix}:{value}")
    name = _text(machine.get("computerDnsName")) or _text(machine.get("id"))
    hostname, fqdn = _hostname_and_fqdn(name)
    ips = [_text(machine.get("lastIpAddress"), 64), _text(machine.get("lastExternalIpAddress"), 64)]
    version = _text(machine.get("version"), 80)
    build = machine.get("osBuild")
    return {
        "name": name,
        "category": category,
        "subcategory": subcategory,
        "tags": tags,
        "source": DEFENDER,
        "external_id": _text(machine.get("id")),
        "device_id": _text(machine.get("aadDeviceId")).lower(),
        "hostname": hostname,
        "fqdn": fqdn,
        "ip": [ip for ip in ips if ip],
        "os_family": os_family(os_platform),
        "os_version": version,
        "os_build": str(build).strip()[:80] if build not in (None, "") else "",
        "last_seen": _seen(machine.get("lastSeen")),
    }


def defender_software_by_machine(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The ``SoftwareInventoryByMachine`` export grouped by machine id, as software entries."""
    out: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    for entry in entries:
        machine = _text(entry.get("deviceId") or entry.get("machineId"))
        product = _text(entry.get("softwareName"), 300)
        if not machine or not product:
            continue
        vendor = _text(entry.get("softwareVendor"), 200)
        version = _text(entry.get("softwareVersion"), 100) or "unknown"
        out.setdefault(machine, {}).setdefault(
            (vendor.lower(), product.lower(), version.lower()),
            {
                "vendor": vendor,
                "product": product,
                "version": version,
            },
        )
    return {machine: list(items.values()) for machine, items in out.items()}


def intune_odata_filter(filters: dict[str, Any], *, now_iso: str = "") -> str:
    """The Graph ``$filter`` of an Intune read (every filter given, ANDed); empty when none."""
    parts: list[str] = []
    for key, prop in (
        ("operating_system", "operatingSystem"),
        ("compliance", "complianceState"),
        ("ownership", "managedDeviceOwnerType"),
    ):
        values = _values(filters, key)
        if values:
            parts.append("(" + " or ".join(f"{prop} eq '{_odata(v)}'" for v in values) + ")")
    if now_iso and filters.get("last_seen_days"):
        parts.append(f"lastSyncDateTime ge {now_iso}")
    return " and ".join(parts)


def defender_odata_filter(filters: dict[str, Any], *, now_iso: str = "") -> str:
    """The Defender ``$filter`` of a machines read; empty when none."""
    parts: list[str] = []
    for key, prop in (
        ("os_platform", "osPlatform"),
        ("health_status", "healthStatus"),
        ("exposure_level", "exposureLevel"),
    ):
        values = _values(filters, key)
        if values:
            parts.append("(" + " or ".join(f"{prop} eq '{_odata(v)}'" for v in values) + ")")
    if now_iso and filters.get("last_seen_days"):
        parts.append(f"lastSeen ge {now_iso}")
    return " and ".join(parts)


def _values(filters: dict[str, Any], key: str) -> list[str]:
    raw = filters.get(key)
    if raw in (None, ""):
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [str(v).strip() for v in items if str(v).strip()]


def _odata(value: str) -> str:
    return value.replace("'", "''")[:100]


def missing_scope_message(source: str, status: int) -> str:
    scopes = ", ".join(REQUIRED_SCOPES.get(source, ()))
    label = "Microsoft Intune" if source == INTUNE else "Microsoft Defender for Endpoint"
    return (
        f"{label} refused the read (HTTP {status}): grant the connected Microsoft integration "
        f"the permission {scopes} and consent it, then run the source again"
    )


#: Why a source is not available, as a code the import card words in the user's language (#1251, UX round 1 B3).
REASON_NOT_CONNECTED = "not_connected"
REASON_NO_TOKEN = "no_token"  # noqa: S105 - a reason code, not a secret


def not_connected_message(source: str) -> str:
    """Why *source* cannot be read: no integration of its own provider (the import card adds where to connect it)."""
    label = "Microsoft Intune" if source == INTUNE else "Microsoft Defender"
    return f"no {label} integration is connected"


def no_token_message(source: str) -> str:
    """Why *source* cannot be read: its integration exists but holds no credential to read with."""
    label = "Microsoft Intune" if source == INTUNE else "Microsoft Defender"
    return f"no token for the {label} integration: authorize it again"


def has_credential(integ: Any) -> bool:
    """Whether an integration row can be read with (no API call): connected, with a stored credential.

    A row created but never authorized, or disconnected, is not a readable source (#1251, UX round 1 B1).
    """
    if getattr(integ, "status", "") != "connected":
        return False
    return any(
        getattr(integ, field, None)
        for field in ("access_token_enc", "refresh_token_enc", "api_key_enc", "client_secret_enc", "extra_config_enc")
    )


# ── Connection (I/O) ──────────────────────────────────────────────────


async def _integration(db: Any, user_id: str, provider: str) -> Any:
    """The administrator's own integration of *provider*, else a company-managed one they may use."""
    import uuid

    from sqlalchemy import case, select

    from app.api.v1.helpers import integration_visible_filter
    from app.models.integration import Integration

    result = await db.execute(
        select(Integration)
        .where(Integration.provider == provider, await integration_visible_filter(db, user_id))
        .order_by(case((Integration.user_id == uuid.UUID(str(user_id)), 0), else_=1), Integration.created_at)
        .limit(1)
    )
    return result.scalars().first()


async def access_token(db: Any, user_id: str, source: str) -> tuple[str | None, str]:
    """A bearer token for *source* (Graph for Intune, Defender for Endpoint for Defender), or the reason."""
    from app.services import integration_tools as it

    provider = INTUNE if source == INTUNE else DEFENDER
    integ = await _integration(db, user_id, provider)
    label = "Microsoft Intune" if source == INTUNE else "Microsoft Defender"
    if integ is None:
        return None, not_connected_message(source)
    token = await it._get_token(db, str(integ.id), provider, user_id=user_id)
    if not token:
        return None, no_token_message(source)
    if source == INTUNE:
        return token, ""
    if token == "__oauth__":  # noqa: S105 - the delegated marker of integration_tools, not a secret
        mde = await it._acquire_azure_delegated_token(str(integ.id), MDE_SCOPE)
        return (mde, "") if mde else (None, f"no token for the {label} integration: authorize it again")
    from app.integrations.providers import defender as mde_provider

    tenant, client_id, secret = mde_provider._parse_token(token)
    mde = await mde_provider._get_token(tenant, client_id, secret, MDE_SCOPE)
    return (mde, "") if mde else (None, f"no token for the {label} integration: check its app registration")


async def connection_status(db: Any, user_id: str, source: str) -> tuple[bool, str, str]:
    """Whether *source* has an integration this administrator may read with (no API call).

    Answers ``(available, reason, reason_code)``; the code is one of ``not_connected`` and ``no_token``.
    """
    provider = INTUNE if source == INTUNE else DEFENDER
    integ = await _integration(db, user_id, provider)
    if integ is None:
        return False, not_connected_message(source), REASON_NOT_CONNECTED
    if not has_credential(integ):
        return False, no_token_message(source), REASON_NO_TOKEN
    return True, "", ""


# ── Reading (I/O) ─────────────────────────────────────────────────────


@dataclass
class EndpointRead:
    """What an endpoint source answered: rows, the software of each row, counts and the error that stopped it."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    software: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    total: int | None = None
    truncated: bool = False
    error: str | None = None
    software_rows: int = 0
    software_cut: int = 0
    #: Devices whose software could not be read (a failed per-device read, a partial inventory):
    #: their stored software is kept, never taken for "no software".
    software_unread: set[str] = field(default_factory=set)
    #: The first reason a software read failed (said in the run status, never a silent zero).
    software_error: str = ""


async def _get_json(client: Any, url: str, headers: dict[str, str], source: str) -> tuple[dict[str, Any] | None, str]:
    """GET *url*, waiting out 429 and 503 with Retry-After (bounded); answers (json, error)."""
    for attempt in range(_RETRIES + 1):
        resp = await client.get(url, headers=headers)
        if resp.status_code in (429, 503) and attempt < _RETRIES:
            try:
                wait = float(resp.headers.get("Retry-After") or 2**attempt)
            except ValueError:
                wait = float(2**attempt)
            await asyncio.sleep(min(max(wait, 0.5), _MAX_WAIT_S))
            continue
        if resp.status_code in (401, 403):
            return None, missing_scope_message(source, resp.status_code)
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            return None, f"{source}: HTTP {resp.status_code}" + (f" ({detail})" if detail else "")
        return resp.json(), ""
    return None, f"{source}: throttled, retry later"


def _error_detail(resp: Any) -> str:
    """The API's own error message (Graph and Defender: ``error.message``), cut short; "" when none."""
    try:
        body = resp.json()
    except Exception:  # not JSON: the status says enough
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return _text(message, 160)


async def _paged(
    client: Any, url: str, headers: dict[str, str], source: str, limit: int
) -> tuple[list[dict[str, Any]], bool, str]:
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        data, err = await _get_json(client, next_url, headers, source)
        if err or data is None:
            return items, False, err or f"{source}: no answer"
        items.extend(v for v in data.get("value") or [] if isinstance(v, dict))
        if len(items) >= limit:
            return items[:limit], bool(data.get("@odata.nextLink")) or len(items) > limit, ""
        next_url = data.get("@odata.nextLink")
    return items, False, ""


def _since(filters: dict[str, Any]) -> str:
    from datetime import UTC, datetime, timedelta

    try:
        days = int(filters.get("last_seen_days") or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return ""
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def read_intune(token: str, filters: dict[str, Any], *, limit: int, client: Any = None) -> EndpointRead:
    """Managed devices and their detected apps, paged (``limit`` devices at most)."""
    from urllib.parse import quote

    from app.core.egress import egress_client

    result = EndpointRead()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    query = f"$top={_PAGE}"
    odata = intune_odata_filter(filters, now_iso=_since(filters))
    if odata:
        query += f"&$filter={quote(odata)}"
    own = client is None
    http = client or egress_client(timeout=60)
    try:
        if own:
            http = await http.__aenter__()
        devices, cut, err = await _paged(
            http, f"{GRAPH_BASE}/deviceManagement/managedDevices?{query}", headers, INTUNE, limit
        )
        if err:
            result.error = err
            return result
        include_owner = bool(filters.get("include_owner"))
        result.rows = [intune_device_row(d, include_owner=include_owner) for d in devices]
        result.total, result.truncated = len(devices), cut
        sem = asyncio.Semaphore(_APP_CONCURRENCY)

        async def apps_of(row: dict[str, Any]) -> None:
            async with sem:
                url = f"{GRAPH_BETA}/deviceManagement/managedDevices/{row['external_id']}/detectedApps"
                apps, app_cut, app_err = await _paged(http, url, headers, INTUNE, MAX_APPS_PER_DEVICE)
            if app_err:
                logger.info("Intune detected apps of %s unreadable: %s", row["external_id"], app_err)
                result.software_unread.add(row["external_id"])
                result.software_error = result.software_error or app_err
                return
            software = intune_software(apps)
            result.software[row["external_id"]] = software
            result.software_rows += len(software)
            result.software_cut += int(app_cut)

        await asyncio.gather(*(apps_of(r) for r in result.rows if r["external_id"]))
        return result
    except Exception as exc:  # reported, never guessed
        logger.warning("Intune read failed: %s", format_exception_message(exc))
        result.error = f"{INTUNE}: {type(exc).__name__}"
        return result
    finally:
        if own:
            await http.__aexit__(None, None, None)


async def read_defender(token: str, filters: dict[str, Any], *, limit: int, client: Any = None) -> EndpointRead:
    """Machines and the tenant's software inventory export, paged (``limit`` machines at most)."""
    from urllib.parse import quote

    from app.core.egress import egress_client

    result = EndpointRead()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    query = ""
    odata = defender_odata_filter(filters, now_iso=_since(filters))
    if odata:
        query = f"?$filter={quote(odata)}"
    own = client is None
    http = client or egress_client(timeout=120)
    try:
        if own:
            http = await http.__aenter__()
        machines, cut, err = await _paged(http, f"{MDE_BASE}/machines{query}", headers, DEFENDER, limit)
        if err:
            result.error = err
            return result
        result.rows = [defender_machine_row(m) for m in machines]
        result.total, result.truncated = len(machines), cut
        wanted = {r["external_id"] for r in result.rows}
        inventory, _cut, inv_err = await _paged(
            http, f"{MDE_BASE}/machines/SoftwareInventoryByMachine?pageSize=50000", headers, DEFENDER, 2_000_000
        )
        if inv_err:
            # Machines without software still sync; the error names the missing permission.
            # A failed or partial inventory says nothing about the machines it did not reach.
            logger.info("Defender software inventory unreadable: %s", inv_err)
            result.error = None
            result.software_unread = set(wanted)
            result.software_error = inv_err
            return result
        by_machine = defender_software_by_machine(inventory)
        for machine_id, software in by_machine.items():
            if machine_id in wanted:
                result.software[machine_id] = software
                result.software_rows += len(software)
        return result
    except Exception as exc:
        logger.warning("Defender read failed: %s", format_exception_message(exc))
        result.error = f"{DEFENDER}: {type(exc).__name__}"
        return result
    finally:
        if own:
            await http.__aexit__(None, None, None)
