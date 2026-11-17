"""OVHcloud Public Cloud (``/cloud/project``): projects, instances, storage, networks, Kubernetes, databases."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    LIMIT,
    OFFSET,
    PROJECT,
    REGION,
    OvhClient,
    clamp,
    list_with_details,
    page_note,
    render,
    seg,
    tool,
)

_DB_ENGINES = (
    "cassandra",
    "grafana",
    "kafka",
    "kafkaConnect",
    "kafkaMirrorMaker",
    "m3aggregator",
    "m3db",
    "mongodb",
    "mysql",
    "opensearch",
    "postgresql",
    "redis",
    "valkey",
)
_INSTANCE = {"type": "string", "description": "Public Cloud instance id."}


def _project_path(project_id: str) -> str:
    return f"/cloud/project/{seg(project_id)}"


def _page(items: Any, limit: int, offset: int) -> tuple[list[Any], int]:
    rows = list(items or [])
    return rows[offset : offset + limit], len(rows)


def instance_summary(instance: dict[str, Any]) -> dict[str, Any]:
    """The fields of an instance an operator reads first."""
    flavor = instance.get("flavor") or {}
    image = instance.get("image") or {}
    return {
        "id": instance.get("id"),
        "name": instance.get("name"),
        "status": instance.get("status"),
        "region": instance.get("region"),
        "flavor": flavor.get("name") or instance.get("flavorId"),
        "vcpus": flavor.get("vcpus"),
        "ram_mb": flavor.get("ram"),
        "image": image.get("name") or instance.get("imageId"),
        "os_type": image.get("type") or flavor.get("osType"),
        "ip_addresses": instance.get("ipAddresses") or [],
        "created": instance.get("created"),
        "monthly_billing": bool(instance.get("monthlyBilling")),
    }


def register(client: OvhClient, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the Public Cloud tools."""

    async def list_ovhcloud_cloud_projects() -> str:
        ids, rows, err = await list_with_details(
            client, "/cloud/project", _project_path, "Public Cloud projects", limit=100, offset=0
        )
        if err:
            return err
        return render(f"{len(ids or [])} Public Cloud project(s)", rows)

    async def list_ovhcloud_cloud_instances(project_id: str, region: str = "", limit: int = 50, offset: int = 0) -> str:
        limit, offset = clamp(limit, 1, 200, 50), clamp(offset, 0, 100_000, 0)
        data, err = await client.get(
            f"{_project_path(project_id)}/instance",
            f"list the instances of project {project_id}",
            {"region": region.strip() or None},
        )
        if err:
            return err
        page, total = _page(data, limit, offset)
        rows = [instance_summary(item) for item in page if isinstance(item, dict)]
        return render(f"{total} instance(s) in project {project_id}{page_note(total, offset, len(rows))}", rows)

    async def get_ovhcloud_cloud_instance(project_id: str, instance_id: str) -> str:
        data, err = await client.get(
            f"{_project_path(project_id)}/instance/{seg(instance_id)}",
            f"read instance {instance_id}",
        )
        return err or render(f"Instance {instance_id} of project {project_id}", data)

    async def list_ovhcloud_cloud_volumes(project_id: str, region: str = "", limit: int = 50, offset: int = 0) -> str:
        limit, offset = clamp(limit, 1, 200, 50), clamp(offset, 0, 100_000, 0)
        data, err = await client.get(
            f"{_project_path(project_id)}/volume",
            f"list the volumes of project {project_id}",
            {"region": region.strip() or None},
        )
        if err:
            return err
        page, total = _page(data, limit, offset)
        return render(f"{total} volume(s) in project {project_id}{page_note(total, offset, len(page))}", page)

    async def list_ovhcloud_cloud_networks(project_id: str) -> str:
        path = _project_path(project_id)
        private, err = await client.get(f"{path}/network/private", f"list the private networks of {project_id}")
        if err:
            return err
        public, pub_err = await client.get(f"{path}/network/public", f"list the public networks of {project_id}")
        return render(
            f"Networks of project {project_id} (private networks are vRack VLANs)",
            {"private": private or [], "public": pub_err or public},
        )

    async def list_ovhcloud_cloud_load_balancers(project_id: str, region: str = "") -> str:
        path = _project_path(project_id)
        regions = [region.strip()] if region.strip() else None
        if regions is None:
            listed, err = await client.get(f"{path}/region", f"list the regions of project {project_id}")
            if err:
                return err
            regions = [str(r) for r in (listed or [])]
        found: dict[str, Any] = {}
        for name in regions[:40]:
            data, err = await client.get(
                f"{path}/region/{seg(name)}/loadbalancing/loadbalancer",
                f"list the load balancers of {project_id} in {name}",
            )
            if not err and data:
                found[name] = data
        total = sum(len(v) for v in found.values() if isinstance(v, list))
        return render(f"{total} load balancer(s) in project {project_id} across {len(regions)} region(s) read", found)

    async def list_ovhcloud_kubernetes_clusters(project_id: str, include_node_pools: bool = True) -> str:
        path = f"{_project_path(project_id)}/kube"
        ids, rows, err = await list_with_details(
            client, path, lambda kube_id: f"{path}/{seg(kube_id)}", "Kubernetes clusters", limit=50, offset=0
        )
        if err:
            return err
        if include_node_pools:
            for row in rows:
                kube_id = row.get("id")
                if kube_id and "error" not in row:
                    pools, pool_err = await client.get(
                        f"{path}/{seg(kube_id)}/nodepool", f"list the node pools of {kube_id}"
                    )
                    row["node_pools"] = pool_err or pools
        return render(f"{len(ids or [])} Managed Kubernetes cluster(s) in project {project_id}", rows)

    async def list_ovhcloud_cloud_databases(project_id: str, engine: str = "") -> str:
        path = f"{_project_path(project_id)}/database"
        engines = [engine.strip()] if engine.strip() else list(_DB_ENGINES)
        found: dict[str, Any] = {}
        for name in engines:
            _ids, rows, err = await list_with_details(
                client,
                f"{path}/{seg(name)}",
                lambda cluster_id, n=name: f"{path}/{seg(n)}/{seg(cluster_id)}",
                f"{name} databases",
                limit=50,
                offset=0,
            )
            if err:
                if engine.strip():
                    return err
                continue
            if rows:
                found[name] = rows
        total = sum(len(v) for v in found.values())
        return render(f"{total} Managed Database service(s) in project {project_id}", found)

    async def list_ovhcloud_cloud_storage(project_id: str, region: str = "") -> str:
        path = _project_path(project_id)
        swift, err = await client.get(f"{path}/storage", f"list the object storage of {project_id}")
        if err:
            return err
        rows = [
            {"name": c.get("name"), "region": c.get("region"), "type": "swift"}
            for c in (swift or [])
            if isinstance(c, dict)
        ]
        if region.strip():
            s3, s3_err = await client.get(
                f"{path}/region/{seg(region.strip())}/storage",
                f"list the S3 buckets of {project_id} in {region}",
            )
            if not s3_err:
                rows += [
                    {"name": c.get("name"), "region": c.get("region") or region, "type": "s3"}
                    for c in (s3 or [])
                    if isinstance(c, dict)
                ]
        return render(f"{len(rows)} object storage container(s) in project {project_id} (names and regions)", rows)

    async def list_ovhcloud_cloud_ssh_keys(project_id: str) -> str:
        data, err = await client.get(f"{_project_path(project_id)}/sshkey", f"list the SSH keys of {project_id}")
        if err:
            return err
        rows = [
            {"id": k.get("id"), "name": k.get("name"), "regions": k.get("regions")}
            for k in (data or [])
            if isinstance(k, dict)
        ]
        return render(f"{len(rows)} SSH key(s) in project {project_id} (names only)", rows)

    async def get_ovhcloud_cloud_quota(project_id: str) -> str:
        data, err = await client.get(f"{_project_path(project_id)}/quota", f"read the quota of {project_id}")
        return err or render(f"Quota of project {project_id} per region", data)

    async def _instance_action(project_id: str, instance_id: str, action: str, body: Any = None) -> str:
        result, err = await client.request(
            "POST",
            f"{_project_path(project_id)}/instance/{seg(instance_id)}/{action}",
            f"{action} instance {instance_id}",
            body=body,
        )
        return err or render(f"{action.capitalize()} of instance {instance_id} requested", result or {})

    async def reboot_ovhcloud_cloud_instance(project_id: str, instance_id: str, hard: bool = False) -> str:
        return await _instance_action(project_id, instance_id, "reboot", {"type": "hard" if hard else "soft"})

    async def start_ovhcloud_cloud_instance(project_id: str, instance_id: str) -> str:
        return await _instance_action(project_id, instance_id, "start")

    async def stop_ovhcloud_cloud_instance(project_id: str, instance_id: str) -> str:
        return await _instance_action(project_id, instance_id, "stop")

    handlers = {
        "list_ovhcloud_cloud_projects": list_ovhcloud_cloud_projects,
        "list_ovhcloud_cloud_instances": list_ovhcloud_cloud_instances,
        "get_ovhcloud_cloud_instance": get_ovhcloud_cloud_instance,
        "list_ovhcloud_cloud_volumes": list_ovhcloud_cloud_volumes,
        "list_ovhcloud_cloud_networks": list_ovhcloud_cloud_networks,
        "list_ovhcloud_cloud_load_balancers": list_ovhcloud_cloud_load_balancers,
        "list_ovhcloud_kubernetes_clusters": list_ovhcloud_kubernetes_clusters,
        "list_ovhcloud_cloud_databases": list_ovhcloud_cloud_databases,
        "list_ovhcloud_cloud_storage": list_ovhcloud_cloud_storage,
        "list_ovhcloud_cloud_ssh_keys": list_ovhcloud_cloud_ssh_keys,
        "get_ovhcloud_cloud_quota": get_ovhcloud_cloud_quota,
        "reboot_ovhcloud_cloud_instance": reboot_ovhcloud_cloud_instance,
        "start_ovhcloud_cloud_instance": start_ovhcloud_cloud_instance,
        "stop_ovhcloud_cloud_instance": stop_ovhcloud_cloud_instance,
    }
    for name, handler in handlers.items():
        executor.register(name, handler)
    project = {"project_id": PROJECT}
    instance = {**project, "instance_id": _INSTANCE}
    return [
        tool(
            "list_ovhcloud_cloud_projects",
            "List the OVHcloud Public Cloud projects: id, description, status, plan.",
            priority=0,
        ),
        tool(
            "list_ovhcloud_cloud_instances",
            "List one Public Cloud project's instances: name, status, region, flavor, image, IP addresses.",
            {**project, "region": REGION, "limit": LIMIT, "offset": OFFSET},
            ["project_id"],
            priority=0,
        ),
        tool(
            "get_ovhcloud_cloud_instance",
            "Read one Public Cloud instance in full: flavor, image, IP addresses, attached volumes, SSH key name.",
            instance,
            ["project_id", "instance_id"],
        ),
        tool(
            "list_ovhcloud_cloud_volumes",
            "List one Public Cloud project's block storage volumes: size, type, region, attached instances.",
            {**project, "region": REGION, "limit": LIMIT, "offset": OFFSET},
            ["project_id"],
        ),
        tool(
            "list_ovhcloud_cloud_networks",
            "List one Public Cloud project's private (vRack) and public networks.",
            project,
            ["project_id"],
        ),
        tool(
            "list_ovhcloud_cloud_load_balancers",
            "List one Public Cloud project's load balancers, per region (every region when none is given).",
            {**project, "region": REGION},
            ["project_id"],
            priority=2,
        ),
        tool(
            "list_ovhcloud_kubernetes_clusters",
            "List one Public Cloud project's Managed Kubernetes clusters: version, region, status, node pools.",
            {
                **project,
                "include_node_pools": {
                    "type": "boolean",
                    "description": "Read each cluster's node pools (default true).",
                },
            },
            ["project_id"],
        ),
        tool(
            "list_ovhcloud_cloud_databases",
            "List one Public Cloud project's Managed Databases per engine: plan, version, status, nodes, endpoints.",
            {
                **project,
                "engine": {
                    "type": "string",
                    "enum": list(_DB_ENGINES),
                    "description": "One engine (default: every engine).",
                },
            },
            ["project_id"],
            priority=2,
        ),
        tool(
            "list_ovhcloud_cloud_storage",
            "List one Public Cloud project's object storage containers: names and regions only (S3 buckets of a region when given).",
            {**project, "region": REGION},
            ["project_id"],
            priority=2,
        ),
        tool(
            "list_ovhcloud_cloud_ssh_keys",
            "List the names of one Public Cloud project's SSH keys.",
            project,
            ["project_id"],
            priority=2,
        ),
        tool(
            "get_ovhcloud_cloud_quota",
            "Read one Public Cloud project's quota and usage per region (instances, cores, RAM, volumes).",
            project,
            ["project_id"],
            priority=2,
        ),
        tool(
            "reboot_ovhcloud_cloud_instance",
            "Reboot one Public Cloud instance (soft by default, hard when asked).",
            {**instance, "hard": {"type": "boolean", "description": "Hard reboot (power cycle) instead of soft."}},
            ["project_id", "instance_id"],
            priority=2,
        ),
        tool(
            "start_ovhcloud_cloud_instance",
            "Start one stopped (shelved or shut off) Public Cloud instance.",
            instance,
            ["project_id", "instance_id"],
            priority=2,
        ),
        tool(
            "stop_ovhcloud_cloud_instance",
            "Stop one Public Cloud instance. Interrupts its service.",
            instance,
            ["project_id", "instance_id"],
            priority=2,
        ),
    ]
