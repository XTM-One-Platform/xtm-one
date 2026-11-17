"""OVHcloud bare metal (``/dedicated/server``): servers, IPs, hardware, tasks, reboot."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import LIMIT, OFFSET, OvhClient, clamp, list_with_details, page_note, render, seg, tool

_SERVER = {"type": "string", "description": "Dedicated server service name, e.g. ns1234567.ip-1-2-3.eu."}


def register(client: OvhClient, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the bare metal tools."""

    async def list_ovhcloud_dedicated_servers(limit: int = 25, offset: int = 0) -> str:
        limit, offset = clamp(limit, 1, 100, 25), clamp(offset, 0, 100_000, 0)
        ids, rows, err = await list_with_details(
            client,
            "/dedicated/server",
            lambda name: f"/dedicated/server/{seg(name)}",
            "dedicated servers",
            limit=limit,
            offset=offset,
        )
        if err:
            return err
        title = f"{len(ids or [])} dedicated server(s){page_note(len(ids or []), offset, len(rows))}"
        return render(title, rows)

    async def get_ovhcloud_dedicated_server(service_name: str) -> str:
        path = f"/dedicated/server/{seg(service_name)}"
        server, err = await client.get(path, f"read dedicated server {service_name}")
        if err:
            return err
        ips, ips_err = await client.get(f"{path}/ips", f"list the IPs of {service_name}")
        info, info_err = await client.get(f"{path}/serviceInfos", f"read the service of {service_name}")
        tags, _ = await client.get(f"{path}/tags", f"list the tags of {service_name}")
        data: dict[str, Any] = {
            "server": server,
            "ips": ips_err or ips,
            "service": info_err or info,
        }
        if tags:
            data["tags"] = tags
        return render(f"Dedicated server {service_name}", data)

    async def list_ovhcloud_dedicated_server_ips(service_name: str) -> str:
        ips, err = await client.get(f"/dedicated/server/{seg(service_name)}/ips", f"list the IPs of {service_name}")
        return err or render(f"IPs of {service_name}", ips)

    async def get_ovhcloud_dedicated_server_hardware(service_name: str) -> str:
        path = f"/dedicated/server/{seg(service_name)}/specifications"
        hardware, err = await client.get(f"{path}/hardware", f"read the hardware of {service_name}")
        if err:
            return err
        network, net_err = await client.get(f"{path}/network", f"read the network of {service_name}")
        return render(
            f"Hardware and network specifications of {service_name}",
            {"hardware": hardware, "network": net_err or network},
        )

    async def list_ovhcloud_dedicated_server_tasks(service_name: str, limit: int = 25) -> str:
        limit = clamp(limit, 1, 100, 25)
        path = f"/dedicated/server/{seg(service_name)}"
        ids, rows, err = await list_with_details(
            client,
            f"{path}/task",
            lambda task_id: f"{path}/task/{seg(task_id)}",
            f"tasks of {service_name}",
            limit=limit,
            offset=0,
        )
        if err:
            return err
        interventions, _ = await client.get(f"{path}/intervention", f"list the interventions of {service_name}")
        return render(
            f"{len(ids or [])} task(s) of {service_name} ({len(rows)} newest read)",
            {"tasks": rows, "interventions": interventions or []},
        )

    async def reboot_ovhcloud_dedicated_server(service_name: str) -> str:
        task, err = await client.request(
            "POST",
            f"/dedicated/server/{seg(service_name)}/reboot",
            f"reboot dedicated server {service_name}",
        )
        return err or render(f"Hard reboot of {service_name} requested", task)

    handlers = {
        "list_ovhcloud_dedicated_servers": list_ovhcloud_dedicated_servers,
        "get_ovhcloud_dedicated_server": get_ovhcloud_dedicated_server,
        "list_ovhcloud_dedicated_server_ips": list_ovhcloud_dedicated_server_ips,
        "get_ovhcloud_dedicated_server_hardware": get_ovhcloud_dedicated_server_hardware,
        "list_ovhcloud_dedicated_server_tasks": list_ovhcloud_dedicated_server_tasks,
        "reboot_ovhcloud_dedicated_server": reboot_ovhcloud_dedicated_server,
    }
    for name, handler in handlers.items():
        executor.register(name, handler)
    server = {"service_name": _SERVER}
    return [
        tool(
            "list_ovhcloud_dedicated_servers",
            "List OVHcloud bare metal servers with their detail: state, datacenter, rack, OS, "
            "commercial range, reverse, IP, monitoring, support level.",
            {"limit": LIMIT, "offset": OFFSET},
            priority=0,
        ),
        tool(
            "get_ovhcloud_dedicated_server",
            "Read one bare metal server: its detail, IPs, service info (expiration, renewal) and tags.",
            server,
            ["service_name"],
        ),
        tool(
            "list_ovhcloud_dedicated_server_ips",
            "List the IP addresses and blocks routed to one bare metal server.",
            server,
            ["service_name"],
        ),
        tool(
            "get_ovhcloud_dedicated_server_hardware",
            "Read one bare metal server's hardware (CPU, memory, disks) and network specifications.",
            server,
            ["service_name"],
        ),
        tool(
            "list_ovhcloud_dedicated_server_tasks",
            "List one bare metal server's tasks (newest read in detail) and its interventions.",
            {**server, "limit": LIMIT},
            ["service_name"],
            priority=2,
        ),
        tool(
            "reboot_ovhcloud_dedicated_server",
            "Hard reboot one bare metal server (power cycle). Interrupts its service.",
            server,
            ["service_name"],
            priority=2,
        ),
    ]
