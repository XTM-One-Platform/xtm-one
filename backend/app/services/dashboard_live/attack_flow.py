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

"""An Attack Flow, as the ``graph`` shape draws it.

`Attack Flow <https://center-for-threat-informed-defense.github.io/attack-flow/>`_
(MITRE Center for Threat-Informed Defense) writes an attack as a STIX 2.1
bundle extended with five objects: the flow itself (``attack-flow``, whose
``start_refs`` name where it starts), the steps (``attack-action``: a
technique and its tactic), the branches (``attack-condition``, true or
false), the joins (``attack-operator``, AND / OR) and what is touched
(``attack-asset``). Its builder exports that bundle; an agent, a feed or a
knowledge base document holds it as JSON.

:func:`flow_graph` reads such a bundle into the nodes and edges of the
``graph`` shape, each node carrying its ``kind`` and what its card shows. It
only reads: unknown objects are left out, every value is cut again by
``graph_data``.
"""

from __future__ import annotations

from typing import Any

#: The flow's own objects, read; any other object of the bundle is ignored, except the
#: ATT&CK techniques and tactics an action names by reference.
_FLOW_TYPES = frozenset({"attack-flow", "attack-action", "attack-condition", "attack-operator", "attack-asset"})


def is_flow_bundle(data: object) -> bool:
    """Whether *data* is a STIX bundle holding an Attack Flow."""
    if not isinstance(data, dict) or data.get("type") != "bundle" or not isinstance(data.get("objects"), list):
        return False
    return any(isinstance(o, dict) and o.get("type") in _FLOW_TYPES for o in data["objects"])


def _mitre_id(obj: dict) -> str | None:
    for ref in obj.get("external_references") or []:
        if isinstance(ref, dict) and str(ref.get("source_name") or "").startswith("mitre") and ref.get("external_id"):
            return str(ref["external_id"])
    return None


def _refs(obj: dict, key: str) -> list[str]:
    value = obj.get(key)
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


def flow_graph(bundle: dict) -> dict:
    """The nodes (``id, label, kind`` and a card's details) and edges of the Attack Flow in *bundle*.

    An action is labelled by its name, with its technique and tactic (ids, or read from the
    ``attack-pattern`` / ``x-mitre-tactic`` it references); a condition by its description; an
    operator by AND / OR; an asset by its name; the flow is the start, pointing at its first steps.
    """
    objects: dict[str, dict] = {
        str(o["id"]): o for o in bundle.get("objects") or [] if isinstance(o, dict) and isinstance(o.get("id"), str)
    }
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def edge(source: str, targets: list[str], label: str | None = None) -> None:
        edges.extend({"source": source, "target": target, "label": label} for target in targets if target in objects)

    for oid, obj in objects.items():
        kind = obj.get("type")
        if kind == "attack-flow":
            nodes.append({"id": oid, "label": obj.get("name") or "Attack flow", "kind": "start", "description": obj.get("description")})
            edge(oid, _refs(obj, "start_refs"))
        elif kind == "attack-action":
            technique = obj.get("technique_id")
            pattern = objects.get(str(obj.get("technique_ref")))
            if not technique and pattern:
                technique = _mitre_id(pattern)
            tactic = objects.get(str(obj.get("tactic_ref")))
            nodes.append({
                "id": oid,
                "label": obj.get("name") or (pattern or {}).get("name") or technique or "Action",
                "kind": "action",
                "technique": technique,
                "tactic": (tactic or {}).get("name") or obj.get("tactic_id"),
                "description": obj.get("description"),
            })  # fmt: skip
            edge(oid, _refs(obj, "effect_refs"))
            edge(oid, _refs(obj, "asset_refs"))
        elif kind == "attack-condition":
            nodes.append({"id": oid, "label": obj.get("description") or "Condition", "kind": "condition"})
            edge(oid, _refs(obj, "on_true_refs"), "true")
            edge(oid, _refs(obj, "on_false_refs"), "false")
        elif kind == "attack-operator":
            operator = str(obj.get("operator") or "").upper()
            nodes.append({"id": oid, "label": operator or "OR", "kind": "operator", "operator": operator})
            edge(oid, _refs(obj, "effect_refs"))
        elif kind == "attack-asset":
            nodes.append({"id": oid, "label": obj.get("name") or "Asset", "kind": "asset", "description": obj.get("description")})
    drawn = {n["id"] for n in nodes}
    return {"nodes": nodes, "edges": [e for e in edges if e["source"] in drawn and e["target"] in drawn]}


__all__ = ["flow_graph", "is_flow_bundle"]
