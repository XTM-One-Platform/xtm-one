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

"""The Scope(s) a run works on, and the part of it the tools enforce.

An agent's ``integration_element_bindings`` - refined by an assignment's - names
the elements (repositories, channels, ...) a run should work on, keyed
``"{provider}:{element_type}"``. By default a selection is prompt guidance: it
is written into the system prompt and the tools can still reach other
elements, as it always was.

A selection is a hard boundary on the tools only when its element type is
listed in the same entity's ``integration_element_enforcement`` - the "Enforce
as a boundary" switch of the Scope(s) card - and a provider enforces that type
(:func:`enforceable_scope_keys`: GitHub repositories, Slack channels, Microsoft
Teams channels, HubSpot CRM object types). The providers read the run's
ENFORCED scope from the ``element_scope`` build-context key of
``build_integration_tools`` (``integrations/providers/github/scope.py`` and
siblings), so a type nobody enforces never reaches them.

A scope is a map ``{key: [element, ...]}``:

* a key that is absent does not restrict that element type;
* a key with elements restricts it to those elements;
* a key with NO element restricts it to nothing. Only a narrowing produces it:
  an assignment refining to elements its agent no longer offers, a delegated
  run whose parent allowed none of that type. A STORED selection never means
  "nothing": an emptied selection is not saved (the Scope(s) cards drop the
  type), so an empty stored list is read as "not selected", as the prompt hint
  reads it (:func:`selected_elements`).

The stricter wins wherever two selections meet (:class:`RunScope`):

* an assignment's selection of a type refines its agent's; the refined
  selection is a boundary when the agent OR the assignment enforces the type
  (:func:`assignment_run_scope`);
* a handover - consult, transfer, background task - carries the ENFORCED scope
  of the run that makes it (``handover_context["element_scope"]``, the
  sub-agent task payload with its marker, :func:`queued_task_scope`), and the
  target narrows it with its own selection:
  a delegated agent never reaches past a boundary of the run that delegated to
  it, and is never narrowed by a selection that run only had as guidance
  (:func:`agent_run_scope`).
"""

from __future__ import annotations

import functools
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: A run's scope: element-binding key -> the elements it may work on.
ElementScope = dict[str, list[dict]]

#: ``handover_context`` / sub-agent payload key carrying the delegating run's
#: enforced scope.
INHERITED_SCOPE_KEY = "element_scope"

#: Sub-agent execution payload key saying its ``element_scope`` is an ENFORCED
#: scope. A task queued by an older release holds the delegating run's whole
#: selection - guidance included - under the same key.
ENFORCED_SCOPE_MARKER_KEY = "element_scope_enforced"


def selected_elements(bindings: Any) -> ElementScope:
    """A stored ``integration_element_bindings`` map as a scope: the element
    types with a selection. An empty (or malformed) entry is not a selection."""
    if not isinstance(bindings, Mapping):
        return {}
    return {
        key: [item for item in items if isinstance(item, Mapping)]
        for key, items in bindings.items()
        if isinstance(key, str) and isinstance(items, list) and items
    }


def _as_scope(scope: Any) -> ElementScope:
    """A scope handed over by another run; an empty list keeps meaning "nothing"."""
    if not isinstance(scope, Mapping):
        return {}
    return {
        key: [item for item in items if isinstance(item, Mapping)]
        for key, items in scope.items()
        if isinstance(key, str) and isinstance(items, list)
    }


def narrow_element_scope(outer: Any, inner: Any) -> ElementScope:
    """*outer* narrowed by *inner*, which can only restrict further.

    A type in both keeps the elements *inner* names that *outer* allows (by
    element id - an empty result allows nothing); a type in one of them keeps
    that one's elements. Both are read as scopes (:func:`_as_scope`): pass a
    stored selection through :func:`selected_elements` first.

    A kept element is *outer*'s entry, never *inner*'s: the provider reads the
    element's metadata (a GitHub ``full_name`` wins over the id), so an inner
    entry reusing an allowed id with other metadata would otherwise widen the
    scope it narrows.
    """
    merged = _as_scope(outer)
    for key, items in _as_scope(inner).items():
        if key not in merged:
            merged[key] = items
            continue
        allowed = {
            item["id"]: item
            for item in merged[key]
            if isinstance(item.get("id"), (str, int))
        }
        kept: list[dict] = []
        for item in items:
            element_id = item.get("id")
            if isinstance(element_id, (str, int)) and element_id in allowed:
                kept.append(allowed.pop(element_id))
        merged[key] = kept
    return merged


