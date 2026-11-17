"""OpenCRQ proven control posture tool (read-only).

* ``get_opencrq_control_validation`` - what attack simulations proved about a
  control: the ATT&CK techniques it was seen to prevent or detect, and those
  where a simulation contradicted it.
"""

from __future__ import annotations

import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    OFFSET_PROP,
    ORGANIZATION_SCOPE_PROP,
    TENANT_PROP,
    _as_int,
    _client,
    _client_only_hint,
    _grc_link,
    _organization_scope,
    _page,
    _resolve_tenant,
    _scoped,
    _text,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

_EVIDENCE_PAGE = 15


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def get_opencrq_control_validation(
        control_id: str = "",
        organization_id: str = "",
        offset: int = 0,
        tenant_id: str = "",
    ) -> str:
        control_id = _text(control_id)
        organization_id = _text(organization_id)
        if not control_id:
            return "control_id is required."
        offset = _as_int(offset)
        if offset is None or offset < 0:
            return "offset must be an integer >= 0."
        payload = _scoped({"capabilityId": control_id}, organization_id)
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            scope, err = await _organization_scope(
                base_url, headers, tenant, organization_id, client=client
            )
            if err:
                return err
            control, err = await _trpc(
                base_url, headers, "controlCapabilities.getControlCapability",
                "get control", input={"id": control_id}, tenant_id=tenant,
                client=client,
            )
            if err:
                return err
            if not control:
                return f"Control {control_id} not found."
            validation, err = await _trpc(
                base_url, headers, "controlValidation.getControlValidation",
                "get control validation", input=payload, tenant_id=tenant,
                client=client,
            )
            if err:
                return _client_only_hint(err)
            evidence, evidence_err = await _trpc(
                base_url, headers, "controlValidation.getControlValidationEvidence",
                "get control validation evidence", input=payload,
                tenant_id=tenant, client=client,
            )

        name = " ".join(str(v) for v in (control.get("externalId"), control.get("name")) if v)
        lines = [f"[Control validation] {name or 'control'} (id: {control_id}, {scope})"]
        link = _grc_link(base_url, tenant, "control_validation", control_id)
        if link:
            lines.append(link)
        summary = (validation or {}).get("summary") or {}
        tested = summary.get("testedTechniques", 0)
        if not tested:
            lines.append(
                "  No attack simulation has tested this control yet: its "
                "posture is declared, not proven."
            )
            return "\n".join(lines)
        lines.append(
            f"  Techniques tested: {tested}, validated: "
            f"{summary.get('validatedTechniques', 0)}, contradicted: "
            f"{summary.get('contradictedTechniques', 0)}"
        )
        if summary.get("latestProofAt"):
            lines.append(f"  Latest proof: {str(summary['latestProofAt'])[:10]}")

        cells = (validation or {}).get("cells") or []
        for state in ("contradicted", "validated"):
            matching = [c for c in cells if c.get("state") == state]
            if matching:
                lines.append(
                    f"  {state.capitalize()} technique checks ({len(matching)}): "
                    + ", ".join(
                        f"{c.get('techniqueExternalId', '?')} {c.get('function', '?')}"
                        f" ({c.get('validatedVotes', 0)}/{c.get('totalVotes', 0)} votes)"
                        for c in matching
                    )
                )

        if evidence_err:
            lines.append(f"  (Simulation evidence unavailable: {_client_only_hint(evidence_err)})")
        elif evidence:
            ranked = sorted(
                evidence, key=lambda e: str(e.get("evaluatedAt") or ""), reverse=True
            )
            shown, more = _page(ranked, offset, _EVIDENCE_PAGE)
            lines.append("  Simulation evidence, latest first:")
            for e in shown:
                assets = ", ".join(
                    a.get("name") or a.get("derivedName") or "?"
                    for a in (e.get("assets") or [])[:5]
                )
                lines.append(
                    f"    - {e.get('techniqueExternalId', '?')} {e.get('function', '?')}: "
                    f"{e.get('verdict', '?')} in {e.get('scenarioName') or 'a simulation'}"
                    f" ({e.get('securityPlatformType') or 'security platform'}, "
                    f"{str(e.get('evaluatedAt') or '?')[:10]})"
                    + (f" on {assets}" if assets else "")
                )
            if more:
                lines.append(f"  {more}")
        return "\n".join(lines)

    executor.register("get_opencrq_control_validation", get_opencrq_control_validation)

    return [
        ToolDefinition(
            name="get_opencrq_control_validation",
            description=(
                "Get the proven posture of an OpenCRQ control: which ATT&CK "
                "techniques attack simulations showed it prevents or detects, "
                "which ones contradicted it, and the simulation evidence, "
                f"{_EVIDENCE_PAGE} at a time. Needs a user session (a service "
                "account is refused)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "control_id": {
                        "type": "string",
                        "description": "Control (capability) UUID.",
                    },
                    **ORGANIZATION_SCOPE_PROP,
                    **OFFSET_PROP,
                    **TENANT_PROP,
                },
                "required": ["control_id"],
            },
            source=_SOURCE,
        ),
    ]
