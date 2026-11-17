# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Agent Card projection.

The card is built by **explicit field assignment**, never by dumping an
internal object. That is a requirement rather than a style preference: this
document is served anonymously to anyone holding the URL, so a field added to
``Agent`` for an internal purpose must not be able to appear here just because
somebody added it upstream.

The skills it advertises are the agent's own built-ins, in full - external work
is bounded by the run-as identity, not by a tool list (``a2a.exposure``). Both
target platforms configure themselves from this document, so it must name what
the run actually offers.
"""

from __future__ import annotations

from typing import Any

from app.a2a import protocol as proto
from app.a2a.extensions import partner_discovery_extension
from app.models.a2a import A2AExposure
from app.models.agent import Agent


def card_url(base: str, token: str) -> str:
    """Where the card for *token* is served, under the deployment's base URL.

    The ONE spelling of that path, shared by the admin surface that hands the
    URL to operators and the discovery endpoint that hands it to partners —
    two spellings would eventually disagree about where a card lives. Callers
    pass a proxy-aware base (``external_base``), never raw ``request.base_url``:
    behind a TLS-terminating proxy the raw value is ``http://``, so the card
    would advertise a plaintext URL to every partner that reads it.
    """
    return f"{base}/a2a/{token}/.well-known/agent-card.json"


def token_url(base: str) -> str:
    """Where a partner exchanges its client id and secret for a bearer token.

    The ONE spelling of that URL, shared by the card (``tokenUrl`` of its
    client-credentials scheme) and the admin status the integration page shows
    next to the client id, so the value an operator copies is the value the
    card publishes. Same *base* rule as ``card_url``: proxy-aware, never raw.
    """
    return f"{base}/mcp/oauth/token"


#: Text is always accepted and always produced. Structured data is accepted
#: from any partner (the agent simply sees it as content) but only *produced*
#: by an agent configured for it - see ``_output_modes``.
_TEXT_MODE = "text/plain"
_JSON_MODE = "application/json"
_FILE_MODE = "application/octet-stream"


def _input_modes() -> list[str]:
    return [_TEXT_MODE, _JSON_MODE]


def _output_modes(agent: Agent) -> list[str]:
    """Output content types, derived from the agent's own configuration.

    Declared separately from the input modes on purpose. A partner's tooling
    reads this when registering the agent, so an agent that cannot produce
    structured output must not advertise that it can - otherwise the mismatch
    surfaces as a surprise at task time instead of at registration time.
    """
    modes = [_TEXT_MODE, _FILE_MODE]
    if (agent.output_format or "").lower() == "json":
        modes.append(_JSON_MODE)
    return modes


def _skills(agent: Agent) -> list[dict[str, Any]]:
    """The agent's own built-ins - exactly what the task path runs with."""
    skills: list[dict[str, Any]] = []
    for tool_id in [str(t) for t in (agent.builtin_tool_ids or [])]:
        skills.append(
            {
                "id": tool_id,
                "name": tool_id.replace("_", " ").title(),
                "description": f"{agent.name} capability: {tool_id}",
                "tags": ["builtin"],
                "inputModes": _input_modes(),
                "outputModes": _output_modes(agent),
            }
        )
    if not skills:
        # An agent with no tools is still a usable conversational agent; the
        # card must describe *something* or a partner cannot register it.
        skills.append(
            {
                "id": "converse",
                "name": "Converse",
                "description": agent.description or f"Interact with {agent.name}",
                "tags": ["conversation"],
                "inputModes": _input_modes(),
                "outputModes": _output_modes(agent),
            }
        )
    return skills


def build_agent_card(
    agent: Agent,
    exposure: A2AExposure,
    *,
    base_url: str,
    version: str,
    app_version: str,
) -> dict[str, Any]:
    """Project *agent* into an Agent Card under the negotiated *version*.

    No ``signature`` field is emitted. The representation stays
    forward-compatible - adding one changes no existing field's name,
    position or type - so a partner never has to re-register for it.

    ``tenant`` is deliberately absent. The protocol defines it as an optional
    opaque routing hint, and this platform has no tenant entity; mapping it to
    the company-managed flag would invent a second, divergent tenancy concept.
    """
    endpoint = f"{base_url}/a2a/{exposure.card_token}"
    legacy = proto.is_legacy(version)

    card: dict[str, Any] = {
        "name": agent.name,
        "description": agent.description or "",
        "version": app_version,
        "protocolVersion": version,
        "capabilities": {
            # Stated rather than omitted either way: a partner reading a
            # missing field may probe, and one reading false will not try.
            "streaming": False,
            # A task can now stay open while the work it delegated runs, which
            # is the case the protocol defines push notifications for.
            "pushNotifications": True,
            # The one thing this deployment serves that A2A does not define:
            # an endpoint listing every agent the caller's own credential
            # reaches. ``extensions`` is the spec's own way to say so, spelled
            # identically in v0.3 and v1.0, so it needs no per-version branch
            # below. See ``a2a.extensions`` for why declaring it beats being
            # probed for it.
            #
            # Declared on EVERY card, including one served to an agent no
            # OAuth partner can reach. That matches how ``securitySchemes``
            # below already behaves: this document is anonymous, so it
            # describes the deployment's inbound surface rather than the
            # reader, and the declaration's own description states which
            # callers the endpoint serves.
            "extensions": [partner_discovery_extension()],
        },
        "defaultInputModes": _input_modes(),
        "defaultOutputModes": _output_modes(agent),
        "skills": _skills(agent),
        "securitySchemes": {
            "oauth2ClientCredentials": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": token_url(base_url),
                        "scopes": {},
                    }
                },
            }
        },
    }

    requirement = [{"oauth2ClientCredentials": []}]
    # ``preferredTransport`` is required in v0.3, and ``additionalInterfaces``
    # repeats the sole binding because the spec wants that list complete.
    v03_names: dict[str, Any] = {
        "security": requirement,
        "url": f"{endpoint}/jsonrpc",
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [
            {"url": f"{endpoint}/jsonrpc", "transport": "JSONRPC"}
        ],
    }
    v10_names: dict[str, Any] = {
        "securityRequirements": requirement,
        "supportedInterfaces": [
            {
                "url": f"{endpoint}/jsonrpc",
                "protocolBinding": "JSONRPC",
                "protocolVersion": proto.PROTOCOL_VERSION_1_0,
            }
        ],
    }

    if legacy:
        # Declared, so answered in that version's names alone.
        card.update(v03_names)
    else:
        # What an undeclared fetch gets, in both spellings: the platforms that
        # fetch that way require different ones - ``supportedInterfaces`` and
        # ``url`` - and send no header to tell them apart by. A reader finding
        # neither of its own names sees no endpoint and no credential.
        card.update(v10_names)
        card.update(v03_names)

    return card
