# SPDX-License-Identifier: Apache-2.0

"""The filter keys of OpenAEV's analytics engine, and the check of a widget against them.

The ad-hoc analytics endpoints (``/api/dashboards/adhoc/*``) filter on the
fields the engine indexes (``base_tags_side``, ``asset_hostname``...), which
differ from the JPA attributes ``/api/schemas/{class}`` lists (``asset_tags``).
A JPA key sent to the engine is a 400 ``Unknown field`` after the call, so the
keys are read from ``GET /api/engine/schemas`` (the source OpenAEV's own widget
editor reads) and every key of a widget is checked before it is sent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: Always accepted: the entity discriminator every engine document carries.
_ALWAYS_VALID = frozenset({"base_entity"})

#: The model classes ``filter_keys`` is asked about, with the engine entity indexing them.
ENGINE_ENTITY_OF_CLASS = {
    "asset": "asset",
    "endpoint": "asset",
    "assetgroup": "asset-group",
    "attackpattern": "attack-pattern",
    "injectexpectation": "expectation-inject",
    "expectationinject": "expectation-inject",
    "finding": "finding",
    "inject": "inject",
    "scenario": "scenario",
    "exercise": "simulation",
    "simulation": "simulation",
    "tag": "tag",
    "team": "team",
    "securityplatform": "security-platform",
    "vulnerableendpoint": "vulnerable-endpoint",
}


def engine_entity_of(class_name: str) -> str:
    """The engine entity a model class (Asset, Exercise) or entity name (asset) maps to, else ``""``."""
    return ENGINE_ENTITY_OF_CLASS.get(class_name.replace("-", "").replace("_", "").lower(), "")


async def engine_filter_keys(
    api: Callable[..., Awaitable[Any]],
    base_url: str,
    headers: dict[str, str],
    entities: list[str],
) -> tuple[dict[str, dict[str, Any]], str]:
    """Answer ``({key: {type, entities, operators}}, error)`` for the engine entities.

    *api* is the calling module's ``_api``. An empty *entities* reads every
    engine model. The error is a sentence when the schema could not be read;
    the caller then sends the query unchecked.
    """
    params = {"classNames": sorted(set(entities))} if entities else None
    resp, err = await api(
        base_url,
        headers,
        "get",
        "/api/engine/schemas",
        "read the engine filter keys",
        params=params,
    )
    if err:
        return {}, err
    try:
        answer = resp.json() if resp is not None else None
    except ValueError:
        answer = None
    if not isinstance(answer, list):
        return {}, "the engine schema answer was not a list"
    keys: dict[str, dict[str, Any]] = {}
    for item in answer:
        if not isinstance(item, dict):
            continue
        name = item.get("schema_property_name")
        if not isinstance(name, str) or not name:
            continue
        entry = keys.setdefault(
            name,
            {
                "type": str(item.get("schema_property_type") or ""),
                "entities": set(),
                "operators": set(),
            },
        )
        if item.get("schema_property_entity"):
            entry["entities"].add(str(item["schema_property_entity"]))
        for op in item.get("schema_property_override_operators") or []:
            entry["operators"].add(str(op).lower())
    return keys, ""


def _filters_of(node: object, out: list[dict[str, Any]]) -> None:
    """Collect every filter object (a dict with a 'key') nested in *node*."""
    if isinstance(node, dict):
        if "key" in node and ("operator" in node or "values" in node):
            out.append(node)
        for value in node.values():
            _filters_of(value, out)
    elif isinstance(node, list):
        for item in node:
            _filters_of(item, out)


def _key_names(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [k for k in raw if isinstance(k, str)]
    return []


def widget_entities(config: object) -> list[str]:
    """The ``base_entity`` values a widget configuration filters on."""
    found: list[dict[str, Any]] = []
    _filters_of(config, found)
    entities: list[str] = []
    for f in found:
        if "base_entity" in _key_names(f.get("key")):
            values = f.get("values")
            for v in values if isinstance(values, list) else [values]:
                if isinstance(v, str) and v.strip() and v.strip() not in entities:
                    entities.append(v.strip())
    return entities


def widget_key_error(config: object, keys: dict[str, dict[str, Any]], entities: list[str]) -> str:
    """Name the first filter key or operator the engine would reject, else ``""``."""
    if not keys:
        return ""
    found: list[dict[str, Any]] = []
    _filters_of(config, found)
    scope = f" for {', '.join(entities)}" if entities else ""
    for f in found:
        for name in _key_names(f.get("key")):
            if name in _ALWAYS_VALID:
                continue
            entry = keys.get(name)
            if entry is None:
                valid = sorted(keys)
                listed = ", ".join(valid[:60]) + (" ..." if len(valid) > 60 else "")
                return (
                    f"filter key '{name}' is not an OpenAEV engine field{scope}, so OpenAEV "
                    f"would answer 'Unknown field'. Engine keys: {listed}. "
                    "List them with manage_openaev_settings action='filter_keys' "
                    "and the engine entity as class_name (asset, finding...)."
                )
            operator = f.get("operator")
            allowed = entry["operators"]
            if allowed and operator is not None and str(operator).lower() not in allowed:
                return (
                    f"operator '{operator}' is not accepted by the engine on '{name}'. "
                    f"Use one of: {', '.join(sorted(allowed))}."
                )
    return ""
