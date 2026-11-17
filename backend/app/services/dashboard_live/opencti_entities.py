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

"""How OpenCTI draws an entity: its colour, its icon, its name, a marking's colour.

The OpenCTI adapter (:mod:`.opencti`) sends these with its data, so that the
dashboard's engine draws an OpenCTI widget as OpenCTI does without knowing
OpenCTI: a colour is a strict hex, an icon a Lucide name, a type's name text.

- :func:`entity_color` is OpenCTI's ``itemColor`` (``utils/Colors.ts``),
  kept once in :mod:`.opencti_colors`.
- :func:`entity_icon` is OpenCTI's ``ItemIcon`` (``components/ItemIcon.tsx``),
  its Material and MDI icons mapped to Lucide through the design system's
  ``icon-mapping.json``; where the mapping has none, the nearest Lucide icon.
- :func:`entity_label` is OpenCTI's English ``entity_<type>`` translation:
  the type with its dashes as spaces, but for the types it names otherwise.
- :func:`marking_color` is ``ItemMarkings``: the marking's own colour, else by
  its definition (TLP, PAP, CD/DR/NP/SF) on OpenCTI's severity scale, as the
  engine's named colours.
"""

from __future__ import annotations

import re

from .opencti_colors import item_color


def entity_color(entity_type: str | None) -> str:
    """OpenCTI's ``itemColor(type)`` (:func:`.opencti_colors.item_color`): the family's colour, else the type name's hash."""
    return item_color(entity_type)


#: The cyber observables: one icon for all.
_OBSERVABLES: frozenset[str] = frozenset(
    {
        "Stix-Cyber-Observable", "Autonomous-System", "Directory", "Domain-Name", "Email-Addr", "Email-Message",
        "Email-Mime-Part-Type", "StixFile", "X509-Certificate", "AI-Prompt", "IPv4-Addr", "IPv6-Addr", "Mac-Addr",
        "Mutex", "Network-Traffic", "Process", "Software", "Url", "User-Account", "Windows-Registry-Key",
        "Windows-Registry-Value-Type", "Cryptographic-Key", "Cryptocurrency-Wallet", "Text", "User-Agent",
        "Bank-Account", "Credential", "Tracking-Number", "Phone-Number", "Payment-Card", "Media-Content", "SSH-Key",
        "Persona", "IMEI", "IMSI", "ICCID", "Hostname",
    }
)  # fmt: skip


#: The relationship types (``ItemIcon`` draws the lower-case ones as a relationship).
_RELATIONSHIP_TYPES: tuple[str, ...] = (
        "Stix-Core-Relationship", "Relationship", "stix-core-relationship", "targets", "uses", "located-at",
        "related-to", "technology-from", "technology-to", "technology", "transferred-to", "demonstrates",
        "mitigates", "impersonates", "indicates", "comes-after", "attributed-to", "variant-of", "part-of",
        "employed-by", "resides-in", "citizen-of", "national-of", "drops", "delivers", "compromises",
        "belongs-to", "based-on", "communicates-with", "amplifies", "analyses-of", "authored-by", "beacons-to",
        "characterizes", "consists-of", "interpreted-by", "controls", "cooperates-with", "derived-from",
        "downloads", "has", "owns", "hosts", "publishes", "duplicate-of", "contains", "exfiltrates-to",
        "exploits", "investigates", "originates-from", "participates-in", "detects", "remediates",
        "resolves-to", "revoked-by", "subnarrative-of", "subtechnique-of", "known-as", "reports-to",
        "supports", "has-covered",
    )  # fmt: skip


#: ``ItemIcon``'s icon per type (lower case), as Lucide names.
_ICONS: dict[str, tuple[str, ...]] = {
    "earth": ("global", "region"),
    "lock-keyhole": ("attack-pattern",),
    "chess-knight": ("campaign",),
    "text-align-start": ("note",),
    "eye": ("observed-data",),
    "message-square-quote": ("opinion",),
    "file-text": ("report", "grouping"),
    "wrench": ("course-of-action",),
    "user": ("individual", "user"),
    "building-2": ("organization", "identity", "sector", "group"),
    "database": ("system",),
    "scan-search": ("indicator",),
    "router": ("infrastructure",),
    "gem": ("intrusion-set",),
    "map-pin": ("city", "position", "location"),
    "map": ("administrative-area",),
    "flag": ("country",),
    "biohazard": ("malware",),
    "microscope": ("malware-analysis",),
    "laptop-minimal": ("threat-actor", "threat-actor-group", "threat-actor-individual"),
    "monitor": ("tool",),
    "bug": ("vulnerability",),
    "flame": ("incident",),
    "calendar": ("event",),
    "languages": ("narrative", "language"),
    "waves": ("data-source",),
    "file-code": ("data-component",),
    "route": ("kill-chain-phase",),
    "archive": ("artifact",),
    "briefcase-business": ("case",),
    "circle-check-big": ("case-incident", "case-rfi", "case-rft", "feedback", "task"),
    "earth-lock": ("dashboard", "workspace", "investigation"),
    "hexagon": (*(kind.lower() for kind in _OBSERVABLES), "observable"),
    "eye-off": ("stix-sighting-relationship", "sighting"),
    "spline": ("relationship", "stix-core-relationship", *(k for k in _RELATIONSHIP_TYPES if k.islower())),
    "shield": ("securityplatform",),
    "wand-sparkles": ("autofix",),
    "circle-question-mark": ("restricted",),
    "circle": ("default",),
}  # fmt: skip
_ICON_OF: dict[str, str] = {kind: name for name, kinds in _ICONS.items() for kind in kinds}
#: What ``ItemIcon`` draws for a type it does not know.
_UNKNOWN_ICON = "sparkles"


