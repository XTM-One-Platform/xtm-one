# SPDX-License-Identifier: Apache-2.0

"""Read-only integration bindings: the one module for the rule.

An agent can mark a bound integration read-only: ``Agent.integration_read_only``
holds ``{provider: [integration_id, ...]}``, the shape of
``integration_bindings``. For such a binding every WRITE tool - as the
platform's one read/write classifier reads its name
(``services/tool_classification.py``: create / update / delete / send / post /
transition / upload / ..., and any name it cannot read) - is withheld from the
model and, should a call reach it by name anyway, refused by the executor. It
is enforced where the tools are built (``integration_tools``), never by a
sentence in the prompt.

A provider bound by template (``integration_template_bindings``, resolved per
user at run time, so no id exists to name ahead) is marked read-only by its
provider key in ``Agent.integration_template_read_only``: every binding the
run resolves for that provider which is not one of the agent's own id
bindings is read-only too. The provider must be template-bound.

Everything that writes the setting (the agents REST API, the agent-builder
``create_agent`` / ``update_agent`` / ``clone_agent`` tools) parses it here,
and every run that builds an agent's integration tools reads the ids from
:func:`read_only_integration_ids`, given the bindings it resolved.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from typing import Any, Literal

from app.core.utils import safe_uuid
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolDefinition

ToolAccess = Literal["read", "write"]

#: Context key ``build_integration_tools`` reads the read-only ids from.
READ_ONLY_CONTEXT_KEY = "read_only_integration_ids"


def integration_tool_access(tool_name: str) -> ToolAccess:
    """``read`` or ``write`` for an integration tool, from its base name.

    The shared classifier, so the access ``list_integration_tools`` reports is
    exactly the one the runtime enforces. An unknown verb is a ``write``.
    """
    return "read" if infer_tool_type(tool_name) == "read" else "write"


def _binding_ids(bindings: Mapping[str, Any] | None) -> set[str]:
    return {
        str(parsed)
        for ids in (bindings or {}).values()
        for iid in (ids if isinstance(ids, list) else [ids])
        if (parsed := safe_uuid(str(iid))) is not None
    }


def template_read_only_providers(agent: Any) -> frozenset[str]:
    """The template-bound providers of *agent* marked read-only.

    Only providers also in ``integration_template_bindings`` count, so a stale
    entry never narrows a provider the agent binds another way.
    """
    raw = getattr(agent, "integration_template_read_only", None)
    if not isinstance(raw, list) or not raw:
        return frozenset()
    templated = {str(p) for p in (getattr(agent, "integration_template_bindings", None) or []) if p}
    return frozenset(str(p) for p in raw if str(p) in templated)


def read_only_integration_ids(
    agent: Any, resolved_bindings: Mapping[str, Any] | None = None
) -> frozenset[str]:
    """The canonical ids of the bindings of *agent* that are read-only.

    Only ids that are also bound (``integration_bindings``) count, so a stale
    entry never narrows an integration the agent reaches another way.

    *resolved_bindings* are the bindings the run builds its tools from
    (``{provider or provider#N: id or [ids]}``): each of them whose provider
    is a read-only template provider (:func:`template_read_only_providers`)
    and which is not one of the agent's own id bindings is read-only too.
    """
    direct = _binding_ids(getattr(agent, "integration_bindings", None))
    raw = getattr(agent, "integration_read_only", None)
    read_only: set[str] = set()
    if isinstance(raw, Mapping) and raw:
        read_only = {
            str(parsed)
            for ids in raw.values()
            for iid in (ids if isinstance(ids, list) else [ids])
            if (parsed := safe_uuid(str(iid))) is not None and str(parsed) in direct
        }
    providers = template_read_only_providers(agent)
    if providers and resolved_bindings:
        for key, ids in resolved_bindings.items():
            if str(key).split("#", 1)[0] not in providers:
                continue
            read_only |= _binding_ids({key: ids}) - direct
    return frozenset(read_only)


def parse_integration_template_read_only(
    raw: object,
) -> tuple[list[str] | None, str | None, bool]:
    """``(value, error, provided)`` for an ``integration_template_read_only`` argument.

    Accepts a comma-separated string of provider keys, a JSON array or a list;
    ``none`` / ``clear`` (or an empty list) clears it. ``provided`` is
    ``False`` for a blank value, which leaves the setting unchanged.
    """
    if raw is None:
        return None, None, False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, None, False
        if text.lower() in ("none", "clear"):
            return None, None, True
        if text[:1] == "[":
            try:
                raw = json.loads(text)
            except ValueError:
                return None, _TEMPLATE_FORMAT_ERROR, True
        else:
            raw = text.split(",")
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        return None, _TEMPLATE_FORMAT_ERROR, True
    value: list[str] = []
    for provider in raw:
        key = provider.strip().lower()
        if key and key not in value:
            value.append(key)
    return value or None, None, True


_TEMPLATE_FORMAT_ERROR = (
    "integration_template_read_only must be provider keys separated by commas "
    "(or a JSON array of them), or 'none'/'clear' to clear it."
)


def untemplated_read_only_providers(
    providers: Iterable[str] | None, template_bindings: Iterable[str] | None
) -> list[str]:
    """The providers of *providers* that are not in *template_bindings*."""
    templated = {str(p) for p in (template_bindings or []) if p}
    return [str(p) for p in (providers or []) if str(p) not in templated]


def untemplated_read_only_detail(untemplated: Iterable[str]) -> str:
    """The refusal of a template read-only provider the agent does not template-bind."""
    return (
        "integration_template_read_only names providers the agent does not bind by "
        f"template (integration_template_bindings): {', '.join(untemplated)}. "
        "Add them to integration_template_bindings first."
    )


def parse_integration_read_only(
    raw: object,
) -> tuple[dict[str, list[str]] | None, str | None, bool]:
    """``(value, error, provided)`` for an ``integration_read_only`` argument.

    Accepts ``"provider:id,provider:id"``, a JSON array of those strings, a
    list of them, or a ``{provider: [ids]}`` object; ``none`` / ``clear`` (or
    an empty list / object) clears it. ``provided`` is ``False`` for a blank
    value, which leaves the setting unchanged.
    """
    if raw is None:
        return None, None, False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, None, False
        if text.lower() in ("none", "clear"):
            return None, None, True
        if text[:1] in ("[", "{"):
            try:
                raw = json.loads(text)
            except ValueError:
                return None, _FORMAT_ERROR, True
        else:
            raw = [part for part in text.split(",") if part.strip()]
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, Mapping):
        for provider, ids in raw.items():
            for iid in ids if isinstance(ids, list) else [ids]:
                pairs.append((str(provider), str(iid)))
    elif isinstance(raw, list):
        for entry in raw:
            provider, sep, iid = str(entry).strip().partition(":")
            if not sep:
                return None, _FORMAT_ERROR, True
            pairs.append((provider, iid))
    else:
        return None, _FORMAT_ERROR, True

    value: dict[str, list[str]] = {}
    malformed: list[str] = []
    for provider, iid in pairs:
        key, parsed = provider.strip().lower(), safe_uuid(iid.strip())
        if not key or parsed is None:
            malformed.append(f"{provider}:{iid}")
            continue
        ids = value.setdefault(key, [])
        if str(parsed) not in ids:
            ids.append(str(parsed))
    if malformed:
        return None, (
            f"Invalid integration_read_only entry(ies): {', '.join(malformed)}. "
            "Use 'provider:integration_id' pairs (the id must be a valid UUID)."
        ), True
    return value or None, None, True


_FORMAT_ERROR = (
    "integration_read_only must be 'provider:integration_id' pairs separated by "
    "commas (or a JSON array of them), or 'none'/'clear' to clear it."
)


def unbound_read_only_entries(
    read_only: Mapping[str, Iterable[str]] | None,
    bindings: Mapping[str, Iterable[str]] | None,
) -> list[str]:
    """The ``provider:id`` entries of *read_only* that name no binding of *bindings*."""
    bound = {
        (provider, str(safe_uuid(str(iid)) or iid))
        for provider, ids in (bindings or {}).items()
        for iid in (ids if isinstance(ids, list) else [ids])
    }
    return [
        f"{provider}:{iid}"
        for provider, ids in (read_only or {}).items()
        for iid in ids
        if (provider, iid) not in bound
    ]


def carried_read_only(
    read_only: Mapping[str, Iterable[str]] | None,
    bindings: Mapping[str, Iterable[str]] | None,
    *,
    replaced_providers: Collection[str] = (),
) -> dict[str, list[str]] | None:
    """The read-only map of a copy whose bindings were re-chosen (a catalog clone).

    A binding the copy kept stays read-only when it was. A provider whose
    bindings were replaced by another choice (*replaced_providers*) passes its
    mark to every binding chosen for it: fail-closed, the replacement never
    gains write tools the source withheld for that provider.
    """
    carried: dict[str, list[str]] = {}
    for provider, ids in (read_only or {}).items():
        bound = [str(iid) for iid in (bindings or {}).get(provider) or []]
        if provider in replaced_providers:
            kept = bound
        else:
            marked = {str(iid) for iid in ids}
            kept = [iid for iid in bound if iid in marked]
        if kept:
            carried[provider] = kept
    return carried or None


def read_only_refusal(tool_name: str, integration_label: str) -> str:
    """What a write tool of a read-only binding answers."""
    return (
        f"Refused: {tool_name} writes to {integration_label}, which this agent "
        "may only read (the binding is read-only). Use a read tool, or ask the "
        "agent's owner to make the binding writable (integration_read_only)."
    )


def withhold_write_tools(
    defs: list[ToolDefinition],
    executor: CallableToolExecutor,
    *,
    integration_label: str,
) -> list[ToolDefinition]:
    """Drop the write tools of one read-only binding and refuse their calls.

    *defs* are one binding's final definitions (multi-instance names already
    suffixed): each write tool is removed from what the model sees and its
    handler replaced by a refusal, so a call that reaches it by name - a
    replayed transcript, a hand-written call - is refused rather than run.

    A definition declared :attr:`ToolDefinition.read_only` in code (a compare
    of two refs, whose verb the name classifier cannot read) is a read and is
    kept: the declaration is the provider's own statement that no call of it
    changes anything, the one the in-flight repeat guard already trusts.
    One declared :attr:`ToolDefinition.writes` (an imported ``POST`` whose
    generated name reads as a read) is a write and is withheld.
    """
    return withhold_tools(
        defs,
        executor,
        {
            definition.name: read_only_refusal(definition.name, integration_label)
            for definition in defs
            if definition.writes
            or (not definition.read_only and integration_tool_access(definition.base_name or definition.name) != "read")
        },
    )


def withhold_tools(
    defs: list[ToolDefinition],
    executor: CallableToolExecutor,
    refusals: Mapping[str, str],
) -> list[ToolDefinition]:
    """Drop the definitions named in *refusals* and answer a call to each by name with its message.

    The refusal is recorded on *executor* by name (``CallableToolExecutor.refuse``),
    so the composite and the scoped executor that never offered the tool answer
    a call to it with this message rather than "not available".
    """
    kept: list[ToolDefinition] = []
    for definition in defs:
        message = refusals.get(definition.name)
        if message is None:
            kept.append(definition)
            continue
        executor.refuse(definition.name, message)
    return kept


def is_read_only_binding(integration_id: object, read_only_ids: Collection[str] | None) -> bool:
    """Whether the binding to *integration_id* is in *read_only_ids*."""
    if not read_only_ids:
        return False
    parsed = safe_uuid(str(integration_id))
    return parsed is not None and str(parsed) in read_only_ids
