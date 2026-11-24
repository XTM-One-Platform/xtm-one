"""OpenCRQ tenancy, framework catalog, and control capability tools.

* ``list_opencrq_tenants``               - tenants accessible to the caller.
* ``get_opencrq_platform_version``       - the version the platform runs.
* ``list_opencrq_organizations``         - organizations of a tenant.
* ``get_opencrq_organization``           - one organization's stored fields.
* ``list_opencrq_frameworks``            - assessment-catalog and Drata frameworks.
* ``get_opencrq_framework``              - Drata framework detail.
* ``list_opencrq_framework_requirements`` - requirements of a Drata framework.
* ``list_opencrq_drata_controls``        - Drata controls of a framework or requirement.
* ``list_opencrq_control_capabilities``  - control capabilities with their assessment.
* ``get_opencrq_control_capability``     - capability detail (techniques, risk scenarios).
* ``update_opencrq_control_capability_assessment`` - declare strength and coverage.
* ``manage_opencrq_scope_control_posture``         - declare or clear a capability's posture on one scope.
* ``manage_opencrq_capability_techniques``         - add, remove, restore or reset the techniques a capability covers.
* ``get_opencrq_control_capability_history``       - assessment change audit trail.
"""

from __future__ import annotations

import json
import logging

from app.core.module_versions import RegisteredVersionFn, version_answer
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _UUID,
    COVERAGE_BUCKETS,
    OFFSET_PROP,
    STRENGTH_RUNGS,
    TENANT_PROP,
    _client,
    _client_only_hint,
    _fmt_control,
    _fmt_framework,
    _grc_link,
    _one_line,
    _page,
    _quarantined,
    _resolve_tenant,
    _trpc,
    _untrusted,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

#: The axes of a capability assessment that can be declared or re-attached.
_AXES = ("strength", "coverage")

_POSTURE_ACTIONS = ["declare", "clear"]

#: Axis -> the field getResolvedControlPosture reads its level from; ``<axis>Source`` says whose it is.
_POSTURE_LEVEL_FIELDS = {
    "strength": "strengthRung",
    "coverage": "coverageBucket",
}

_TECHNIQUE_ACTIONS = ["add", "remove", "restore", "reset"]

_TECHNIQUE_PROCEDURES = {
    "add": "controlCapabilities.addTechniqueOverride",
    "remove": "controlCapabilities.removeTechnique",
    "restore": "controlCapabilities.restoreTechnique",
}

_CAPABILITY_FUNCTIONS = ["prevent", "detect"]


def _fmt_organization(o: dict) -> str:
    """One organization (risk scope) as getOrganization stores it, one field per line."""

    def _named(rel: object, fallback_id: object) -> str:
        if isinstance(rel, dict) and rel.get("name"):
            return f"{_one_line(rel.get('name'), 200)} (id: {rel.get('id') or fallback_id})"
        return str(fallback_id) if fallback_id else "not set"

    def _value(v: object) -> str:
        return "not set" if v is None or v == "" else _one_line(v, 300)

    times = (o.get("reactionTimeMin"), o.get("reactionTimeLikely"), o.get("reactionTimeMax"))
    reaction = (
        "not set"
        if all(t is None for t in times)
        else "min {} h, likely {} h, max {} h".format(*("?" if t is None else t for t in times))
    )
    mapped = o.get("mappedAssetCount")
    lines = [
        f"Name: {_one_line(o.get('name'), 200, '?')}",
        f"ID: {o.get('id')}",
        f"Description: {_value(o.get('description'))}",
        f"Legal entity name: {_value(o.get('legalEntityName'))}",
        f"Sector: {_named(o.get('sector'), o.get('sectorId'))}",
        f"Country: {_named(o.get('country'), o.get('countryId'))}",
        f"Domain: {_value(o.get('domain'))}",
        f"Revenue band: {_value(o.get('revenueBand'))}",
        f"Employee count: {_value(o.get('employeeCount'))}",
        f"Estate mode: {_value(o.get('estateMode'))}",
        f"Reaction time: {reaction}",
        f"Mapped assets: {'unknown' if mapped is None else mapped}",
    ]
    return "\n".join(lines)


def _fmt_scope_posture(organization_id: str, p: dict) -> str:
    """The posture a scope resolves to, and per axis whether it is the scope's or the tenant's."""
    axes = (
        ("Strength", p.get("strengthRung"), p.get("strengthSource"), p.get("tenantStrengthRung")),
        ("Coverage", p.get("coverageBucket"), p.get("coverageSource"), p.get("tenantCoverageBucket")),
    )
    lines = [f"  Posture on organization {organization_id}:"]
    for label, value, source, tenant in axes:
        shown = value or "not declared"
        lines.append(
            f"    {label}: {shown} (scope; tenant: {tenant or 'not declared'})"
            if source == "scope"
            else f"    {label}: {shown} (tenant)"
        )
    if p.get("scopeDeclaredAt"):
        lines.append(f"    Declared on the scope: {str(p['scopeDeclaredAt'])[:10]}")
    if p.get("comment"):
        lines.append(f"    Scope comment: {_one_line(p['comment'], 300)}")
    return "\n".join(lines)


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def _fmt_edges(edges: list[dict]) -> list[str]:
    def label(edge: dict) -> str:
        mark = " (added)" if edge.get("state") == "added" else ""
        return (
            f"{_one_line(edge.get('techniqueExternalId'), 20, '?')} {_one_line(edge.get('techniqueName'), 120)}"
        ).strip() + mark

    lines = []
    for function in sorted({e["function"] for e in edges if e.get("function")}):
        live = [
            e
            for e in edges
            if e.get("function") == function and e.get("state") != "removed"
        ]
        if live:
            lines.append(
                f"  MITRE techniques, {function} ({len(live)}): "
                + ", ".join(label(e) for e in live[:15])
                + (f" ... and {len(live) - 15} more" if len(live) > 15 else "")
            )
    return lines


