"""OpenCRQ integration provider package.

Comprehensive management of an OpenCRQ (Governance, Risk & Compliance)
platform via its tRPC-over-HTTP API.

The provider is split by domain:

* ``_common``      - tRPC call helper, tenant resolution, UI-link helper,
                     enums, and entity formatters.
* ``frameworks``   - tenants, organizations, compliance frameworks
                     (assessment catalog and Drata mirror), Drata
                     requirements and controls, and control
                     capabilities (assessment, history).
* ``assessments``  - compliance assessments (lifecycle) and requirement
                     answers.
* ``risk``         - risk scenarios (full CRUD), attack outcomes, and
                     threat relevance settings.
* ``crq``          - cyber risk quantification: simulations, runs,
                     what-if analysis, TEF inputs, loss drivers, loss
                     estimates, CRQ engine settings, and a risk scope's
                     financial profile.
* ``assets``       - primary / supporting assets (full CRUD), dependency
                     links, asset evidence, and the CVE knowledge base.
* ``mitre``        - MITRE ATT&CK reference data (read-only).
* ``admin``        - users, tenants, organizations, groups, roles,
                     capabilities, invitations, service accounts
                     (read-only), and platform RBAC introspection.
* ``ops``          - background jobs (CRQ simulations, CVE syncs).
* ``procedure_manifest`` - the procedure list the connected OpenCRQ serves
                     about itself, and the tools it cannot serve.
* ``input_contract`` - whether the connected OpenCRQ accepts the fields a
                     call sends, checked by ``_trpc`` before it sends.
* ``findings``     - vulnerability findings, top vulnerabilities, asset
                     posture, and attack simulation test posture.
* ``threats``      - the threat catalog, threats relevant to a risk
                     scenario, and threats citing a CVE.
* ``treatment``    - risk treatment plans and budget reviews, with their
                     simulated loss reduction and return; creates and
                     updates a plan and its control moves (CTEM-18).
* ``control_validation`` - control posture proven by attack simulations.
* ``dashboard``    - the risk dashboard (exposure, tolerance, top controls,
                     active threats).
* ``connectors``   - connector health and syncs.

The tools of the last six modules are read-only, except the treatment plan
writes of ``treatment``.

Token format passed by the dispatcher: ``base_url||api_token`` (the
``extra_config`` base_url is prepended by ``_get_token`` in
``integration_tools.py``).  For platform-managed integrations (OpenCRQ
registered with XTM One) the api_token is a short-lived per-user Ed25519
JWT verified by OpenCRQ against the XTM One JWKS endpoint.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.module_versions import RegisteredVersionFn
from app.integrations.http import parse_compound_token
from app.services.integration_read_only import withhold_tools
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._common import (
    _fmt_assessment,
    _fmt_control,
    _fmt_framework,
    _fmt_risk_scenario,
    _grc_link,
    _headers,
    _refresh_if_stale,
)
from .admin import register as register_admin
from .assessments import register as register_assessments
from .assets import register as register_assets
from .connectors import register as register_connectors
from .control_validation import register as register_control_validation
from .crq import register as register_crq
from .dashboard import register as register_dashboard
from .findings import register as register_findings
from .frameworks import register as register_frameworks
from .health_reads import register as register_health_reads
from .mitre import register as register_mitre
from .ops import register as register_ops
from .procedure_manifest import INSTANCE_LABEL, ManifestProbe, ProbeLater, hidden_refusal
from .risk import register as register_risk
from .threats import register as register_threats
from .treatment import register as register_treatment

logger = logging.getLogger(__name__)

__all__ = [
    "_fmt_assessment",
    "_fmt_control",
    "_fmt_framework",
    "_fmt_risk_scenario",
    "_grc_link",
    "build_tools",
]


def _gated(tool: str, handler: Callable[..., Awaitable[Any]], probe: ManifestProbe) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        refusal = (await probe.assessment()).refusal(tool)
        if refusal is not None:
            return ToolResult(tool_call_id="", content=refusal, is_error=True)
        return await handler(*args, **kwargs)

    return wrapper


def _refuse_unserved_at_call_time(
    tools: list[ToolDefinition], executor: CallableToolExecutor, probe: ManifestProbe
) -> None:
    """Every tool stays offered; each call first awaits the build's manifest probe.

    With no registration to hold the connected OpenCRQ's manifest, asking it costs a
    request, made at the first call of a tool of this build and not when the
    tools are built. A tool the instance cannot serve then answers its refusal
    instead of running; the others run their handler unchanged.
    """
    for tool in tools:
        executor.wrap_handler(tool.name, lambda handler, name=tool.name: _gated(name, handler, probe))


def _withhold_unserved_tools(
    tools: list[ToolDefinition],
    executor: CallableToolExecutor,
    probe: ManifestProbe,
) -> list[ToolDefinition]:
    """Withhold the tools the connected OpenCRQ cannot serve and refuse their calls by name.

    A tool is unserved when a procedure it sends is not in the manifest *probe* holds.
    With a manifest in hand (a registration's) its definition leaves what the model
    sees and its handler becomes a refusal naming the procedures it needs, so a call
    that reaches it anyway (a replayed transcript) is refused rather than sent to
    fail. With no manifest (``None``) every tool stays. With
    :data:`~.procedure_manifest.PROBE` nothing is known yet: see :func:`_refuse_unserved_at_call_time`.
    """
    if not probe.loaded:
        _refuse_unserved_at_call_time(tools, executor, probe)
        return tools
    assessment = probe.assess()
    if not assessment.hidden:
        return tools
    kept = withhold_tools(
        tools,
        executor,
        {name: hidden_refusal(name, INSTANCE_LABEL, gap.missing) for name, gap in assessment.hidden.items()},
    )
    logger.info("OpenCRQ manifest: %d tool(s) withheld: %s", len(tools) - len(kept), ", ".join(sorted(assessment.hidden)))
    return kept


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    registered_version_fn: RegisteredVersionFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build OpenCRQ integration tools.

    ``token`` is ``base_url||api_token`` (compound format). ``opencrq_manifest``,
    when the dispatcher passes it, is the connected instance's
    :class:`~.procedure_manifest.Manifest` and withholds the tools it cannot
    serve; ``None``: there is none, every tool is built and no input is checked;
    :data:`~.procedure_manifest.PROBE`: each call reads the manifest first, once
    per build. Without the keyword every tool is built and nothing is ever probed.
    ``registered_version_fn`` (injected for an integration a registration owns) reads
    the version XTM One registered, named beside ``get_opencrq_platform_version``'s answer.
    """
    base_url, api_token = parse_compound_token(token, 2)
    # Normalise exactly once so all downstream uses (tRPC endpoints,
    # ``[Open in OpenCRQ](...)`` markdown links) compose cleanly.
    base_url = base_url.strip().rstrip("/")
    api_token = api_token.strip()
    # Carry the dispatcher's JWT re-minter on the headers so every call can
    # recover mid-run: a platform-managed OpenCRQ token lives ten minutes, less
    # than an assessment sweep or a CRQ run, and used to leave the rest of the
    # run failing auth once it expired. None for a static API key.
    headers = _headers(api_token, refresh_fn=_kwargs.get("refresh_fn"))

    if not base_url:
        logger.debug(
            "OpenCRQ base_url is empty - returning no tools. Edit the "
            "integration and ensure the base URL is set."
        )
        return []

    tools: list[ToolDefinition] = []
    tools += register_frameworks(
        base_url, headers, executor, registered_version_fn=registered_version_fn
    )
    tools += register_assessments(base_url, headers, executor)
    tools += register_risk(base_url, headers, executor)
    tools += register_crq(base_url, headers, executor)
    tools += register_assets(base_url, headers, executor)
    tools += register_mitre(base_url, headers, executor)
    tools += register_admin(base_url, headers, executor)
    tools += register_ops(base_url, headers, executor)
    tools += register_findings(base_url, headers, executor)
    tools += register_threats(base_url, headers, executor)
    tools += register_treatment(base_url, headers, executor)
    tools += register_control_validation(base_url, headers, executor)
    tools += register_dashboard(base_url, headers, executor)
    tools += register_connectors(base_url, headers, executor)
    tools += register_health_reads(base_url, headers, executor)
    if "opencrq_manifest" in _kwargs:
        manifest = _kwargs["opencrq_manifest"]
        # One probe per build: what the build's tools and, through the headers, every call's field check read the manifest from.
        probe = (
            ManifestProbe(base_url, headers, lambda: _refresh_if_stale(headers))
            if isinstance(manifest, ProbeLater)
            else ManifestProbe.known(manifest)
        )
        headers.manifest_probe = probe
        tools = _withhold_unserved_tools(tools, executor, probe)
    return tools