def assignment_element_scope(agent_bindings: Any, assignment_bindings: Any) -> ElementScope:
    """An assignment's selection refining its agent's, where a type is enforced."""
    return narrow_element_scope(selected_elements(agent_bindings), selected_elements(assignment_bindings))


# ---------------------------------------------------------------------------
# Enforcement: which selected types are a boundary
# ---------------------------------------------------------------------------


class ScopeEnforcementError(ValueError):
    """An ``integration_element_enforcement`` value a writer must not store."""


@functools.cache
def enforceable_scope_labels() -> dict[str, str]:
    """The element-binding keys a provider enforces, with how a person reads them."""
    from app.integrations.providers.github.scope import GITHUB_REPOSITORY_SCOPE_KEY
    from app.integrations.providers.hubspot_scope import HUBSPOT_OBJECT_TYPE_SCOPE_KEY
    from app.integrations.providers.microsoft.teams_scope import TEAMS_CHANNEL_SCOPE_KEY
    from app.integrations.providers.slack_scope import SLACK_CHANNEL_SCOPE_KEY

    return {
        GITHUB_REPOSITORY_SCOPE_KEY: "GitHub repositories",
        SLACK_CHANNEL_SCOPE_KEY: "Slack channels",
        TEAMS_CHANNEL_SCOPE_KEY: "Microsoft Teams channels",
        HUBSPOT_OBJECT_TYPE_SCOPE_KEY: "HubSpot CRM object types",
    }


def enforceable_scope_keys() -> frozenset[str]:
    """The element-binding keys a provider can hold to a selection."""
    return frozenset(enforceable_scope_labels())


def parse_scope_enforcement(value: Any) -> list[str] | None:
    """A requested ``integration_element_enforcement``, checked for storage.

    ``None`` stays ``None``; otherwise a list naming only the types a provider
    enforces, deduplicated and sorted. Anything else raises
    :class:`ScopeEnforcementError` naming what may be enforced, so a writer is
    told instead of the flag being silently ignored at run time.
    """
    if value is None:
        return None
    labels = enforceable_scope_labels()
    allowed = ", ".join(f"{label} ({key})" for key, label in labels.items())
    if not isinstance(value, (list, tuple)):
        raise ScopeEnforcementError(
            f"integration_element_enforcement must be a list of element types, one of: {allowed}."
        )
    for key in value:
        if not isinstance(key, str) or key not in labels:
            raise ScopeEnforcementError(
                f"{key!r} cannot be enforced as a boundary: only {allowed} can be."
            )
    return sorted(set(value))


def normalize_scope_enforcement(enforcement: Any, bindings: Any) -> list[str] | None:
    """The stored form of *enforcement* for the selection *bindings*.

    Keeps the enforceable types *bindings* selects, sorted; ``None`` when none
    is left. A flag only means something next to a selection, so an emptied or
    dropped selection takes its flag with it. Lenient on purpose: it also
    cleans values written by an import or an older client.
    """
    if not isinstance(enforcement, (list, tuple, set, frozenset)):
        return None
    selected = selected_elements(bindings)
    keys = enforceable_scope_keys()
    kept = sorted({key for key in enforcement if isinstance(key, str) and key in keys and key in selected})
    return kept or None


def enforced_scope_keys(bindings: Any, enforcement: Any) -> frozenset[str]:
    """The types whose selection in *bindings* is a boundary by *enforcement*."""
    return frozenset(normalize_scope_enforcement(enforcement, bindings) or ())


def guidance_only_scope_keys(
    bindings: Any, enforcement: Any, *, enforced_elsewhere: Iterable[str] = ()
) -> list[str]:
    """The enforceable types *bindings* selects that nothing enforces - neither
    *enforcement* nor *enforced_elsewhere* (an assignment's agent)."""
    enforced = enforced_scope_keys(bindings, enforcement) | frozenset(enforced_elsewhere)
    keys = enforceable_scope_keys()
    return sorted(key for key in selected_elements(bindings) if key in keys and key not in enforced)