def _technique_refusal(
    action: str, function: str, label: str, edge: dict | None
) -> str | None:
    """Why OpenCRQ would refuse (with an unmapped 500) the change of a technique, or ``None``."""
    state = edge.get("state") if edge else None
    if action == "add" and edge:
        if state == "removed":
            return (
                f"{label} is a catalogue technique this tenant removed from {function}: "
                "use action=restore to bring it back."
            )
        origin = "added by this tenant" if state == "added" else "recommended by the catalogue"
        return f"{label} is already covered for {function} ({origin}): nothing to add."
    if action == "remove" and (not edge or state == "removed"):
        if edge:
            return (
                f"{label} is already removed from {function} for this tenant: "
                "action=restore brings it back."
            )
        return f"{label} is not covered by this capability for {function}: nothing to remove."
    if action == "restore" and state != "removed":
        return (
            f"{label} is not removed from {function}: only a catalogue technique "
            "this tenant removed can be restored."
        )
    return None


def _fmt_capability_event(e: dict) -> str:
    changes = []
    for axis, old_key, new_key, mode_key in (
        ("strength", "oldStrength", "newStrength", "strengthMode"),
        ("coverage", "oldCoverage", "newCoverage", "coverageMode"),
    ):
        old, new, mode = e.get(old_key), e.get(new_key), e.get(mode_key)
        if old != new:
            changes.append(
                f"{axis} {old or 'unset'} -> {new or 'unset'}"
                + (f" ({mode})" if mode else "")
            )
        elif mode:
            changes.append(f"{axis} now {mode}")
    sources = list(
        dict.fromkeys(
            _one_line(s.get("complianceAssessmentName"), 120)
            or _one_line(s.get("frameworkTitle"), 120)
            or _one_line(s.get("source"), 60)
            for s in e.get("sources") or []
        )
    )
    who = _one_line(e.get("userName"), 120) or _one_line(e.get("connector"), 60, "a user")
    return (
        f"- {str(e.get('createdAt', ''))[:16].replace('T', ' ')}: "
        f"{str(e.get('type', '')).replace('_', ' ')}"
        f"{': ' + '; '.join(changes) if changes else ''}"
        + (f" via {', '.join(sources)}" if sources else "")
        + f" by {who}"
        + (f" (comment: {_one_line(e['comment'], 120)})" if e.get("comment") else "")
    )


