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

"""OpenCTI/pycti-compatible :class:`Canonicalizer` backend.

``pycti`` and ``stix2`` are required dependencies, but ``pycti`` needs the ``libmagic``
system library, which an image can lack: importing this module then raises.

An object is identified as a reviewer would, from what it says:

- **Normalized first:** the STIX timestamp properties in one form, relationship types and
  domain names lowercased, IP addresses compressed, hash names spelled as STIX does and
  hexadecimal digests lowercased, whitespace collapsed in domain objects' names, STIX
  indicator patterns re-serialized by ``stix2``'s parser.
- **Official identifiers:** ``pycti`` for the STIX types it has a helper for, the OASIS
  ``stix2`` deterministic (UUIDv5) identifiers for Cyber-observable Objects, asked first
  without the STIX common properties (who wrote the object and when), then with them.
- **After its references:** an object's identifier is computed once the objects it refers
  to have theirs, nested references included, so raw, random ids never reach it; members
  of one cycle see each other's type in place of the reference.
- **Never a crash:** an object neither implementation can identify (a custom ``x-…`` type,
  a valid object missing a property ``pycti`` expects) gets an identifier derived from its
  content, so it matches only the same content and an unmatched one is a scored miss.
- **Versions:** objects sharing an id are versions of one object; the latest ``modified``
  is kept, the last one on a tie.

No defang normalization: a defanged observable gets a different identifier than its fanged
form, on purpose. Defanging belongs to a caller-side adapter.

Fully offline: the OpenCTI client is built lazily with health checks disabled and only its
local standard-ID helpers are used.
"""

import ipaddress
import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

try:
    from pycti import OpenCTIApiClient
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patching
    raise ImportError(
        "OpenCtiCanonicalizer requires the 'pycti' dependency, which is "
        "missing. It is declared in backend/pyproject.toml."
    ) from exc

try:
    import stix2
    from stix2.pattern_visitor import create_pattern_object
    from stix2.v21 import OBJ_MAP_OBSERVABLE
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patching
    raise ImportError(
        "OpenCtiCanonicalizer requires the 'stix2' dependency, which is "
        "missing. It is declared in backend/pyproject.toml."
    ) from exc

# Namespace of the content-derived identifiers given to objects no implementation identifies.
_CONTENT_ID_NAMESPACE = uuid.UUID("5746c0de-0000-4000-8000-000000005746")

# STIX 2.1 common properties that say who wrote an object and when, not what it says.
# ``extensions`` is a common property too, but it is content: a file's id derives from it.
_COMMON_PROPERTIES = frozenset(
    {
        "id", "spec_version", "created", "modified", "created_by_ref", "revoked", "labels",
        "confidence", "lang", "external_references", "object_marking_refs",
        "granular_markings",
    }
)

# Hash algorithms whose value is a hexadecimal digest, case-insensitive. SSDEEP, TLSH and
# an algorithm not listed are compared as written.
_HEX_HASHES = frozenset(
    {"MD5", "SHA-1", "SHA-224", "SHA-256", "SHA-384", "SHA-512", "SHA3-224", "SHA3-256", "SHA3-384", "SHA3-512"}
)

# The STIX 2.1 timestamp properties, SDO and SCO, and the form they are written in: RFC 3339
# in UTC, any sub-second precision.
_TIMESTAMP_PROPERTIES = frozenset(
    {
        "created", "modified", "published", "first_observed", "last_observed", "valid_from",
        "valid_until", "first_seen", "last_seen", "start_time", "stop_time", "start", "end",
        "date", "ctime", "mtime", "atime", "created_time", "modified_time",
        "validity_not_before", "validity_not_after", "account_created", "account_expires",
        "credential_last_changed", "account_first_login", "account_last_login",
        "analysis_started", "analysis_ended", "submitted", "object_modified",
    }
)
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


