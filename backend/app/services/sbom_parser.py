# SPDX-License-Identifier: Apache-2.0

"""Software components of a CycloneDX or SPDX JSON SBOM, bundled components included (#1178).

An SBOM is one more inventory source of an asset's software
(``dev-docs/asset-model.md`` section 9.4): it describes one application or one
image, so its components join the asset's software list next to what an
endpoint manager or OpenAEV reports.

- CycloneDX: every entry of ``components``, a nested ``components`` giving the
  parent; vendor from ``group``, else ``publisher``, else ``supplier.name``.
- SPDX: every entry of ``packages``; CPE and PURL from ``externalRefs``; vendor
  from ``supplier`` (``Organization: F5, Inc.`` gives ``F5, Inc.``); a
  ``CONTAINS`` relationship (or its inverse ``CONTAINED_BY``) giving the parent.
- Dependencies (#1495): CycloneDX ``dependencies`` (``ref`` -> ``dependsOn``),
  SPDX ``DEPENDS_ON`` and its inverse ``DEPENDENCY_OF``; :meth:`Sbom.depends_on`
  names them per component.

A version that is missing, blank or ``NOASSERTION`` is answered ``None``, so the
store keeps it as ``unknown`` rather than an empty string. An SBOM over
:data:`MAX_COMPONENTS` components is refused, never cut.

Pure: no database, so each format is tested on a document. Ported from the
OpenAEV parser of the closed CTEM-20 branch, which XTM One replaces as the home
of software per asset.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Components one SBOM may hold: a larger one is refused (split it per application or image).
MAX_COMPONENTS = 10_000
#: Bytes one SBOM file may hold.
MAX_BYTES = 20 * 1024 * 1024
#: Values that say nothing about a version or a supplier.
_NO_ASSERTION = frozenset({"", "NOASSERTION", "NONE", "UNKNOWN"})


class SbomError(ValueError):
    """An SBOM refused (the API answers 422 with its message)."""


@dataclass(frozen=True)
class Component:
    """One component of an SBOM, with the local reference of the component that bundles it."""

    ref: str
    parent_ref: str | None
    vendor: str | None
    product: str
    version: str | None
    cpe: str | None
    purl: str | None
    #: The software type the component declares (:data:`COMPONENT_TYPES`), ``application`` when it says nothing.
    software_type: str = "application"


#: CycloneDX ``type`` and SPDX ``primaryPackagePurpose`` (lower-cased) to the software type of the inventory.
COMPONENT_TYPES: Mapping[str, str] = {
    "library": "library",
    "framework": "library",
    "operating-system": "operating_system",
    "operating_system": "operating_system",
    "firmware": "firmware",
    "device-driver": "firmware",
    "container": "container_image",
}


def software_type_of(declared: str | None) -> str:
    """The software type a component's declared type gives; ``application`` for any other or no value."""
    return COMPONENT_TYPES.get((declared or "").strip().lower(), "application")


@dataclass(frozen=True)
class Sbom:
    #: ``cyclonedx`` or ``spdx``.
    format: str
    components: list[Component]
    #: Local reference -> the references it depends on (CycloneDX ``dependencies``, SPDX ``DEPENDS_ON``).
    dependencies: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def depends_on(self, component: Component) -> list[str]:
        """``product version`` of each component *component* depends on, in the document's order (#1495)."""
        out: list[str] = []
        for ref in self.dependencies.get(component.ref, ()):
            target = self._by_ref().get(ref)
            if target is None or target.ref == component.ref:
                continue
            label = f"{target.product} {target.version}".strip() if target.version else target.product
            if label not in out:
                out.append(label)
        return out

    def bundled_in(self, component: Component) -> str:
        """``product version`` of the component that bundles *component* (empty: none)."""
        if not component.parent_ref:
            return ""
        parent = self._by_ref().get(component.parent_ref)
        if parent is None:
            return ""
        return f"{parent.product} {parent.version}".strip() if parent.version else parent.product

    def _by_ref(self) -> dict[str, Component]:
        cached: dict[str, Component] | None = getattr(self, "_refs", None)
        if cached is None:
            cached = {c.ref: c for c in self.components}
            object.__setattr__(self, "_refs", cached)
        return cached


def _text(node: Any, field: str) -> str | None:
    if not isinstance(node, Mapping):
        return None
    value = node.get(field)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _no_assertion(value: str) -> bool:
    return value.strip().upper() in _NO_ASSERTION


def version_of(raw: str | None) -> str | None:
    """The version an SBOM gives, or ``None`` when it says nothing usable."""
    if raw is None or _no_assertion(raw):
        return None
    return raw.strip()


def spdx_supplier(supplier: str | None) -> str | None:
    """``Organization: F5, Inc.`` or ``Person: Jane`` to its name; ``NOASSERTION`` to ``None``."""
    if supplier is None or _no_assertion(supplier):
        return None
    _, colon, rest = supplier.partition(":")
    name = (rest if colon else supplier).strip()
    return name or None


def _check_size(count: int) -> None:
    if count >= MAX_COMPONENTS:
        raise SbomError(f"The SBOM holds more than {MAX_COMPONENTS} components: split it per application or image")


def _list(node: Any, field: str) -> list[Any]:
    value = node.get(field) if isinstance(node, Mapping) else None
    return value if isinstance(value, list) else []


