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

"""Validation of the tool names an ``integration_allowed_tools`` map lists.

``Agent.integration_allowed_tools`` and ``Assignment.integration_allowed_tools``
map an integration provider (or ``<provider>@<integration id>``) to the tool
names it keeps. A name the provider does not expose used to be stored as is
and only showed up at run time as "Tool '<name>' is not available in this
context". Every writer of the field (the agents and assignments APIs, the
agent-builder tools) now calls :func:`validate_integration_allowed_tools`,
which refuses the whole write naming each unknown name and the closest valid
ones, the way ``builtin_allowed_tools`` already does.

The valid names of a provider are the ones the tool-scoping UI lists
(``GET /integrations/{id}/tools``): the provider builder's definitions, built
with a dummy credential, their aliases, and a multi-instance ``__<slug>``
suffix on any of them. A builder's opt-in tools (``live_writes``) are part of
the catalog: a scope may name a tool before the setting that offers it is on.
OpenAPI keeps its per-integration meaning: ``openapi@<id>`` is checked against
that integration's active tools, and the provider-wide ``openapi`` list, read
per integration by ``per_integration_tool_scopes``, is checked against the
union of the bound OpenAPI integrations' tools when the writer passes them: a
name no bound integration exposes would otherwise be dropped by that split and
leave the integration with every tool. A provider
whose catalog cannot be read (no builder, a builder that fails) is not checked:
validation never refuses what it cannot prove wrong.

Nothing here runs at run time and nothing refuses a value already stored:
:func:`unknown_integration_tool_names` lets ``get_agent`` flag a stale list so
a sweep can clean it.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_INSTANCE_SCOPE_SEP = "@"
_INSTANCE_SUFFIX_SEP = "__"
_CUSTOM_PREFIX = "custom:"
_CLOSEST_MAX = 3

#: Provider -> its catalog of tool names; a failed build is never cached.
_CATALOG_CACHE: dict[str, frozenset[str]] = {}


class IntegrationAllowedToolsError(ValueError):
    """An ``integration_allowed_tools`` value names a tool its provider does not expose."""


def _definition_names(definitions: list) -> set[str]:
    names: set[str] = set()
    for definition in definitions or []:
        name = getattr(definition, "name", None)
        if isinstance(name, str) and name:
            names.add(name)
        for alias in getattr(definition, "aliases", None) or ():
            if isinstance(alias, str) and alias:
                names.add(alias)
    return names


def provider_tool_names(provider: str) -> frozenset[str] | None:
    """The tool names *provider* can expose, or ``None`` when they cannot be read."""
    cached = _CATALOG_CACHE.get(provider)
    if cached is not None:
        return cached
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS
    from app.tools.base import CallableToolExecutor

    builder = PROVIDER_TOOLS.get(provider)
    if builder is None:
        return None
    names: set[str] = set()
    for opt_in in (True, False):
        kwargs: dict[str, Any] = {"refresh_fn": lambda: None}
        if opt_in:
            kwargs["live_writes"] = True
        try:
            names |= _definition_names(builder(build_dummy_token(provider), CallableToolExecutor(), **kwargs))
        except TypeError:
            # A builder with a fixed signature takes no opt-in keyword.
            continue
        except Exception:
            logger.debug("Could not read the %s tool catalog", provider, exc_info=True)
            return None
    if not names:
        return None
    resolved = frozenset(names)
    _CATALOG_CACHE[provider] = resolved
    return resolved


async def _openapi_integration_tool_names(db: AsyncSession, integration_id: str) -> frozenset[str] | None:
    from app.core.utils import is_valid_uuid
    from app.services.openapi_import.service import get_spec

    if not is_valid_uuid(integration_id):
        return None
    row = await get_spec(db, integration_id, runtime_only=True)
    if row is None:
        return None
    names = {
        tool.get("name")
        for tool in row.active_tools or []
        if isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool.get("name")
    }
    return frozenset(names) or None


def _metadata_tool_names(metadata_json: Any) -> set[str]:
    """The tool names a custom integration definition declares, as its tool list keeps them
    (a ``tools`` or ``available_tools`` list; an entry needs a name and a description)."""
    if not isinstance(metadata_json, dict):
        return set()
    rows = metadata_json.get("tools")
    if not isinstance(rows, list):
        rows = metadata_json.get("available_tools")
    if not isinstance(rows, list):
        return set()
    return {
        row["name"].strip()
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("name"), str)
        and row["name"].strip()
        and isinstance(row.get("description"), str)
        and row["description"].strip()
    }


async def _custom_provider_tool_names(db: AsyncSession, provider: str) -> frozenset[str] | None:
    from sqlalchemy import select

    from app.models.custom_integration_definition import CustomIntegrationDefinition

    row = (
        await db.execute(
            select(CustomIntegrationDefinition).where(CustomIntegrationDefinition.provider_key == provider)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return frozenset(_metadata_tool_names(row.metadata_json)) or None


async def _openapi_bound_tool_names(db: AsyncSession, integration_ids: list[str]) -> frozenset[str] | None:
    """The tools of every bound OpenAPI integration whose catalog can be read."""
    names: set[str] = set()
    for integration_id in integration_ids:
        found = await _openapi_integration_tool_names(db, str(integration_id))
        if found:
            names |= found
    return frozenset(names) or None


async def _valid_names_for_key(
    db: AsyncSession, key: str, openapi_integration_ids: list[str] | None = None
) -> frozenset[str] | None:
    from app.services.openapi_import.service import PER_USER_UNRESOLVABLE_PROVIDERS

    provider, sep, integration_id = key.partition(_INSTANCE_SCOPE_SEP)
    if provider in PER_USER_UNRESOLVABLE_PROVIDERS:
        # Each integration of these providers is its own API: an instance key
        # is checked against that integration, the provider-wide list against
        # the bound integrations (``per_integration_tool_scopes`` splits it).
        if sep:
            return await _openapi_integration_tool_names(db, integration_id)
        return await _openapi_bound_tool_names(db, openapi_integration_ids) if openapi_integration_ids else None
    if provider.startswith(_CUSTOM_PREFIX):
        return await _custom_provider_tool_names(db, provider)
    return provider_tool_names(provider)


def _is_known(name: str, valid: frozenset[str]) -> bool:
    if name in valid:
        return True
    # A multi-instance ``<tool>__<slug>`` name: a valid tool and a non-empty slug.
    return any(
        name.startswith(f"{tool}{_INSTANCE_SUFFIX_SEP}") and len(name) > len(tool) + len(_INSTANCE_SUFFIX_SEP)
        for tool in valid
    )


async def unknown_integration_tool_names(
    db: AsyncSession, allowed: Any, openapi_integration_ids: list[str] | None = None
) -> dict[str, list[str]]:
    """``{key: unknown names}`` for each list of *allowed* naming a tool its provider lacks.

    *openapi_integration_ids* (the bound OpenAPI integrations) checks the
    provider-wide ``openapi`` list; without them it is not checked.
    """
    if not isinstance(allowed, dict):
        return {}
    unknown: dict[str, list[str]] = {}
    for key, names in allowed.items():
        if not isinstance(names, list) or not names:
            continue
        valid = await _valid_names_for_key(db, str(key), openapi_integration_ids)
        if not valid:
            continue
        missing = sorted({str(n) for n in names if not _is_known(str(n), valid)})
        if missing:
            unknown[str(key)] = missing
    return unknown


def _closest(name: str, valid: frozenset[str]) -> list[str]:
    base = name.partition(_INSTANCE_SUFFIX_SEP)[0] if name not in valid else name
    return difflib.get_close_matches(base, sorted(valid), n=_CLOSEST_MAX, cutoff=0.5)


async def integration_allowed_or_422(
    db: AsyncSession, allowed: Any, openapi_integration_ids: list[str] | None = None
) -> None:
    """:func:`validate_integration_allowed_tools` for an API route: a 422 naming the unknown names."""
    from fastapi import HTTPException

    try:
        await validate_integration_allowed_tools(db, allowed, openapi_integration_ids)
    except IntegrationAllowedToolsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def validate_integration_allowed_tools(
    db: AsyncSession, allowed: Any, openapi_integration_ids: list[str] | None = None
) -> None:
    """Raise :class:`IntegrationAllowedToolsError` when *allowed* names an unknown tool.

    The message names every unknown name with the closest valid names of its
    provider, so the caller can fix the list in one go. *openapi_integration_ids*
    are the OpenAPI integrations the agent or assignment binds once the write
    lands: the provider-wide ``openapi`` list is checked against their tools.
    """
    unknown = await unknown_integration_tool_names(db, allowed, openapi_integration_ids)
    if not unknown:
        return
    parts: list[str] = []
    for key, names in unknown.items():
        valid = await _valid_names_for_key(db, key, openapi_integration_ids) or frozenset()
        described = []
        for name in names:
            closest = _closest(name, valid)
            described.append(f"{name} (closest: {', '.join(closest)})" if closest else name)
        parts.append(f"integration_allowed_tools['{key}']: unknown tool(s) {'; '.join(described)}")
    raise IntegrationAllowedToolsError(
        ". ".join(parts) + ". Nothing was saved. Use the names the integration's tool list shows "
        "(list_integration_tools); a multi-instance suffix such as `__work` is allowed."
    )
