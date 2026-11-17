"""VirusTotal integration provider package.

Best-in-class agentic access to VirusTotal (Google) for SOC and CTI teams,
split by domain:

* ``_common``      - shared API host / headers / HTTP client / formatters.
* ``reports``      - file / URL / domain / IP reports, sandbox behaviour
                     summary, and the generic relationship pivot (the core
                     enrichment surface).
* ``search``       - universal lookup, VT Intelligence corpus search, and
                     popular threat categories.
* ``scanning``     - URL submission, chat-attached file upload, rescans, and
                     analysis polling.
* ``community``    - comments and votes (read + write + delete).
* ``hunting``      - Livehunt YARA rulesets + notifications and Retrohunt
                     jobs (premium).
* ``collections``  - IOC collections (list / read / create / manage).

Authentication: a single API key sent in the ``x-apikey`` header. The
dispatcher hands this builder the bare key (stored in ``api_key_enc`` - no
compound token; the API host is fixed). Public keys are limited to
4 requests/minute and 500/day; premium (VT Enterprise) keys unlock
Intelligence search, Livehunt, Retrohunt, and collection listing.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import VT_API, build_vt_headers
from .collections import register as register_collections
from .community import register as register_community
from .hunting import register as register_hunting
from .reports import register as register_reports
from .scanning import register as register_scanning
from .search import register as register_search

logger = logging.getLogger(__name__)

__all__ = ["VT_API", "VirusTotalIntegration", "build_tools", "build_vt_headers"]

# Lightweight authenticated endpoint used to validate a key. Chosen because
# it is available to every key tier AND never embeds the credential in the
# URL path (unlike ``GET /users/{api_key}``, which would leak the key into
# request-path logs on failure).
_VALIDATE_PATH = "/popular_threat_categories"


class VirusTotalIntegration(BaseIntegration):
    """VirusTotal threat intelligence integration (API key, x-apikey)."""

    provider_name = "virustotal"
    display_name = "VirusTotal"
    icon = "ShieldCheck"
    description = (
        "Connect VirusTotal for threat intelligence: enrich files, URLs, "
        "domains and IPs with multi-engine verdicts; search the corpus; read "
        "and write community comments and votes; submit URLs and files for "
        "analysis; and manage Livehunt rulesets, Retrohunt jobs, and IOC "
        "collections."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """VirusTotal uses an API key (x-apikey header), not OAuth."""
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
                f"{VT_API}{_VALIDATE_PATH}",
                "VirusTotal",
                "validate API key",
                headers=build_vt_headers(api_key),
            )
            return err is None and resp is not None and resp.is_success

    async def get_user_info(self, token: str) -> dict:
        """Return minimal account context.

        VirusTotal's user endpoint (``GET /users/{id}``) requires the key in
        the URL path, which would leak the credential into request logs -
        return static context instead.
        """
        return {"name": "VirusTotal", "base_url": VT_API}


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build VirusTotal tools from the bare API key."""
    api_key = (token or "").strip()
    if not api_key:
        logger.debug(
            "VirusTotal API key is empty - returning no tools. Edit the "
            "integration and set the API key."
        )
        return []

    headers = build_vt_headers(api_key)

    tools: list[ToolDefinition] = []
    tools += register_reports(VT_API, headers, executor)
    tools += register_search(VT_API, headers, executor)
    tools += register_scanning(VT_API, headers, executor)
    tools += register_community(VT_API, headers, executor)
    tools += register_hunting(VT_API, headers, executor)
    tools += register_collections(VT_API, headers, executor)
    return tools