class OpenCtiCanonicalizer:
    """Canonicalize a STIX bundle using official OpenCTI/OASIS identifier generation."""

    def __init__(self) -> None:
        self._helpers: Mapping[str, Any] | None = None

    def _helper_map(self) -> Mapping[str, Any]:
        """Build the pycti standard-ID helpers lazily (local, no network)."""
        if self._helpers is None:
            client = OpenCTIApiClient(
                "http://localhost:0", "dummy", perform_health_check=False
            )
            self._helpers = client.stix2.get_stix_helper()
        return self._helpers

    def _official_id(self, obj: Mapping[str, object]) -> str | None:
        """The ``pycti`` or ``stix2`` identifier, or ``None`` when neither gives one.

        Tried without the authoring metadata first, so a note is its content, then with it,
        for a type whose identifier needs its ``created`` (an incident). Never with the
        object's own ``id``, which ``stix2`` would hand back. Either library can fail on a
        valid object (``pycti`` reads properties STIX makes optional), and ``stix2`` gives a
        random id to a type it cannot derive one for: both are ``None``, never a crash and
        never a random match.
        """
        content = {k: v for k, v in obj.items() if k not in _COMMON_PROPERTIES}
        with_metadata = {k: v for k, v in obj.items() if k != "id"}
        for candidate in (content, with_metadata):
            generated = self._generated_id(candidate)
            if generated is not None:
                return generated
        return None

    def _generated_id(self, obj: dict[str, object]) -> str | None:
        helper = self._helper_map().get(str(obj["type"]))
        try:
            if helper is not None:
                generated = helper.generate_id_from_data(obj)
            else:
                generated = getattr(
                    stix2.parse(obj, allow_custom=True, version="2.1"), "id", None
                )
        except Exception:  # any failure of a third-party id generator
            return None
        if not isinstance(generated, str) or "--" not in generated:
            return None
        try:
            deterministic = uuid.UUID(generated.split("--", 1)[1]).version == 5
        except ValueError:
            return None
        return generated if deterministic else None

    def canonicalize(self, bundle: object) -> object:
        """Return a copy of ``bundle`` with canonical identifiers and rewritten references.

        Args:
            bundle: A STIX bundle. A value that is not a bundle envelope is returned
                unchanged so the caller's structural check reports it.

        Returns:
            A new bundle; the input is never mutated.
        """
        if not isinstance(bundle, Mapping):
            return bundle
        objects = bundle.get("objects")
        if not isinstance(objects, list):
            return bundle
        if not all(isinstance(obj, Mapping) and isinstance(obj.get("type"), str) for obj in objects):
            return bundle

        canonical = _latest_versions([_normalized(obj) for obj in objects])
        position = {obj["id"]: i for i, obj in enumerate(canonical) if isinstance(obj.get("id"), str)}
        refers_to = [
            sorted({position[ref] for ref in _references(obj) if ref in position})
            for obj in canonical
        ]
        remap: dict[str, str] = {}
        new_ids: list[object] = [obj.get("id") for obj in canonical]
        # Referenced objects first. Objects that refer to each other (or to themselves) are
        # identified together, each seeing the others' type in place of the reference, so
        # neither the bundle's order nor their raw ids decide.
        for component in _components_referenced_first(refers_to):
            together = len(component) > 1 or component[0] in refers_to[component[0]]
            placeholders = {str(canonical[i]["id"]) for i in component} if together else set()
            ids = {
                i: self._official_id(view) or _content_id(view)
                for i in component
                for view in [_mapped_references(canonical[i], remap, placeholders)]
            }
            for i, new_id in ids.items():
                new_ids[i] = new_id
                original = canonical[i].get("id")
                if isinstance(original, str):
                    remap[original] = new_id
        canonical = [
            {**_mapped_references(obj, remap, set()), "id": new_ids[i]}
            for i, obj in enumerate(canonical)
        ]

        result = dict(bundle)
        result["objects"] = canonical
        return result


def _normalized(obj: Mapping[str, object]) -> dict[str, object]:
    """A copy whose values compare as a reviewer compares them."""
    observable = obj.get("type") in OBJ_MAP_OBSERVABLE
    copied: dict[str, object] = {}
    for key, value in obj.items():
        if key in _TIMESTAMP_PROPERTIES and isinstance(value, str) and _TIMESTAMP.match(value):
            value = _timestamp(value)
        elif key == "relationship_type" and isinstance(value, str):
            value = value.strip().lower()
        elif key == "name" and isinstance(value, str) and not observable:
            # A malware's name; a file's or a mutex's is compared as written.
            value = " ".join(value.split())
        elif key == "pattern" and isinstance(value, str) and obj.get("pattern_type", "stix") == "stix":
            value = _pattern(value)
        elif key == "hashes" and isinstance(value, Mapping):
            value = _hashes(value)
        elif key == "value" and isinstance(value, str):
            value = _observable_value(str(obj.get("type")), value)
        copied[key] = value
    return copied


def _hashes(hashes: Mapping[str, object]) -> dict[str, object]:
    """Algorithm names in their STIX spelling (``SHA256`` is ``SHA-256``), hexadecimal
    digests lowercased."""
    normalized: dict[str, object] = {}
    for name, digest in hashes.items():
        key = re.sub(r"^SHA(1|224|256|384|512)$", r"SHA-\1", str(name).upper().replace("_", "-"))
        normalized[key] = digest.lower() if key in _HEX_HASHES and isinstance(digest, str) else digest
    return normalized


