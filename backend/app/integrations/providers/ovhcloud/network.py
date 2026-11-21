"""OVHcloud network and exposure: IP blocks, firewall (read, approval-gated block), mitigation, IP Load Balancing, vRack, DNS, any GET."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import LIMIT, OFFSET, OvhClient, clamp, list_with_details, page_note, render, seg, tool

_IP_BLOCK = {"type": "string", "description": "IP block, e.g. 203.0.113.0/28 or 203.0.113.4/32."}
_IPLB = {"type": "string", "description": "IP Load Balancing service name, e.g. loadbalancer-1a2b3c."}
_ZONE = {"type": "string", "description": "DNS zone name, e.g. example.com."}
# The Edge Network Firewall applies rules by ascending sequence (0 to 19).
_BLOCK_SEQUENCE = 0
_IP_TYPES = (
    "cdn",
    "cloud",
    "dedicated",
    "failover",
    "hosted_ssl",
    "housing",
    "loadBalancing",
    "mail",
    "overthebox",
    "pcc",
    "pci",
    "private",
    "vpn",
    "vps",
    "vrack",
    "xdsl",
)


def normalize_api_path(path: str) -> str | None:
    """*path* as a path under ``/1.0``, or ``None`` when it is not one."""
    value = (path or "").strip()
    if not value or "://" in value or ".." in value or "\\" in value:
        return None
    if value.startswith("/1.0/"):
        value = value[len("/1.0") :]
    if not value.startswith("/"):
        value = "/" + value
    return value.split("?", 1)[0]


def register(client: OvhClient, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the network, DNS and generic read tools."""

    async def list_ovhcloud_ips(ip_type: str = "", routed_to: str = "", limit: int = 50, offset: int = 0) -> str:
        limit, offset = clamp(limit, 1, 200, 50), clamp(offset, 0, 100_000, 0)
        ids, rows, err = await list_with_details(
            client,
            "/ip",
            lambda block: f"/ip/{seg(block)}",
            "IP blocks",
            limit=limit,
            offset=offset,
            params={"type": ip_type.strip() or None, "routedTo.serviceName": routed_to.strip() or None},
        )
        if err:
            return err
        return render(f"{len(ids or [])} IP block(s){page_note(len(ids or []), offset, len(rows))}", rows)

    async def get_ovhcloud_ip(ip_block: str) -> str:
        path = f"/ip/{seg(ip_block)}"
        block, err = await client.get(path, f"read IP block {ip_block}")
        if err:
            return err
        reverse, _ = await client.get(f"{path}/reverse", f"list the reverses of {ip_block}")
        firewall, _ = await client.get(f"{path}/firewall", f"list the firewalled IPs of {ip_block}")
        mitigation, _ = await client.get(f"{path}/mitigation", f"list the mitigated IPs of {ip_block}")
        return render(
            f"IP block {ip_block}",
            {
                "block": block,
                "reverse_ips": reverse or [],
                "firewall_ips": firewall or [],
                "mitigation_ips": mitigation or [],
            },
        )

    async def get_ovhcloud_ip_firewall(ip_block: str, ip: str = "") -> str:
        path = f"/ip/{seg(ip_block)}/firewall"
        targets = [ip.strip()] if ip.strip() else None
        if targets is None:
            listed, err = await client.get(path, f"list the firewalled IPs of {ip_block}")
            if err:
                return err
            targets = [str(item) for item in (listed or [])][:20]
        found: list[dict[str, Any]] = []
        for target in targets:
            state, err = await client.get(f"{path}/{seg(target)}", f"read the firewall of {target}")
            if err:
                found.append({"ip": target, "error": err})
                continue
            rules_ids, _ = await client.get(f"{path}/{seg(target)}/rule", f"list the firewall rules of {target}")
            rules = await client.details(
                list(rules_ids or [])[:20],
                lambda seq, t=target: f"{path}/{seg(t)}/rule/{seg(seq)}",
                "read firewall rule",
            )
            found.append({"ip": target, "firewall": state, "rules": rules})
        return render(f"Network firewall of {ip_block} ({len(found)} IP(s) read)", found)

    async def get_ovhcloud_ip_mitigation(ip_block: str) -> str:
        path = f"/ip/{seg(ip_block)}/mitigation"
        ids, err = await client.get(path, f"list the mitigated IPs of {ip_block}")
        if err:
            return err
        rows = await client.details(list(ids or [])[:50], lambda target: f"{path}/{seg(target)}", "read mitigation")
        return render(f"DDoS mitigation of {ip_block}: {len(ids or [])} IP(s) under mitigation", rows)

    async def list_ovhcloud_ip_load_balancers(include_frontends: bool = True) -> str:
        ids, rows, err = await list_with_details(
            client,
            "/ipLoadbalancing",
            lambda name: f"/ipLoadbalancing/{seg(name)}",
            "IP Load Balancing services",
            limit=50,
            offset=0,
        )
        if err:
            return err
        if include_frontends:
            for row in rows:
                name = row.get("serviceName")
                if not name or "error" in row:
                    continue
                frontends: dict[str, Any] = {}
                for proto in ("http", "tcp", "udp"):
                    path = f"/ipLoadbalancing/{seg(name)}/{proto}/frontend"
                    fe_ids, fe_err = await client.get(path, f"list the {proto} frontends of {name}")
                    if not fe_err and fe_ids:
                        frontends[proto] = await client.details(
                            list(fe_ids)[:25], lambda fe, p=path: f"{p}/{seg(fe)}", "read frontend"
                        )
                row["frontends"] = frontends
        return render(f"{len(ids or [])} IP Load Balancing service(s)", rows)

    async def list_ovhcloud_vracks() -> str:
        ids, rows, err = await list_with_details(
            client, "/vrack", lambda name: f"/vrack/{seg(name)}", "vRacks", limit=50, offset=0
        )
        if err:
            return err
        for row in rows:
            name = row.get("name") if isinstance(row, dict) else None
            if not name or "error" in row:
                continue
            services: dict[str, Any] = {}
            for kind in ("dedicatedServer", "cloudProject", "dedicatedCloud", "ip", "ipLoadbalancing"):
                data, kind_err = await client.get(f"/vrack/{seg(name)}/{kind}", f"list the {kind} of vRack {name}")
                if not kind_err and data:
                    services[kind] = data
            row["services"] = services
        return render(f"{len(ids or [])} vRack(s) and the services they connect", rows)

    async def list_ovhcloud_domains() -> str:
        domains, err = await client.get("/domain", "list the domains")
        if err:
            return err
        zones, zones_err = await client.get("/domain/zone", "list the DNS zones")
        return render(
            f"{len(domains or [])} domain(s), {len(zones or []) if not zones_err else 0} DNS zone(s)",
            {"domains": domains or [], "dns_zones": zones_err or zones},
        )

    async def list_ovhcloud_dns_records(
        zone_name: str, field_type: str = "", sub_domain: str = "", limit: int = 100, offset: int = 0
    ) -> str:
        limit, offset = clamp(limit, 1, 200, 100), clamp(offset, 0, 100_000, 0)
        path = f"/domain/zone/{seg(zone_name)}/record"
        ids, rows, err = await list_with_details(
            client,
            path,
            lambda record_id: f"{path}/{seg(record_id)}",
            f"DNS records of {zone_name}",
            limit=limit,
            offset=offset,
            params={"fieldType": field_type.strip().upper() or None, "subDomain": sub_domain.strip() or None},
        )
        if err:
            return err
        return render(
            f"{len(ids or [])} DNS record(s) in {zone_name}{page_note(len(ids or []), offset, len(rows))}", rows
        )

    async def get_ovhcloud_api_resource(path: str, query: dict[str, Any] | None = None) -> str:
        api_path = normalize_api_path(path)
        if api_path is None:
            return "Error: give a path of the OVHcloud API under /1.0, e.g. /dedicated/server or /me."
        if api_path.rstrip("/").lower().startswith(("/auth/credential", "/me/api/credential", "/me/api/oauth2")):
            return "Error: this tool does not read API credentials."
        params = {str(k): v for k, v in (query or {}).items() if isinstance(v, (str, int, float, bool))}
        data, err = await client.get(api_path, f"read {api_path}", params)
        return err or render(f"GET {api_path}", data)

    def _firewall_target(ip_block: str, ip: str) -> tuple[str, str] | str:
        block, target = (ip_block or "").strip(), (ip or "").strip()
        if not block or not target or "/" in target:
            return "Error: give the IP block and one IP of it (a single address, no /prefix)."
        return f"/ip/{seg(block)}/firewall", target

    async def block_ovhcloud_ip(ip_block: str, ip: str, reason: str) -> str:
        """Deny every inbound packet to one IP at the Edge Network Firewall."""
        parsed = _firewall_target(ip_block, ip)
        if isinstance(parsed, str):
            return parsed
        if not (reason or "").strip():
            return "Error: reason is required."
        path, target = parsed
        listed, err = await client.get(path, f"list the firewalled IPs of {ip_block}")
        if err:
            return err
        if target not in [str(item) for item in (listed or [])]:
            _, err = await client.request("POST", path, f"add {target} to the firewall", body={"ipOnFirewall": target})
            if err:
                return err
        rule_path = f"{path}/{seg(target)}/rule"
        sequences, err = await client.get(rule_path, f"list the firewall rules of {target}")
        if err:
            return err
        if _BLOCK_SEQUENCE in [int(s) for s in (sequences or []) if str(s).isdigit()]:
            return (
                f"Error: rule {_BLOCK_SEQUENCE} of the firewall of {target} is already used; "
                "the block must be the first rule. Read it with get_ovhcloud_ip_firewall."
            )
        _, err = await client.request(
            "POST",
            rule_path,
            f"add a deny-all rule for {target}",
            body={"action": "deny", "protocol": "ipv4", "sequence": _BLOCK_SEQUENCE},
        )
        if err:
            return err
        _, err = await client.request(
            "PUT", f"{path}/{seg(target)}", f"enable the firewall of {target}", body={"enabled": True}
        )
        if err:
            return f"{err}\nThe deny-all rule is written but the firewall of {target} may not be enabled."
        return (
            f"{target} blocked: deny-all rule {_BLOCK_SEQUENCE} added and its Edge Network "
            "Firewall enabled. It filters inbound traffic from the internet only. "
            "Undo: unblock_ovhcloud_ip."
        )

    async def unblock_ovhcloud_ip(ip_block: str, ip: str) -> str:
        """Remove the deny-all rule block_ovhcloud_ip wrote."""
        parsed = _firewall_target(ip_block, ip)
        if isinstance(parsed, str):
            return parsed
        path, target = parsed
        rule_path = f"{path}/{seg(target)}/rule/{_BLOCK_SEQUENCE}"
        rule, err = await client.get(rule_path, f"read rule {_BLOCK_SEQUENCE} of {target}")
        if err:
            return err
        rule = rule if isinstance(rule, dict) else {}
        if (
            rule.get("action") != "deny"
            or rule.get("protocol") != "ipv4"
            or any(rule.get(k) for k in ("source", "sourcePort", "destinationPort"))
        ):
            return f"Error: rule {_BLOCK_SEQUENCE} of {target} is not the deny-all rule block_ovhcloud_ip writes; nothing removed."
        _, err = await client.request("DELETE", rule_path, f"remove rule {_BLOCK_SEQUENCE} of {target}")
        if err:
            return err
        return f"{target} unblocked: deny-all rule {_BLOCK_SEQUENCE} removed (the firewall stays enabled with its other rules)."

    handlers = {
        "block_ovhcloud_ip": block_ovhcloud_ip,
        "unblock_ovhcloud_ip": unblock_ovhcloud_ip,
        "list_ovhcloud_ips": list_ovhcloud_ips,
        "get_ovhcloud_ip": get_ovhcloud_ip,
        "get_ovhcloud_ip_firewall": get_ovhcloud_ip_firewall,
        "get_ovhcloud_ip_mitigation": get_ovhcloud_ip_mitigation,
        "list_ovhcloud_ip_load_balancers": list_ovhcloud_ip_load_balancers,
        "list_ovhcloud_vracks": list_ovhcloud_vracks,
        "list_ovhcloud_domains": list_ovhcloud_domains,
        "list_ovhcloud_dns_records": list_ovhcloud_dns_records,
        "get_ovhcloud_api_resource": get_ovhcloud_api_resource,
    }
    for name, handler in handlers.items():
        executor.register(name, handler)
    _one_ip = {"type": "string", "description": "One IP of the block."}
    blocking = [
        tool(
            "block_ovhcloud_ip",
            "Block all inbound internet traffic to one IP at the Edge Network Firewall "
            "(each call asks a person). Undo: unblock_ovhcloud_ip.",
            {"ip_block": _IP_BLOCK, "ip": _one_ip, "reason": {"type": "string", "description": "Reason (required)."}},
            ["ip_block", "ip", "reason"],
            priority=3,
            always_ask=True,
        ),
        tool(
            "unblock_ovhcloud_ip",
            "Remove the deny-all firewall rule block_ovhcloud_ip wrote on one IP.",
            {"ip_block": _IP_BLOCK, "ip": _one_ip},
            ["ip_block", "ip"],
            priority=3,
            always_ask=True,
        ),
    ]
    return [
        *blocking,
        tool(
            "list_ovhcloud_ips",
            "List the account's IP blocks: type, routed-to service, country, description.",
            {
                "ip_type": {"type": "string", "enum": list(_IP_TYPES), "description": "Filter by IP type."},
                "routed_to": {"type": "string", "description": "Filter by the service the block is routed to."},
                "limit": LIMIT,
                "offset": OFFSET,
            },
            priority=0,
        ),
        tool(
            "get_ovhcloud_ip",
            "Read one IP block: routed-to service, reverse DNS, firewalled and mitigated IPs.",
            {"ip_block": _IP_BLOCK},
            ["ip_block"],
        ),
        tool(
            "get_ovhcloud_ip_firewall",
            "Read the network firewall of an IP block (or one IP of it): enabled state and its rules.",
            {"ip_block": _IP_BLOCK, "ip": {"type": "string", "description": "One IP of the block (optional)."}},
            ["ip_block"],
        ),
        tool(
            "get_ovhcloud_ip_mitigation",
            "Read the DDoS mitigation state of the IPs of one IP block (auto or permanent).",
            {"ip_block": _IP_BLOCK},
            ["ip_block"],
            priority=2,
        ),
        tool(
            "list_ovhcloud_ip_load_balancers",
            "List the IP Load Balancing services with their HTTP, TCP and UDP frontends (ports, zones, default farms).",
            {"include_frontends": {"type": "boolean", "description": "Read each service's frontends (default true)."}},
        ),
        tool(
            "list_ovhcloud_vracks",
            "List the vRack private networks and the servers, projects, IP blocks and services each connects.",
            priority=2,
        ),
        tool("list_ovhcloud_domains", "List the account's domain names and DNS zones.", priority=2),
        tool(
            "list_ovhcloud_dns_records",
            "List the records of one DNS zone: sub-domain, type, target, TTL.",
            {
                "zone_name": _ZONE,
                "field_type": {"type": "string", "description": "Record type filter, e.g. A, AAAA, CNAME, MX, TXT."},
                "sub_domain": {"type": "string", "description": "Sub-domain filter, e.g. www."},
                "limit": LIMIT,
                "offset": OFFSET,
            },
            ["zone_name"],
        ),
        tool(
            "get_ovhcloud_api_resource",
            "GET any path of the OVHcloud API under /1.0 the other tools do not cover (read only), e.g. /me or /dedicated/server/NAME/statistics.",
            {
                "path": {
                    "type": "string",
                    "description": "API path under /1.0, e.g. /dedicated/server or /ip/203.0.113.0%2F28.",
                },
                "query": {"type": "object", "description": "Query string parameters (optional)."},
            },
            ["path"],
            priority=2,
        ),
    ]
