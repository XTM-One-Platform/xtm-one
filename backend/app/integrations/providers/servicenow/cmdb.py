"""ServiceNow CMDB - configuration items and their relationships.

Search and inspect configuration items (the ``cmdb_ci`` table and its class
subtables) and walk CI relationships (``cmdb_rel_ci``). CI writes go through the
generic ``manage_servicenow_record`` tool (pass the CI class as the table).
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _SOURCE,
    build_encoded_query,
    disp,
    field_lines,
    get_record,
    list_table,
    record_url,
    truncate,
)

_BASE_CLASS = "cmdb_ci"
_REL_TABLE = "cmdb_rel_ci"
_LIST_FIELDS = (
    "name,sys_class_name,operational_status,install_status,ip_address,"
    "serial_number,sys_updated_on,sys_id"
)


def _format_line(rec: dict[str, Any]) -> str:
    return (
        f"  {disp(rec.get('name')) or '?'} [{disp(rec.get('sys_class_name')) or 'cmdb_ci'}]\n"
        f"    Status: {disp(rec.get('operational_status')) or '?'}/"
        f"{disp(rec.get('install_status')) or '?'} | "
        f"IP: {disp(rec.get('ip_address')) or 'n/a'} | "
        f"Serial: {disp(rec.get('serial_number')) or 'n/a'}\n"
        f"    sys_id: {disp(rec.get('sys_id'))}"
    )


def _detail_formatter(base: str) -> Any:
    def _format(rec: dict[str, Any]) -> str:
        cls = disp(rec.get("sys_class_name")) or _BASE_CLASS
        lines = [f"{disp(rec.get('name')) or '?'} [{cls}]"]
        lines += field_lines(
            rec,
            [
                ("Operational status", "operational_status"),
                ("Install status", "install_status"),
                ("IP address", "ip_address"),
                ("FQDN", "fqdn"),
                ("Serial", "serial_number"),
                ("Asset tag", "asset_tag"),
                ("Manufacturer", "manufacturer"),
                ("Model", "model_id"),
                ("Location", "location"),
                ("Owned by", "owned_by"),
                ("Managed by", "managed_by"),
                ("Support group", "support_group"),
                ("Environment", "environment"),
                ("Updated", "sys_updated_on"),
            ],
        )
        comments = disp(rec.get("short_description")) or disp(rec.get("comments"))
        if comments:
            lines.append(f"\n  Notes: {truncate(comments, 1000)}")
        sys_id = disp(rec.get("sys_id"))
        link = record_url(base, cls, sys_id)
        if link:
            lines.append(f"  Link: {link}")
        lines.append(f"  sys_id: {sys_id} | class: {cls}")
        return "\n".join(lines)

    return _format


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    detail = _detail_formatter(base)

    async def search_servicenow_cmdb_ci(
        text: str = "",
        ci_class: str = _BASE_CLASS,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Search configuration items (CIs) by name, IP, or serial number.

        ``ci_class`` is the CMDB table to search (default ``cmdb_ci`` covers ALL
        classes; narrow it to e.g. ``cmdb_ci_server``, ``cmdb_ci_service``,
        ``cmdb_ci_app_server``). ``query`` is a raw encoded query for advanced
        filters. Returns sys_ids + the concrete class for follow-up calls.
        """
        clauses = []
        if text and text.strip():
            t = text.strip()
            clauses.append(
                f"nameLIKE{t}^ORip_addressLIKE{t}^ORserial_numberLIKE{t}"
                f"^ORasset_tagLIKE{t}"
            )
        encoded = build_encoded_query({}, raw_query=query, order_by="sys_updated_on")
        full = "^".join(c for c in ("^".join(clauses), encoded) if c)
        return await list_table(
            base,
            headers,
            (ci_class or _BASE_CLASS).strip(),
            "search CMDB CIs",
            _format_line,
            noun="configuration item",
            query=full,
            limit=limit,
            offset=offset,
            fields=_LIST_FIELDS,
        )

    async def get_servicenow_cmdb_ci(ci: str, ci_class: str = _BASE_CLASS) -> str:
        """Get full detail of a configuration item by sys_id or name.

        ``ci_class`` defaults to ``cmdb_ci`` (resolves any class). Querying the
        base class returns the common CI fields; pass the concrete class to see
        class-specific columns.
        """
        return await get_record(
            base,
            headers,
            (ci_class or _BASE_CLASS).strip(),
            ci,
            f"get CI {ci}",
            detail,
            number_field="name",
        )

    async def list_servicenow_ci_relationships(
        ci_sys_id: str, limit: int = 50, offset: int = 0
    ) -> str:
        """List the CMDB relationships of a CI (upstream + downstream) by sys_id.

        Shows each related CI and the relationship type (e.g. "Depends on::Used
        by"), covering both directions (the CI as parent and as child).
        """
        ci_sys_id = (ci_sys_id or "").strip()
        if not ci_sys_id:
            return "Error: ci_sys_id is required (resolve it with search_servicenow_cmdb_ci)."
        query = f"parent={ci_sys_id}^ORchild={ci_sys_id}"

        def _fmt(rec: dict[str, Any]) -> str:
            return (
                f"  {disp(rec.get('parent')) or '?'} --[{disp(rec.get('type')) or 'related'}]--> "
                f"{disp(rec.get('child')) or '?'}\n    rel sys_id: {disp(rec.get('sys_id'))}"
            )

        return await list_table(
            base,
            headers,
            _REL_TABLE,
            f"list relationships for CI {ci_sys_id}",
            _fmt,
            noun="relationship",
            query=query,
            limit=limit,
            offset=offset,
            fields="parent,child,type,sys_id",
        )

    executor.register("search_servicenow_cmdb_ci", search_servicenow_cmdb_ci)
    executor.register("get_servicenow_cmdb_ci", get_servicenow_cmdb_ci)
    executor.register(
        "list_servicenow_ci_relationships", list_servicenow_ci_relationships
    )

    _limit = {"type": "integer", "default": 20}
    _offset = {"type": "integer", "default": 0}
    return [
        ToolDefinition(
            name="search_servicenow_cmdb_ci",
            description=(
                "Search CMDB configuration items by name, IP, serial, or asset tag. "
                "ci_class defaults to cmdb_ci (all classes); narrow to a class like "
                "cmdb_ci_server / cmdb_ci_service for class-specific results. "
                "Returns sys_ids + concrete class."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Name, IP, serial, or asset tag"},
                    "ci_class": {
                        "type": "string",
                        "default": "cmdb_ci",
                        "description": "CMDB table/class to search",
                    },
                    "query": {"type": "string", "description": "Raw encoded query"},
                    "limit": _limit,
                    "offset": _offset,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_servicenow_cmdb_ci",
            description=(
                "Get full detail of a configuration item by sys_id or name. "
                "ci_class defaults to cmdb_ci; pass the concrete class to see "
                "class-specific columns."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ci": {"type": "string", "description": "CI sys_id or name"},
                    "ci_class": {"type": "string", "default": "cmdb_ci"},
                },
                "required": ["ci"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_servicenow_ci_relationships",
            description=(
                "List a CI's CMDB relationships (both directions) by sys_id - each "
                "related CI and the relationship type. Resolve the CI sys_id with "
                "search_servicenow_cmdb_ci first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ci_sys_id": {"type": "string", "description": "CI sys_id"},
                    "limit": {"type": "integer", "default": 50},
                    "offset": _offset,
                },
                "required": ["ci_sys_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
