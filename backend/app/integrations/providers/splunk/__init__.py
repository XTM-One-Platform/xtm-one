"""Splunk integration provider package.

Full-fledged Splunk SIEM integration covering the detection -> investigation ->
hunting -> response -> intelligence loop for an agentic security team, across
both **Splunk Enterprise / Splunk Cloud** (core) and **Splunk Enterprise
Security** (the premium SIEM app):

* ``search``       - asynchronous SPL search (threat hunting), saved /
                     correlation searches, indexes, fired alerts, status.
* ``kvstore``      - KV Store collections, querying, and pushing records
                     (enrichment / IOC lookups - "pushing info into Splunk").
* ``notable``      - Enterprise Security notable events (gather/triage/respond)
                     + review statuses + correlation searches.
* ``threat_intel`` - Enterprise Security threat-intel collections (IOC
                     management - "creating intelligence").

Authentication
--------------
One credential + base URL drives both products. ``integration_tools._get_token``
assembles the compound ``base_url||username||token`` this package parses:

* ``base_url`` - the management REST endpoint (e.g. ``https://splunk.example.com:8089``).
* ``username`` - blank for **Bearer token** auth (a Splunk authentication token,
  recommended; works for Splunk Cloud + ES), or set for **HTTP Basic** auth
  (on-prem Splunk Enterprise) where the secret is the password.
* the secret (token or password) lives in the integration's API-key field.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import parse_compound_token, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    PROVIDER,
    build_splunk_headers,
    content,
    entries,
    make_api,
    splunk_api_base,
)
from .kvstore import register as register_kvstore
from .notable import register as register_notable
from .search import register as register_search
from .threat_intel import register as register_threat_intel

logger = logging.getLogger(__name__)

__all__ = [
    "SplunkIntegration",
    "build_splunk_headers",
    "build_tools",
    "splunk_api_base",
]


class SplunkIntegration(BaseIntegration):
    """Splunk Enterprise + Splunk Enterprise Security integration."""

    provider_name = "splunk"
    display_name = "Splunk"
    icon = "Search"
    description = (
        "Connect Splunk Enterprise and Enterprise Security for SPL threat "
        "hunting, notable events, correlation searches, KV Store lookups, and "
        "threat intelligence."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        base_url, username, secret = parse_compound_token(token, 3, greedy_last=True)
        base_url = splunk_api_base(base_url)
        if not base_url or not secret.strip():
            return False
        headers = build_splunk_headers(username, secret)
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/services/server/info",
                PROVIDER,
                "verify connection",
                headers=headers,
                params={"output_mode": "json"},
            )
            return err is None and resp is not None

    async def get_user_info(self, token: str) -> dict:
        base_url, username, secret = parse_compound_token(token, 3, greedy_last=True)
        base_url = splunk_api_base(base_url)
        if not base_url or not secret.strip():
            return {}
        headers = build_splunk_headers(username, secret)
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/services/server/info",
                PROVIDER,
                "get server info",
                headers=headers,
                params={"output_mode": "json"},
            )
            if err or resp is None:
                return {}
            try:
                data = resp.json()
            except Exception:
                return {}
            ents = entries(data)
            c = content(ents[0]) if ents else {}
            return {
                "server_name": c.get("serverName"),
                "version": c.get("version"),
                "guid": c.get("guid"),
            }


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Splunk tools from a ``base_url||username||token`` compound token."""
    base_url, username, secret = parse_compound_token(token, 3, greedy_last=True)
    base_url = splunk_api_base(base_url)
    username = username.strip()
    secret = secret.strip()
    if not base_url:
        logger.debug(
            "Splunk base_url is empty - returning no tools. Edit the integration "
            "and set the management base URL (e.g. https://splunk.example.com:8089)."
        )
        return []

    api = make_api(base_url, username, secret)
    tools: list[ToolDefinition] = []
    tools += register_search(base_url, api, executor)
    tools += register_kvstore(base_url, api, executor)
    tools += register_notable(base_url, api, executor)
    tools += register_threat_intel(base_url, api, executor)
    return tools
