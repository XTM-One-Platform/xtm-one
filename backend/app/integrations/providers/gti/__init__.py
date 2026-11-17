"""Google Threat Intelligence (GTI) integration provider package.

Best-in-class agentic access to Google Threat Intelligence - the Google /
Mandiant / VirusTotal merged threat intel product (docs at
gtidocs.virustotal.com) - split by domain:

* ``_common``       - shared API hosts / headers / formatters (incl. the
                      ``x-tool`` header required for ``gti_assessment``).
* ``enrichment``    - GTI assessments for files / URLs / domains / IPs
                      (verdict, threat score, severity), batch IoC summaries,
                      and Code Insight code analysis.
* ``threats``       - the Threat Landscape: curated threat actors, malware
                      families, software/toolkits, campaigns, IoC
                      collections, reports, vulnerabilities, country and
                      industry profiles (search, read, relationships, IoC
                      search, MITRE tree, timeline).
* ``threat_lists``  - hourly categorised Threat List IoC packages.
* ``ioc_stream``    - the unified IoC notification feed (Livehunt, followed
                      threats, threat profiles, saved searches).
* ``threat_profiles`` - personalised threat tracking with ML
                      recommendations (list / read / recommendations /
                      manage).
* ``dtm``           - Digital Threat Monitoring: deep/dark-web alerts,
                      monitors, and document search (base ``/dtm``).
* ``asm``           - Attack Surface Management: projects, entities,
                      issues, technologies (base ``/asm``).
* ``files``         - the files the other tools meet: samples (only
                      as Google TI's password-protected ZIP), report PDFs,
                      IoC exports, DTM alert attachments (list / download /
                      upload by file_id).

The classic community VirusTotal surface (raw reports, scanning, comments,
votes, Livehunt/Retrohunt, user collections) lives in the separate
``virustotal`` provider - this provider focuses on the curated Google TI
intelligence surface.

Authentication: a single API key sent in the ``x-apikey`` header (stored in
``api_key_enc`` - no compound token; the API host is fixed). Feature
availability is license-tiered: Enterprise / Enterprise Plus unlock threat
actors, campaigns, most threat lists, DTM, and ASM.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import PersistFileFn
from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import GTI_API, GTI_ASM_API, GTI_DTM_API, build_gti_headers
from .asm import register as register_asm
from .dtm import register as register_dtm
from .enrichment import register as register_enrichment
from .files import register as register_files
from .ioc_stream import register as register_ioc_stream
from .threat_lists import register as register_threat_lists
from .threat_profiles import register as register_threat_profiles
from .threats import register as register_threats

logger = logging.getLogger(__name__)

__all__ = [
    "GTI_API",
    "GTI_ASM_API",
    "GTI_DTM_API",
    "GTIIntegration",
    "build_gti_headers",
    "build_tools",
]

# Lightweight authenticated endpoint used to validate a key. Chosen because
# it is available to every key tier AND never embeds the credential in the
# URL path (unlike ``GET /users/{api_key}``, which would leak the key into
# request-path logs on failure).
_VALIDATE_PATH = "/popular_threat_categories"


class GTIIntegration(BaseIntegration):
    """Google Threat Intelligence integration (API key, x-apikey)."""

    provider_name = "gti"
    display_name = "Google Threat Intelligence"
    icon = "ShieldAlert"
    description = (
        "Connect Google Threat Intelligence for curated threat intel: GTI "
        "assessments (verdict, threat score, severity) on files, URLs, "
        "domains and IPs; the Threat Landscape (threat actors, malware "
        "families, campaigns, reports, vulnerabilities); categorised threat "
        "lists; the IoC Stream; threat profiles; digital threat monitoring; "
        "attack surface management; and Code Insight."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """Google TI uses an API key (x-apikey header), not OAuth."""
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate the API key against a cheap authenticated endpoint."""
        api_key = (token or "").strip()
        if not api_key:
            return False
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GTI_API}{_VALIDATE_PATH}",
                "Google Threat Intelligence",
                "validate API key",
                headers=build_gti_headers(api_key),
            )
            return err is None and resp is not None and resp.is_success

    async def get_user_info(self, token: str) -> dict:
        """Return minimal account context.

        Google TI's user endpoint (``GET /users/{id}``) requires the key in
        the URL path, which would leak the credential into request logs -
        return static context instead.
        """
        return {"name": "Google Threat Intelligence", "base_url": GTI_API}


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Google Threat Intelligence tools from the bare API key.

    ``persist_file_fn`` (injected by ``integration_tools``) stores a sample,
    report, IoC export or DTM attachment as a file of the run.
    """
    api_key = (token or "").strip()
    if not api_key:
        logger.debug(
            "Google Threat Intelligence API key is empty - returning no "
            "tools. Edit the integration and set the API key."
        )
        return []

    headers = build_gti_headers(api_key)

    tools: list[ToolDefinition] = []
    tools += register_enrichment(GTI_API, headers, executor)
    tools += register_threats(GTI_API, headers, executor)
    tools += register_threat_lists(GTI_API, headers, executor)
    tools += register_ioc_stream(GTI_API, headers, executor)
    tools += register_threat_profiles(GTI_API, headers, executor)
    tools += register_dtm(GTI_DTM_API, headers, executor)
    tools += register_asm(GTI_ASM_API, headers, executor)
    tools += register_files(
        GTI_API, GTI_DTM_API, headers, executor, persist_file_fn=persist_file_fn
    )
    return tools
