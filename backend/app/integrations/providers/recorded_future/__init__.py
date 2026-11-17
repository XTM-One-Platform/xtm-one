"""Recorded Future integration provider package.

Best-in-class agentic access to the Recorded Future Intelligence Cloud for CTI
analysts and SOC teams, split by domain:

* ``_common``      - shared base URL / headers / HTTP client / formatters.
* ``enrichment``   - per-type Intelligence Card lookups + SOAR batch enrichment
                     + SOAR verdict triage (the core enrichment surface).
* ``search``       - Connect API entity search, risk-rule discovery, and
                     name -> entity-id resolution (Entity Match).
* ``alerts``       - classic alerts + Playbook Alerts (search / get / detail
                     panels / update).
* ``cases``        - Case API triage workflow (search / get / create / update).
* ``intelligence`` - analyst notes (search / get / publish), detection rules
                     (YARA/Sigma/Snort, with deployable source), the Links
                     relationship graph, threat actors + actor and malware
                     threat maps, risk history, Malware Intelligence
                     natural-language sandbox queries.
* ``management``   - list (watchlist / block list) management, Collective
                     Insights detection submission + history search, Identity
                     Intelligence (domain sweep, per-identity lookup, password
                     exposure check).

Authentication: a single Recorded Future API token sent in the ``X-RFToken``
header. The dispatcher (``integration_tools._get_token``) hands this builder the
compound ``base_url||api_token`` token; ``base_url`` is optional and defaults to
``https://api.recordedfuture.com`` (only a sovereign / dedicated tenant needs
to override it).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import parse_compound_token, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import build_rf_headers, rf_api_base
from .alerts import register as register_alerts
from .cases import register as register_cases
from .enrichment import register as register_enrichment
from .intelligence import register as register_intelligence
from .management import register as register_management
from .search import register as register_search

logger = logging.getLogger(__name__)

__all__ = ["RecordedFutureIntegration", "build_rf_headers", "build_tools", "rf_api_base"]

# Lightweight, broadly-available authenticated endpoint used to validate a
# token (the IP risk-rule catalog requires only basic Connect API access).
_VALIDATE_PATH = "/v2/ip/riskrules"


class RecordedFutureIntegration(BaseIntegration):
    """Recorded Future threat intelligence integration (API key, X-RFToken)."""

    provider_name = "recorded_future"
    display_name = "Recorded Future"
    icon = "Radar"
    description = (
        "Connect Recorded Future for threat intelligence: enrich IPs, domains, "
        "URLs, hashes, CVEs, malware and companies; triage alerts and Playbook "
        "Alerts; read analyst notes and detection rules; and manage IOC lists."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """Recorded Future uses an API key (X-RFToken), not OAuth."""
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate the API token against the Connect API risk-rule catalog."""
        base_url, api_token = (p.strip() for p in parse_compound_token(token, 2))
        if not api_token:
            return False
        api = rf_api_base(base_url)
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}{_VALIDATE_PATH}",
                "Recorded Future",
                "validate API token",
                headers=build_rf_headers(api_token),
            )
            return err is None and resp is not None and resp.is_success

    async def get_user_info(self, token: str) -> dict:
        """Return minimal account context (Recorded Future has no user-info endpoint)."""
        base_url, _ = (p.strip() for p in parse_compound_token(token, 2))
        return {"name": "Recorded Future", "base_url": rf_api_base(base_url)}


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Recorded Future tools from a ``base_url||api_token`` compound token."""
    # ``parse_compound_token`` does not normalise - strip each segment so pasted
    # whitespace can't yield a malformed base URL / ``X-RFToken`` header, and so
    # the empty-token guard treats a whitespace-only token as missing.
    base_url, api_token = (part.strip() for part in parse_compound_token(token, 2))
    if not api_token:
        logger.debug(
            "Recorded Future API token is empty - returning no tools. Edit the "
            "integration and set the API key."
        )
        return []

    api = rf_api_base(base_url)
    headers = build_rf_headers(api_token)

    tools: list[ToolDefinition] = []
    tools += register_enrichment(api, headers, executor)
    tools += register_search(api, headers, executor)
    tools += register_alerts(api, headers, executor)
    tools += register_cases(api, headers, executor)
    tools += register_intelligence(api, headers, executor)
    tools += register_management(api, headers, executor)
    return tools
