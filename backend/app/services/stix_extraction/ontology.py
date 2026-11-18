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

"""The extraction ontology: what kinds of object the pipeline extracts, how each becomes a
STIX 2.1 object, and which relationships may link them.

One module owns the vocabulary so the prompts, the validation of a relationship and the
bundle builder never disagree. A *kind* is finer than a STIX type where OpenCTI is (a
sector, an organization and an individual are three ``identity`` classes; a country, a
region and a city three ``location`` types) and is what the model chooses from.

The relationship matrix follows STIX 2.1 and the relationships OpenCTI accepts. A model
often picks the right link with the wrong verb (an intrusion set "located at" the country
it comes from, a malware "targeting" the CVE it exploits): :data:`REMAPS` rewrites those
deterministically, and anything neither allowed nor remappable is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Kinds the model may assign while detecting. Wider than what the bundle keeps
#: (``unknown``), so a hesitation is visible and settled in the resolution step.
LLM_KINDS: Final[tuple[str, ...]] = (
    "intrusion-set",
    "threat-actor",
    "campaign",
    "malware",
    "tool",
    "attack-pattern",
    "vulnerability",
    "sector",
    "organization",
    "individual",
    "country",
    "region",
    "city",
    "channel",
    "infrastructure",
    "product",
    "unknown",
)

#: Kinds the extraction derives, never detected in the text: the report's unnamed main
#: activity (``activity_hub``), an incident against one victim or a campaign.
DERIVED_KINDS: Final[tuple[str, ...]] = ("incident",)

#: Kinds found by pattern, never by the model: identifiers and observables.
PATTERN_KINDS: Final[tuple[str, ...]] = (
    "attack-pattern-id",
    "cve",
    "ipv4-addr",
    "ipv6-addr",
    "domain-name",
    "url",
    "email-addr",
    "file-name",
    "mac-addr",
    "autonomous-system",
    "md5",
    "sha1",
    "sha256",
    "sha512",
    "windows-registry-key",
)

RELATION_TYPES: Final[tuple[str, ...]] = (
    "uses",
    "targets",
    "exploits",
    "located-at",
    "originates-from",
    "attributed-to",
    "variant-of",
    "communicates-with",
    "beacons-to",
    "exfiltrates-to",
    "drops",
    "downloads",
    "resolves-to",
    "indicates",
    "has",
)

#: Not offered to the model: the verb OpenCTI gives a malware's author (the model says
#: "attributed to").
AUTHORED_BY: Final[str] = "authored-by"

#: A link the text states that no other type expresses ("checks for", "impersonates"):
#: STIX's own fallback, which OpenCTI accepts between any two objects. Offered only to the
#: candidate-pair step, where each decision needs a quote of the chunk.
RELATED_TO: Final[str] = "related-to"

#: The class a kind belongs to for the relationship matrix.
_CLASS: Final[dict[str, str]] = {
    "intrusion-set": "actor",
    "threat-actor": "actor",
    "campaign": "campaign",
    "incident": "incident",
    "malware": "malware",
    "tool": "tool",
    "channel": "channel",
    "infrastructure": "infrastructure",
    "product": "product",
    "attack-pattern": "attack-pattern",
    "attack-pattern-id": "attack-pattern",
    "vulnerability": "vulnerability",
    "cve": "vulnerability",
    "sector": "identity",
    "organization": "identity",
    "individual": "identity",
    "country": "location",
    "region": "location",
    "city": "location",
}


_NETWORK = frozenset({"ipv4-addr", "ipv6-addr", "domain-name", "url"})
_HASHES = frozenset({"md5", "sha1", "sha256", "sha512"})


def kind_class(kind: str) -> str:
    """The relationship-matrix class of *kind*: ``network`` (an address, a domain, a URL),
    ``file`` (a file name), ``hash`` (a file known by its digest), ``observable`` (any
    other) for observables."""
    if kind in _NETWORK:
        return "network"
    if kind == "file-name":
        return "file"
    if kind in _HASHES:
        return "hash"
    return _CLASS.get(kind, "observable")


_ACTORS = ("actor", "campaign")
_SOFTWARE = ("malware", "tool")
_THREATS = ("actor", "campaign", "malware", "tool", "infrastructure")
_INDICATORS = ("network", "file", "hash")

_ALLOWED: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        *((s, "uses", t) for s in _ACTORS for t in ("malware", "tool", "attack-pattern", "infrastructure", "channel")),
        *((s, "targets", t) for s in _ACTORS for t in ("identity", "location", "vulnerability")),
        *((s, "originates-from", "location") for s in _ACTORS),
        *((s, "attributed-to", "actor") for s in _ACTORS),
        *(("malware", "uses", t) for t in ("malware", "tool", "attack-pattern", "infrastructure")),
        # STIX 2.1: a malware or a tool targets an identity, an infrastructure, a location
        # or a vulnerability (the platform it attacks is infrastructure).
        *(("malware", "targets", t) for t in ("identity", "location", "infrastructure")),
        ("malware", "exploits", "vulnerability"),
        ("malware", "originates-from", "location"),
        ("malware", "attributed-to", "actor"),
        ("malware", "variant-of", "malware"),
        *(("malware", verb, "infrastructure") for verb in ("beacons-to", "exfiltrates-to")),
        *(("malware", verb, t) for verb in ("drops", "downloads") for t in ("file", "hash", "malware", "tool")),
        ("tool", "drops", "malware"),
        *(("tool", "uses", t) for t in ("attack-pattern", "infrastructure")),
        *(("tool", "targets", t) for t in ("identity", "location", "vulnerability", "infrastructure")),
        ("infrastructure", "uses", "infrastructure"),
        *(("attack-pattern", "uses", t) for t in _SOFTWARE),
        # A technique targets what it acts on (an identity, a vulnerability); a place is
        # targeted by the threat, never by a technique (no gold of 14,397 relationships has one).
        *(("attack-pattern", "targets", t) for t in ("identity", "vulnerability")),
        # An incident: what it used, whom and what it hit.
        *(("incident", "uses", t) for t in ("malware", "tool", "attack-pattern", "infrastructure", "channel")),
        *(("incident", "targets", t) for t in ("identity", "location", "vulnerability")),
        *(("incident", "attributed-to", t) for t in ("actor", "campaign")),
        ("identity", "located-at", "location"),
        ("location", "located-at", "location"),
        ("infrastructure", "located-at", "location"),
        ("channel", "targets", "identity"),
        ("channel", "targets", "location"),
        # Observables: what a malware talks to, a domain's address, and an observable as
        # evidence of a threat (the bundle writes it as an indicator based on it).
        *((s, "communicates-with", "network") for s in ("malware", "infrastructure")),
        ("infrastructure", "communicates-with", "infrastructure"),
        ("network", "resolves-to", "network"),
        # The software or system a vulnerability is in (OpenCTI: software, tool,
        # infrastructure or organization has vulnerability).
        *((s, "has", "vulnerability") for s in ("product", "tool", "infrastructure")),
        *((s, "indicates", t) for s in _INDICATORS for t in _THREATS),
    }
)

# (source class, verb, target class) → the verb the matrix expects for that pair.
REMAPS: Final[dict[tuple[str, str, str], str]] = {
    # A malware "using" an address talks to it, "using" a file drops it, and the server
    # it "communicates with" is where it beacons.
    **{(s, "uses", "network"): "communicates-with" for s in ("malware", "infrastructure")},
    ("malware", "targets", "network"): "communicates-with",
    ("malware", "uses", "file"): "drops",
    ("malware", "communicates-with", "infrastructure"): "beacons-to",
    ("tool", "downloads", "malware"): "drops",
    ("actor", "located-at", "location"): "originates-from",
    ("campaign", "located-at", "location"): "originates-from",
    ("malware", "located-at", "location"): "originates-from",
    ("identity", "originates-from", "location"): "located-at",
    ("location", "originates-from", "location"): "located-at",
    ("location", "targets", "location"): "located-at",
    ("malware", "targets", "vulnerability"): "exploits",
    ("actor", "exploits", "vulnerability"): "targets",
    ("campaign", "exploits", "vulnerability"): "targets",
    ("attack-pattern", "exploits", "vulnerability"): "targets",
    ("tool", "exploits", "vulnerability"): "targets",
    **{("actor", "exploits", t): "targets" for t in ("identity", "location")},
    **{("malware", "exploits", t): "targets" for t in ("identity", "location")},
    **{("actor", "exploits", t): "uses" for t in ("malware", "tool", "attack-pattern")},
    **{("malware", "exploits", t): "uses" for t in ("malware", "tool", "attack-pattern")},
    **{("actor", "targets", t): "uses" for t in ("malware", "tool", "attack-pattern")},
}

# (source class, verb, target class) → the verb of the relationship read the other way:
# a threat "using" an address, a sample or a file it is not said to drop is what that
# observable indicates.
SWAPS: Final[dict[tuple[str, str, str], str]] = {
    (s, verb, t): "indicates"
    for s in _THREATS
    for verb in ("uses", "targets", "communicates-with", "drops", "downloads", "indicates", "related-to")
    for t in _INDICATORS
    if (s, verb, t) not in _ALLOWED and (s, verb, t) not in REMAPS
}

_LOCATION_RANK = {"city": 0, "country": 1, "region": 2}
_ADDRESSES = frozenset({"ipv4-addr", "ipv6-addr", "domain-name"})


def _platform_verb(source_kind: str, verb: str, target_kind: str) -> str | None:
    """*verb* as OpenCTI's schema takes it between these two kinds, where the schema is
    narrower than the class matrix (``stix.ts`` and the entity modules; pinned by
    ``tests/test_stix_extraction_ontology_conformance.py``), or ``None`` when it refuses it."""
    if verb == "attributed-to":
        if source_kind == "malware":
            return AUTHORED_BY
        if source_kind == "threat-actor" or (source_kind == "intrusion-set" and target_kind != "threat-actor"):
            return None
    if verb == "originates-from" and source_kind == "threat-actor":
        return "located-at"
    if verb == "located-at" and source_kind in _LOCATION_RANK and target_kind in _LOCATION_RANK:
        lower, upper = _LOCATION_RANK[source_kind], _LOCATION_RANK[target_kind]
        return verb if lower < upper or source_kind == target_kind == "region" else None
    if verb == "resolves-to" and (source_kind != "domain-name" or target_kind not in _ADDRESSES):
        return None
    return verb


def resolve_triple(source_kind: str, verb: str, target_kind: str) -> tuple[str, bool] | None:
    """The verb to keep for this relationship and whether its ends swap, or ``None`` when
    it may not exist.

    An allowed triple keeps its verb, a known mistake is remapped or read the other way,
    a triple only allowed the other way round is swapped, and anything else is dropped
    (never guessed). The verb is then the one OpenCTI's schema takes for the two kinds.
    """
    source, target = kind_class(source_kind), kind_class(target_kind)
    if (source, verb, target) in _ALLOWED:
        resolved, swapped = verb, False
    elif (remapped := REMAPS.get((source, verb, target))) and (source, remapped, target) in _ALLOWED:
        resolved, swapped = remapped, False
    elif (swap := SWAPS.get((source, verb, target))) and (target, swap, source) in _ALLOWED:
        resolved, swapped = swap, True
    elif (target, verb, source) in _ALLOWED and verb not in ("uses", "targets"):
        resolved, swapped = verb, True
    elif verb == RELATED_TO:
        return RELATED_TO, False
    else:
        return None
    ends = (target_kind, source_kind) if swapped else (source_kind, target_kind)
    platform = _platform_verb(ends[0], resolved, ends[1])
    return (platform, swapped) if platform else None


def resolve_relation(source_kind: str, verb: str, target_kind: str) -> str | None:
    """The verb to keep for this relationship when its ends stay as given, or ``None``
    (see :func:`resolve_triple`, which also reads it the other way)."""
    resolved = resolve_triple(source_kind, verb, target_kind)
    return resolved[0] if resolved and not resolved[1] else None


def relation_schema_lines() -> list[str]:
    """The relationship matrix as one line per (source class, verb), for a prompt."""
    grouped: dict[tuple[str, str], list[str]] = {}
    for source, verb, target in sorted(_ALLOWED):
        grouped.setdefault((source, verb), []).append(target)
    return [f"{source} {verb} {' | '.join(targets)}" for (source, verb), targets in grouped.items()]


@dataclass(frozen=True, slots=True)
class StixShape:
    """How a kind is written as a STIX object."""

    stix_type: str
    extra: tuple[tuple[str, str], ...] = ()


_SHAPES: Final[dict[str, StixShape]] = {
    "intrusion-set": StixShape("intrusion-set"),
    # A threat actor is an individual (a group is an intrusion set): OpenCTI imports a
    # threat actor without these as a group, under the group's standard id.
    "threat-actor": StixShape(
        "threat-actor", (("resource_level", "individual"), ("x_opencti_type", "Threat-Actor-Individual"))
    ),
    "campaign": StixShape("campaign"),
    "incident": StixShape("incident"),
    "malware": StixShape("malware", (("is_family", "true"),)),
    "tool": StixShape("tool"),
    "channel": StixShape("channel"),
    "infrastructure": StixShape("infrastructure"),
    "attack-pattern": StixShape("attack-pattern"),
    "attack-pattern-id": StixShape("attack-pattern"),
    "vulnerability": StixShape("vulnerability"),
    "cve": StixShape("vulnerability"),
    "sector": StixShape("identity", (("identity_class", "class"),)),
    "organization": StixShape("identity", (("identity_class", "organization"),)),
    "individual": StixShape("identity", (("identity_class", "individual"),)),
    "country": StixShape("location", (("x_opencti_location_type", "Country"),)),
    "region": StixShape("location", (("x_opencti_location_type", "Region"),)),
    "city": StixShape("location", (("x_opencti_location_type", "City"),)),
}


def stix_shape(kind: str) -> StixShape | None:
    """The STIX shape of a named kind, ``None`` for observables (built from their value)."""
    return _SHAPES.get(kind)


#: Kinds a nominal (unnamed) mention may still form: a sector is a generic term by nature.
NOMINAL_KINDS: Final[frozenset[str]] = frozenset({"sector"})


__all__ = [
    "AUTHORED_BY",
    "DERIVED_KINDS",
    "LLM_KINDS",
    "NOMINAL_KINDS",
    "PATTERN_KINDS",
    "RELATED_TO",
    "RELATION_TYPES",
    "REMAPS",
    "SWAPS",
    "StixShape",
    "kind_class",
    "relation_schema_lines",
    "resolve_relation",
    "resolve_triple",
    "stix_shape",
]