def _observable_value(stix_type: str, value: str) -> str:
    """A domain name is ASCII case-insensitive; an IP address or network has one form."""
    if stix_type == "domain-name":
        return value.strip().lower()
    if stix_type in ("ipv4-addr", "ipv6-addr"):
        try:
            if "/" in value:
                # Strict: a network written with host bits is another value, kept as written.
                return ipaddress.ip_network(value.strip()).compressed
            return ipaddress.ip_address(value.strip()).compressed
        except ValueError:
            return value
    return value


def _timestamp(value: str) -> str:
    """Milliseconds, truncated: ``…00Z`` and ``…00.000Z`` are one instant."""
    try:
        parsed = datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return value
    return f"{parsed:%Y-%m-%dT%H:%M:%S}.{parsed.microsecond // 1000:03d}Z"


def _pattern(value: str) -> str:
    try:
        return str(create_pattern_object(value))
    except Exception:  # stix2's parser raises several types on a bad pattern
        return " ".join(value.split())


def _latest_versions(objects: list[dict[str, object]]) -> list[dict[str, object]]:
    """One object per id: the latest ``modified``, the last one on a tie, in bundle order."""
    latest: dict[str, int] = {}
    for index, obj in enumerate(objects):
        key = obj.get("id")
        if not isinstance(key, str):
            continue
        kept = latest.get(key)
        if kept is None or str(obj.get("modified", "")) >= str(objects[kept].get("modified", "")):
            latest[key] = index
    return [
        obj
        for index, obj in enumerate(objects)
        if not isinstance(obj.get("id"), str) or latest[str(obj["id"])] == index
    ]


def _references(obj: Mapping[str, object]) -> list[str]:
    """The ids an object's content refers to, nested ones included (an archive's
    ``contains_refs``); the top-level common properties' are metadata."""
    found: list[str] = []

    def walk(value: object, key: str = "") -> None:
        if key.endswith("_ref") and isinstance(value, str):
            found.append(value)
        elif key.endswith("_refs") and isinstance(value, list):
            found.extend(item for item in value if isinstance(item, str))
        elif isinstance(value, Mapping):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for key, value in obj.items():
        if key not in _COMMON_PROPERTIES:
            walk(value, key)
    return found


def _components_referenced_first(refers_to: list[list[int]]) -> list[list[int]]:
    """The objects grouped by the cycles they form (strongly connected components),
    a group listed after every group it refers to. Iterative Tarjan, linear."""
    index: list[int | None] = [None] * len(refers_to)
    low = [0] * len(refers_to)
    on_stack = [False] * len(refers_to)
    stack: list[int] = []
    components: list[list[int]] = []
    counter = 0
    for root in range(len(refers_to)):
        if index[root] is not None:
            continue
        work = [(root, 0)]
        while work:
            node, edge = work.pop()
            if edge == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack[node] = True
            if edge < len(refers_to[node]):
                work.append((node, edge + 1))
                target = refers_to[node][edge]
                if index[target] is None:
                    work.append((target, 0))
                elif on_stack[target]:
                    low[node] = min(low[node], index[target])  # type: ignore[arg-type]
                continue
            if low[node] == index[node]:
                component: list[int] = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return components


def _mapped_references(
    obj: Mapping[str, object], remap: Mapping[str, str], placeholders: set[str]
) -> dict[str, object]:
    """A copy whose references, nested ones included, read as identified so far: each
    mapped once, from its original value; one to *placeholders* as its type alone."""

    def mapped(ref: object) -> object:
        if not isinstance(ref, str):
            return ref
        if ref in remap:
            return remap[ref]
        return f"{ref.split('--', 1)[0]}--" if ref in placeholders else ref

    def walk(value: object, key: str = "") -> object:
        if key.endswith("_ref"):
            return mapped(value)
        if key.endswith("_refs") and isinstance(value, list):
            return [mapped(item) for item in value]
        if isinstance(value, Mapping):
            return {child_key: walk(child, str(child_key)) for child_key, child in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return {key: walk(value, key) for key, value in obj.items()}


def _content_id(obj: Mapping[str, object]) -> str:
    content = {k: v for k, v in obj.items() if k not in _COMMON_PROPERTIES}
    serialized = json.dumps(content, sort_keys=True, default=str)
    return f"{obj['type']}--{uuid.uuid5(_CONTENT_ID_NAMESPACE, serialized)}"


__all__ = ["OpenCtiCanonicalizer"]
