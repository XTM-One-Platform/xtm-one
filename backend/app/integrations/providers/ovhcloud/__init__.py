"""OVHcloud integration provider package.

Reads the compute and network assets of an OVHcloud account through the
OVHcloud API (``/1.0``) and runs a few lifecycle actions, split by domain:

* ``_common``       - endpoints, the compound token, the request signature,
                      the server clock offset, OAuth2, rate limit, rendering.
* ``dedicated``     - bare metal servers (``/dedicated/server``).
* ``public_cloud``  - Public Cloud projects, instances, volumes, networks,
                      load balancers, Managed Kubernetes, Managed Databases,
                      object storage names, SSH key names, quota.
* ``vps_hpc``       - VPS and Hosted Private Cloud (VMware) VMs.
* ``network``       - IP blocks, firewall, mitigation, IP Load Balancing,
                      vRack, domains and DNS records, a generic GET.
* ``inventory``     - one normalized compute inventory across the products.

Write tools are lifecycle actions only (reboot, start, stop), named with a
write verb so a read-only binding withholds them. No delete, order, billing,
reinstall or credential creation exists here. Secret-named values are never
answered (``_common.redact``).
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    ENDPOINTS,
    MODE_APPLICATION_KEY,
    MODE_OAUTH2,
    PROVIDER,
    OvhClient,
    build_ovhcloud_token,
    compute_signature,
    parse_ovhcloud_token,
    token_from_config,
)
from .dedicated import register as register_dedicated
from .inventory import register as register_inventory
from .network import register as register_network
from .public_cloud import register as register_public_cloud
from .vps_hpc import register as register_vps_hpc

logger = logging.getLogger(__name__)

__all__ = [
    "ENDPOINTS",
    "MODE_APPLICATION_KEY",
    "MODE_OAUTH2",
    "build_ovhcloud_token",
    "build_tools",
    "compute_signature",
    "parse_ovhcloud_token",
    "token_from_config",
    "validate_connection",
]


async def validate_connection(token: str) -> tuple[bool, str]:
    """Test Connection: who the credential is and what it may reach.

    The key mode reads ``/auth/currentCredential`` (its access rules), the
    OAuth2 mode ``/me``; both name the account. Never echoes a secret.
    """
    creds = parse_ovhcloud_token(token)
    missing = creds.missing()
    if missing:
        return False, f"Missing {', '.join(missing)} for the {creds.mode} mode."
    client = OvhClient(creds)
    me, err = await client.get("/me", "read the account")
    if err:
        return False, err.removeprefix("Error: ")
    nic = (me or {}).get("nichandle") if isinstance(me, dict) else None
    message = f"{PROVIDER} ({creds.endpoint}) is connected as account {nic or 'unknown'}"
    if creds.mode == MODE_APPLICATION_KEY:
        credential, cred_err = await client.get("/auth/currentCredential", "read the current credential")
        rules = (credential or {}).get("rules") if isinstance(credential, dict) and not cred_err else None
        if rules:
            granted = ", ".join(f"{r.get('method')} {r.get('path')}" for r in rules[:8] if isinstance(r, dict))
            more = f" (+{len(rules) - 8} more)" if len(rules) > 8 else ""
            message += f"; access rules: {granted}{more}"
    else:
        message += " (OAuth2 service account)"
    return True, message


def build_tools(token: str, executor: CallableToolExecutor, **_kwargs: Any) -> list[ToolDefinition]:
    """Build the OVHcloud tools from ``endpoint||mode||key_or_client_id||consumer_key||secret``.

    Every tool builds whatever the credential holds; a missing part is named
    at call time, so a half-configured integration says what to fix.
    """
    client = OvhClient(parse_ovhcloud_token(token))
    tools: list[ToolDefinition] = []
    tools += register_inventory(client, executor)
    tools += register_dedicated(client, executor)
    tools += register_public_cloud(client, executor)
    tools += register_vps_hpc(client, executor)
    tools += register_network(client, executor)
    return tools
