# SPDX-License-Identifier: Apache-2.0

"""CTEM reconcile: product identity before a bare-name match (#2036).

The CVE matcher links what the organization runs (a stack entry of
``organization_software_store.matcher_entries``) to the OpenCTI Software a
vulnerability is related to. A product name alone says little: ``minio`` is the
MinIO server and the Python SDK, ``Installer`` is an Apple macOS tool and an
Autodesk Windows product. This module compares the identity of both sides field
by field, and only a field KNOWN ON BOTH SIDES can reject a match:

1. vendor: both known and different (``vendor``, else the vendor the CPE names);
2. ecosystem: the entry is a package of a language ecosystem (its purl is
   ``pkg:pypi``, ``pkg:npm``, ``pkg:maven``, ...) and the OpenCTI Software is
   identified as something else: another purl, an operating system or hardware
   CPE, or an application CPE whose ``target_sw`` does not name that ecosystem
   (the vendor agreeing is not enough: the MinIO server CPE is
   ``minio:minio`` too). Package CVEs reach a repository through the purl match
   of ``ctem_dependency_matches``; distribution packages (``pkg:deb``,
   ``pkg:rpm``, ...) are not a language ecosystem and keep the CPE match;
3. CPE: both known and their ``vendor:product`` differ;
4. platform: the OpenCTI Software names one operating system (an ``o`` CPE,
   or its CPE ``target_sw`` / ``target_hw``) and every asset running the entry
   reports another OS family.

A field unknown on either side never rejects: with no purl, no CPE, no vendor
and no OS, the bare product name still matches (Sam, 25 Nov 2026). Pure
functions over plain data; the version, priority and SLA logic are elsewhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.services import ctem_fixed_version as fixed_versions
from app.services.ctem_dependency_matches import purl_parts

#: Language package ecosystems (purl type) and the CPE ``target_sw`` words that name them.
ECOSYSTEM_TARGETS: dict[str, frozenset[str]] = {
    "pypi": frozenset({"python", "pypi"}),
    "npm": frozenset({"node.js", "nodejs", "node", "npm"}),
    "maven": frozenset({"java", "maven"}),
    "gradle": frozenset({"java", "gradle"}),
    "gem": frozenset({"ruby", "rubygems", "gem"}),
    "cargo": frozenset({"rust", "cargo"}),
    "golang": frozenset({"go", "golang"}),
    "nuget": frozenset({".net", "asp.net", "nuget", ".net_core", ".net_framework"}),
    "composer": frozenset({"php", "composer"}),
    "hex": frozenset({"elixir", "erlang", "hex"}),
    "pub": frozenset({"dart", "flutter", "pub"}),
    "cocoapods": frozenset({"cocoapods", "swift"}),
    "swift": frozenset({"swift"}),
    "conda": frozenset({"python", "conda"}),
}
_UNSET = frozenset({"", "*", "-"})


def _word(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _token(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def cpe_fields(cpe: str | None) -> dict[str, str] | None:
    """``part``, ``vendor``, ``product``, ``target_sw``, ``target_hw`` of a CPE 2.3 or 2.2 name, else ``None``."""
    value = (cpe or "").strip().lower()
    if value.startswith("cpe:2.3:"):
        parts = value.split(":")
    elif value.startswith("cpe:/"):
        parts = ["cpe", "2.2", *value[5:].split(":")]
    else:
        return None
    parts += [""] * (12 - len(parts))

    def field(index: int) -> str:
        return "" if parts[index] in _UNSET else parts[index]

    return {
        "part": field(2),
        "vendor": field(3),
        "product": field(4),
        "target_sw": field(10),
        "target_hw": field(11),
    }


def purl_fields(purl: str | None) -> tuple[str, str] | None:
    """``(type, namespace/name)`` of a Package URL, lower-cased and percent-decoded, without version or qualifiers.

    Read through :func:`ctem_dependency_matches.purl_parts`, so ``pkg:npm/%40babel/core`` and
    ``pkg:npm/@babel/core`` (an unencoded scope, with or without a version) give the same path; else ``None``.
    """
    base, _version = purl_parts(purl)
    if not base:
        return None
    kind, _, path = base[4:].partition("/")
    return (kind, path.strip("/")) if kind and path.strip("/") else None


def ecosystem(purl: str | None) -> str | None:
    """The language ecosystem a purl names (a key of :data:`ECOSYSTEM_TARGETS`), else ``None``."""
    found = purl_fields(purl)
    return found[0] if found and found[0] in ECOSYSTEM_TARGETS else None


def software_ecosystem(node: Mapping[str, Any]) -> str | None:
    """The language ecosystem an OpenCTI Software is a package of (#2223), else ``None``.

    Its purl names one (``pkg:pypi``, ``pkg:maven``, ...), else its application CPE's
    ``target_sw`` names one (``cpe:2.3:a:minio:minio:*:*:*:*:*:python:*:*``). A server
    CPE with no ``target_sw`` (``cpe:2.3:a:minio:minio:*``) is no package.
    """
    kind = ecosystem(str(node.get("x_opencti_purl") or node.get("purl") or ""))
    if kind:
        return kind
    fields = cpe_fields(str(node.get("cpe") or ""))
    if fields and fields["part"] == "a" and fields["target_sw"]:
        for name, targets in ECOSYSTEM_TARGETS.items():
            if fields["target_sw"] in targets:
                return name
    return None


def platform_of(cpe: str | None) -> str | None:
    """The one OS family a CPE names: the product of an ``o`` CPE, else its ``target_sw`` / ``target_hw``."""
    fields = cpe_fields(cpe)
    if not fields:
        return None
    if fields["part"] == "o" and fields["product"]:
        named = fixed_versions.platform_of_software(fields["product"].replace("_", " "), None, None)
        if named:
            return named
    return fixed_versions.platform_of_software(None, None, cpe) if (cpe or "").lower().startswith("cpe:2.3:") else None


def _vendor(vendor: Any, cpe: str | None) -> str:
    fields = cpe_fields(cpe)
    return _token(vendor) or _token(fields["vendor"] if fields else "")


def conflict(entry: Mapping[str, Any], node: Mapping[str, Any]) -> str | None:
    """Why *node* (an OpenCTI Software) is not *entry*'s product, or ``None`` when nothing known contradicts.

    *entry* holds ``vendor``, ``cpe``, and optionally ``purl`` and ``os_families``
    (the OS families of every asset running it; empty when one is not known).
    *node* holds ``vendor``, ``cpe`` and optionally ``x_opencti_purl``.
    """
    entry_cpe, node_cpe = str(entry.get("cpe") or ""), str(node.get("cpe") or "")
    entry_vendor, node_vendor = _vendor(entry.get("vendor"), entry_cpe), _vendor(node.get("vendor"), node_cpe)
    if entry_vendor and node_vendor and entry_vendor != node_vendor:
        return "vendor"
    entry_fields, node_fields = cpe_fields(entry_cpe), cpe_fields(node_cpe)
    if (
        entry_fields
        and node_fields
        and entry_fields["vendor"]
        and entry_fields["product"]
        and node_fields["vendor"]
        and node_fields["product"]
        and (entry_fields["vendor"], entry_fields["product"]) != (node_fields["vendor"], node_fields["product"])
    ):
        return "cpe"
    kind = ecosystem(str(entry.get("purl") or ""))
    if kind:
        node_purl = purl_fields(str(node.get("x_opencti_purl") or node.get("purl") or ""))
        entry_purl = purl_fields(str(entry.get("purl") or ""))
        if node_purl and node_purl != entry_purl:
            return "ecosystem"
        if (
            not node_purl
            and node_fields
            and node_fields["part"]
            and (node_fields["part"] != "a" or node_fields["target_sw"] not in ECOSYSTEM_TARGETS[kind])
        ):
            return "ecosystem"
    platform = platform_of(node_cpe)
    families = {_word(f) for f in entry.get("os_families") or () if _word(f)}
    if platform and families and _word(platform) not in families:
        return "platform"
    return None


def os_families(values: Iterable[Any]) -> list[str]:
    """The OS families of the assets running a software, sorted; ``[]`` when one of them is not known."""
    found: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if not text:
            return []
        found.add(text)
    return sorted(found)
