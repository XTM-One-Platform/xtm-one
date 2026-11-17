"""Intel 471 (Titan API) integration provider package.

Best-in-class agentic access to Intel 471's Titan cybercrime-intelligence
platform (``https://api.intel471.com/v1``) for CTI analysts and SOC teams,
split by domain:

* ``_common``    - shared base URL / HTTP Basic headers / request helpers /
                   formatters / pagination + time-window helpers.
* ``adversary``  - Global Search, actors, finished-intelligence reports (info
                   reports, breach alerts, spot/situation reports, news),
                   entity resolution, and the GIR / tag reference catalogues.
* ``malware``    - Malware Intelligence (indicators, YARA, events, malware
                   reports/families), adversary-intelligence IOCs, CVE reports,
                   compromised-credential intelligence, and underground
                   monitoring (forum posts, private/instant messages,
                   marketplaces, data-leak blogs).
* ``watchers``   - the alert feed plus watcher-group / watcher management (the
                   write surface for autonomous underground monitoring).

Authentication is **HTTP Basic**: the account email as the username and a Titan
API key as the password. There is no OAuth and no token refresh; access is
bounded by the account's Titan subscriptions. The dispatcher
(``integration_tools._get_token``) hands this builder the compound
``api_username||api_key`` token (email prepended from ``extra_config``); it is
split back into ``(email, api_key)`` by ``parse_compound_token``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import parse_compound_token, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import api_base, build_basic_headers, client
from .adversary import register as register_adversary
from .malware import register as register_malware
from .watchers import register as register_watchers

logger = logging.getLogger(__name__)

__all__ = [
    "Intel471Integration",
    "api_base",
    "build_basic_headers",
    "build_tools",
    "validate_connection",
]

# Cheap, universally-available authenticated probe: ``/girs?count=1`` reads one
# GIR from the reference catalogue - available to every Titan account regardless
# of which intelligence subscriptions it holds, so it validates auth without
# tripping a per-subscription 403.
_VALIDATE_PATH = "/girs"


async def validate_connection(api_username: str, api_key: str) -> tuple[bool, str]:
    """Verify Intel 471 credentials with a 1-record GIR read (shared with the test)."""
    if not (api_username or "").strip() or not (api_key or "").strip():
        return False, "Missing Intel 471 account email or API key."
    headers = build_basic_headers(api_username, api_key)
    async with client(15) as c:
        resp, err = await safe_request(
            c,
            "get",
            f"{api_base()}{_VALIDATE_PATH}",
            "Intel 471",
            "validate credentials",
            headers=headers,
            params={"count": 1},
            expected_statuses={401, 403},
        )
    if err:
        if resp is not None and resp.status_code in (401, 403):
            return False, (
                "Authentication failed - check the account email (username) and "
                "the Titan API key (password)."
            )
        return False, err
    return True, "Intel 471 is connected and authenticated."


class Intel471Integration(BaseIntegration):
    """Intel 471 Titan cybercrime-intelligence integration (API key, HTTP Basic)."""

    provider_name = "intel471"
    display_name = "Intel 471"
    icon = "Radar"
    description = (
        "Connect Intel 471 (Titan) for cybercrime intelligence: threat actors, "
        "finished-intelligence reports, breach alerts, malware intelligence, "
        "IOCs, credentials, and underground forum/marketplace monitoring."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """Intel 471 uses HTTP Basic (email + API key), not OAuth."""
        return None

    async def validate_connection(self, token: str) -> bool:
        api_username, api_key = parse_compound_token(token, 2)
        ok, _ = await validate_connection(api_username, api_key)
        return ok

    async def get_user_info(self, token: str) -> dict:
        """Return minimal context (Titan has no user-info endpoint)."""
        api_username, _ = parse_compound_token(token, 2)
        return {"name": "Intel 471", "account": (api_username or "").strip()}


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Intel 471 tools from the compound ``api_username||api_key`` token."""
    if "||" not in (token or ""):
        # ``_get_token`` only prepends the email when ``api_username`` is set on
        # the integration; a delimiter-less token means the email is missing and
        # must not be misreported as an empty API key.
        logger.debug(
            "Intel 471 token is missing the account email - returning no tools. "
            "Edit the integration and set 'api_username' (Titan account email)."
        )
        return []
    api_username, api_key = (p.strip() for p in parse_compound_token(token, 2))
    if not api_username or not api_key:
        logger.debug(
            "Intel 471 account email or API key is empty - returning no tools. "
            "Edit the integration and set both credentials."
        )
        return []

    headers = build_basic_headers(api_username, api_key)

    tools: list[ToolDefinition] = []
    tools += register_adversary(headers, executor)
    tools += register_malware(headers, executor)
    tools += register_watchers(headers, executor)
    return tools
