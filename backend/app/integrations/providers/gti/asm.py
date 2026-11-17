"""Google TI Attack Surface Management (ASM) tools.

ASM (the former Mandiant Attack Surface Management) continuously discovers
and assesses internet-facing assets. All endpoints live under ``/asm`` on
the core host with the same ``x-apikey`` key; every endpoint except
``GET /projects`` REQUIRES a project scope via the ``PROJECT-ID`` request
header and paginates with ``page_size`` + ``page_token``.

* ``list_gti_asm_projects`` - ``GET /projects``.
* ``search_gti_asm_entities`` - ``GET /search/entities/{search_string}``
  with the documented keyword operators (type:, name:, tag:, country:,
  vuln:, critical_or_high:, ...).
* ``get_gti_asm_entity`` - ``GET /entities/{entity_uid}``.
* ``search_gti_asm_issues`` - ``GET /search/issues/{search_string}``
  (severity:, status_new:, entity_type:, ...).
* ``get_gti_asm_issue`` - ``GET /issues/{id}``.
* ``set_gti_asm_issue_status`` - ``POST /issues/{id}/status`` - write.
* ``search_gti_asm_technologies`` - ``GET /search/technologies/{search_string}``.

ASM requires the Attack Surface Management module on the Google TI license -
permission errors are surfaced verbatim.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.integrations.http import format_pagination_hint, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    as_int,
    clamp,
    client,
    trunc,
)

_ISSUE_STATUSES = (
    "open_new",
    "open_triaged",
    "open_in_progress",
    "closed_resolved",
    "closed_duplicate",
    "closed_out_of_scope",
    "closed_benign",
    "closed_risk_accepted",
    "closed_false_positive",
    "closed_no_repro",
    "closed_tracked_externally",
    "closed",
)


def _result_list(payload: Any) -> list[dict[str, Any]]:
    """Extract the result list from an ASM envelope ({'result': [...]})."""
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, list):
            return [r for r in result if isinstance(r, dict)]
        if isinstance(result, dict):
            hits = result.get("hits")
            if isinstance(hits, list):
                return [h for h in hits if isinstance(h, dict)]
    return []


def _next_token(payload: Any) -> str | None:
    """Extract the next_page_token from an ASM search response."""
    if isinstance(payload, dict):
        for container in (payload, payload.get("result")):
            if isinstance(container, dict):
                token = container.get("next_page_token")
                if isinstance(token, str) and token:
                    return token
    return None


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register ASM tools and return their definitions.

    ``api`` is the ASM base (``.../api/v3/asm``).
    """

    def _project_headers(project_id: str) -> dict[str, str] | str:
        """Merge the required PROJECT-ID scoping header; error string when invalid.

        Every ASM endpoint except ``GET /projects`` is scoped by the
        PROJECT-ID header (documented as required) - fail fast with guidance
        instead of sending an unscoped request that errors server-side or
        silently hits the wrong project.
        """
        project_id = (project_id or "").strip()
        if not project_id:
            return (
                "Error: project_id is required - every ASM search/read/write "
                "is scoped to a project. Get the numeric id from "
                "list_gti_asm_projects first."
            )
        if not project_id.isdigit():
            return (
                "Error: project_id must be the numeric id from "
                "list_gti_asm_projects."
            )
        return {**headers, "PROJECT-ID": project_id}

    async def list_gti_asm_projects() -> str:
        """List the ASM projects visible to this account."""
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/projects",
                _PROVIDER,
                "list ASM projects",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        projects = _result_list(payload)
        if not projects:
            return (
                "No ASM projects found - Attack Surface Management may not "
                "be enabled for this Google TI license."
            )
        lines = [f"ASM projects ({len(projects)}):"]
        for project in projects:
            bits = []
            role = project.get("role")
            if role:
                bits.append(trunc(role, 30))
            org = project.get("organization_name")
            if org:
                bits.append(trunc(org, 60))
            suffix = f" [{', '.join(bits)}]" if bits else ""
            lines.append(
                f"  {trunc(project.get('name'), 100) or '?'}{suffix} "
                f"(id: {trunc(project.get('id'), 20) or '?'})"
            )
        lines.append(
            "Pass a project id as project_id - it is required to scope every "
            "entity/issue/technology call."
        )
        return "\n".join(lines)

    def _format_entity(entity: dict[str, Any]) -> str:
        bits = []
        entity_type = entity.get("type") or entity.get("entity_type")
        if entity_type:
            bits.append(trunc(entity_type, 40))
        country = entity.get("country")
        if country:
            bits.append(trunc(country, 20))
        issues = entity.get("issue_count")
        if issues is not None:
            bits.append(f"{as_int(issues)} issue(s)")
        critical = entity.get("critical_or_high")
        if critical:
            bits.append("critical/high issues")
        suffix = f" [{', '.join(bits)}]" if bits else ""
        return (
            f"  {trunc(entity.get('name'), 120) or '?'}{suffix} "
            f"(uid: {trunc(entity.get('uid') or entity.get('id'), 60) or '?'})"
        )

    async def search_gti_asm_entities(
        search_string: str = "",
        project_id: str = "",
        page_size: int = 20,
        page_token: str = "",
    ) -> str:
        """Search discovered attack-surface entities (domains, IPs, services, buckets, ...)."""
        scoped = _project_headers(project_id)
        if isinstance(scoped, str):
            return scoped
        term = (search_string or "").strip()
        params: dict[str, Any] = {"page_size": clamp(page_size, default=20, hi=100)}
        if page_token.strip():
            # Unlike DTM's opaque ``page`` token, ASM allows page_size
            # alongside page_token - keep it so the page size is stable
            # across pagination.
            params["page_token"] = page_token.strip()
        url = f"{api}/search/entities/{quote(term, safe=':')}"
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"search ASM entities ({trunc(term, 80) or 'all'})",
                headers=scoped,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        entities = _result_list(payload)
        if not entities:
            return f"No ASM entities match '{trunc(term, 120) or 'all'}'."
        lines = [f"ASM entities ({len(entities)} shown):"]
        lines.extend(_format_entity(e) for e in entities)
        return "\n".join(lines) + format_pagination_hint(_next_token(payload), "page_token")

    async def get_gti_asm_entity(entity_uid: str, project_id: str = "") -> str:
        """Read one attack-surface entity's searchable details."""
        scoped = _project_headers(project_id)
        if isinstance(scoped, str):
            return scoped
        entity_uid = (entity_uid or "").strip()
        if not entity_uid:
            return "Error: entity_uid is required (from search_gti_asm_entities)."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/entities/{quote(entity_uid, safe='')}",
                _PROVIDER,
                f"get ASM entity {trunc(entity_uid, 60)}",
                headers=scoped,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"ASM entity '{trunc(entity_uid, 80)}' not found."
                return err
            payload = resp.json() or {}
        entity = payload.get("result") if isinstance(payload, dict) else None
        entity = entity if isinstance(entity, dict) else {}
        if not entity:
            return f"ASM entity '{trunc(entity_uid, 80)}' not found."
        lines = [f"ASM entity {trunc(entity.get('uid') or entity_uid, 60)}:"]
        for key, label in (
            ("name", "Name"),
            ("type", "Type"),
            ("country", "Country"),
            ("first_seen", "First seen"),
            ("last_seen", "Last seen"),
        ):
            value = entity.get(key)
            if value:
                lines.append(f"{label}: {trunc(value, 150)}")
        lines.append(f"In scope: {'yes' if entity.get('scoped') else 'no'}")
        tags = entity.get("tags")
        if isinstance(tags, list) and tags:
            lines.append(f"Tags: {trunc(', '.join(str(t) for t in tags[:10]), 300)}")
        issues = entity.get("issue_count")
        if issues is not None:
            lines.append(f"Open issues: {as_int(issues)}")
        technologies = entity.get("technologies")
        if isinstance(technologies, list) and technologies:
            names = ", ".join(
                trunc(t.get("name") if isinstance(t, dict) else t, 40)
                for t in technologies[:10]
            )
            lines.append(f"Technologies: {trunc(names, 300)}")
        return "\n".join(lines)

    def _format_issue(issue: dict[str, Any]) -> str:
        bits = []
        severity = issue.get("severity")
        if severity is not None:
            bits.append(f"severity {as_int(severity)}/5")
        status = issue.get("status_detailed") or issue.get("status_new")
        if status:
            bits.append(trunc(status, 40))
        entity_name = issue.get("entity_name")
        if entity_name:
            bits.append(f"on {trunc(entity_name, 60)}")
        suffix = f" [{', '.join(bits)}]" if bits else ""
        return (
            f"  {trunc(issue.get('pretty_name') or issue.get('name'), 120) or '?'}"
            f"{suffix} (id: {trunc(issue.get('uid') or issue.get('id'), 60) or '?'})"
        )

    async def search_gti_asm_issues(
        search_string: str = "",
        project_id: str = "",
        page_size: int = 20,
        page_token: str = "",
    ) -> str:
        """Search attack-surface issues (misconfigurations, exposures, vulnerabilities)."""
        scoped = _project_headers(project_id)
        if isinstance(scoped, str):
            return scoped
        term = (search_string or "").strip()
        params: dict[str, Any] = {"page_size": clamp(page_size, default=20, hi=100)}
        if page_token.strip():
            params["page_token"] = page_token.strip()
        url = f"{api}/search/issues/{quote(term, safe=':')}"
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"search ASM issues ({trunc(term, 80) or 'all'})",
                headers=scoped,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        issues = _result_list(payload)
        if not issues:
            return f"No ASM issues match '{trunc(term, 120) or 'all'}'."
        lines = [f"ASM issues ({len(issues)} shown):"]
        lines.extend(_format_issue(i) for i in issues)
        return "\n".join(lines) + format_pagination_hint(_next_token(payload), "page_token")

    async def get_gti_asm_issue(issue_id: str, project_id: str = "") -> str:
        """Read one attack-surface issue: severity, status, entity, summary."""
        scoped = _project_headers(project_id)
        if isinstance(scoped, str):
            return scoped
        issue_id = (issue_id or "").strip()
        if not issue_id:
            return "Error: issue_id is required (from search_gti_asm_issues)."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/issues/{quote(issue_id, safe='')}",
                _PROVIDER,
                f"get ASM issue {trunc(issue_id, 60)}",
                headers=scoped,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"ASM issue '{trunc(issue_id, 80)}' not found."
                return err
            payload = resp.json() or {}
        issue = payload.get("result") if isinstance(payload, dict) else None
        issue = issue if isinstance(issue, dict) else {}
        if not issue:
            return f"ASM issue '{trunc(issue_id, 80)}' not found."
        lines = [f"ASM issue {trunc(issue.get('uid') or issue_id, 60)}:"]
        for key, label in (
            ("pretty_name", "Name"),
            ("name", "Internal name"),
            ("summary", "Summary"),
            ("entity_name", "Entity"),
            ("entity_uid", "Entity uid"),
            ("first_seen", "First seen"),
            ("last_seen", "Last seen"),
            ("status_detailed", "Status"),
        ):
            value = issue.get(key)
            if value:
                lines.append(f"{label}: {trunc(value, 400)}")
        severity = issue.get("severity")
        if severity is not None:
            lines.append(f"Severity: {as_int(severity)}/5 (1 = critical, 5 = info)")
        return "\n".join(lines)

    async def set_gti_asm_issue_status(
        issue_id: str, status: str, project_id: str = ""
    ) -> str:
        """Set the triage status of an attack-surface issue."""
        scoped = _project_headers(project_id)
        if isinstance(scoped, str):
            return scoped
        issue_id = (issue_id or "").strip()
        if not issue_id:
            return "Error: issue_id is required."
        status = (status or "").strip().lower()
        if status not in _ISSUE_STATUSES:
            return f"Error: status must be one of {', '.join(_ISSUE_STATUSES)}."
        async with client() as c:
            _resp, err = await safe_request(
                c,
                "post",
                f"{api}/issues/{quote(issue_id, safe='')}/status",
                _PROVIDER,
                f"set ASM issue {trunc(issue_id, 60)} status",
                headers=scoped,
                json={"status": status},
            )
            if err:
                return err
        return f"ASM issue '{trunc(issue_id, 80)}' status set to {status}."

    async def search_gti_asm_technologies(
        search_string: str = "",
        project_id: str = "",
        page_size: int = 20,
        page_token: str = "",
    ) -> str:
        """Search technologies detected across the attack surface (name, vendor, CPE)."""
        scoped = _project_headers(project_id)
        if isinstance(scoped, str):
            return scoped
        term = (search_string or "").strip()
        params: dict[str, Any] = {"page_size": clamp(page_size, default=20, hi=100)}
        if page_token.strip():
            params["page_token"] = page_token.strip()
        url = f"{api}/search/technologies/{quote(term, safe=':')}"
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"search ASM technologies ({trunc(term, 80) or 'all'})",
                headers=scoped,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        technologies = _result_list(payload)
        if not technologies:
            return f"No ASM technologies match '{trunc(term, 120) or 'all'}'."
        lines = [f"ASM technologies ({len(technologies)} shown):"]
        for tech in technologies:
            bits = []
            vendor = tech.get("vendor")
            if vendor:
                bits.append(trunc(vendor, 50))
            product = tech.get("product")
            if product:
                bits.append(trunc(product, 50))
            cpe = tech.get("cpe") or tech.get("cpe_type")
            if cpe:
                bits.append(trunc(cpe, 80))
            suffix = f" [{', '.join(bits)}]" if bits else ""
            lines.append(
                f"  {trunc(tech.get('name'), 100) or '?'}{suffix} "
                f"(uid: {trunc(tech.get('uid') or tech.get('id'), 60) or '?'})"
            )
        return "\n".join(lines) + format_pagination_hint(_next_token(payload), "page_token")

    executor.register("list_gti_asm_projects", list_gti_asm_projects)
    executor.register("search_gti_asm_entities", search_gti_asm_entities)
    executor.register("get_gti_asm_entity", get_gti_asm_entity)
    executor.register("search_gti_asm_issues", search_gti_asm_issues)
    executor.register("get_gti_asm_issue", get_gti_asm_issue)
    executor.register("set_gti_asm_issue_status", set_gti_asm_issue_status)
    executor.register("search_gti_asm_technologies", search_gti_asm_technologies)

    return [
        ToolDefinition(
            name="list_gti_asm_projects",
            description=(
                "List the Google TI Attack Surface Management projects "
                "visible to this account (a project is the top-level "
                "container for discovered assets). Returns the numeric "
                "project ids used to scope search_gti_asm_entities / "
                "search_gti_asm_issues / search_gti_asm_technologies."
            ),
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_gti_asm_entities",
            description=(
                "Search internet-facing entities discovered by ASM - "
                "domains, IPs, network services, certificates, cloud "
                "storage, repositories. Keyword operators: type: (Domain, "
                "IpAddress, NetworkService, SslCertificate, AwsS3Bucket, "
                "GithubRepository, ...), name:, tag:, country: (alpha-2), "
                "scoped:true|false, technology:, vuln:CVE, "
                "critical_or_high:true, issue_count_gte:, port_tcp:, "
                "http_title:. Empty search returns every entity. Returns "
                "entity uids for get_gti_asm_entity."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_string": {
                        "type": "string",
                        "description": (
                            "Search keywords, e.g. 'type:Domain name:acme' or "
                            "'critical_or_high:true' (empty = all entities)"
                        ),
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Required numeric project id from list_gti_asm_projects (scopes the search)",
                    },
                    "page_size": {
                        "type": "integer",
                        "default": 20,
                        "description": "Results per page (1-100)",
                    },
                    "page_token": {
                        "type": "string",
                        "description": "next_page_token from a previous call",
                    },
                },
                "required": ["project_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_asm_entity",
            description=(
                "Read one ASM entity's details: name, type, country, "
                "first/last seen, scope status, tags, open-issue count, and "
                "detected technologies. Use after search_gti_asm_entities."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_uid": {
                        "type": "string",
                        "description": "Entity uid from search_gti_asm_entities",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Required numeric project id from list_gti_asm_projects",
                    },
                },
                "required": ["entity_uid", "project_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_gti_asm_issues",
            description=(
                "Search attack-surface issues found by ASM "
                "(misconfigurations, exposures, vulnerable services). "
                "Keyword operators: severity:1-5 / severity_lte: / "
                "severity_gte: (1 = critical), status_new:open|closed, "
                "status_detailed:(open_triaged, closed_resolved, ...), "
                "entity_type:, entity_name:, name:, tag:, scoped:. Empty "
                "search returns every issue. Returns issue ids for "
                "get_gti_asm_issue / set_gti_asm_issue_status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_string": {
                        "type": "string",
                        "description": (
                            "Search keywords, e.g. 'severity_lte:2 "
                            "status_new:open' (empty = all issues)"
                        ),
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Required numeric project id from list_gti_asm_projects (scopes the search)",
                    },
                    "page_size": {
                        "type": "integer",
                        "default": 20,
                        "description": "Results per page (1-100)",
                    },
                    "page_token": {
                        "type": "string",
                        "description": "next_page_token from a previous call",
                    },
                },
                "required": ["project_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_asm_issue",
            description=(
                "Read one ASM issue in full: name, summary, severity (1 = "
                "critical to 5 = info), detailed status, affected entity, "
                "and first/last seen dates. Use after search_gti_asm_issues."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue id from search_gti_asm_issues",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Required numeric project id from list_gti_asm_projects",
                    },
                },
                "required": ["issue_id", "project_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="set_gti_asm_issue_status",
            description=(
                "Set the triage status of an ASM issue: open_new, "
                "open_triaged, open_in_progress, closed_resolved, "
                "closed_duplicate, closed_out_of_scope, closed_benign, "
                "closed_risk_accepted, closed_false_positive, "
                "closed_no_repro, closed_tracked_externally, or closed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue id from search_gti_asm_issues",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(_ISSUE_STATUSES),
                        "description": "New issue status",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Required numeric project id from list_gti_asm_projects",
                    },
                },
                "required": ["issue_id", "status", "project_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="search_gti_asm_technologies",
            description=(
                "Search the technologies ASM detected across your attack "
                "surface (software, services, frameworks). Keyword "
                "operators: name: (e.g. name:google_analytics), label: (e.g. "
                "label:wordpress_plugin), product:, vendor:, "
                "cpe_type:(application|hardware|os). Use to map exposure to "
                "a vulnerable product (then pivot to entities with "
                "technology:NAME in search_gti_asm_entities)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_string": {
                        "type": "string",
                        "description": "Search keywords, e.g. 'vendor:apache' (empty = all technologies)",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Required numeric project id from list_gti_asm_projects (scopes the search)",
                    },
                    "page_size": {
                        "type": "integer",
                        "default": 20,
                        "description": "Results per page (1-100)",
                    },
                    "page_token": {
                        "type": "string",
                        "description": "next_page_token from a previous call",
                    },
                },
                "required": ["project_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
