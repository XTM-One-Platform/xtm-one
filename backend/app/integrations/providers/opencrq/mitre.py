"""OpenCRQ MITRE ATT&CK reference data tools (read-only).

* ``list_opencrq_mitre_tactics``      - ATT&CK tactics (+ optional matrix).
* ``search_opencrq_mitre_techniques`` - search techniques by text.
* ``get_opencrq_mitre_technique``     - technique detail (by UUID or ATT&CK ID).
* ``list_opencrq_mitre_mitigations``  - mitigations (+ optional detail).
"""

from __future__ import annotations

import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _UUID,
    TENANT_PROP,
    _client,
    _grc_link,
    _one_line,
    _quarantined,
    _resolve_tenant,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

#: Where the entries of this module come from, named on their quarantine block.
_CATALOGUE = "MITRE ATT&CK catalogue"


def _entry(row: dict, missing_name: str = "?") -> str:
    """An ATT&CK entry as ``<external id> <name>``, each value on one line and capped."""
    return f"{_one_line(row.get('externalId'), 20, '?')} {_one_line(row.get('name'), 200, missing_name)}"


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _external_id(
        client, tenant: str, reference: str, procedure: str, action: str
    ) -> tuple[str | None, str | None]:
        """An ATT&CK ID passes through upper-cased; an OpenCRQ UUID is looked up in the catalogue list."""
        if not _UUID.match(reference):
            return reference.upper(), None
        rows, err = await _trpc(
            base_url, headers, procedure, action, tenant_id=tenant, client=client
        )
        if err:
            return None, err
        match = next((r for r in rows or [] if r.get("id") == reference.lower()), None)
        if not match:
            return None, f"MITRE entry {reference} not found."
        return match.get("externalId"), None

    async def list_opencrq_mitre_tactics(
        include_matrix: bool = False, tenant_id: str = ""
    ) -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        if include_matrix:
            matrix, err = await _trpc(
                base_url, headers, "mitreAttack.getMatrix",
                "get MITRE matrix", tenant_id=tenant,
            )
            if err:
                return err
            tactics = (matrix or {}).get("tactics") or []
            techniques = (matrix or {}).get("techniques") or []
            junctions = (matrix or {}).get("junctions") or []
            by_tactic: dict[str, list[str]] = {}
            names = {
                t.get("id"): _entry(t, "")
                for t in techniques
                if not t.get("isSubtechnique")
            }
            for j in junctions:
                label = names.get(j.get("techniqueId"))
                if label:
                    by_tactic.setdefault(j.get("tacticId"), []).append(label)
            lines = []
            for t in tactics:
                lines.append(f"- {_entry(t)} (id: {t.get('id')})")
                tech = by_tactic.get(t.get("id")) or []
                for label in tech[:20]:
                    lines.append(f"    - {label}")
                if len(tech) > 20:
                    lines.append(f"    ... and {len(tech) - 20} more.")
            return _quarantined(f"MITRE ATT&CK matrix ({len(tactics)} tactics):", "\n".join(lines), _CATALOGUE)
        tactics, err = await _trpc(
            base_url, headers, "mitreAttack.getTactics",
            "list MITRE tactics", tenant_id=tenant,
        )
        if err:
            return err
        tactics = tactics or []
        if not tactics:
            return "No MITRE tactics found (data may not be synced yet)."
        return _quarantined(
            f"MITRE tactics ({len(tactics)}):",
            "\n".join(f"- {_entry(t)} (id: {t.get('id')})" for t in tactics),
            _CATALOGUE,
        )

    async def search_opencrq_mitre_techniques(
        search: str = "", include_subtechniques: bool = True, tenant_id: str = ""
    ) -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        techniques, err = await _trpc(
            base_url, headers, "mitreAttack.getTechniques",
            "list MITRE techniques", tenant_id=tenant,
        )
        if err:
            return err
        techniques = techniques or []
        if not include_subtechniques:
            techniques = [
                t for t in techniques if not t.get("isSubtechnique")
            ]
        if search:
            q = search.lower()
            techniques = [
                t
                for t in techniques
                if q in (t.get("name", "") or "").lower()
                or q in (t.get("externalId", "") or "").lower()
            ]
        if not techniques:
            return "No MITRE techniques matched."
        shown = techniques[:40]
        lines = [
            f"- {_entry(t)} (id: {t.get('id')})" + (" [sub-technique]" if t.get("isSubtechnique") else "")
            for t in shown
        ]
        return _quarantined(
            f"MITRE techniques ({len(techniques)}, showing {len(shown)}):", "\n".join(lines), _CATALOGUE
        )

    async def get_opencrq_mitre_technique(
        technique_id: str = "", tenant_id: str = ""
    ) -> str:
        technique_id = (technique_id or "").strip()
        if not technique_id:
            return "technique_id is required (UUID or ATT&CK ID like T1566)."
        # One client for the whole handler (tenant resolution + optional
        # external-id resolution + detail) so the calls share a pool.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            external_id, err = await _external_id(
                client, tenant, technique_id,
                "mitreAttack.getTechniques", "list MITRE techniques",
            )
            if err:
                return err
            technique, err = await _trpc(
                base_url, headers, "mitreAttack.getTechniqueByExternalId",
                "get MITRE technique",
                input={"externalId": external_id}, tenant_id=tenant,
                client=client,
            )
        if err:
            return err
        if not technique:
            return f"MITRE technique {technique_id} not found."
        lines = [f"[MitreTechnique] {_entry(technique)}"]
        lines.append(f"  ID: {technique.get('id', 'N/A')}")
        link = _grc_link(
            base_url, tenant, "mitre_technique", technique.get("externalId")
        )
        if link:
            lines.append(link)
        if technique.get("revoked"):
            lines.append(
                "  Revoked: yes"
                + (
                    f" (replaced by {_one_line(technique['replacedByTechniqueExternalId'], 20)})"
                    if technique.get("replacedByTechniqueExternalId")
                    else ""
                )
            )
        if technique.get("description"):
            lines.append(f"  Description: {_one_line(technique['description'], 400)}")
        tactics = technique.get("tactics") or []
        if tactics:
            lines.append(
                "  Tactics: "
                + ", ".join(_entry(t, "") for t in tactics)
            )
        subs = technique.get("subTechniques") or []
        if subs:
            lines.append(f"  Sub-techniques ({len(subs)}):")
            for s in subs[:15]:
                lines.append(f"    - {_entry(s)}" + (" [revoked]" if s.get("revoked") else ""))
        mitigations = technique.get("mitigations") or []
        if mitigations:
            lines.append(f"  Mitigations ({len(mitigations)}):")
            for m in mitigations[:15]:
                lines.append(f"    - {_entry(m)}")
        return _quarantined("", "\n".join(lines), _CATALOGUE)

    async def list_opencrq_mitre_mitigations(
        search: str = "", mitigation_id: str = "", tenant_id: str = ""
    ) -> str:
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            if mitigation_id:
                external_id, err = await _external_id(
                    client, tenant, mitigation_id.strip(),
                    "mitreAttack.getMitigations", "list MITRE mitigations",
                )
                if err:
                    return err
                mitigation, err = await _trpc(
                    base_url, headers, "mitreAttack.getMitigationByExternalId",
                    "get MITRE mitigation",
                    input={"externalId": external_id}, tenant_id=tenant,
                    client=client,
                )
                if err:
                    return err
                if not mitigation:
                    return f"MITRE mitigation {mitigation_id} not found."
                lines = [f"[MitreMitigation] {_entry(mitigation)}"]
                lines.append(f"  ID: {mitigation.get('id', 'N/A')}")
                link = _grc_link(
                    base_url, tenant, "mitre_mitigation", mitigation.get("externalId")
                )
                if link:
                    lines.append(link)
                if mitigation.get("description"):
                    lines.append(f"  Description: {_one_line(mitigation['description'], 400)}")
                techniques = mitigation.get("techniques") or []
                if techniques:
                    lines.append(f"  Addresses techniques ({len(techniques)}):")
                    for t in techniques[:25]:
                        lines.append(f"    - {_entry(t)}")
                return _quarantined("", "\n".join(lines), _CATALOGUE)
            mitigations, err = await _trpc(
                base_url, headers, "mitreAttack.getMitigations",
                "list MITRE mitigations", tenant_id=tenant, client=client,
            )
        if err:
            return err
        mitigations = mitigations or []
        if search:
            q = search.lower()
            mitigations = [
                m
                for m in mitigations
                if q in (m.get("name", "") or "").lower()
                or q in (m.get("externalId", "") or "").lower()
                or q in (m.get("description", "") or "").lower()
            ]
        if not mitigations:
            return "No MITRE mitigations matched."
        shown = mitigations[:40]
        return _quarantined(
            f"MITRE mitigations ({len(mitigations)}, showing {len(shown)}):",
            "\n".join(f"- {_entry(m)} (id: {m.get('id')})" for m in shown),
            _CATALOGUE,
        )

    executor.register("list_opencrq_mitre_tactics", list_opencrq_mitre_tactics)
    executor.register(
        "search_opencrq_mitre_techniques", search_opencrq_mitre_techniques
    )
    executor.register("get_opencrq_mitre_technique", get_opencrq_mitre_technique)
    executor.register(
        "list_opencrq_mitre_mitigations", list_opencrq_mitre_mitigations
    )

    return [
        ToolDefinition(
            name="list_opencrq_mitre_tactics",
            description=(
                "List the MITRE ATT&CK tactics known to OpenCRQ. Set "
                "include_matrix=true for the full tactic -> technique "
                "matrix."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_matrix": {
                        "type": "boolean",
                        "description": "Include techniques per tactic.",
                        "default": False,
                    },
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="search_opencrq_mitre_techniques",
            description=(
                "Search the MITRE ATT&CK techniques known to OpenCRQ by "
                "name or ATT&CK ID (e.g. T1566)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Text filter."},
                    "include_subtechniques": {
                        "type": "boolean",
                        "default": True,
                    },
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_mitre_technique",
            description=(
                "Get a MITRE ATT&CK technique's detail from OpenCRQ: "
                "tactics, sub-techniques, and mitigations. Accepts the "
                "ATT&CK ID (e.g. T1566) or the OpenCRQ UUID."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "technique_id": {
                        "type": "string",
                        "description": "ATT&CK ID or OpenCRQ UUID.",
                    },
                    **TENANT_PROP,
                },
                "required": ["technique_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_mitre_mitigations",
            description=(
                "List or search the MITRE ATT&CK mitigations known to "
                "OpenCRQ. Pass mitigation_id for one mitigation's detail "
                "with the techniques it addresses."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Text filter."},
                    "mitigation_id": {
                        "type": "string",
                        "description": (
                            "Mitigation ATT&CK ID (e.g. M1013) or OpenCRQ "
                            "UUID for the detail view."
                        ),
                    },
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
    ]
