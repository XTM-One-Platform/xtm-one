"""CrowdSec Service API tools - Blocklist as a Service.

Private blocklist curation pushed straight to Security Engines and firewall
integrations, plus the public/premium blocklist catalog.

* ``list_crowdsec_blocklists``      - GET /blocklists or POST /blocklists/search
* ``get_crowdsec_blocklist``        - GET /blocklists/{id} (stats + subscribers)
* ``create_crowdsec_blocklist``     - POST /blocklists
* ``add_crowdsec_blocklist_ips``    - POST /blocklists/{id}/ips (expiration
                                      mandatory)
* ``remove_crowdsec_blocklist_ips`` - POST /blocklists/{id}/ips/delete
* ``manage_crowdsec_blocklist``     - long tail: update (incl. from_cti_query
                                      auto-refresh), delete, subscribe/
                                      unsubscribe, subscribers, share/unshare,
                                      download
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    MISSING_SERVICE_KEY_MSG,
    SERVICE_API,
    clamp,
    client,
    dt,
    is_valid_ip_or_network,
    is_valid_object_id,
    page_footer,
    preview_text,
    trunc,
    validate_cti_duration,
    validate_iso_datetime,
)

_MANAGE_ACTIONS = (
    "update",
    "delete",
    "subscribe",
    "unsubscribe",
    "subscribers",
    "share",
    "unshare",
    "download",
)

_SUBSCRIBER_ENTITY_TYPES = (
    "org",
    "tag",
    "engine",
    "firewall_integration",
    "remediation_component_integration",
)

def _format_blocklist(b: dict[str, Any]) -> str:
    stats = b.get("stats") or {}
    subscribers = b.get("subscribers") or []
    line = (
        f"  {b.get('label') or b.get('name', '?')} [{b.get('id', '?')}]"
        f" | source: {b.get('source') or '?'}"
        + (" | private" if b.get("is_private") else "")
        + (f" | tier: {b['pricing_tier']}" if b.get("pricing_tier") else "")
        + (f" | IPs: {stats.get('count')}" if stats.get("count") is not None else "")
    )
    if isinstance(subscribers, list) and subscribers:
        line += f" | subscribers: {len(subscribers)}"
    if b.get("from_cti_query"):
        line += f" | CTI query: {trunc(b['from_cti_query'], 60)}"
    if b.get("description"):
        line += f"\n    {trunc(b.get('description'), 140)}"
    return line


def _format_subscriber(s: dict[str, Any]) -> str:
    return (
        f"  {s.get('entity_type', '?')} {s.get('id', '?')}"
        + (f" | remediation: {s['remediation']}" if s.get("remediation") else "")
    )


def register(
    service_headers: dict[str, str] | None,
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Register the blocklist tools."""

    def _guard(blocklist_id: str, needed: bool = True) -> str | None:
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        if needed and not is_valid_object_id(blocklist_id):
            return (
                "Error: blocklist_id must be the 24-character id from "
                "list_crowdsec_blocklists."
            )
        return None

    async def list_crowdsec_blocklists(
        query: str = "",
        subscribed_only: bool = False,
        include_private: bool = True,
        include_public: bool = True,
        page: int = 1,
        size: int = 50,
    ) -> str:
        """Inventory owned/shared/subscribed blocklists or search the catalog."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        page = clamp(page, default=1, hi=10_000)
        size = clamp(size, default=50, hi=100)
        query = str(query or "").strip()
        async with client() as http:
            if query:
                body: dict[str, Any] = {
                    "query": query,
                    "page": page,
                    "page_size": size,
                }
                if subscribed_only:
                    body["is_subscribed"] = True
                resp, err = await safe_request(
                    http,
                    "post",
                    f"{SERVICE_API}/blocklists/search",
                    _PROVIDER,
                    "search blocklists",
                    headers=service_headers,
                    json=body,
                )
            else:
                include_filter: list[str] = []
                if include_private:
                    include_filter += ["private", "shared"]
                if include_public:
                    include_filter.append("public")
                params: dict[str, Any] = {
                    "page": page,
                    "page_size": size,
                    "subscribed_only": subscribed_only,
                    "include_filter": include_filter or ["all"],
                }
                resp, err = await safe_request(
                    http,
                    "get",
                    f"{SERVICE_API}/blocklists",
                    _PROVIDER,
                    "list blocklists",
                    headers=service_headers,
                    params=params,
                )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items") or []
        out = format_entity_list(
            items,
            _format_blocklist,
            noun="blocklist",
            total=data.get("total"),
            empty_msg="No blocklists found.",
        )
        return out + page_footer(data)

    async def get_crowdsec_blocklist(blocklist_id: str) -> str:
        """Blocklist detail: stats, content stats, subscribers, shares."""
        guard = _guard(blocklist_id)
        if guard:
            return guard
        async with client() as http:
            resp, err = await safe_request(
                http,
                "get",
                f"{SERVICE_API}/blocklists/{blocklist_id.strip()}",
                _PROVIDER,
                f"get blocklist {blocklist_id}",
                headers=service_headers,
            )
            if err:
                return err
            b = resp.json() or {}
        stats = b.get("stats") or {}
        content = b.get("content_stats") or {}
        lines = [
            f"Blocklist: {b.get('label') or b.get('name', '?')} [{b.get('id', '?')}]",
            f"  Source: {b.get('source') or '?'}"
            + (" | private" if b.get("is_private") else "")
            + (f" | tier: {b['pricing_tier']}" if b.get("pricing_tier") else "")
            + (f" | created: {dt(b.get('created_at'))}" if b.get("created_at") else ""),
        ]
        if b.get("description"):
            lines.append(f"  Description: {trunc(b['description'], 250)}")
        if b.get("from_cti_query"):
            lines.append(
                f"  Auto-refresh CTI query: {trunc(b['from_cti_query'], 150)}"
                + (f" (since {b['since']})" if b.get("since") else "")
            )
        if b.get("tags"):
            lines.append(f"  Tags: {', '.join(str(t) for t in b['tags'][:10])}")
        if stats:
            lines.append(
                f"  Stats: {stats.get('count', 0)} IP(s)"
                + (
                    f" | added last month: {stats.get('addition_month')}"
                    if stats.get("addition_month") is not None
                    else ""
                )
                + (
                    f" | removed last month: {stats.get('suppression_month')}"
                    if stats.get("suppression_month") is not None
                    else ""
                )
            )
        if content:
            behaviors = ", ".join(
                str(x.get("label") or x.get("name") or "?")
                for x in (content.get("most_present_behaviors") or [])[:5]
            )
            countries = ", ".join(
                str(x.get("label") or x.get("name") or "?")
                for x in (content.get("top_attacking_countries") or [])[:5]
            )
            lines.append(
                f"  Content: {content.get('total_seen', 0)} seen by the network"
                f" | {content.get('total_fire', 0)} in the community blocklist"
                f" | {content.get('total_false_positive', 0)} known false positives"
            )
            if behaviors:
                lines.append(f"  Top behaviors: {behaviors}")
            if countries:
                lines.append(f"  Top attacking countries: {countries}")
        subscribers = b.get("subscribers") or []
        if isinstance(subscribers, list) and subscribers:
            lines.append(f"  Subscribers ({len(subscribers)}):")
            lines += [_format_subscriber(s) for s in subscribers[:10]]
        shared_with = b.get("shared_with") or []
        if isinstance(shared_with, list) and shared_with:
            lines.append(f"  Shared with {len(shared_with)} organization(s).")
        return "\n".join(lines)

    async def create_crowdsec_blocklist(
        name: str,
        description: str,
        label: str = "",
        tags: list[str] | None = None,
        references: list[str] | None = None,
    ) -> str:
        """Create a private blocklist for IOC distribution."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        name = str(name or "").strip()
        description = str(description or "").strip()
        if not name or not description:
            return "Error: both 'name' and 'description' are required."
        body: dict[str, Any] = {"name": name, "description": description}
        if str(label or "").strip():
            body["label"] = str(label).strip()
        cleaned_tags = [str(t).strip() for t in (tags or []) if str(t or "").strip()]
        if cleaned_tags:
            body["tags"] = cleaned_tags
        cleaned_refs = [
            str(r).strip() for r in (references or []) if str(r or "").strip()
        ]
        if cleaned_refs:
            body["references"] = cleaned_refs
        async with client() as http:
            resp, err = await safe_request(
                http,
                "post",
                f"{SERVICE_API}/blocklists",
                _PROVIDER,
                f"create blocklist '{name}'",
                headers=service_headers,
                json=body,
            )
            if err:
                return err
            created = resp.json() or {}
        return (
            f"Blocklist created: {created.get('label') or name} "
            f"[{created.get('id', '?')}].\n"
            "Add IPs with add_crowdsec_blocklist_ips, then subscribe engines "
            "or integrations with manage_crowdsec_blocklist action='subscribe'."
        )

    async def add_crowdsec_blocklist_ips(
        blocklist_id: str, ips: list[str], expiration: str
    ) -> str:
        """Push IPs/networks into a blocklist (expiration mandatory)."""
        guard = _guard(blocklist_id)
        if guard:
            return guard
        candidates = [str(i).strip() for i in (ips or []) if str(i or "").strip()]
        cleaned = [i for i in candidates if is_valid_ip_or_network(i)]
        invalid = [i for i in candidates if not is_valid_ip_or_network(i)]
        if not cleaned:
            return "Error: provide at least one valid IP or CIDR network in 'ips'."
        expiration = str(expiration or "").strip()
        exp_err = validate_iso_datetime(expiration, "expiration")
        if exp_err:
            return exp_err
        async with client(timeout=60) as http:
            _, err = await safe_request(
                http,
                "post",
                f"{SERVICE_API}/blocklists/{blocklist_id.strip()}/ips",
                _PROVIDER,
                f"add {len(cleaned)} IPs to blocklist",
                headers=service_headers,
                json={"ips": cleaned, "expiration": expiration},
            )
            if err:
                return err
        out = (
            f"Added {len(cleaned)} IP(s)/network(s) to blocklist "
            f"{blocklist_id.strip()} (expire {expiration})."
        )
        if invalid:
            shown = ", ".join(invalid[:20])
            extra = f" (+{len(invalid) - 20} more)" if len(invalid) > 20 else ""
            out += f"\nSkipped {len(invalid)} invalid value(s): {shown}{extra}"
        return out

    async def remove_crowdsec_blocklist_ips(blocklist_id: str, ips: list[str]) -> str:
        """Remove IPs/networks from a blocklist."""
        guard = _guard(blocklist_id)
        if guard:
            return guard
        candidates = [str(i).strip() for i in (ips or []) if str(i or "").strip()]
        cleaned = [i for i in candidates if is_valid_ip_or_network(i)]
        if not cleaned:
            return "Error: provide at least one valid IP or CIDR network in 'ips'."
        async with client(timeout=60) as http:
            _, err = await safe_request(
                http,
                "post",
                f"{SERVICE_API}/blocklists/{blocklist_id.strip()}/ips/delete",
                _PROVIDER,
                f"remove {len(cleaned)} IPs from blocklist",
                headers=service_headers,
                json={"ips": cleaned},
            )
            if err:
                return err
        return (
            f"Removed {len(cleaned)} IP(s)/network(s) from blocklist "
            f"{blocklist_id.strip()}."
        )

    async def manage_crowdsec_blocklist(
        action: str,
        blocklist_id: str,
        label: str = "",
        description: str = "",
        tags: list[str] | None = None,
        references: list[str] | None = None,
        from_cti_query: str = "",
        since: str = "",
        subscriber_ids: list[str] | None = None,
        entity_type: str = "",
        remediation: str = "",
        entity_id: str = "",
        organization_id: str = "",
        permission: str = "read",
        page: int = 1,
        size: int = 50,
    ) -> str:
        """Blocklist long tail: update/delete/subscribe/unsubscribe/subscribers/share/unshare/download."""
        act = str(action or "").strip().lower()
        if act not in _MANAGE_ACTIONS:
            return f"Error: action must be one of {', '.join(_MANAGE_ACTIONS)}."
        guard = _guard(blocklist_id)
        if guard:
            return guard
        bl_id = blocklist_id.strip()
        base = f"{SERVICE_API}/blocklists/{bl_id}"

        async with client(timeout=60) as http:
            if act == "update":
                body: dict[str, Any] = {}
                if str(label or "").strip():
                    body["label"] = str(label).strip()
                if str(description or "").strip():
                    body["description"] = str(description).strip()
                cleaned_tags = [
                    str(t).strip() for t in (tags or []) if str(t or "").strip()
                ]
                if cleaned_tags:
                    body["tags"] = cleaned_tags
                cleaned_refs = [
                    str(r).strip() for r in (references or []) if str(r or "").strip()
                ]
                if cleaned_refs:
                    body["references"] = cleaned_refs
                if str(from_cti_query or "").strip():
                    body["from_cti_query"] = str(from_cti_query).strip()
                    window = str(since or "").strip() or "7d"
                    dur_err = validate_cti_duration(window)
                    if dur_err:
                        return dur_err
                    body["since"] = window
                if not body:
                    return (
                        "Error: nothing to update - pass label, description, "
                        "tags, references, or from_cti_query (+ since)."
                    )
                _, err = await safe_request(
                    http,
                    "patch",
                    base,
                    _PROVIDER,
                    f"update blocklist {bl_id}",
                    headers=service_headers,
                    json=body,
                )
                if err:
                    return err
                out = f"Blocklist {bl_id} updated."
                if "from_cti_query" in body:
                    out += (
                        " The list now auto-refreshes from the CTI query "
                        f"'{trunc(body['from_cti_query'], 80)}' over the last "
                        f"{body['since']} (max window 30d)."
                    )
                return out

            if act == "delete":
                _, err = await safe_request(
                    http,
                    "delete",
                    base,
                    _PROVIDER,
                    f"delete blocklist {bl_id}",
                    headers=service_headers,
                )
                if err:
                    return err
                return (
                    f"Blocklist {bl_id} permanently deleted (subscribers stop "
                    "receiving it on their next pull)."
                )

            if act == "subscribe":
                etype = str(entity_type or "").strip().lower()
                if etype not in _SUBSCRIBER_ENTITY_TYPES:
                    return (
                        "Error: entity_type must be one of "
                        f"{', '.join(_SUBSCRIBER_ENTITY_TYPES)}."
                    )
                ids = [
                    str(i).strip()
                    for i in (subscriber_ids or [])
                    if str(i or "").strip()
                ]
                if not ids:
                    return "Error: subscriber_ids is required for 'subscribe'."
                body = {"ids": ids, "entity_type": etype}
                if str(remediation or "").strip():
                    body["remediation"] = str(remediation).strip()
                _, err = await safe_request(
                    http,
                    "post",
                    f"{base}/subscribers",
                    _PROVIDER,
                    f"subscribe to blocklist {bl_id}",
                    headers=service_headers,
                    json=body,
                )
                if err:
                    return err
                return (
                    f"Subscribed {len(ids)} {etype}(s) to blocklist {bl_id}"
                    + (f" with remediation '{remediation}'" if remediation else "")
                    + "."
                )

            if act == "unsubscribe":
                eid = str(entity_id or "").strip()
                if not eid or any(c in eid for c in "/?#% \t"):
                    return "Error: entity_id is required for 'unsubscribe'."
                _, err = await safe_request(
                    http,
                    "delete",
                    f"{base}/subscribers/{eid}",
                    _PROVIDER,
                    f"unsubscribe {eid} from blocklist {bl_id}",
                    headers=service_headers,
                )
                if err:
                    return err
                return f"Unsubscribed {eid} from blocklist {bl_id}."

            if act == "subscribers":
                resp, err = await safe_request(
                    http,
                    "get",
                    f"{base}/subscribers",
                    _PROVIDER,
                    f"list blocklist subscribers {bl_id}",
                    headers=service_headers,
                    params={
                        "page": clamp(page, default=1, hi=10_000),
                        "size": clamp(size, default=50, hi=100),
                    },
                )
                if err:
                    return err
                data = resp.json() or {}
                out = format_entity_list(
                    data.get("items") or [],
                    _format_subscriber,
                    noun="subscriber",
                    total=data.get("total"),
                    empty_msg=f"Blocklist {bl_id} has no subscribers.",
                )
                return out + page_footer(data)

            if act == "share":
                org = str(organization_id or "").strip()
                if not org:
                    return "Error: organization_id is required for 'share'."
                perm = str(permission or "read").strip().lower()
                if perm not in ("read", "write"):
                    return "Error: permission must be 'read' or 'write'."
                _, err = await safe_request(
                    http,
                    "post",
                    f"{base}/shares",
                    _PROVIDER,
                    f"share blocklist {bl_id}",
                    headers=service_headers,
                    json={
                        "organizations": [
                            {"organization_id": org, "permission": perm}
                        ]
                    },
                )
                if err:
                    return err
                return f"Blocklist {bl_id} shared with organization {org} ({perm})."

            if act == "unshare":
                org = str(organization_id or "").strip()
                if not org or any(c in org for c in "/?#% \t"):
                    return "Error: organization_id is required for 'unshare'."
                _, err = await safe_request(
                    http,
                    "delete",
                    f"{base}/shares/{org}",
                    _PROVIDER,
                    f"unshare blocklist {bl_id}",
                    headers=service_headers,
                )
                if err:
                    return err
                return f"Blocklist {bl_id} unshared from organization {org}."

            # download (content preview)
            resp, err = await safe_request(
                http,
                "get",
                f"{base}/download",
                _PROVIDER,
                f"download blocklist {bl_id}",
                headers=service_headers,
            )
            if err:
                return err
            return preview_text(resp.text or "", f"Blocklist {bl_id} content -")

    executor.register("list_crowdsec_blocklists", list_crowdsec_blocklists)
    executor.register("get_crowdsec_blocklist", get_crowdsec_blocklist)
    executor.register("create_crowdsec_blocklist", create_crowdsec_blocklist)
    executor.register("add_crowdsec_blocklist_ips", add_crowdsec_blocklist_ips)
    executor.register("remove_crowdsec_blocklist_ips", remove_crowdsec_blocklist_ips)
    executor.register("manage_crowdsec_blocklist", manage_crowdsec_blocklist)

    return [
        ToolDefinition(
            name="list_crowdsec_blocklists",
            description=(
                "List CrowdSec blocklists: your private lists, lists shared "
                "with your organization, and the public/premium catalog. Pass "
                "'query' to full-text search the catalog instead; "
                "subscribed_only=true restricts to lists your fleet already "
                "consumes. Returns ids for get_crowdsec_blocklist / "
                "manage_crowdsec_blocklist."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Full-text catalog search (switches to POST /blocklists/search)",
                    },
                    "subscribed_only": {"type": "boolean", "default": False},
                    "include_private": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include private + org-shared lists (list mode only)",
                    },
                    "include_public": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include the public catalog (list mode only)",
                    },
                    "page": {"type": "integer", "default": 1},
                    "size": {"type": "integer", "default": 50, "description": "Page size (max 100)"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_crowdsec_blocklist",
            description=(
                "Get one blocklist in detail: description, auto-refresh CTI "
                "query, IP count and monthly add/remove stats, content stats "
                "(how many entries the network has seen, community-blocklist "
                "overlap, known false positives, top behaviors and attacking "
                "countries), subscribers, and org shares."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "blocklist_id": {
                        "type": "string",
                        "description": "Blocklist id (24-char, from list_crowdsec_blocklists)",
                    },
                },
                "required": ["blocklist_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_crowdsec_blocklist",
            description=(
                "Create a private CrowdSec blocklist for IOC distribution. "
                "After creating, push entries with add_crowdsec_blocklist_ips "
                "and wire it to your fleet with manage_crowdsec_blocklist "
                "action='subscribe' (engines or firewall integrations pull it "
                "automatically). The name must be unique in the organization."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique machine name"},
                    "description": {"type": "string", "description": "What the list contains / why"},
                    "label": {
                        "type": "string",
                        "description": "Human-readable display name (defaults to name)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Classification tags",
                    },
                    "references": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Reference URLs on the list's origins",
                    },
                },
                "required": ["name", "description"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_crowdsec_blocklist_ips",
            description=(
                "Add IPs or CIDR networks to a CrowdSec blocklist - the write "
                "path for distributing confirmed IOCs to every subscribed "
                "engine/firewall. An expiration datetime is mandatory (entries "
                "auto-expire, keeping the list clean); re-adding an IP bumps "
                "its expiration."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "blocklist_id": {"type": "string", "description": "Target blocklist id"},
                    "ips": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IPs or CIDR networks to add",
                    },
                    "expiration": {
                        "type": "string",
                        "description": "ISO 8601 expiration, e.g. 2026-12-31T00:00:00Z",
                    },
                },
                "required": ["blocklist_id", "ips", "expiration"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="remove_crowdsec_blocklist_ips",
            description=(
                "Remove IPs or CIDR networks from a CrowdSec blocklist (e.g. a "
                "false positive reported by a user). Subscribers drop the "
                "entries on their next pull."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "blocklist_id": {"type": "string", "description": "Target blocklist id"},
                    "ips": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IPs or CIDR networks to remove",
                    },
                },
                "required": ["blocklist_id", "ips"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_crowdsec_blocklist",
            description=(
                "Blocklist long-tail operations. Actions: 'update' (label/"
                "description/tags/references, or set from_cti_query + since to "
                "turn the list into an auto-refreshing CTI-query-driven list, "
                "max 30d window), 'delete' (permanent, cannot be undone), "
                "'subscribe' (attach engines/orgs/tags/firewall integrations "
                "with an optional remediation like ban), 'unsubscribe', "
                "'subscribers' (who consumes the list), 'share'/'unshare' "
                "(cross-organization sharing with read or write permission), "
                "'download' (preview the raw plain-text content)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_MANAGE_ACTIONS),
                        "description": "Operation to perform",
                    },
                    "blocklist_id": {"type": "string", "description": "Target blocklist id"},
                    "label": {"type": "string", "description": "New display name (update)"},
                    "description": {"type": "string", "description": "New description (update)"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New tags (update)",
                    },
                    "references": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New references (update)",
                    },
                    "from_cti_query": {
                        "type": "string",
                        "description": (
                            "CTI Lucene query that auto-populates the list "
                            "(update; e.g. 'behaviors.label:\"SSH Bruteforce\"')"
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": "CTI query window, e.g. 24h / 7d (max 30d; update)",
                    },
                    "subscriber_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Entity ids to subscribe (subscribe)",
                    },
                    "entity_type": {
                        "type": "string",
                        "enum": list(_SUBSCRIBER_ENTITY_TYPES),
                        "description": "Type of the subscriber entities (subscribe)",
                    },
                    "remediation": {
                        "type": "string",
                        "description": "Remediation for subscribed engines, e.g. ban (subscribe)",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Subscriber entity id to detach (unsubscribe)",
                    },
                    "organization_id": {
                        "type": "string",
                        "description": "Organization id (share/unshare)",
                    },
                    "permission": {
                        "type": "string",
                        "enum": ["read", "write"],
                        "default": "read",
                        "description": "Share permission (share)",
                    },
                    "page": {"type": "integer", "default": 1, "description": "Page (subscribers)"},
                    "size": {"type": "integer", "default": 50, "description": "Page size (subscribers)"},
                },
                "required": ["action", "blocklist_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
