"""CrowdSec integration provider package.

CrowdSec (https://www.crowdsec.net) is the collaborative security platform:
100k+ Security Engine installations share attack signals that CrowdSec curates
into IP reputation intelligence and actionable blocklists. The provider spans
both CrowdSec APIs, split by domain:

* ``_common``     - shared hosts / headers / token parsing / CTI formatters.
* ``cti``         - CTI API v2 (IP reputation): single lookup, Lucene smoke
                    search, premium batch lookup, the community blocklist
                    (fire), plus the Service API info probe.
* ``decisions``   - console decisions: fleet-wide ban/captcha/throttle
                    (list / create / delete).
* ``blocklists``  - Blocklist as a Service: private list curation, catalog
                    search, incremental IPs, subscriptions, sharing, download.
* ``allowlists``  - centralized allowlists (consolidated lifecycle tool).
* ``tracker``     - the CVE Live Exploit Tracker: tracked CVEs, exploiting
                    IPs, phase-change events, protect rules / timeline /
                    indicators, fingerprints / vendors / products / tags.
* ``console``     - integration endpoints (firewall pull URLs) + remediation
                    metrics.

Authentication: two independent API keys, both sent in the ``x-api-key``
header - a CTI API key (Console > Settings > CTI API Keys, free tier
available) for ``https://cti.api.crowdsec.net/v2``, and a Service API key
(Console > Settings > Service API Keys, premium) for
``https://admin.api.crowdsec.net/v1``. The dispatcher
(``integration_tools._get_token``) hands this builder the compound
``cti_api_key||service_api_key`` token; either key may be omitted and the
matching tool subset returns an actionable error at call time.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CTI_API,
    SERVICE_API,
    build_crowdsec_headers,
    parse_crowdsec_token,
)
from .allowlists import register as register_allowlists
from .blocklists import register as register_blocklists
from .console import register as register_console
from .cti import register as register_cti
from .decisions import register as register_decisions
from .tracker import register as register_tracker

logger = logging.getLogger(__name__)

__all__ = [
    "CTI_API",
    "SERVICE_API",
    "CrowdSecIntegration",
    "build_crowdsec_headers",
    "build_tools",
    "parse_crowdsec_token",
    "validate_cti_key",
    "validate_service_key",
]


async def validate_cti_key(cti_key: str, timeout: int = 15) -> bool:
    """Validate a CTI API key with a cheap single-IP lookup.

    ``GET /smoke/8.8.8.8`` is a freemium endpoint available to every key tier;
    a 404 (IP unknown) still proves the key authenticated, so both 200 and 404
    count as valid. Shared with the connectivity test.
    """
    async with egress_client(timeout=timeout) as client:
        resp, _ = await safe_request(
            client,
            "get",
            f"{CTI_API}/smoke/8.8.8.8",
            "CrowdSec",
            "validate CTI API key",
            headers=build_crowdsec_headers(cti_key),
            expected_statuses={404},
        )
        return resp is not None and resp.status_code in (200, 404)


async def validate_service_key(service_key: str, timeout: int = 15) -> bool:
    """Validate a Service API key against ``GET /info``.

    Shared with the connectivity test.
    """
    async with egress_client(timeout=timeout) as client:
        resp, err = await safe_request(
            client,
            "get",
            f"{SERVICE_API}/info",
            "CrowdSec",
            "validate Service API key",
            headers=build_crowdsec_headers(service_key),
        )
        return err is None and resp is not None and resp.is_success


class CrowdSecIntegration(BaseIntegration):
    """CrowdSec collaborative security / CTI integration (x-api-key auth)."""

    provider_name = "crowdsec"
    display_name = "CrowdSec"
    icon = "ShieldAlert"
    description = (
        "Connect CrowdSec for crowd-sourced IP reputation (CTI API smoke + "
        "fire) and console automation: decisions, blocklists, allowlists, "
        "and the CVE Live Exploit Tracker (Service API)."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """CrowdSec uses API keys in the ``x-api-key`` header, not OAuth."""
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate whichever of the two keys is configured (both must pass)."""
        cti_key, service_key = parse_crowdsec_token(token)
        if not cti_key and not service_key:
            return False
        if cti_key and not await validate_cti_key(cti_key):
            return False
        return not (service_key and not await validate_service_key(service_key))

    async def get_user_info(self, token: str) -> dict:
        """Return console org context when a Service API key is configured."""
        _, service_key = parse_crowdsec_token(token)
        if service_key:
            async with egress_client(timeout=15) as client:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{SERVICE_API}/info",
                    "CrowdSec",
                    "get Service API info",
                    headers=build_crowdsec_headers(service_key),
                )
                if err is None and resp is not None:
                    return resp.json() or {}
        return {"name": "CrowdSec", "cti_api": CTI_API}


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build CrowdSec tools from the ``cti_api_key||service_api_key`` token."""
    cti_key, service_key = parse_crowdsec_token(token)
    if not cti_key and not service_key:
        logger.debug(
            "CrowdSec integration has neither a CTI API key nor a Service API "
            "key - returning no tools. Edit the integration and set at least "
            "one key."
        )
        return []

    cti_headers = build_crowdsec_headers(cti_key) if cti_key else None
    service_headers = build_crowdsec_headers(service_key) if service_key else None

    tools: list[ToolDefinition] = []
    tools += register_cti(cti_headers, service_headers, executor)
    tools += register_decisions(service_headers, executor)
    tools += register_blocklists(service_headers, executor)
    tools += register_allowlists(service_headers, executor)
    tools += register_tracker(service_headers, executor)
    tools += register_console(service_headers, executor)
    return tools
