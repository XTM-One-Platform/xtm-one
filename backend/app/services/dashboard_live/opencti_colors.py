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

"""The colours OpenCTI gives what its widgets count: an entity type's family colour, a string's hashed colour.

Ported from OpenCTI's ``utils/Colors.ts`` (``itemColor``, ``stringToColour``),
so a widget drawn by XTM One colours an entity type, a relationship type or
any other label as OpenCTI does. Hex values, the same in both themes, as
OpenCTI draws them (the engine's ``themedColor`` turns white black on a light
surface, as OpenCTI does).
"""

from __future__ import annotations

#: The family colours (``COLOR_FAMILIES``).
FAMILY_COLORS: dict[str, str] = {
    "analyse": "#70B23B",
    "cases": "#EA80FC",
    "events": "#F96C9B",
    "observations": "#FF6F42",
    "allThreats": "#FF9800",
    "arsenal": "#F0B60A",
    "techniques": "#D3E157",
    "victimology": "#BA88FF",
    "locations": "#05ACC1",
    "observables": "#84ffff",
    "relationships": "#616161",
    "restricted": "#424242",
}

#: The entity and relationship types of each family (``ENTITY_TYPE_TO_FAMILY``).
_FAMILY_TYPES: dict[str, tuple[str, ...]] = {
    "analyse": ("Dashboard", "Report", "Grouping", "Malware-Analysis", "Note", "External-Reference", "Investigation"),
    "cases": ("Case-Incident", "Case-Rfi", "Case-Rft", "Task", "Case-Feedback"),
    "events": ("Incident", "Sighting", "Observed-Data"),
    "observations": ("Observable", "Artifact", "Indicator", "Infrastructure", "Opinion"),
    "allThreats": ("Threat-Actor-Group", "Threat-Actor", "Threat-Actor-Individual", "Intrusion-Set", "Campaign"),
    "arsenal": ("Malware", "Variant", "Channel", "Tool", "Vulnerability", "Session", "SecurityPlatform"),
    "techniques": ("Attack-Pattern", "Narrative", "Course-Of-Action", "Data-Component", "Data-Source", "Language"),
    "victimology": ("Sector", "Event", "Organization", "System", "Individual", "User", "Group"),
    "locations": ("Region", "Country", "Administrative-Area", "City", "Position"),
    "observables": (
        "Stix-Cyber-Observable", "Autonomous-System", "Directory", "Domain-Name", "Email-Addr", "Email-Message",
        "Email-Mime-Part-Type", "StixFile", "X509-Certificate", "AI-Prompt", "IPv4-Addr", "IPv6-Addr", "Mac-Addr",
        "Mutex", "Network-Traffic", "Process", "Software", "Url", "User-Account", "Windows-Registry-Key",
        "Windows-Registry-Value-Type", "Cryptographic-Key", "Cryptocurrency-Wallet", "Text", "User-Agent",
        "Bank-Account", "Credential", "Tracking-Number", "Phone-Number", "Payment-Card", "Media-Content", "SSH-Key",
        "Persona", "IMEI", "IMSI", "ICCID",
    ),
    "relationships": (
        "Stix-Core-Relationship", "Relationship", "stix-core-relationship", "targets", "uses", "located-at",
        "related-to", "technology-from", "technology-to", "technology", "transferred-to", "demonstrates", "mitigates",
        "impersonates", "indicates", "comes-after", "attributed-to", "variant-of", "part-of", "employed-by",
        "resides-in", "citizen-of", "national-of", "drops", "delivers", "compromises", "belongs-to", "based-on",
        "communicates-with", "amplifies", "analyses-of", "authored-by", "beacons-to", "characterizes", "consists-of",
        "interpreted-by", "controls", "cooperates-with", "derived-from", "downloads", "has", "bcc", "cc",
        "obs_belongs-to", "owns", "dst", "from", "hosts", "image", "publishes", "duplicate-of", "obs_content",
        "service-dll", "dynamic-analyses-of", "contains", "exfiltrates-to", "exploits", "investigates",
        "originates-from", "participates-in", "body-multipart", "body-raw", "child", "creator-user", "detects",
        "dst-payload", "encapsulated-by", "encapsulates", "opened-connection", "operating-system", "parent",
        "parent-directory", "raw-email", "src-payload", "remediates", "resolves-to", "obs_resolves-to", "revoked-by",
        "sample", "sender", "src", "to", "values", "static-analyses-of", "subnarrative-of", "subtechnique-of",
        "numberOfConnectedElement", "known-as", "reports-to", "supports", "has-covered",
    ),
    "restricted": ("Restricted", "Capability"),
}  # fmt: skip

_TYPE_FAMILY: dict[str, str] = {kind: family for family, kinds in _FAMILY_TYPES.items() for kind in kinds}

#: ``true`` and ``false`` (reversed: the other way round), and no value at all.
TRUE_COLOR = "#2e7d32"
FALSE_COLOR = "#bf360c"
NULL_COLOR = "#5d4037"


def _int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x1_0000_0000 if value >= 0x8000_0000 else value


def string_to_colour(value: str | None, reversed_: bool = False) -> str:
    """A string's colour: ``true`` / ``false`` fixed, anything else hashed (``stringToColour``, JavaScript's 32-bit arithmetic)."""
    if not value:
        return NULL_COLOR
    if value == "true":
        return FALSE_COLOR if reversed_ else TRUE_COLOR
    if value == "false":
        return TRUE_COLOR if reversed_ else FALSE_COLOR
    hash_ = 0
    # ``charCodeAt`` reads UTF-16 code units: a character past the BMP is two.
    encoded = value.encode("utf-16-le")
    for at in range(0, len(encoded), 2):
        hash_ = int.from_bytes(encoded[at : at + 2], "little") + (_int32(_int32(hash_) << 5) - hash_)
    return "#" + "".join(f"{(_int32(hash_) >> (index * 8)) & 0xFF:02x}" for index in range(3))


def item_color(kind: str | None, reversed_: bool = False) -> str:
    """The colour OpenCTI gives an entity or relationship type (``itemColor``): its family's, hashed for an observable or an unknown string."""
    normalized = "Dynamic options" if kind in ("Dynamic from context", "Dynamic from draft") else kind
    family = _TYPE_FAMILY.get(normalized or "")
    if family == "observables":
        return string_to_colour(normalized)
    if family:
        return FAMILY_COLORS[family]
    return string_to_colour(normalized, reversed_)


__all__ = ["FAMILY_COLORS", "item_color", "string_to_colour"]
