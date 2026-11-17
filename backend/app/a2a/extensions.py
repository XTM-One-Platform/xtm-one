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

"""The non-standard capabilities this deployment declares on its agent cards.

A2A defines one well-known path serving exactly ONE agent card, and no method
that enumerates agents (the SDK's whole vocabulary is ``message/*``,
``tasks/*`` and ``agent/getAuthenticatedExtendedCard`` — that last one returns
a fuller version of the SAME agent's card, not a directory of its siblings).
So a partner exposing three agents publishes three cards and a client has no
specified way to learn about the second and third. This deployment closes that
gap with an endpoint of its own, :func:`api.v1.a2a_server.list_partner_agents`.

The spec has a sanctioned way to SAY so, and this module is it:
``AgentCapabilities.extensions``, an ``AgentExtension`` of ``{uri,
description, required, params}``. Present under that exact name in both v0.3
and v1.0, which is why the card needs no per-version spelling for it.

Declaring it turns a blind probe into a negotiated capability: a client
learns support from the card instead of GETting an unadvertised path with its
bearer attached and reading a 404 as the answer — and a partner willing to
implement discovery has something to implement against.

**One definition, three readers.** The card that declares the extension, the
route that serves it and the client that calls it all read the constants
below, so an endpoint that moves cannot leave a card advertising where it used
to be. ``tests/test_a2a_client_discovery.py`` pins that agreement, including
against the router prefix the route is mounted under.
"""

from __future__ import annotations

from typing import Any

#: Identifies the extension SPEC — never a deployment's endpoint. An extension
#: URI is a name, so it must be stable across deployments and must not be
#: built from ``base_url``: two deployments implementing the same extension
#: have to name it identically or neither can recognise the other. Versioned
#: in the URI itself, because that is the only way an incompatible change can
#: be told apart from the original by a client that knows only the old one.
PARTNER_DISCOVERY_URI = (
    "https://filigran.io/a2a/extensions/partner-agent-discovery/v1"
)

#: The route, relative to the A2A router's own prefix. What the endpoint is
#: DECORATED with.
PARTNER_DISCOVERY_ROUTE = "/partner/agents"

#: The same path from the outside: what the card advertises and what a client
#: appends to the partner's origin. Spelled once here rather than derived at
#: import time from the router, because the card projection must not have to
#: import the API layer to describe itself.
PARTNER_DISCOVERY_PATH = f"/a2a{PARTNER_DISCOVERY_ROUTE}"

#: What the endpoint asks of a caller, stated in the declaration because the
#: card is served ANONYMOUSLY and cannot know who is reading it. A federated
#: partner (Foundry, Google) holds no platform-minted bearer, so discovery is
#: not available to it however loudly the card advertises the extension — and
#: a partner that reads this knows that before it tries, instead of learning
#: it from a 404 that looks identical to "not implemented".
_DESCRIPTION = (
    "Lists every agent on this deployment that the calling partner's own "
    "credential already reaches, each with its agent card URL. Closes the gap "
    "left by one card per agent and no standard way to enumerate them. GET "
    f"{PARTNER_DISCOVERY_PATH} on this origin, presenting the OAuth "
    "client-credentials bearer this card's security scheme describes; the "
    "answer is {\"agents\": [{\"name\", \"card_url\"}]}. Callers "
    "authenticating with a federated token (Microsoft Entra, Google) are not "
    "served: the endpoint identifies the partner by its minted registration."
)


def partner_discovery_extension() -> dict[str, Any]:
    """The ``AgentExtension`` entry for partner agent discovery.

    ``required`` is false and must stay so. The extension is an optimisation —
    it saves an operator copying card URLs by hand — and a client that ignores
    it loses nothing but that convenience. Marking it required would tell every
    partner it must implement a non-standard endpoint to talk to us at all,
    which is both untrue and the opposite of what declaring it is for.

    ``params`` carries the path so a client reads where to look rather than
    assuming it. A deployment serving this extension elsewhere stays usable by
    a client that never hard-codes the value.
    """
    return {
        "uri": PARTNER_DISCOVERY_URI,
        "description": _DESCRIPTION,
        "required": False,
        "params": {"path": PARTNER_DISCOVERY_PATH},
    }