def register(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    registered_version_fn: RegisteredVersionFn | None = None,
) -> list[ToolDefinition]:
    async def list_opencrq_tenants() -> str:
        tenants, err = await _trpc(
            base_url, headers, "tenants.getTenants", "list tenants"
        )
        if err:
            return err
        tenants = tenants or []
        if not tenants:
            return "No OpenCRQ tenants are accessible with this account."
        return _quarantined(
            f"Tenants ({len(tenants)}):",
            "\n".join(f"- {_one_line(t.get('name'), 200, '?')} (id: {t.get('id')})" for t in tenants),
            "tenants",
        )

    async def list_opencrq_organizations(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        orgs, err = await _trpc(
            base_url, headers, "organizations.getOrganizations",
            "list organizations", tenant_id=tenant,
        )
        if err:
            return err
        orgs = orgs or []
        if not orgs:
            return "No organizations found in this tenant."
        lines = []
        for o in orgs:
            entry = f"- {_one_line(o.get('name'), 200, '?')} (id: {o.get('id')})"
            link = _grc_link(base_url, tenant, "organization", o.get("id"))
            lines.append(entry + (f"\n{link}" if link else ""))
        return _quarantined(
            f"Organizations ({len(orgs)}) in tenant {tenant}:", "\n".join(lines), "organizations"
        )

    async def get_opencrq_organization(organization_id: str = "", tenant_id: str = "") -> str:
        organization_id = (organization_id or "").strip()
        if not organization_id:
            return "organization_id is required (use list_opencrq_organizations to discover)."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        org, err = await _trpc(
            base_url,
            headers,
            "organizations.getOrganization",
            "get organization",
            input={"id": organization_id},
            tenant_id=tenant,
        )
        if err:
            return err
        if not isinstance(org, dict):
            return f"Organization {organization_id} not found in tenant {tenant}."
        link = _grc_link(base_url, tenant, "organization", org.get("id"))
        return _quarantined(
            f"Organization in tenant {tenant}:",
            _fmt_organization(org),
            "organization",
            link or "",
        )

    async def list_opencrq_frameworks(tenant_id: str = "") -> str:
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            catalog, catalog_err = await _trpc(
                base_url, headers, "complianceAssessments.getFrameworks",
                "list assessment catalog frameworks",
                tenant_id=tenant, client=client,
            )
            drata, drata_err = await _trpc(
                base_url, headers, "drata.getFrameworks",
                "list Drata frameworks", tenant_id=tenant, client=client,
            )
        if catalog_err and drata_err:
            return f"{catalog_err}\n{drata_err}"
        catalog = catalog or []
        drata = drata or []
        if not catalog_err and not drata_err and not catalog and not drata:
            return "No compliance frameworks found."
        sections = []
        for source, title, hint, rows, source_err in (
            (
                "catalog",
                "Assessment catalog",
                "compliance assessments run against these; pass the ID as "
                "framework_id to create_opencrq_compliance_assessment",
                catalog,
                catalog_err,
            ),
            (
                "drata",
                "Drata mirror",
                "synced from Drata; pass the ID as framework_id to "
                "get_opencrq_framework, list_opencrq_framework_requirements "
                "or list_opencrq_drata_controls",
                drata,
                drata_err,
            ),
        ):
            if source_err:
                sections.append(f"{title}: could not be read - {source_err}")
            elif not rows:
                sections.append(f"{title}: no framework.")
            else:
                sections.append(
                    _quarantined(
                        f"{title} ({len(rows)}) - {hint}:",
                        "\n\n".join(
                            _fmt_framework(f, base_url, tenant, source=source)
                            for f in rows[:30]
                        ),
                        f"frameworks ({title.lower()})",
                    )
                )
        return "Frameworks by source.\n\n" + "\n\n".join(sections)

    async def get_opencrq_framework(framework_id: str = "", tenant_id: str = "") -> str:
        if not framework_id:
            return "framework_id is required."
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            framework, err = await _trpc(
                base_url, headers, "drata.getFramework", "get Drata framework",
                input={"id": framework_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if framework:
                return _untrusted(
                    _fmt_framework(framework, base_url, tenant, source="drata"),
                    "framework (Drata mirror)",
                )
            catalog, catalog_err = await _trpc(
                base_url, headers, "complianceAssessments.getFrameworks",
                "list assessment catalog frameworks",
                tenant_id=tenant, client=client,
            )
        if not catalog_err and any(
            f.get("id") == framework_id for f in catalog or []
        ):
            return (
                f"Framework {framework_id} belongs to the assessment catalog, "
                "not the Drata mirror. Its compliance assessments: "
                "list_opencrq_compliance_assessments."
            )
        return f"Framework {framework_id} not found in the Drata mirror."

    async def list_opencrq_framework_requirements(
        framework_id: str = "", offset: int = 0, tenant_id: str = ""
    ) -> str:
        if not framework_id:
            return "framework_id is required."
        if offset < 0:
            return "offset must be >= 0."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        requirements, err = await _trpc(
            base_url, headers, "drata.getRequirements",
            "list Drata framework requirements",
            input={"frameworkId": framework_id}, tenant_id=tenant,
        )
        if err:
            return err
        requirements = requirements or []
        if not requirements:
            return "No requirements found for this Drata framework."
        shown, more = _page(requirements, offset, 100)
        lines = []
        for r in shown:
            code = _one_line(r.get("code"), 40)
            name = _one_line(r.get("name"), 200, "?")
            label = f"{code} {name}" if code else name
            category = f" [{_one_line(r['category'], 80)}]" if r.get("category") else ""
            lines.append(
                f"- {label}{category} in scope: {_yes_no(r.get('isInScope'))}, "
                f"ready: {_yes_no(r.get('isReady'))}, "
                f"controls: {r.get('controlCount', 0)} (id: {r.get('id')})"
            )
        return _quarantined(
            f"Requirements ({len(requirements)}):",
            "\n".join(lines),
            "Drata framework requirements",
            more,
        )

    async def list_opencrq_control_capabilities(
        tenant_id: str = "", search: str = "", offset: int = 0
    ) -> str:
        if offset < 0:
            return "offset must be >= 0."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        capabilities, err = await _trpc(
            base_url, headers, "controlCapabilities.getControlCapabilities",
            "list control capabilities", tenant_id=tenant,
        )
        if err:
            return err
        capabilities = capabilities or []
        if search:
            q = search.lower()
            capabilities = [
                c
                for c in capabilities
                if q in (c.get("name") or "").lower()
                or q in (c.get("externalId") or "").lower()
                or q in (c.get("description") or "").lower()
            ]
        if not capabilities:
            return "No control capabilities matched."
        size = 40
        shown, more = _page(capabilities, offset, size)
        total = len(capabilities)
        header = f"Control capabilities ({total}):"
        if shown and total > size:
            # Said before the rows, so a reader that stops at the first page knows it did.
            pages = -(-total // size)
            end = offset + len(shown)
            header = f"Control capabilities ({total}; page {offset // size + 1} of {pages}, " + (
                f"next offset {end}):" if end < total else "last page):"
            )
        return _quarantined(
            header,
            "\n\n".join(_fmt_control(c, base_url, tenant) for c in shown),
            "control capabilities",
            more,
        )

    async def get_opencrq_control_capability(
        capability_id: str = "", organization_id: str = "", tenant_id: str = ""
    ) -> str:
        if not capability_id:
            return "capability_id is required."
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            capability, err = await _trpc(
                base_url, headers, "controlCapabilities.getControlCapability",
                "get control capability",
                input={"id": capability_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not capability:
                return f"Control capability {capability_id} not found."
            scenarios, scenarios_err = await _trpc(
                base_url, headers,
                "controlCapabilities.getControlCapabilityRiskScenarios",
                "list capability risk scenarios",
                input={"id": capability_id}, tenant_id=tenant, client=client,
            )
            postures, posture_err = [], None
            if organization_id:
                postures, posture_err = await _trpc(
                    base_url, headers,
                    "controlCapabilities.getResolvedControlPosture",
                    "get scope control posture",
                    input={"organizationId": organization_id},
                    tenant_id=tenant, client=client,
                )
        out = _fmt_control(capability, base_url, tenant)
        notes: list[str] = []
        if capability.get("description"):
            out += f"\n  Description: {_one_line(capability['description'], 300)}"
        sources = capability.get("assessmentSources") or []
        if sources:
            out += "\n  Fed by: " + "; ".join(
                f"{_one_line(s.get('axis'), 20)}: "
                f"{_one_line(s.get('frameworkTitle'), 120) or _one_line(s.get('source'), 60)}"
                for s in sources
            )
        rungs = capability.get("rungs") or []
        if rungs:
            out += "\n  Strength rungs:\n" + "\n".join(
                f"    {_one_line(r.get('rung'), 10)}: {_one_line(r.get('label'), 160)}" for r in rungs
            )
        edge_lines = _fmt_edges(capability.get("edges") or [])
        if edge_lines:
            out += "\n" + "\n".join(edge_lines)
        if organization_id:
            posture = next(
                (p for p in postures or [] if p.get("capabilityId") == capability_id),
                None,
            )
            if posture_err:
                notes.append(f"Posture on organization {organization_id}: lookup failed - {posture_err}")
            elif posture:
                out += "\n" + _fmt_scope_posture(organization_id, posture)
            else:
                out += f"\n  Posture on organization {organization_id}: not listed"
        if scenarios_err:
            notes.append(f"Linked risk scenarios: lookup failed - {scenarios_err}")
        elif scenarios:
            out += f"\n  Linked risk scenarios ({len(scenarios)}):\n" + "\n".join(
                f"    - {_one_line(s.get('name'), 200, '?')} "
                f"(organization {_one_line(s.get('organizationName'), 200)}, "
                f"shared techniques: {s.get('sharedTtpCount', 0)}, id: {s.get('id')})"
                for s in scenarios[:10]
            )
            if len(scenarios) > 10:
                out += f"\n    ... and {len(scenarios) - 10} more."
        else:
            out += "\n  Linked risk scenarios: none"
        return _quarantined("", out, "control capability", *notes)

    async def update_opencrq_control_capability_assessment(
        capability_id: str = "",
        strength: str = "",
        coverage: str = "",
        reattach: list[str] | None = None,
        comment: str = "",
        tenant_id: str = "",
    ) -> str:
        if not capability_id:
            return "capability_id is required."
        reattach = sorted({(axis or "").strip().lower() for axis in reattach or []} - {""})
        if any(axis not in _AXES for axis in reattach):
            return f"reattach must name axes among: {', '.join(_AXES)}."
        declared: dict[str, str | None] = {}
        for label, value, allowed in (
            ("strength", strength, STRENGTH_RUNGS),
            ("coverage", coverage, COVERAGE_BUCKETS),
        ):
            value = (value or "").strip().upper()
            if value and value not in allowed:
                return f"{label} must be one of: {', '.join(allowed)}."
            declared[label] = value or None
        if not any(declared.values()) and not reattach:
            return "Provide at least one of strength, coverage or reattach."
        both = [axis for axis in reattach if declared[axis]]
        if both:
            return f"An axis is either declared or re-attached, not both: {', '.join(both)}."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "controlCapabilities.upsertAssessment",
            "update control capability assessment",
            input={
                "capabilityId": capability_id,
                "strengthRung": declared["strength"],
                "coverageBucket": declared["coverage"],
                "reattach": reattach,
                "comment": (comment or "").strip() or None,
            },
            mutation=True,
            tenant_id=tenant,
        )
        if err:
            return _client_only_hint(err)
        changes = []
        if any(declared.values()):
            changes.append(
                f"declared {', '.join(f'{k} {v}' for k, v in declared.items() if v)}, "
                "now manual"
            )
        if reattach:
            changes.append(
                f"re-attached {', '.join(reattach)} to "
                f"{'its' if len(reattach) == 1 else 'their'} sources"
            )
        return f"Control capability {capability_id}: {'; '.join(changes)}."

    async def manage_opencrq_scope_control_posture(
        action: str = "",
        organization_id: str = "",
        capability_id: str = "",
        strength: str = "",
        coverage: str = "",
        comment: str = "",
        dimension: str = "",
        replace_overlay: bool = False,
        tenant_id: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _POSTURE_ACTIONS:
            return "action must be declare or clear."
        if not organization_id:
            return "organization_id is required."
        if not capability_id:
            return "capability_id is required."
        levels = {
            "strength": (strength or "").strip().upper(),
            "coverage": (coverage or "").strip().upper(),
        }
        dimension = (dimension or "").strip().lower()
        if action == "clear":
            if dimension not in _AXES:
                return f"dimension must be one of: {', '.join(_AXES)}."
            stray = [
                name
                for name, value in (
                    *levels.items(),
                    ("comment", comment),
                    ("replace_overlay", replace_overlay),
                )
                if value
            ]
            if stray:
                return f"clear takes a dimension only: do not pass {', '.join(stray)}."
            _, err = await _scope_posture_call(
                "controlCapabilities.clearScopeControlPosture",
                "clear scope control posture",
                {
                    "organizationId": organization_id,
                    "capabilityId": capability_id,
                    "dimension": dimension,
                },
                tenant_id,
            )
            if err:
                return _client_only_hint(err)
            return (
                f"Organization {organization_id}, control capability {capability_id}: "
                f"{dimension} follows the tenant again; the scope's other levels are "
                "unchanged, and a scope left with none overrides nothing."
            )
        if dimension:
            return "declare takes strength and coverage: do not pass dimension."
        for label, allowed in (
            ("strength", STRENGTH_RUNGS),
            ("coverage", COVERAGE_BUCKETS),
        ):
            if levels[label] and levels[label] not in allowed:
                return f"{label} must be one of: {', '.join(allowed)}."
        if not any(levels.values()):
            return "Provide at least one of strength or coverage."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        passed = {k: v for k, v in levels.items() if v}
        kept: dict[str, str] = {}
        if not replace_overlay:
            # The write replaces the whole overlay: an axis left out would
            # follow the tenant again, so the scope's own levels ride along.
            postures, err = await _trpc(
                base_url, headers,
                "controlCapabilities.getResolvedControlPosture",
                "get scope control posture",
                input={"organizationId": organization_id},
                tenant_id=tenant,
            )
            if err:
                return _client_only_hint(err)
            current = next(
                (p for p in postures or [] if p.get("capabilityId") == capability_id),
                {},
            )
            for axis, field in _POSTURE_LEVEL_FIELDS.items():
                value = current.get(field)
                if axis not in passed and value and current.get(f"{axis}Source") == "scope":
                    kept[axis] = str(value)
        sent = {**kept, **passed}
        _, err = await _scope_posture_call(
            "controlCapabilities.declareScopeControlPosture",
            "declare scope control posture",
            {
                "organizationId": organization_id,
                "capabilityId": capability_id,
                "strengthRung": sent.get("strength"),
                "coverageBucket": sent.get("coverage"),
                "comment": (comment or "").strip() or None,
            },
            tenant,
        )
        if err:
            return _client_only_hint(err)
        overrides = [f"{k} {sent[k]}" for k in _AXES if k in sent]
        inherits = [k for k in _AXES if k not in sent]
        return (
            f"Organization {organization_id}, control capability {capability_id}: "
            f"the scope now overrides {', '.join(overrides)}"
            + (f"; set: {', '.join(f'{k} {v}' for k, v in passed.items())}" if kept else "")
            + (f"; kept from the scope: {', '.join(f'{k} {v}' for k, v in kept.items())}" if kept else "")
            + (f"; {', '.join(inherits)} follow{'s' if len(inherits) == 1 else ''} the tenant" if inherits else "")
            + "."
        )

    async def _scope_posture_call(
        procedure: str, action: str, payload: dict, tenant_id: str
    ) -> tuple[object, str | None]:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return None, err
        return await _trpc(
            base_url, headers, procedure, action,
            input=payload, mutation=True, tenant_id=tenant,
        )

    async def manage_opencrq_capability_techniques(
        action: str = "",
        capability_id: str = "",
        function: str = "",
        technique_id: str = "",
        tenant_id: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _TECHNIQUE_ACTIONS:
            return "action must be add, remove, restore or reset."
        if not capability_id:
            return "capability_id is required."
        function = (function or "").strip().lower()
        if function not in _CAPABILITY_FUNCTIONS:
            return "function must be prevent or detect."
        technique_id = (technique_id or "").strip()
        if action == "reset" and technique_id:
            return "reset takes no technique_id: it clears every override of the function."
        if action != "reset" and not technique_id:
            return "technique_id is required (an ATT&CK ID like T1059, or the technique UUID)."
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            if action == "reset":
                _, err = await _trpc(
                    base_url, headers, "controlCapabilities.resetTechniqueOverrides",
                    "reset capability technique overrides",
                    input={"capabilityId": capability_id, "function": function},
                    mutation=True, tenant_id=tenant, client=client,
                )
                if err:
                    return _client_only_hint(err)
                return (
                    f"Control capability {capability_id}: cleared this tenant's overrides "
                    f"on {function}; the techniques it added are gone and the ones it "
                    "removed are back, as the catalogue recommends."
                )
            technique: dict = {}
            if _UUID.match(technique_id):
                technique_uuid = technique_id.lower()
            else:
                found, err = await _trpc(
                    base_url, headers, "mitreAttack.getTechniqueByExternalId",
                    "get MITRE technique",
                    input={"externalId": technique_id.upper()},
                    tenant_id=tenant, client=client,
                )
                if err:
                    return err
                if not found:
                    return f"MITRE technique {technique_id} not found."
                technique = found
                technique_uuid = str(found["id"]).lower()
            capability, err = await _trpc(
                base_url, headers, "controlCapabilities.getControlCapability",
                "get control capability",
                input={"id": capability_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not capability:
                return f"Control capability {capability_id} not found."
            edge = next(
                (
                    e
                    for e in capability.get("edges") or []
                    if str(e.get("techniqueId", "")).lower() == technique_uuid
                    and e.get("function") == function
                ),
                None,
            )
            if action == "add" and not technique and not edge:
                # A UUID the catalogue does not hold comes back from the mutation as an opaque 500.
                rows, err = await _trpc(
                    base_url, headers, "mitreAttack.getTechniques",
                    "list MITRE techniques", tenant_id=tenant, client=client,
                )
                if err:
                    return err
                technique = next(
                    (r for r in rows or [] if str(r.get("id", "")).lower() == technique_uuid),
                    {},
                )
                if not technique:
                    return f"MITRE technique {technique_id} not found."
            source = technique or edge or {}
            label = (
                f"{_one_line(source.get('externalId') or source.get('techniqueExternalId'), 20) or technique_uuid} "
                f"{_one_line(source.get('name') or source.get('techniqueName'), 120)}"
            ).strip()
            refusal = _technique_refusal(action, function, label, edge)
            if refusal:
                return refusal
            _, err = await _trpc(
                base_url, headers,
                _TECHNIQUE_PROCEDURES[action],
                f"{action} capability technique",
                input={
                    "capabilityId": capability_id,
                    "techniqueId": technique_uuid,
                    "function": function,
                },
                mutation=True, tenant_id=tenant, client=client,
            )
            if err:
                return _client_only_hint(err)
        prefix = f"Control capability {capability_id}: "
        if action == "add":
            return f"{prefix}now covers {label} on {function}, added by this tenant."
        if action == "restore":
            return f"{prefix}{label} is back on {function}, as the catalogue recommends."
        if edge and edge.get("state") == "added":
            return (
                f"{prefix}{label}, which this tenant had added, is erased from {function}: "
                "it cannot be restored."
            )
        return (
            f"{prefix}{label} is removed from {function} for this tenant; the catalogue "
            "still recommends it, and action=restore brings it back."
        )

    async def list_opencrq_drata_controls(
        framework_id: str = "",
        requirement_id: str = "",
        offset: int = 0,
        tenant_id: str = "",
    ) -> str:
        if offset < 0:
            return "offset must be >= 0."
        if not framework_id and not requirement_id:
            return "Provide framework_id or requirement_id."
        if framework_id and requirement_id:
            return "Provide framework_id or requirement_id, not both."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        if framework_id:
            procedure, params = "drata.getControlsForFramework", {
                "frameworkId": framework_id
            }
        else:
            procedure, params = "drata.getControlsForRequirement", {
                "requirementId": requirement_id
            }
        controls, err = await _trpc(
            base_url, headers, procedure, "list Drata controls",
            input=params, tenant_id=tenant,
        )
        if err:
            return err
        controls = controls or []
        if not controls:
            return "No Drata controls found."
        shown, more = _page(controls, offset, 100)
        lines = []
        for c in shown:
            flags = (
                f"ready: {_yes_no(c.get('isReady'))}, "
                f"monitored: {_yes_no(c.get('isMonitored'))}, "
                f"evidence: {_yes_no(c.get('hasEvidence'))}, "
                f"policy: {_yes_no(c.get('hasPolicy'))}"
            )
            if c.get("requirementCount") is not None:
                flags += f", requirements: {c['requirementCount']}"
            if c.get("archivedAt"):
                flags += f", archived {str(c['archivedAt'])[:10]}"
            lines.append(
                f"- {_one_line(c.get('code'), 40, '?')} {_one_line(c.get('name'), 200, '?')}: {flags} "
                f"(id: {c.get('id')})"
            )
        return _quarantined(f"Drata controls ({len(controls)}):", "\n".join(lines), "Drata controls", more)

    async def get_opencrq_control_capability_history(
        capability_id: str = "", tenant_id: str = ""
    ) -> str:
        if not capability_id:
            return "capability_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        events, err = await _trpc(
            base_url, headers, "controlCapabilities.getCapabilityEvents",
            "get control capability history",
            input={"capabilityId": capability_id}, tenant_id=tenant,
        )
        if err:
            return err
        events = events or []
        if not events:
            return "No assessment history recorded for this control capability."
        return _quarantined(
            f"Assessment history of control capability {capability_id} ({len(events)}, newest first):",
            "\n".join(_fmt_capability_event(e) for e in events[:40]),
            "control capability history",
            f"... and {len(events) - 40} more." if len(events) > 40 else "",
        )

    async def get_opencrq_platform_version() -> str:
        health, err = await _trpc(base_url, headers, "health", "read the platform version")
        version = health.get("version") if isinstance(health, dict) and not err else None
        if err:
            answer, version = err, ""
        elif not isinstance(version, str) or not version.strip():
            answer, version = "OpenCRQ answered health without a version: the platform version could not be read.", ""
        else:
            version = version.strip()[:200]
            answer = _quarantined("OpenCRQ version:", json.dumps({"version": version}), "platform version")
        if registered_version_fn is None:
            return answer
        # The live value stays inside its quarantine: the comparison names it, never quotes it.
        return version_answer("OpenCRQ", answer, version, await registered_version_fn(), quote_live=False)

    executor.register("list_opencrq_tenants", list_opencrq_tenants)
    executor.register("get_opencrq_platform_version", get_opencrq_platform_version)
    executor.register("list_opencrq_organizations", list_opencrq_organizations)
    executor.register("get_opencrq_organization", get_opencrq_organization)
    executor.register("list_opencrq_frameworks", list_opencrq_frameworks)
    executor.register("get_opencrq_framework", get_opencrq_framework)
    executor.register(
        "list_opencrq_framework_requirements", list_opencrq_framework_requirements
    )
    executor.register("list_opencrq_drata_controls", list_opencrq_drata_controls)
    executor.register(
        "list_opencrq_control_capabilities", list_opencrq_control_capabilities
    )
    executor.register(
        "get_opencrq_control_capability", get_opencrq_control_capability
    )
    executor.register(
        "update_opencrq_control_capability_assessment",
        update_opencrq_control_capability_assessment,
    )
    executor.register(
        "manage_opencrq_scope_control_posture",
        manage_opencrq_scope_control_posture,
    )
    executor.register(
        "manage_opencrq_capability_techniques",
        manage_opencrq_capability_techniques,
    )
    executor.register(
        "get_opencrq_control_capability_history",
        get_opencrq_control_capability_history,
    )

    _tenant_prop = TENANT_PROP

    return [
        ToolDefinition(
            name="list_opencrq_tenants",
            description=(
                "List the OpenCRQ tenants accessible to the current user. "
                "Use the returned tenant IDs to scope other OpenCRQ tools."
            ),
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_platform_version",
            description=(
                "Read the version the bound OpenCRQ platform runs (tRPC health). "
                "Read-only; compare it with a release to tell whether an upgrade landed."
            ),
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_organizations",
            description=(
                "List the organizations of an OpenCRQ tenant. Organizations "
                "scope risk scenarios, assessments, and primary assets."
            ),
            input_schema={"type": "object", "properties": {**_tenant_prop}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_organization",
            description=(
                "Read one OpenCRQ organization's stored fields: sector, country, "
                "domain, revenue band, employee count, estate mode, reaction "
                "times, mapped asset count. Read-only; verifies an update."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string", "description": "Organization UUID."},
                    **_tenant_prop,
                },
                "required": ["organization_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_frameworks",
            description=(
                "List the compliance frameworks of an OpenCRQ tenant from "
                "both sources, each labelled: the assessment catalog "
                "(frameworks a compliance assessment is run against, e.g. "
                "NIST CSF) and the Drata mirror (frameworks synced from "
                "Drata, e.g. ISO 27001, SOC 2, with their requirement "
                "readiness counts). Framework IDs of one source do not work "
                "with the tools of the other."
            ),
            input_schema={"type": "object", "properties": {**_tenant_prop}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_framework",
            description=(
                "Get a Drata-mirror framework's details: readiness and "
                "requirement counts. For an assessment-catalog framework, "
                "use list_opencrq_compliance_assessments."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "framework_id": {
                        "type": "string",
                        "description": "Drata framework UUID (list_opencrq_frameworks).",
                    },
                    **_tenant_prop,
                },
                "required": ["framework_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_framework_requirements",
            description=(
                "List the requirements of a Drata-mirror framework with "
                "their code, category, scope, readiness and number of "
                "mapped Drata controls, 100 at a time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "framework_id": {
                        "type": "string",
                        "description": "Drata framework UUID (list_opencrq_frameworks).",
                    },
                    **OFFSET_PROP,
                    **_tenant_prop,
                },
                "required": ["framework_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_drata_controls",
            description=(
                "List the Drata controls mirrored in OpenCRQ, either those "
                "mapped to a Drata framework's requirements or those of one "
                "requirement, with their readiness, monitoring, evidence "
                "and policy flags, 100 at a time. These are Drata's controls, "
                "not OpenCRQ's control capabilities."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "framework_id": {
                        "type": "string",
                        "description": "Drata framework UUID.",
                    },
                    "requirement_id": {
                        "type": "string",
                        "description": (
                            "Drata requirement UUID "
                            "(list_opencrq_framework_requirements)."
                        ),
                    },
                    **OFFSET_PROP,
                    **_tenant_prop,
                },
                "anyOf": [
                    {"required": ["framework_id"]},
                    {"required": ["requirement_id"]},
                ],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_control_capabilities",
            description=(
                "List the control capabilities of an OpenCRQ tenant with their "
                "assessment: strength and coverage (each L0 to L3, "
                "with its auto or manual mode), when and by whom the capability "
                "was last assessed, or 'not assessed'. Supports text search on "
                "name / external ID / description. Shows 40 at a time, from offset."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Text filter."},
                    **OFFSET_PROP,
                    **_tenant_prop,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_control_capability",
            description=(
                "Get an OpenCRQ control capability's detail: its assessment and "
                "what feeds each axis, its functions (prevent, detect), the "
                "meaning of each strength rung, the MITRE ATT&CK techniques it "
                "acts on, and the risk scenarios that share those techniques. "
                "With organization_id, also the posture that organization "
                "resolves to: per axis, whether it comes from the scope's own "
                "declaration or from the tenant."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capability_id": {
                        "type": "string",
                        "description": (
                            "Control capability UUID "
                            "(list_opencrq_control_capabilities)."
                        ),
                    },
                    "organization_id": {
                        "type": "string",
                        "description": (
                            "Organization (scope) UUID, to read the capability's "
                            "posture there (list_opencrq_organizations)."
                        ),
                    },
                    **_tenant_prop,
                },
                "required": ["capability_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_control_capability_assessment",
            description=(
                "Declare the assessment of an OpenCRQ control capability: "
                "strength and coverage (each L0 to L3), "
                "with a comment. The first declaration of a capability gives "
                "both strength and coverage; a later one may give either "
                "and the other stays as it is. A declared axis becomes "
                "manual and no longer follows its sources, until reattach gives "
                "it back to them. The comment is the "
                "reason for this change and replaces the previous one. Needs a "
                "user session (a service account is refused). Confirm with the "
                "user before changing an assessment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capability_id": {
                        "type": "string",
                        "description": (
                            "Control capability UUID "
                            "(list_opencrq_control_capabilities)."
                        ),
                    },
                    "strength": {
                        "type": "string",
                        "enum": STRENGTH_RUNGS,
                        "description": (
                            "Strength rung; get_opencrq_control_capability "
                            "gives the meaning of each rung."
                        ),
                    },
                    "coverage": {
                        "type": "string",
                        "enum": COVERAGE_BUCKETS,
                        "description": "Coverage bucket.",
                    },
                    "reattach": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(_AXES)},
                        "description": (
                            "Axes to give back to their sources (connectors, "
                            "published assessments), undoing a manual declaration."
                        ),
                    },
                    "comment": {"type": "string", "description": "Justification."},
                    **_tenant_prop,
                },
                "required": ["capability_id"],
                "anyOf": [
                    {"required": ["strength"]},
                    {"required": ["coverage"]},
                ],
                "additionalProperties": False,
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencrq_scope_control_posture",
            description=(
                "Declare or clear an OpenCRQ control capability's posture on one "
                "organization (a scope), overriding the tenant's assessment there "
                "only. OpenCRQ replaces the whole scope overlay on a declaration, "
                "so action=declare first reads the scope's posture and carries "
                "over every level the scope already declares that you do not "
                "pass; only the levels you pass change, and the answer lists "
                "what was set and what was kept. Pass replace_overlay=true to "
                "start over: only the levels you pass are declared and every "
                "other one follows the tenant again. action=declare takes at "
                "least one of strength or coverage (each L0 to L3) "
                "and an optional comment (the scope's earlier comment is not "
                "carried over); action=clear takes dimension and gives that one "
                "level back to the tenant, leaving the others. A CRQ simulation "
                "is queued after a change. Needs a user session (a service "
                "account is refused). Confirm with the user before changing a "
                "scope's posture."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": _POSTURE_ACTIONS},
                    "organization_id": {
                        "type": "string",
                        "description": (
                            "Organization (scope) UUID "
                            "(list_opencrq_organizations)."
                        ),
                    },
                    "capability_id": {
                        "type": "string",
                        "description": (
                            "Control capability UUID "
                            "(list_opencrq_control_capabilities)."
                        ),
                    },
                    "strength": {
                        "type": "string",
                        "enum": STRENGTH_RUNGS,
                        "description": "declare: strength rung the scope overrides.",
                    },
                    "coverage": {
                        "type": "string",
                        "enum": COVERAGE_BUCKETS,
                        "description": "declare: coverage bucket the scope overrides.",
                    },
                    "comment": {
                        "type": "string",
                        "description": "declare: justification of the scope's declaration.",
                    },
                    "dimension": {
                        "type": "string",
                        "enum": list(_AXES),
                        "description": "clear: the level to give back to the tenant.",
                    },
                    "replace_overlay": {
                        "type": "boolean",
                        "description": (
                            "declare: true replaces the scope's overlay by the levels "
                            "passed (the others follow the tenant); false (default) "
                            "keeps the levels the scope already declares."
                        ),
                    },
                    **_tenant_prop,
                },
                "required": ["action", "organization_id", "capability_id"],
                "additionalProperties": False,
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencrq_capability_techniques",
            description=(
                "Change which MITRE ATT&CK techniques an OpenCRQ control capability "
                "covers for this tenant, on one function (prevent or detect), as "
                "overrides of the catalogue's recommendation. action=add covers a "
                "technique the capability does not cover on that function; "
                "action=remove drops one (a catalogue technique stays listed as "
                "removed and can be restored, one this tenant added is erased); "
                "action=restore brings back a catalogue technique this tenant "
                "removed; action=reset deletes every override of the function, "
                "techniques added and removed alike. Read the coverage first "
                "(get_opencrq_control_capability lists the techniques and the "
                "tenant's overrides): an add of a technique already covered, a "
                "remove of one that is not covered and a restore of one that is "
                "not removed are refused. Needs a user session (a service account "
                "is refused). Confirm with the user before changing a capability's "
                "techniques; reset discards all the function's overrides."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": _TECHNIQUE_ACTIONS},
                    "capability_id": {
                        "type": "string",
                        "description": (
                            "Control capability UUID "
                            "(list_opencrq_control_capabilities)."
                        ),
                    },
                    "function": {
                        "type": "string",
                        "enum": _CAPABILITY_FUNCTIONS,
                        "description": "The function whose coverage changes.",
                    },
                    "technique_id": {
                        "type": "string",
                        "description": (
                            "add, remove, restore: the technique, an ATT&CK ID "
                            "like T1059 or its OpenCRQ UUID "
                            "(search_opencrq_mitre_techniques). Omit for reset."
                        ),
                    },
                    **_tenant_prop,
                },
                "required": ["action", "capability_id", "function"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_control_capability_history",
            description=(
                "Get the assessment history of an OpenCRQ control capability, "
                "newest first: when its strength or coverage "
                "moved, from and to which values, who or which connector moved "
                "it, and the sources behind a derived value. Needs a user "
                "session: a service-account token is refused."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capability_id": {
                        "type": "string",
                        "description": (
                            "Control capability UUID "
                            "(list_opencrq_control_capabilities)."
                        ),
                    },
                    **_tenant_prop,
                },
                "required": ["capability_id"],
            },
            source=_SOURCE,
        ),
    ]
