"""OVHcloud VPS (``/vps``) and Hosted Private Cloud VMware (``/dedicatedCloud``)."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import LIMIT, OFFSET, OvhClient, clamp, list_with_details, page_note, render, seg, tool

_VPS = {"type": "string", "description": "VPS service name, e.g. vps-1a2b3c4d.vps.ovh.net."}
_HPC = {"type": "string", "description": "Hosted Private Cloud service name, e.g. pcc-1-2-3-4."}
_DATACENTER = {"type": "integer", "description": "Datacenter id (from list_ovhcloud_private_cloud_datacenters)."}


async def private_cloud_vms(client: OvhClient, service: str, datacenter_id: Any) -> tuple[list[dict], str | None]:
    """Every VM of one Hosted Private Cloud datacenter, in detail."""
    path = f"/dedicatedCloud/{seg(service)}/datacenter/{seg(datacenter_id)}/vm"
    ids, err = await client.get(path, f"list the VMs of {service} datacenter {datacenter_id}")
    if err:
        return [], err
    return await client.details(list(ids or [])[:200], lambda vm_id: f"{path}/{seg(vm_id)}", "read VM"), None


def register(client: OvhClient, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the VPS and Hosted Private Cloud tools."""

    async def list_ovhcloud_vps(limit: int = 25, offset: int = 0) -> str:
        limit, offset = clamp(limit, 1, 100, 25), clamp(offset, 0, 100_000, 0)
        ids, rows, err = await list_with_details(
            client, "/vps", lambda name: f"/vps/{seg(name)}", "VPS", limit=limit, offset=offset
        )
        if err:
            return err
        return render(f"{len(ids or [])} VPS{page_note(len(ids or []), offset, len(rows))}", rows)

    async def get_ovhcloud_vps(service_name: str) -> str:
        path = f"/vps/{seg(service_name)}"
        vps, err = await client.get(path, f"read VPS {service_name}")
        if err:
            return err
        ips, ips_err = await client.get(f"{path}/ips", f"list the IPs of {service_name}")
        distribution, _ = await client.get(f"{path}/distribution", f"read the distribution of {service_name}")
        datacenter, _ = await client.get(f"{path}/datacenter", f"read the datacenter of {service_name}")
        return render(
            f"VPS {service_name}",
            {
                "vps": vps,
                "ips": ips_err or ips,
                "distribution": distribution,
                "datacenter": datacenter,
            },
        )

    async def reboot_ovhcloud_vps(service_name: str) -> str:
        task, err = await client.request("POST", f"/vps/{seg(service_name)}/reboot", f"reboot VPS {service_name}")
        return err or render(f"Reboot of VPS {service_name} requested", task)

    async def list_ovhcloud_private_clouds() -> str:
        ids, rows, err = await list_with_details(
            client,
            "/dedicatedCloud",
            lambda name: f"/dedicatedCloud/{seg(name)}",
            "Hosted Private Cloud services",
            limit=100,
            offset=0,
        )
        if err:
            return err
        return render(f"{len(ids or [])} Hosted Private Cloud service(s)", rows)

    async def list_ovhcloud_private_cloud_datacenters(service_name: str) -> str:
        path = f"/dedicatedCloud/{seg(service_name)}/datacenter"
        ids, rows, err = await list_with_details(
            client, path, lambda dc: f"{path}/{seg(dc)}", f"datacenters of {service_name}", limit=100, offset=0
        )
        if err:
            return err
        return render(f"{len(ids or [])} datacenter(s) in {service_name}", rows)

    async def list_ovhcloud_private_cloud_vms(
        service_name: str, datacenter_id: int, limit: int = 50, offset: int = 0
    ) -> str:
        limit, offset = clamp(limit, 1, 200, 50), clamp(offset, 0, 100_000, 0)
        rows, err = await private_cloud_vms(client, service_name, datacenter_id)
        if err:
            return err
        page = rows[offset : offset + limit]
        return render(
            f"{len(rows)} VM(s) in {service_name} datacenter {datacenter_id}{page_note(len(rows), offset, len(page))}",
            page,
        )

    handlers = {
        "list_ovhcloud_vps": list_ovhcloud_vps,
        "get_ovhcloud_vps": get_ovhcloud_vps,
        "reboot_ovhcloud_vps": reboot_ovhcloud_vps,
        "list_ovhcloud_private_clouds": list_ovhcloud_private_clouds,
        "list_ovhcloud_private_cloud_datacenters": list_ovhcloud_private_cloud_datacenters,
        "list_ovhcloud_private_cloud_vms": list_ovhcloud_private_cloud_vms,
    }
    for name, handler in handlers.items():
        executor.register(name, handler)
    return [
        tool(
            "list_ovhcloud_vps",
            "List OVHcloud VPS with their detail: state, model, vCores, memory, zone, display name.",
            {"limit": LIMIT, "offset": OFFSET},
            priority=0,
        ),
        tool(
            "get_ovhcloud_vps",
            "Read one VPS: detail, IP addresses, distribution (OS) and datacenter.",
            {"service_name": _VPS},
            ["service_name"],
        ),
        tool(
            "reboot_ovhcloud_vps",
            "Reboot one VPS. Interrupts its service.",
            {"service_name": _VPS},
            ["service_name"],
            priority=2,
        ),
        tool(
            "list_ovhcloud_private_clouds",
            "List the Hosted Private Cloud (VMware) services: location, version, state, management interface.",
        ),
        tool(
            "list_ovhcloud_private_cloud_datacenters",
            "List the datacenters (virtual datacenters) of one Hosted Private Cloud service.",
            {"service_name": _HPC},
            ["service_name"],
        ),
        tool(
            "list_ovhcloud_private_cloud_vms",
            "List the VMs of one Hosted Private Cloud datacenter: name, power state, CPU, memory, network config.",
            {"service_name": _HPC, "datacenter_id": _DATACENTER, "limit": LIMIT, "offset": OFFSET},
            ["service_name", "datacenter_id"],
        ),
    ]