# ── CycloneDX ─────────────────────────────────────────────────────────


def _cyclonedx_dependencies(doc: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """``dependencies``: ``[{ref, dependsOn: [ref]}]`` to ref -> the refs it depends on."""
    out: dict[str, tuple[str, ...]] = {}
    for entry in _list(doc, "dependencies"):
        ref = _text(entry, "ref")
        if ref is None:
            continue
        targets = [t.strip() for t in _list(entry, "dependsOn") if isinstance(t, str) and t.strip()]
        if targets:
            out[ref] = tuple(dict.fromkeys([*out.get(ref, ()), *targets]))
    return out


def _cyclonedx(doc: Mapping[str, Any]) -> list[Component]:
    out: list[Component] = []
    counter = 0
    # Depth-first with an explicit stack: a deeply nested SBOM never reaches the recursion limit.
    stack: list[Iterator[Any]] = [iter(_list(doc, "components"))]
    parents: list[str | None] = [None]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            parents.pop()
            continue
        product = _text(node, "name")
        if product is None:
            continue
        _check_size(len(out))
        ref = _text(node, "bom-ref") or f"component-{counter}"
        counter += 1
        vendor = _text(node, "group") or _text(node, "publisher") or _text(node.get("supplier"), "name")
        out.append(
            Component(
                ref=ref,
                parent_ref=parents[-1],
                vendor=vendor,
                product=product,
                version=version_of(_text(node, "version")),
                cpe=_text(node, "cpe"),
                purl=_text(node, "purl"),
                software_type=software_type_of(_text(node, "type")),
            )
        )
        children = _list(node, "components")
        if children:
            stack.append(iter(children))
            parents.append(ref)
    return out


# ── SPDX ──────────────────────────────────────────────────────────────


def _spdx_dependencies(doc: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """``DEPENDS_ON`` (and its inverse ``DEPENDENCY_OF``) to SPDXID -> the SPDXIDs it depends on."""
    out: dict[str, list[str]] = {}
    for rel in _list(doc, "relationships"):
        kind = (_text(rel, "relationshipType") or "").upper()
        element = _text(rel, "spdxElementId")
        related = _text(rel, "relatedSpdxElement")
        if element is None or related is None:
            continue
        if kind == "DEPENDS_ON":
            source, target = element, related
        elif kind == "DEPENDENCY_OF":
            source, target = related, element
        else:
            continue
        targets = out.setdefault(source, [])
        if target not in targets:
            targets.append(target)
    return {k: tuple(v) for k, v in out.items()}


def _spdx(doc: Mapping[str, Any]) -> list[Component]:
    parent_of: dict[str, str] = {}
    for rel in _list(doc, "relationships"):
        kind = _text(rel, "relationshipType")
        element = _text(rel, "spdxElementId")
        related = _text(rel, "relatedSpdxElement")
        if kind is None or element is None or related is None:
            continue
        kind = kind.upper()
        if kind == "CONTAINS":
            parent_of.setdefault(related, element)
        elif kind == "CONTAINED_BY":
            parent_of.setdefault(element, related)
        # Other relationships are not a bundling (DEPENDS_ON is read by _spdx_dependencies).
    out: list[Component] = []
    for pkg in _list(doc, "packages"):
        product = _text(pkg, "name")
        ref = _text(pkg, "SPDXID")
        if product is None or ref is None:
            continue
        _check_size(len(out))
        cpe: str | None = None
        purl: str | None = None
        for ext in _list(pkg, "externalRefs"):
            kind = _text(ext, "referenceType")
            locator = _text(ext, "referenceLocator")
            if kind is None or locator is None:
                continue
            if cpe is None and kind.lower().startswith("cpe"):
                cpe = locator
            elif purl is None and kind.lower() == "purl":
                purl = locator
        out.append(
            Component(
                ref=ref,
                parent_ref=parent_of.get(ref),
                vendor=spdx_supplier(_text(pkg, "supplier")),
                product=product,
                version=version_of(_text(pkg, "versionInfo")),
                cpe=cpe,
                purl=purl,
                software_type=software_type_of(_text(pkg, "primaryPackagePurpose")),
            )
        )
    return out


def parse(doc: Any) -> Sbom:
    """The components of the SBOM *doc* (a decoded JSON object); ``SbomError`` on a document it cannot read."""
    if not isinstance(doc, Mapping):
        raise SbomError("The SBOM must be a JSON object")
    if (_text(doc, "bomFormat") or "").lower() == "cyclonedx":
        return Sbom(format="cyclonedx", components=_cyclonedx(doc), dependencies=_cyclonedx_dependencies(doc))
    if doc.get("spdxVersion"):
        return Sbom(format="spdx", components=_spdx(doc), dependencies=_spdx_dependencies(doc))
    raise SbomError("Unsupported SBOM: expected CycloneDX JSON (bomFormat) or SPDX JSON (spdxVersion)")


def parse_bytes(data: bytes) -> Sbom:
    """:func:`parse` on the bytes of an uploaded file (size and JSON checked)."""
    if len(data) > MAX_BYTES:
        raise SbomError(f"The SBOM file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise SbomError("The SBOM is not valid JSON (CycloneDX JSON or SPDX JSON expected)") from None
    return parse(doc)
