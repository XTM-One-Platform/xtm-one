"""One normalized compute inventory across bare metal, Public Cloud, VPS and Hosted Private Cloud.

Each row has the same keys whatever the product: ``type``, ``id``, ``name``,
``hostname``, ``platform`` (the OS), ``arch``, ``public_ips``,
``private_ips``, ``region``, ``state``, ``project``, ``tags``. The OpenAEV
OVHcloud collector reads the same shape.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import OFFSET, OvhClient, clamp, page_note, render, seg, tool
from .vps_hpc import private_cloud_vms

TYPES = ("dedicated_server", "cloud_instance", "vps", "private_cloud_vm")
#: The most assets read per product, so one call stays bounded.
_MAX_PER_TYPE = 500


#: Internal address ranges (RFC 1918, shared CGNAT, loopback, link-local, ULA);
#: any other address is answered as public. ``ip_address.is_private`` is not
#: used: it also calls documentation and reserved ranges private.
_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)


def _is_internal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address.version == net.version and address in net for net in _INTERNAL_NETWORKS)


def _split_ips(values: list[Any]) -> tuple[list[str], list[str]]:
    public: list[str] = []
    private: list[str] = []
    for value in values:
        text = str(value).split("/", 1)[0].strip()
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        (private if _is_internal(address) else public).append(text)
    return sorted(set(public)), sorted(set(private))


def _arch(text: Any) -> str:
    lowered = str(text or "").lower()
    if any(token in lowered for token in ("arm", "aarch64")):
        return "arm64"
    if any(token in lowered for token in ("x86_64", "amd64", "x64", "64")):
        return "x86_64"
    return ""


def dedicated_row(server: dict[str, Any], ips: list[Any], tags: dict[str, Any] | None) -> dict[str, Any]:
    public, private = _split_ips([server.get("ip"), *ips])
    iam = server.get("iam") if isinstance(server.get("iam"), dict) else {}
    return {
        "type": "dedicated_server",
        "id": server.get("name"),
        "name": iam.get("displayName") or server.get("name"),
        "hostname": server.get("reverse") or server.get("name"),
        "platform": server.get("os") or "",
        "arch": _arch(server.get("os")),
        "public_ips": public,
        "private_ips": private,
        "region": server.get("datacenter") or server.get("region") or "",
        "state": server.get("state") or "",
        "project": "",
        "tags": tags or {},
    }


def cloud_row(instance: dict[str, Any], project_id: str) -> dict[str, Any]:
    addresses = instance.get("ipAddresses") or []
    public = sorted(
        {str(a["ip"]) for a in addresses if isinstance(a, dict) and a.get("type") == "public" and a.get("ip")}
    )
    private = sorted(
        {str(a["ip"]) for a in addresses if isinstance(a, dict) and a.get("type") == "private" and a.get("ip")}
    )
    image = instance.get("image") or {}
    flavor = instance.get("flavor") or {}
    platform = image.get("name") or image.get("type") or flavor.get("osType") or ""
    return {
        "type": "cloud_instance",
        "id": instance.get("id"),
        "name": instance.get("name"),
        "hostname": instance.get("name"),
        "platform": platform,
        "arch": _arch(platform),
        "public_ips": public,
        "private_ips": private,
        "region": instance.get("region") or "",
        "state": instance.get("status") or "",
        "project": project_id,
        "tags": {},
    }


def vps_row(vps: dict[str, Any], ips: list[Any], distribution: dict[str, Any] | None) -> dict[str, Any]:
    public, private = _split_ips(ips)
    model = vps.get("model") or {}
    platform = (distribution or {}).get("name") or (distribution or {}).get("distribution") or ""
    return {
        "type": "vps",
        "id": vps.get("name"),
        "name": vps.get("displayName") or vps.get("name"),
        "hostname": vps.get("name"),
        "platform": platform,
        "arch": _arch((distribution or {}).get("bitFormat") or platform),
        "public_ips": public,
        "private_ips": private,
        "region": vps.get("zone") or "",
        "state": vps.get("state") or "",
        "project": "",
        "tags": {"model": model.get("name")} if isinstance(model, dict) and model.get("name") else {},
    }


def private_cloud_row(vm: dict[str, Any], service: str, datacenter: Any) -> dict[str, Any]:
    raw_ips: list[Any] = []
    for nic in vm.get("networks") or vm.get("vmNetworks") or []:
        if isinstance(nic, dict):
            raw_ips += nic.get("ips") or nic.get("ipAddresses") or []
    public, private = _split_ips(raw_ips)
    return {
        "type": "private_cloud_vm",
        "id": str(vm.get("vmId") or vm.get("moRef") or ""),
        "name": vm.get("name"),
        "hostname": vm.get("hostName") or vm.get("name"),
        "platform": vm.get("guestOs") or vm.get("guestOsFamily") or "",
        "arch": _arch(vm.get("guestOs")),
        "public_ips": public,
        "private_ips": private,
        "region": f"{service}/{datacenter}",
        "state": vm.get("powerState") or "",
        "project": service,
        "tags": {},
    }


async def collect_inventory(client: OvhClient, types: set[str], project_id: str = "") -> tuple[list[dict], list[str]]:
    """Every compute asset of *types*, normalized; the errors met are listed, never raised."""
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if "dedicated_server" in types:
        names, err = await client.get("/dedicated/server", "list dedicated servers")
        if err:
            errors.append(err)
        for name in list(names or [])[:_MAX_PER_TYPE]:
            path = f"/dedicated/server/{seg(name)}"
            server, s_err = await client.get(path, f"read dedicated server {name}")
            if s_err or not isinstance(server, dict):
                errors.append(s_err or f"dedicated server {name}: no detail")
                continue
            ips, _ = await client.get(f"{path}/ips", f"list the IPs of {name}")
            tags, _ = await client.get(f"{path}/tags", f"list the tags of {name}")
            rows.append(dedicated_row(server, list(ips or []), tags if isinstance(tags, dict) else None))
    if "cloud_instance" in types:
        projects = [project_id] if project_id else None
        if projects is None:
            listed, err = await client.get("/cloud/project", "list Public Cloud projects")
            if err:
                errors.append(err)
            projects = [str(p) for p in (listed or [])]
        for project in projects:
            instances, err = await client.get(
                f"/cloud/project/{seg(project)}/instance", f"list the instances of {project}"
            )
            if err:
                errors.append(err)
                continue
            rows += [cloud_row(i, project) for i in list(instances or [])[:_MAX_PER_TYPE] if isinstance(i, dict)]
    if "vps" in types:
        names, err = await client.get("/vps", "list VPS")
        if err:
            errors.append(err)
        for name in list(names or [])[:_MAX_PER_TYPE]:
            path = f"/vps/{seg(name)}"
            vps, v_err = await client.get(path, f"read VPS {name}")
            if v_err or not isinstance(vps, dict):
                errors.append(v_err or f"VPS {name}: no detail")
                continue
            ips, _ = await client.get(f"{path}/ips", f"list the IPs of {name}")
            distribution, _ = await client.get(f"{path}/distribution", f"read the distribution of {name}")
            rows.append(vps_row(vps, list(ips or []), distribution if isinstance(distribution, dict) else None))
    if "private_cloud_vm" in types:
        services, err = await client.get("/dedicatedCloud", "list Hosted Private Cloud services")
        if err:
            errors.append(err)
        for service in list(services or [])[:50]:
            datacenters, dc_err = await client.get(
                f"/dedicatedCloud/{seg(service)}/datacenter", f"list the datacenters of {service}"
            )
            if dc_err:
                errors.append(dc_err)
                continue
            for datacenter in list(datacenters or []):
                vms, vm_err = await private_cloud_vms(client, str(service), datacenter)
                if vm_err:
                    errors.append(vm_err)
                rows += [private_cloud_row(vm, str(service), datacenter) for vm in vms if "error" not in vm]
    return rows, errors


def filter_rows(rows: list[dict[str, Any]], region: str = "", tag: str = "") -> list[dict[str, Any]]:
    """Rows in *region* (case-insensitive substring) carrying *tag* (``key`` or ``key=value``)."""
    out = rows
    if region.strip():
        needle = region.strip().lower()
        out = [r for r in out if needle in str(r.get("region") or "").lower()]
    if tag.strip():
        key, _, value = tag.strip().partition("=")
        out = [
            r
            for r in out
            if key in (r.get("tags") or {}) and (not value or str((r.get("tags") or {}).get(key)) == value)
        ]
    return out


def register(client: OvhClient, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the cross-product inventory tool."""

    async def list_ovhcloud_compute_inventory(
        types: list[str] | None = None,
        region: str = "",
        project_id: str = "",
        tag: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> str:
        wanted = {t for t in (types or TYPES) if t in TYPES} or set(TYPES)
        limit, offset = clamp(limit, 1, 500, 100), clamp(offset, 0, 100_000, 0)
        rows, errors = await collect_inventory(client, wanted, project_id.strip())
        rows = filter_rows(rows, region, tag)
        page = rows[offset : offset + limit]
        title = (
            f"{len(rows)} compute asset(s) across {', '.join(sorted(wanted))}{page_note(len(rows), offset, len(page))}"
        )
        data: dict[str, Any] = {"assets": page}
        if errors:
            data["errors"] = errors[:10]
            title += f"; {len(errors)} read(s) failed, listed under errors"
        return render(title, data)

    executor.register("list_ovhcloud_compute_inventory", list_ovhcloud_compute_inventory)
    return [
        tool(
            "list_ovhcloud_compute_inventory",
            "Every OVHcloud compute asset in one call as normalized rows (type, id, name, hostname, OS, arch, "
            "public and private IPs, region, state, project, tags) across bare metal, Public Cloud, VPS and "
            "Hosted Private Cloud, with filters.",
            {
                "types": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(TYPES)},
                    "description": "Asset types (default: all).",
                },
                "region": {
                    "type": "string",
                    "description": "Region or datacenter filter (substring), e.g. GRA or rbx.",
                },
                "project_id": {"type": "string", "description": "Only this Public Cloud project for cloud instances."},
                "tag": {"type": "string", "description": "Tag filter, key or key=value (bare metal IAM tags)."},
                "limit": {"type": "integer", "description": "Rows answered (default 100, max 500)."},
                "offset": OFFSET,
            },
            priority=0,
        ),
    ]
