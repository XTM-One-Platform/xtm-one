# SPDX-License-Identifier: Apache-2.0

"""Aikido Security: the whole public REST API as tools, and webhook events.

Aikido is an application security platform (dependency, SAST, IaC, secrets,
container, cloud, DAST and runtime scanning). The provider covers every
operation of its public API (``operations.json``: 218 operations in 28
sections), one tool each, so an AppSec agent can triage issues (detail,
counts, reachability, severity, snooze, ignore, solve, notes), read the
inventory (code repositories, containers, clouds, domains, virtual
machines, teams, users), export SBOMs and compliance reports, drive
Autofix, Zen, pentests, task tracking and webhooks.

Credential: an API client (client id + client secret) of the workspace and
its data region; see ``_client.py`` for auth, pacing and retries,
``_tools.py`` for how an operation becomes a tool and ``webhooks.py`` for
the event types and the signature check.
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._client import (
    DEFAULT_REGION,
    PROVIDER,
    REGIONS,
    AikidoClient,
    build_aikido_token,
    parse_aikido_token,
    token_from_config,
)
from ._tools import build_operation_tools, operations
from .webhooks import EVENT_TYPES, SIGNATURE_HEADER, verify_signature

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_REGION",
    "EVENT_TYPES",
    "REGIONS",
    "SIGNATURE_HEADER",
    "build_aikido_token",
    "build_tools",
    "operations",
    "parse_aikido_token",
    "token_from_config",
    "validate_connection",
    "verify_signature",
]


async def validate_connection(token: str) -> tuple[bool, str]:
    """Test Connection: mint a token and read the workspace it belongs to.

    Never echoes a secret.
    """
    creds = parse_aikido_token(token)
    missing = creds.missing()
    if missing:
        return False, f"Missing {', '.join(missing)}."
    resp, err = await AikidoClient(creds).request("GET", "/workspace", "read the workspace")
    if err or resp is None:
        return False, (err or "No answer").removeprefix("Error: ")
    info = resp.data if isinstance(resp.data, dict) else {}
    name = info.get("name") or info.get("workspace_name") or "unknown"
    return True, f"{PROVIDER} ({creds.region}, {creds.host}) is connected to workspace {name}"


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every Aikido tool for the compound ``region||client_id||client_secret``.

    The tools are built whatever the credential holds; a missing part is
    named at call time, so a half-configured integration says what to fix.
    """
    client = AikidoClient(parse_aikido_token(token))
    return build_operation_tools(client, executor, persist_file_fn=kwargs.get("persist_file_fn"))