def entity_icon(entity_type: str | None) -> str:
    """OpenCTI's ``ItemIcon(type)``, as a Lucide name."""
    return _ICON_OF.get((entity_type or "").lower(), _UNKNOWN_ICON)


#: OpenCTI's English names of the types whose name is not the type with spaces.
_LABELS: dict[str, str] = {
    "Administrative-Area": "Area", "AI-Prompt": "AI Prompt", "Case-Incident": "Incident response",
    "Case-Rfi": "Request for information", "Case-Rft": "Request for takedown", "Email-Addr": "Email address",
    "Email-Mime-Part-Type": "Email message - MIME Part", "IPv4-Addr": "IPv4 address", "IPv6-Addr": "IPv6 address",
    "Kill-Chain-Phase": "Kill Chain phase", "Mac-Addr": "MAC address", "SecurityPlatform": "Security platform",
    "Stix-Core-Object": "Entity and observable", "Stix-Core-Relationship": "Relationships",
    "Stix-Cyber-Observable": "Observable", "Stix-Domain-Object": "Entity", "StixFile": "File", "Task": "Tasks",
    "Url": "URL", "Windows-Registry-Value-Type": "Windows Registry Key Value",
}  # fmt: skip


#: OpenCTI's English names of the relationship types whose name is not the type with spaces (``relationship_<type>``).
_RELATIONSHIP_LABELS: dict[str, str] = {
    "analysis-sco": "has captured", "creator-user": "creator", "dst-payload": "payload",
    "external-reference": "references", "granted": "shared with", "host-vm": "host VM", "in-pir": "in PIR",
    "object": "contains", "object-assignee": "has assignee", "object-covered": "covers", "object-label": "has label",
    "object-marking": "has marking", "object-participant": "has participant", "obs_belongs-to": "belongs to (ref)",
    "obs_content": "content (ref)", "obs_resolves-to": "resolves to (ref)", "participate-to": "participates to",
    "relation-has": "has", "stix-core-relationship": "relationship", "stix-sighting-relationship": "sighted in/at",
}  # fmt: skip


def is_relationship_type(kind: str | None) -> bool:
    """Whether *kind* names a relationship: OpenCTI writes them in lower case (``targets``), its entity types capitalized."""
    return bool(kind) and kind[0].islower()  # type: ignore[index]


def relationship_label(relationship_type: str) -> str:
    """OpenCTI's English name of a relationship type (``relationship_<type>``): ``targets``, ``sighted in/at``."""
    return _RELATIONSHIP_LABELS.get(relationship_type, relationship_type.replace("-", " "))


def entity_label(entity_type: str | None) -> str:
    """OpenCTI's English name of an entity type (``entity_<type>``), or of a relationship type (``relationship_<type>``)."""
    if not entity_type:
        return "Unknown"
    if is_relationship_type(entity_type):
        return relationship_label(entity_type)
    return _LABELS.get(entity_type, entity_type.replace("-", " "))


def entity_chip(entity_type: str | None) -> dict | None:
    """A type as OpenCTI's ``ItemEntityType`` draws it: its name, colour and icon (none for a relationship)."""
    if not entity_type:
        return None
    if is_relationship_type(entity_type):
        # Its name with the first letter up, as the chip shows it ("Targets"), and no icon.
        name = relationship_label(entity_type)
        return {"label": name[:1].upper() + name[1:], "color": entity_color(entity_type)}
    return {"label": entity_label(entity_type), "color": entity_color(entity_type), "icon": entity_icon(entity_type)}


_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
#: ``ItemMarkings``' severity of each definition, as the engine's named colours.
_MARKING_SEVERITY: dict[str, str | None] = {
    **dict.fromkeys(("CD", "CD-SF", "DR", "DR-SF", "TLP:RED", "PAP:RED"), "danger"),
    **dict.fromkeys(("TLP:AMBER", "TLP:AMBER+STRICT", "PAP:AMBER"), "alert"),
    **dict.fromkeys(("NP", "TLP:GREEN", "PAP:GREEN"), "success"),
    "SF": "info",
    "NONE": None,
}


def marking_color(definition: str | None, stored: str | None) -> str | None:
    """A marking's colour: its own when it has one, else its definition's severity (``neutral`` when unknown)."""
    if isinstance(stored, str) and _HEX.match(stored.strip()):
        return stored.strip()
    return _MARKING_SEVERITY.get(definition or "", "neutral")


def marking_chip(marking: object) -> dict | None:
    if not isinstance(marking, dict):
        return None
    definition = marking.get("definition")
    chip = {"label": definition or "no definition"}
    color = marking_color(definition, marking.get("x_opencti_color"))
    return {**chip, "color": color} if color else chip


__all__ = ["entity_chip", "entity_color", "entity_icon", "entity_label", "marking_chip", "marking_color"]
