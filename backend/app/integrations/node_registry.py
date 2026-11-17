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

"""Provider-contributed flow node kinds (integration seam).

An integration can contribute node kinds to the visual flow builder — e.g. Nova
contributing a prompt-safety guardrail node. A contributed kind carries an
optional ``requires_provider``: it is offered in the builder's library only
while that integration is connected, so a node that could do nothing is never
placeable.

**Deliberately standalone.** This registry stores node kinds as plain
JSON-ready dicts and imports nothing from the flow builder, so it exists
independently of it. That matters for two reasons: the builder is not part of
every deployment, and an integration must be able to declare its node at import
time without dragging the builder's module graph into the import. When the
builder is present it reads this registry and merges the result into its own
catalog; when it is absent, registering is simply inert.

The dict shape is the builder's own serialized ``NodeKind`` contract (kind /
category / label / description / icon / inputs / outputs / config_fields / …).
It is not validated here — the builder is the authority on what a node is, and
duplicating that schema would create exactly the drift this seam avoids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegisteredNodeKind:
    """A contributed node kind and the integration it depends on."""

    kind: str
    definition: dict[str, Any]
    requires_provider: str | None = None


_REGISTERED: list[RegisteredNodeKind] = []


def register_node_kind(
    definition: dict[str, Any], *, requires_provider: str | None = None
) -> None:
    """Contribute a node kind. Idempotent by ``kind`` (re-registering replaces).

    *definition* is the builder's serialized node-kind dict; it must carry a
    ``kind`` key, which is the registry's identity.
    """
    kind = definition.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("node kind definition must carry a non-empty 'kind'")
    entry = RegisteredNodeKind(kind, definition, requires_provider)
    for i, existing in enumerate(_REGISTERED):
        if existing.kind == kind:
            _REGISTERED[i] = entry
            return
    _REGISTERED.append(entry)


def registered_node_kinds(
    connected_providers: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Contributed node kinds available to a caller, as definition dicts.

    A kind gated on a provider is included only when that provider is in
    *connected_providers*. Passing ``None`` returns only the ungated kinds —
    the safe default for a caller that has not resolved connectivity.
    """
    connected = connected_providers or set()
    return [
        r.definition
        for r in _REGISTERED
        if r.requires_provider is None or r.requires_provider in connected
    ]


def node_kind_required_provider(kind: str) -> str | None:
    """The provider *kind* is gated on, or ``None`` (unknown or ungated)."""
    for r in _REGISTERED:
        if r.kind == kind:
            return r.requires_provider
    return None


def get_registered_node_kind(kind: str) -> dict[str, Any] | None:
    """The definition for *kind* regardless of connectivity, or ``None``.

    Connectivity-independent on purpose: a saved flow referencing a contributed
    node must still resolve its ports when the integration is disconnected, so
    the graph stays legible instead of failing to load.
    """
    for r in _REGISTERED:
        if r.kind == kind:
            return r.definition
    return None
