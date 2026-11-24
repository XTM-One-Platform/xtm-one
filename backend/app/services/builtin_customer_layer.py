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

"""The two layers of a built-in agent: product mechanics and the customer layer (#914).

A built-in agent (``Agent.is_builtin``) is seeded on every boot by
``app.agents.seed``. What the seed owns is the **mechanics**: persona core,
members, roles, handovers, gates, assignments, built-in tool groups. Each
release rewrites it, so an edit there would silently drift back, and the API
refuses it.

The **customer layer** is what makes a built-in fleet reach the customer's own
estate: integration bindings and their scoping, MCP servers, knowledge bases,
customer skills, custom tools, an "organization notes" addendum to the persona,
a model override and the approval switches. The seed never writes it, so it
survives every upgrade.

Two columns carry what the seed must know:

* ``Agent.customer_notes``: the organization notes, appended after the seeded
  persona at prompt time (:func:`persona_with_customer_notes`), never replacing
  it, bounded by :data:`CUSTOMER_NOTES_MAX_CHARS`.
* ``Agent.customer_fields``: the customer-layer fields a person set on this
  built-in, so the seed leaves the ones it would otherwise rewrite (the model)
  as the customer set them (:func:`seed_keeps_model`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: Upper bound of the organization notes appended to a built-in persona.
CUSTOMER_NOTES_MAX_CHARS = 4000

#: The heading the organization notes open with in the system prompt.
CUSTOMER_NOTES_HEADING = "## Organization notes (set by this organization's administrators)"

#: Integration fields (the carve-out that already existed, ``_BUILTIN_INTEGRATION_FIELDS``).
INTEGRATION_LAYER_FIELDS = frozenset(
    {
        "integration_bindings",
        "lent_integration_ids",
        "integration_template_bindings",
        "integration_allowed_tools",
        "integration_element_bindings",
        "integration_element_enforcement",
        "integration_read_only",
        "integration_template_read_only",
        "integration_signatures",
        "integration_live_writes",
    }
)

#: Customer-layer integration fields the seed writes a default for: once a
#: person set one, it is recorded in ``customer_fields`` and the seed keeps it.
SEEDED_DEFAULT_FIELDS = frozenset({"integration_template_bindings", "integration_template_read_only"})

#: The model a customer may pin on a built-in; the seed keeps it once set.
MODEL_LAYER_FIELDS = frozenset({"llm_provider", "llm_provider_id", "llm_model", "llm_model_source"})

#: Every field of the customer layer: writable on a built-in by whoever may
#: manage it (an administrator for a company-managed built-in).
CUSTOMER_LAYER_FIELDS = frozenset(
    INTEGRATION_LAYER_FIELDS
    | MODEL_LAYER_FIELDS
    | {
        "mcp_server_ids",
        "mcp_allowed_tools",
        "knowledge_base_ids",
        "knowledge_base_permissions",
        "skill_ids",
        "skill_load_modes",
        "custom_tool_ids",
        "customer_notes",
        "enabled",
        "approve_tool_calls",
        "hide_activity_from_non_admins",
    }
)

#: Fields that annotate an update rather than change the agent.
_ANNOTATION_FIELDS = frozenset({"commit_message"})

#: Why a mechanics field is refused, in the 403 detail.
LOCKED_REASON = (
    "locked by the product: the seeded mechanics of a built-in agent are rewritten by each "
    "release. Fork the fleet to change them."
)


def normalised(value: Any) -> Any:
    """Order-insensitive form of a stored or sent value, for a diff."""
    if isinstance(value, dict):
        return {str(k): normalised(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = [normalised(v) for v in value]
        return sorted(items, key=repr)
    return value


def _differs(agent: Any, field: str, value: Any) -> bool:
    stored = getattr(agent, field, None)
    if value is None:
        return bool(stored)
    return normalised(value) != normalised(stored)


def changed_fields(agent: Any, sent: dict[str, Any]) -> set[str]:
    """The fields of *sent* whose value differs from what *agent* stores."""
    return {f for f, v in sent.items() if f not in _ANNOTATION_FIELDS and _differs(agent, f, v)}


def locked_changes(agent: Any, sent: dict[str, Any]) -> list[str]:
    """Mechanics fields *sent* would change on a built-in, sorted.

    A client re-sends whole forms, so a mechanics field sent with its stored
    value is not a change and is not refused.
    """
    return sorted(f for f in changed_fields(agent, sent) if f not in CUSTOMER_LAYER_FIELDS)


def customer_layer_changes(agent: Any, sent: dict[str, Any]) -> set[str]:
    """Customer-layer fields *sent* would change."""
    return {f for f in changed_fields(agent, sent) if f in CUSTOMER_LAYER_FIELDS}


def locked_refusal(fields: Iterable[str]) -> str:
    """The 403 detail naming the locked fields and why."""
    names = sorted(fields)
    if not names:
        return "Built-in agents cannot be modified"
    return f"Built-in agents cannot be modified: {', '.join(names)} {'is' if len(names) == 1 else 'are'} {LOCKED_REASON}"


def record_customer_fields(agent: Any, changed: Iterable[str]) -> None:
    """Remember the customer-layer fields a person set on a built-in.

    Clearing the whole model (every model field empty) hands it back to the
    release, so the seed writes the seeded model again.
    """
    kept = set(getattr(agent, "customer_fields", None) or [])
    kept |= {f for f in changed if f in CUSTOMER_LAYER_FIELDS}
    if not any(getattr(agent, f, None) for f in MODEL_LAYER_FIELDS):
        kept -= MODEL_LAYER_FIELDS
    agent.customer_fields = sorted(kept) or None


def seed_keeps_model(agent: Any) -> bool:
    """Whether the seed must leave this built-in's model as the customer set it."""
    return bool(set(getattr(agent, "customer_fields", None) or []) & MODEL_LAYER_FIELDS)


def seed_keeps_field(agent: Any, field: str) -> bool:
    """Whether the seed must leave *field* as a person set it on this built-in.

    The seed writes a default for a few customer-layer fields (the template-bound
    providers and their read-only set); once a person set one, it is theirs.
    """
    return field in set(getattr(agent, "customer_fields", None) or [])


def persona_with_customer_notes(agent: Any, persona: str | None) -> str | None:
    """*persona* with the agent's organization notes appended after it."""
    notes = (getattr(agent, "customer_notes", None) or "").strip()
    if not notes:
        return persona
    notes = notes[:CUSTOMER_NOTES_MAX_CHARS]
    return f"{(persona or '').rstrip()}\n\n{CUSTOMER_NOTES_HEADING}\n{notes}"
