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

"""The A2A liaisons the agent graphs draw: partner nodes and directed edges.

A liaison is derived, never stored: an agent binding an A2A integration is
the whole relationship (``app.a2a.providers``), its direction lives on the
integration's settings row, and an outbound registration's partner agents are
its client targets. This module turns those three reads into what a graph
draws:

- an INBOUND integration is one partner node, its edge running partner ->
  agent ("tasks"): the partner submits work to the bound agent;
- an OUTBOUND integration is one partner node per partner agent card, each
  edge running agent -> partner agent ("delegates"); a registration with no
  card yet is one node for the integration itself, so the binding is never
  invisible;
- one integration bound to several agents fans out to each of them.

An integration with no settings row belongs to neither direction (the
fail-closed rule of ``directions_for``) and draws nothing. Nothing here
carries a credential: names, the direction, the provider and the partner
card URLs, which an operator typed and the integration page shows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.a2a.providers import bound_integration_ids
from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND

#: Prefix of every partner node id, so a graph can tell one from an agent id.
PARTNER_NODE_PREFIX = "a2a:"


@dataclass(frozen=True)
class LiaisonAgent:
    """The agent side of a liaison: its id and its A2A bindings."""

    id: uuid.UUID
    integration_bindings: Mapping[str, object] | None


@dataclass(frozen=True)
class LiaisonIntegration:
    """A visible A2A integration."""

    id: uuid.UUID
    name: str
    provider: str


@dataclass(frozen=True)
class LiaisonTarget:
    """One partner agent of an outbound registration."""

    id: uuid.UUID
    integration_id: uuid.UUID
    card_url: str
    declared_name: str | None = None


@dataclass
class PartnerNode:
    id: str
    name: str
    integration_id: str
    integration_name: str
    provider: str
    direction: str
    card_urls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LiaisonEdge:
    source_id: str
    target_id: str
    direction: str
    integration_id: str


@dataclass
class Liaisons:
    partners: list[PartnerNode]
    edges: list[LiaisonEdge]


def partner_node_id(integration_id: uuid.UUID, target_id: uuid.UUID | None = None) -> str:
    """The graph id of a partner node: the integration, or one of its cards."""
    if target_id is None:
        return f"{PARTNER_NODE_PREFIX}{integration_id}"
    return f"{PARTNER_NODE_PREFIX}{integration_id}:{target_id}"


#: Path segments that name the card document, not the partner agent.
_CARD_PATH_NOISE = frozenset({".well-known", "agent-card.json", "agent.json", "card", "card.json"})


def _card_url_label(card_url: str) -> str:
    """A short label for a card with no declared name, its distinct part first.

    Two cards of one partner differ by their path, rarely by their host, and a
    graph cuts a long name at its end: the last path segment that names the
    agent leads (``triage · partner.example``), the host follows, and a card
    at the root of its host is the host alone. The full URL stays on the node's
    tooltip and on the integration page.
    """
    parts = urlsplit(card_url.strip())
    host = parts.hostname or ""
    segments = [s for s in parts.path.split("/") if s and s.lower() not in _CARD_PATH_NOISE]
    if not host:
        return card_url
    return f"{segments[-1]} · {host}" if segments else host


def _card_name(target: LiaisonTarget) -> str:
    """The partner agent's name from its card, else a label from its URL."""
    name = (target.declared_name or "").strip()
    return name or _card_url_label(target.card_url)


def build_liaisons(
    agents: Iterable[LiaisonAgent],
    integrations: Iterable[LiaisonIntegration],
    directions: Mapping[uuid.UUID, str],
    targets: Sequence[LiaisonTarget],
) -> Liaisons:
    """The partner nodes and edges of the given agents' visible A2A bindings.

    ``integrations`` is the set the caller may see: a binding to any other
    integration is not drawn. ``directions`` maps an integration id to its
    direction; an id absent from it draws nothing. ``targets`` are the client
    targets of the outbound integrations, in the order they are drawn.
    Deterministic: nodes and edges come out sorted, so the graph's layout
    fingerprint never changes for an unchanged platform.
    """
    by_id = {i.id: i for i in integrations}
    cards: dict[uuid.UUID, list[LiaisonTarget]] = {}
    for target in targets:
        cards.setdefault(target.integration_id, []).append(target)

    partners: dict[str, PartnerNode] = {}
    edges: set[LiaisonEdge] = set()

    def partner(node_id: str, name: str, integration: LiaisonIntegration, direction: str, card_urls: list[str]) -> str:
        if node_id not in partners:
            partners[node_id] = PartnerNode(
                id=node_id,
                name=name,
                integration_id=str(integration.id),
                integration_name=integration.name,
                provider=integration.provider,
                direction=direction,
                card_urls=card_urls,
            )
        return node_id

    for agent in agents:
        agent_id = str(agent.id)
        for integration_id in bound_integration_ids(agent):
            integration = by_id.get(integration_id)
            direction = directions.get(integration_id)
            if integration is None or direction is None:
                continue
            if direction == DIRECTION_INBOUND:
                node_id = partner(
                    partner_node_id(integration.id),
                    integration.name,
                    integration,
                    direction,
                    [],
                )
                edges.add(LiaisonEdge(node_id, agent_id, direction, str(integration.id)))
            elif direction == DIRECTION_OUTBOUND:
                integration_cards = cards.get(integration.id) or []
                if not integration_cards:
                    node_id = partner(
                        partner_node_id(integration.id),
                        integration.name,
                        integration,
                        direction,
                        [],
                    )
                    edges.add(LiaisonEdge(agent_id, node_id, direction, str(integration.id)))
                    continue
                for card in integration_cards:
                    node_id = partner(
                        partner_node_id(integration.id, card.id),
                        _card_name(card),
                        integration,
                        direction,
                        [card.card_url],
                    )
                    edges.add(LiaisonEdge(agent_id, node_id, direction, str(integration.id)))

    return Liaisons(
        partners=sorted(partners.values(), key=lambda p: p.id),
        edges=sorted(edges, key=lambda e: (e.source_id, e.target_id)),
    )