@dataclass(frozen=True)
class RunScope:
    """A run's Scope(s): what its prompt names, and what its tools are held to.

    ``prompt`` is the selection the system prompt asks the agent to focus on.
    ``enforced`` is the part of it that is a boundary: what the integration
    tools receive as ``element_scope`` and what a handover carries.
    """

    prompt: ElementScope
    enforced: ElementScope


def agent_run_scope(agent_bindings: Any, agent_enforcement: Any = None, handover_context: Any = None) -> RunScope:
    """An agent run's scope, delegated or not.

    The run is held to the boundary it inherited (``handover_context
    ["element_scope"]``, only ever an enforced scope), narrowed by the agent's
    own selection of those types, plus the types the agent itself enforces.
    A selection the agent only has as guidance stays in the prompt.
    """
    own = selected_elements(agent_bindings)
    inherited_value = handover_context.get(INHERITED_SCOPE_KEY) if isinstance(handover_context, Mapping) else None
    keys = enforceable_scope_keys()
    inherited = {key: items for key, items in _as_scope(inherited_value).items() if key in keys}
    own_enforced = enforced_scope_keys(agent_bindings, agent_enforcement)
    enforced = narrow_element_scope(
        inherited, {key: items for key, items in own.items() if key in own_enforced or key in inherited}
    )
    return RunScope(prompt={**own, **enforced}, enforced=enforced)


def queued_task_scope(payload: Any) -> ElementScope | None:
    """The boundary a queued sub-agent task inherits from its execution payload.

    Read only from a payload carrying :data:`ENFORCED_SCOPE_MARKER_KEY`: a task
    queued before the upgrade, or by an older replica during a
    rolling deploy, holds the delegating run's whole selection, which must not
    become a boundary nobody chose. ``None``: the target keeps its own scope.
    """
    if not isinstance(payload, Mapping) or payload.get(ENFORCED_SCOPE_MARKER_KEY) is not True:
        return None
    scope = payload.get(INHERITED_SCOPE_KEY)
    return dict(scope) if isinstance(scope, Mapping) else None


def _refined_prompt_selection(agent_elements: Any, assignment_elements: Any) -> ElementScope:
    """An assignment's selection refining its agent's, as the prompt has always
    read it: the assignment's own entry of every element both select, a type
    one side selects as that side stores it, a side left empty returned as
    stored."""
    agent_elements = agent_elements if isinstance(agent_elements, Mapping) else {}
    assignment_elements = assignment_elements if isinstance(assignment_elements, Mapping) else {}
    if not agent_elements and not assignment_elements:
        return {}
    if not agent_elements:
        return assignment_elements
    if not assignment_elements:
        return agent_elements

    merged: ElementScope = {}
    for key in dict.fromkeys([*agent_elements, *assignment_elements]):
        agent_items = agent_elements.get(key)
        assign_items = assignment_elements.get(key)
        if agent_items and assign_items:
            # A malformed stored id (a list, an object) matches nothing instead
            # of raising and aborting the run.
            agent_ids = {
                item.get("id")
                for item in agent_items
                if isinstance(item, Mapping) and isinstance(item.get("id"), Hashable)
            }
            merged[key] = [
                item
                for item in assign_items
                if isinstance(item, Mapping) and isinstance(item.get("id"), Hashable) and item.get("id") in agent_ids
            ]
        elif agent_items:
            merged[key] = agent_items
        elif assign_items:
            merged[key] = assign_items
    return merged


def assignment_run_scope(
    agent_bindings: Any, agent_enforcement: Any, assignment_bindings: Any, assignment_enforcement: Any
) -> RunScope:
    """An assignment run's scope: its selection refines its agent's.

    A type is a boundary when the agent or the assignment enforces it - the
    stricter wins - and is then narrowed (the agent's entry
    of each kept element; refining to elements the agent does not select allows
    nothing). Every other type is the prompt guidance it has always been.
    """
    prompt = _refined_prompt_selection(agent_bindings, assignment_bindings)
    keys = enforced_scope_keys(agent_bindings, agent_enforcement) | enforced_scope_keys(
        assignment_bindings, assignment_enforcement
    )
    if not keys:
        return RunScope(prompt=prompt, enforced={})
    narrowed = assignment_element_scope(agent_bindings, assignment_bindings)
    enforced = {key: items for key, items in narrowed.items() if key in keys}
    return RunScope(prompt={**prompt, **enforced}, enforced=enforced)
