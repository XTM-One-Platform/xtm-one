# SPDX-License-Identifier: Apache-2.0

"""The primary assets a CTEM signal reaches through the asset inventory (#1591, S3).

The scope gate (``ctem_scope_gate.decide``) reads criticality from the
organization profile; this module adds the path the profile alone cannot
follow: software named by a signal -> the inventory assets that run it (the
asset's software list, the software an endpoint source such as Intune,
Defender or OpenAEV reports per device, and the operating system family the
device reports) -> the asset groups they belong to -> the primary assets
(primary assets) the record or the group supports through
``organization_primary_asset_links`` (sources ``manual``, ``opencrq``, ``rule``;
a person's removal of a synced link is a tombstone and reaches nothing).

:func:`load_index` reads it once per organization (only the assets that
reach a primary asset are read, so the cost follows the links, not the
fleet); :meth:`PrimaryAssetIndex.reach` is pure and answers each primary
asset reached with its chain (software -> asset or group -> primary asset,
role and source), the decision record's evidence.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: The order a link source is shown in when several reach the same primary asset: a person's first.
SOURCE_ORDER: tuple[str, ...] = ("manual", "rule", "opencrq")
#: The link sources a person or OpenCRQ recorded (#2396): a primary asset they reach is ``proven``.
PROVEN_SOURCES: frozenset[str] = frozenset({"manual", "opencrq"})


def link_basis(chain: Iterable[Any] | None) -> str:
    """``proven`` when a hop of *chain* is a ``manual`` or ``opencrq`` link, else ``inferred`` (#2396).

    A rule link, a name match or a primary asset named in the evidence is inferred.
    """
    for hop in chain or ():
        if isinstance(hop, dict) and str(hop.get("source") or "") in PROVEN_SOURCES:
            return "proven"
    return "inferred"


def decision_primary_assets(decision: Any) -> list[dict[str, str]]:
    """``[{name, basis}]`` of the primary assets a gate decision's scope names, in order (#2396).

    *basis* is the one the decision recorded; a decision written before #2396 has
    none and is read from its kept chain (no chain: a name match, ``inferred``).
    """
    scope = decision.get("scope") if isinstance(decision, dict) else None
    sources = scope.get("sources") if isinstance(scope, dict) else None
    out: dict[str, str] = {}
    for entry in sources if isinstance(sources, list) else []:
        if not isinstance(entry, dict) or entry.get("from") != "primary_asset" or not entry.get("name"):
            continue
        basis = entry.get("basis")
        if basis not in ("proven", "inferred"):
            basis = link_basis(entry.get("chain"))
        name = str(entry["name"])
        out[name] = "proven" if "proven" in (out.get(name), basis) else "inferred"
    return [{"name": n, "basis": b} for n, b in out.items()]


def decision_assets(decision: Any) -> list[str]:
    """The inventory assets a gate decision matched, each name once, in order (#2635).

    The assets the reconcile resolved the subject to (``reconcile.assets``), then the
    assets of the chains that reached a primary asset (``reconcile.primary_assets``
    and each scope source's kept ``chain``): the assets a case opened from the
    signal is about, so the case counts them instead of an empty list.
    """
    if not isinstance(decision, dict):
        return []
    raw_reconcile = decision.get("reconcile")
    raw_scope = decision.get("scope")
    reconcile: dict[str, Any] = raw_reconcile if isinstance(raw_reconcile, dict) else {}
    scope: dict[str, Any] = raw_scope if isinstance(raw_scope, dict) else {}
    raw_assets = reconcile.get("assets")
    names: list[Any] = list(raw_assets) if isinstance(raw_assets, list) else []
    reached = reconcile.get("primary_assets")
    hops: list[Any] = list(reached) if isinstance(reached, list) else []
    raw_sources = scope.get("sources")
    sources: list[Any] = raw_sources if isinstance(raw_sources, list) else []
    for entry in sources:
        if isinstance(entry, dict) and isinstance(entry.get("chain"), list):
            hops += entry["chain"]
    names += [hop.get("asset") for hop in hops if isinstance(hop, dict)]
    return list(dict.fromkeys(str(n).strip() for n in names if isinstance(n, str) and n.strip()))


def decision_scope_kinds(decision: Any) -> list[str]:
    """What put a gate decision in scope, by kind, each once in order (#2635).

    The ``from`` of each scope source (``asset``, ``asset_group``, ``primary_asset``,
    ``owned_domain``, ``risk_scenario``): the reason a case whose members match no
    asset and no primary asset was still opened.
    """
    scope = decision.get("scope") if isinstance(decision, dict) else None
    sources = scope.get("sources") if isinstance(scope, dict) else None
    kinds = [str(s["from"]) for s in sources or [] if isinstance(s, dict) and s.get("from")]
    return list(dict.fromkeys(kinds))


#: Records read at most as members of a linked group (a larger group is cut, said in ``truncated``).
MAX_MEMBERS = 20000
#: Installed software rows read at most for the assets that reach a primary asset.
MAX_SOFTWARE_ROWS = 200000


def _key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


_TOKEN = re.compile(r"[a-z0-9]+")


def _word_char(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("0" <= ch <= "9")


def contains_word(p: str, r: str) -> bool:
    """*p* equal to *r* or found in *r* between non ``[a-z0-9]`` characters (or the ends).

    The whole-word rule the gate states as ``(^|[^a-z0-9])p($|[^a-z0-9])``, checked
    with ``str.find`` so that no pattern is compiled per call (#2032: a regex per
    pair thrashed the ``re`` cache and blocked the event loop for minutes).
    """
    if p == r:
        return True
    size, start = len(p), r.find(p)
    while start != -1:
        end = start + size
        if (start == 0 or not _word_char(r[start - 1])) and (end == len(r) or not _word_char(r[end])):
            return True
        start = r.find(p, start + 1)
    return False


@lru_cache(maxsize=65536)
def _needle(product: str) -> str:
    """*product* as the word :func:`product_matches` looks for."""
    return product.strip().lower().replace("_", " ")


@lru_cache(maxsize=65536)
def _haystack(ref: str) -> str:
    """*ref* as the text :func:`product_matches` looks in (a CPE's product part)."""
    r = ref.strip().lower()
    if r.startswith("cpe:"):
        parts = r.split(":")
        r = parts[4] if len(parts) > 4 else r
    return r.replace("_", " ")


#: #2628: words that name a whole operating system or vendor family, never one product. As the
#: needle of a match they name only themselves: ``windows`` (a device's OS family) is not
#: ``Windows Server 2019``, ``linux`` is not ``Linux Kernel``, ``microsoft`` is not ``Microsoft Excel``.
FAMILY_WORDS: frozenset[str] = frozenset(
    {
        "windows",
        "microsoft",
        "microsoft windows",
        "linux",
        "gnu linux",
        "macos",
        "mac os",
        "mac os x",
        "os x",
        "osx",
        "apple",
        "ios",
        "ipados",
        "android",
        "chromeos",
        "chrome os",
        "unix",
    }
)


#: #2679: top-level domains that make a dotted label a host name (``tenant-my.sharepoint.com-1A2B``), not a product.
_HOST_TLDS: frozenset[str] = frozenset(
    {
        "com",
        "net",
        "org",
        "io",
        "co",
        "app",
        "dev",
        "cloud",
        "ai",
        "biz",
        "info",
        "gov",
        "edu",
        "eu",
        "us",
        "uk",
        "fr",
        "de",
    }
)
#: A host of at least three labels (``a.b.com``), so ``ASP.NET`` or ``socket.io`` stay product names.
_HOST = re.compile(r"[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+\.([a-z]{2,6})(?![a-z0-9])")


@lru_cache(maxsize=65536)
def is_url_like(value: str) -> bool:
    """Whether *value* is a URL or a host name label (a web-app shortcut row), not a product name (#2679).

    ``https://...``, ``www.`` or a host of three labels or more ending in a known top-level domain
    (``tenant-my.sharepoint.com-1A2B``); a CPE, a version (``4.8.1``) or a product with a dot
    (``node.js``, ``ASP.NET Core``, ``.NET Framework``) is not one.
    """
    text = str(value or "").strip().lower()
    if not text or text.startswith("cpe:"):
        return False
    if "://" in text or text.startswith("www."):
        return True
    return any(m.group(1) in _HOST_TLDS for m in _HOST.finditer(text))


def is_family_word(value: str) -> bool:
    """Whether *value* is a bare OS or vendor family (``Windows``, ``Linux``, ``macOS``), not a product (#2628)."""
    return _needle(str(value or "")) in FAMILY_WORDS


#: #2685: words that name a kind of software, never one product. As a reference alone (a CVE
#: whose OpenCTI software list holds only ``Client``) they name only an equal label: ``Client``
#: is not ``Cisco AnyConnect Secure Mobility Client`` nor ``TrueConf Client``.
GENERIC_PRODUCT_WORDS: frozenset[str] = frozenset(
    {
        "agent",
        "app",
        "application",
        "client",
        "core",
        "daemon",
        "desktop",
        "driver",
        "framework",
        "helper",
        "installer",
        "launcher",
        "library",
        "manager",
        "module",
        "plugin",
        "runtime",
        "sdk",
        "server",
        "service",
        "tool",
        "tools",
        "update",
        "updater",
    }
)


def is_generic_word(value: str) -> bool:
    """Whether *value* is a bare generic product word (``Client``, ``Agent``, ``Updater``), not a product (#2685)."""
    return _needle(str(value or "")) in GENERIC_PRODUCT_WORDS


@lru_cache(maxsize=65536)
def _generic_cpe(ref: str) -> tuple[str, str] | None:
    """``(vendor, product)`` of a CPE whose product part is a generic word (#2685), else ``None``.

    ``cpe:2.3:a:trueconf:client:...`` names TrueConf's client: an installed label matches it
    only when it names both the vendor and the product, never by the word ``client`` alone.
    """
    text = ref.strip().lower()
    if text.startswith("cpe:2.3:"):
        parts = text.split(":")
        vendor, product = (parts[3], parts[4]) if len(parts) > 4 else ("", "")
    elif text.startswith("cpe:/"):
        parts = text[len("cpe:/") :].split(":")
        vendor, product = (parts[1], parts[2]) if len(parts) > 2 else ("", "")
    else:
        return None
    vendor, product = vendor.replace("_", " ").strip(), product.replace("_", " ").strip()
    if product not in GENERIC_PRODUCT_WORDS or vendor in {"", "*", "-"}:
        return None
    return vendor, product


def product_matches(product: str, ref: str, *, family_exact: bool = True) -> bool:
    """An installed *product* named by a software reference *ref* (a name, or a CPE's product part).

    The same rule as the gate's software match: equal, or *product* as a whole
    word of *ref* (``Nginx`` in ``nginx_plus``, never ``win`` in ``windows``).
    A bare OS or vendor family (:data:`FAMILY_WORDS`) matches only itself (#2628):
    a device whose OS family is ``Windows`` never runs ``Windows Server 2019`` by that word.
    *family_exact* False lets a family reference name a product of that family (a CVE
    naming only ``Windows`` reaches ``Windows 11``): a family-only match, never proven.
    A URL or host name label (:func:`is_url_like`, a web-app shortcut such as
    ``tenant-my.sharepoint.com-1A2B``) matches only itself (#2679): ``SharePoint`` is a
    word of it, yet the device runs no SharePoint.
    A generic product word (:data:`GENERIC_PRODUCT_WORDS`) matches only itself whatever
    *family_exact* says, and a CPE whose product is one names only a label that also
    names its vendor (#2685).
    """
    p, r = _needle(str(product or "")), _haystack(str(ref or ""))
    if not p or not r:
        return False
    if p != r and (is_url_like(p) or is_url_like(r)):
        return False
    generic = _generic_cpe(str(ref or ""))
    if generic is not None:
        vendor, word = generic
        return contains_word(vendor, p) and contains_word(word, p)
    if p in GENERIC_PRODUCT_WORDS:
        return p == r
    if family_exact and p in FAMILY_WORDS and p != r:
        return False
    return contains_word(p, r)


def platform_compatible(ref: str, os_family: str) -> bool:
    """Whether software *ref* can run on a device of *os_family* (#2628).

    A reference built for one platform (the CPE ``target_sw``, else one OS named in
    its name: ``Office for Windows``, ``Windows Server``) never matches a device that
    reports another OS family (macOS, Linux). Unknown on either side: compatible.
    """
    if not os_family:
        return True
    from app.services.ctem_fixed_version import platform_of_software

    text = str(ref or "")
    cpe = text if text.strip().lower().startswith("cpe:") else ""
    built_for = platform_of_software("" if cpe else text, None, cpe or None)
    device = platform_of_software(os_family, None, None)
    return not built_for or not device or built_for == device


class ProductIndex:
    """Software names matched against references in either direction, by token (#2032).

    :meth:`matching` answers the values *v* for which ``product_matches(v, ref)``
    or ``product_matches(ref, v)`` holds, as a scan of every value would, but
    reads only the values that share a word with *ref*: a whole-word match
    keeps every ``[a-z0-9]`` token of the needle a token of the haystack.
    """

    def __init__(self, values: Iterable[Any], *, values_are_refs: bool = False) -> None:
        """*values_are_refs* True: the values are software references and :meth:`matching` reads
        inventory products, so a family value (``Windows``) may name a product of it (#2628)."""
        self.values: list[str] = list(dict.fromkeys(str(v) for v in values if str(v or "").strip()))
        self._values_are_refs = values_are_refs
        self._by_first: dict[str, list[int]] = {}
        self._by_token: dict[str, list[int]] = {}
        self._untokened: list[int] = []
        for i, value in enumerate(self.values):
            first = _TOKEN.search(_needle(value))
            if _generic_cpe(value) is not None:
                # #2685: a generic CPE matches a label by its vendor, whatever word the label starts with.
                self._untokened.append(i)
            elif first:
                self._by_first.setdefault(first.group(), []).append(i)
            else:
                self._untokened.append(i)
            for token in set(_TOKEN.findall(_haystack(value))):
                self._by_token.setdefault(token, []).append(i)

    def matching(self, ref: Any) -> list[str]:
        """The values matching *ref* in either direction, in insertion order."""
        text = str(ref or "")
        if not text.strip():
            return []
        candidates = set(self._untokened)
        for token in set(_TOKEN.findall(_haystack(text))):
            candidates.update(self._by_first.get(token, ()))
        first = _TOKEN.search(_needle(text))
        if first and _generic_cpe(text) is None:
            candidates.update(self._by_token.get(first.group(), ()))
        else:
            candidates = set(range(len(self.values)))
        return [
            self.values[i]
            for i in sorted(candidates)
            # #2628: an inventory label that is a bare family (a device's ``Windows``) names only
            # itself; a family reference (a CVE naming only ``Windows``) may name a product of it.
            if self._match(self.values[i], text)
        ]

    def _match(self, value: str, text: str) -> bool:
        if self._values_are_refs:
            return product_matches(value, text, family_exact=False) or product_matches(text, value)
        return product_matches(value, text) or product_matches(text, value, family_exact=False)

    def any_match(self, ref: Any) -> bool:
        """Whether one value matches *ref* in either direction."""
        return bool(self.matching(ref))


@dataclass(frozen=True)
class Link:
    """One link of a record or a group to a primary asset."""

    primary_asset_key: str
    source: str
    roles: tuple[str, ...] = ()
    via: str = ""


@dataclass
class Reacher:
    """An inventory asset that reaches at least one primary asset, with what it runs."""

    name: str
    keys: set[str] = field(default_factory=set)
    #: product key -> product label (installed software, the asset's own list, the OS family).
    products: dict[str, str] = field(default_factory=dict)
    #: #2628: the OS family the device reports (empty: unknown); software built for another never matches it.
    os_family: str = ""
    #: #2628: the product labels an installed software row reports with a version (a running version known).
    versioned: set[str] = field(default_factory=set)
    #: #2685: a code repository record: its SBOM dependencies never run a bare generic word (``client``).
    repository: bool = False
    #: (link, through kind ``asset`` or ``asset_group``, through name)
    links: list[tuple[Link, str, str]] = field(default_factory=list)


@dataclass
class PrimaryAssetIndex:
    """The assets that reach a primary asset, read once per organization."""

    loaded: bool = False
    reachers: list[Reacher] = field(default_factory=list)
    truncated: bool = False
    _cache: dict[str, frozenset[int]] = field(default_factory=dict, repr=False)
    _labels: tuple[ProductIndex, dict[str, set[int]]] | None = field(default=None, repr=False)

    def _running(self, ref: str) -> frozenset[int]:
        """Indexes of the reachers running software *ref*, cached per reference."""
        hit = self._cache.get(ref)
        if hit is None:
            if self._labels is None:
                holders: dict[str, set[int]] = {}
                for i, r in enumerate(self.reachers):
                    for label in r.products.values():
                        holders.setdefault(str(label), set()).add(i)
                self._labels = (ProductIndex(holders), holders)
            labels, holders = self._labels
            hit = frozenset(i for label in labels.matching(ref) for i in holders[label])
            self._cache[ref] = hit
        return hit

    def _matching(self, software: list[str], keys: list[str] | None) -> list[tuple[Reacher, str]]:
        """The reachers *software* or subject *keys* name, each with the software reference it runs."""
        wanted = {k for k in (keys or []) if k}
        out: list[tuple[Reacher, str]] = []
        for i, reacher in enumerate(self.reachers):
            matched = next(
                (
                    ref
                    for ref in software
                    if ref
                    and i in self._running(ref)
                    and platform_compatible(ref, reacher.os_family)
                    and not (reacher.repository and is_generic_word(ref))
                ),
                "",
            )
            if matched or wanted & reacher.keys:
                out.append((reacher, matched))
        return out

    def reach(self, software: list[str], keys: list[str] | None = None) -> list[dict[str, Any]]:
        """The primary assets *software* (names, CPEs, products) or subject *keys* reach.

        One entry per (primary asset key, through, source), best source first:
        ``{primary_asset_key, software, version_verified, asset, through, through_kind, source, roles, via}``.
        ``version_verified`` holds over every asset the chain reaches (a group of many devices
        included), not only the first one met: one versioned installed row is enough.
        """
        if not self.reachers:
            return []
        generic = generic_refs(software)
        # The specific references first, so a device matched by both reports the specific one.
        ordered = [r for r in software if r not in generic] + [r for r in software if r in generic]
        found: dict[tuple[str, str, str], dict[str, Any]] = {}
        for reacher, matched in self._matching(ordered, keys):
            verified = matched not in generic and runs_versioned(reacher, matched)
            for link, kind, through in reacher.links:
                key = (link.primary_asset_key, through, link.source)
                if key in found:
                    if not found[key]["version_verified"] and verified:
                        found[key]["version_verified"] = True
                    continue
                found[key] = {
                    "primary_asset_key": link.primary_asset_key,
                    "software": matched,
                    "version_verified": verified,
                    "asset": reacher.name,
                    "through": through,
                    "through_kind": kind,
                    "source": link.source,
                    "roles": list(link.roles),
                    "via": link.via,
                }
        rank = {s: n for n, s in enumerate(SOURCE_ORDER)}
        return sorted(
            found.values(), key=lambda h: (h["primary_asset_key"], rank.get(h["source"], 9), h["through"], h["asset"])
        )

    def supporting(self, software: list[str], keys: list[str] | None, primary_asset_key: str) -> list[dict[str, Any]]:
        """Every inventory asset *software* or *keys* name that reaches *primary_asset_key*, one entry per asset.

        Unlike :meth:`reach`, which keeps one chain per (through, source), this lists each
        supporting asset, its best link first: ``{asset, software, through, through_kind,
        source, roles}``, sorted by asset name. The loop trace pages it (#1979).
        """
        rank = {s: n for n, s in enumerate(SOURCE_ORDER)}
        out: list[dict[str, Any]] = []
        for reacher, matched in self._matching(software, keys):
            links = [
                (link, kind, through)
                for link, kind, through in reacher.links
                if link.primary_asset_key == primary_asset_key
            ]
            if not links:
                continue
            link, kind, through = min(links, key=lambda e: (rank.get(e[0].source, 9), e[1] != "asset", e[2]))
            out.append(
                {
                    "asset": reacher.name,
                    "software": matched,
                    "through": through,
                    "through_kind": kind,
                    "source": link.source,
                    "roles": sorted({r for lk, _, _ in links for r in lk.roles}),
                }
            )
        return sorted(out, key=lambda e: (e["asset"].casefold(), e["asset"]))

    def totals(self, software: list[str], keys: list[str] | None = None) -> dict[str, dict[str, int]]:
        """Per primary asset key, how many inventory assets reach it: ``{assets, direct, via_group}``.

        The decision record stores these next to the first chains, so a primary asset
        reached by 10,000 workstations reads 10,000, never the five chains kept (#1979).
        """
        out: dict[str, dict[str, int]] = {}
        for reacher, _ in self._matching(software, keys):
            kinds: dict[str, set[str]] = {}
            for link, kind, _through in reacher.links:
                kinds.setdefault(link.primary_asset_key, set()).add(kind)
            for primary_asset_key, seen in kinds.items():
                row = out.setdefault(primary_asset_key, {"assets": 0, "direct": 0, "via_group": 0})
                row["assets"] += 1
                if "asset" in seen:
                    row["direct"] += 1
                else:
                    row["via_group"] += 1
        return out


def primary_asset_key(name: Any) -> str:
    """The key a link stores for the primary asset *name*: its words, case-folded, cut at 200."""
    return _key(name)[:200]


def member_primary_assets(
    index: PrimaryAssetIndex, names: dict[str, str], *, software: list[str], keys: list[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(reached, inferred)``: the primary assets a product case member reaches, by name (#2563).

    *software* is what the member runs (an OpenCTI member's product), *keys* the
    assets it was found on (an OpenAEV member's asset names, folded with
    :func:`_key`); *names* maps a link key to the primary asset's name in the
    profile (a link to a primary asset the profile no longer names keeps its key).
    A primary asset is inferred unless a manual or OpenCRQ link supports it, as
    the gate records it for a signal member (#2396).
    """
    chains: dict[str, list[dict[str, Any]]] = {}
    for hit in index.reach(software, keys):
        chains.setdefault(str(hit["primary_asset_key"]), []).append(hit)
    reached = tuple(names.get(key, key) for key in chains)
    inferred = tuple(names.get(key, key) for key, chain in chains.items() if link_basis(chain) == "inferred")
    return reached, inferred


def generic_refs(software: list[str]) -> frozenset[str]:
    """The references of *software* that a more specific reference of the same list names (#2679).

    ``SharePoint`` beside ``SharePoint Server 2019``: the short row is the product family the
    CVE record repeats, not an affected product, so a match through it alone is never version-verified.
    """
    refs = [str(r) for r in software if str(r or "").strip()]
    return frozenset(
        ref for ref in refs if any(_needle(other) != _needle(ref) and product_matches(ref, other) for other in refs)
    )


def runs_versioned(reacher: Reacher, ref: str) -> bool:
    """Whether *reacher* runs software *ref* from an installed row that reports its version (#2628).

    A bare family reference (``Windows``) is never verified: it names no product to version,
    nor is a bare generic word (``Client``, #2685).
    """
    if not ref or is_family_word(ref) or is_generic_word(ref):
        return False
    return any(product_matches(label, ref) or product_matches(ref, label) for label in reacher.versioned)


#: Versions an installed software row stores when the source reported none.
_UNKNOWN_VERSIONS: frozenset[str] = frozenset({"", "unknown", "-", "*"})


def _is_repository(category: Any, fields: dict[str, Any]) -> bool:
    """A code repository record (#2628): it runs nothing but the dependencies its SBOM declares."""
    return str(category or "").strip().lower() == "source_code" or (
        str(fields.get("subcategory") or "").strip().lower() == "code_repository"
    )


def _record_products(fields: dict[str, Any], *, category: Any = "") -> dict[str, str]:
    """What a record says it runs: its own software list and the OS family a device reports.

    A code repository runs no product by its own list or a family word (#2628): only the
    dependencies its SBOM declares (the installed software rows ``load_index`` reads).
    """
    out: dict[str, str] = {}
    if _is_repository(category, fields):
        return out
    for item in fields.get("products") or []:
        label = str(item or "").strip()
        if label:
            out.setdefault(_key(label), label)
    os_family = str(fields.get("os_family") or "").strip()
    if os_family:
        out.setdefault(_key(os_family), os_family)
    return out


async def load_index(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    primary_asset_key: str | None = None,
    software: list[str] | None = None,
) -> PrimaryAssetIndex:
    """Read the assets of *organization_id* that reach a primary asset. Never raises on empty data.

    The loop trace's supporting asset list (UX r2 B4 on #1995) reads a narrower index:
    *primary_asset_key* keeps the links of that primary asset alone, and *software*
    keeps the installed software rows of the products those references name (the
    distinct products are matched first, then only their rows are read), so a cold
    read of 10,000 supporting assets does not pull every installed product of the fleet.
    """
    from app.models.organization_asset import OrganizationAssetSoftware
    from app.models.organization_asset_record import (
        OrganizationAssetGroup,
        OrganizationAssetGroupMember,
        OrganizationAssetRecord,
        OrganizationPrimaryAssetLink,
    )

    link = OrganizationPrimaryAssetLink
    rows = (
        await db.execute(
            select(link.record_id, link.group_id, link.primary_asset_key, link.source, link.roles, link.via).where(
                link.organization_id == organization_id,
                link.removed.is_(False),
                *([link.primary_asset_key == primary_asset_key] if primary_asset_key is not None else []),
            )
        )
    ).all()
    index = PrimaryAssetIndex(loaded=True)
    if not rows:
        return index
    record_links: dict[uuid.UUID, list[tuple[Link, str, str]]] = {}
    group_links: dict[uuid.UUID, list[Link]] = {}
    for record_id, group_id, linked_key, source, roles, via in rows:
        item = Link(str(linked_key), str(source or "manual"), tuple(str(r) for r in roles or ()), str(via or ""))
        if record_id is not None:
            record_links.setdefault(record_id, []).append((item, "asset", ""))
        elif group_id is not None:
            group_links.setdefault(group_id, []).append(item)
    if group_links:
        grp, member = OrganizationAssetGroup, OrganizationAssetGroupMember
        group_rows = (
            await db.execute(
                select(grp.id, grp.name).where(grp.organization_id == organization_id, grp.id.in_(list(group_links)))
            )
        ).all()
        names: dict[uuid.UUID, str] = {gid: str(name) for gid, name in group_rows}
        members = (
            await db.execute(
                select(member.group_id, member.record_id)
                .where(member.group_id.in_(list(group_links)))
                .limit(MAX_MEMBERS + 1)
            )
        ).all()
        if len(members) > MAX_MEMBERS:
            index.truncated, members = True, members[:MAX_MEMBERS]
        for group_id, record_id in members:
            for item in group_links[group_id]:
                record_links.setdefault(record_id, []).append((item, "asset_group", str(names.get(group_id) or "")))
    rec = OrganizationAssetRecord
    records = (
        await db.execute(
            select(rec.id, rec.asset_id, rec.name, rec.external_id, rec.fields, rec.category).where(
                rec.organization_id == organization_id, rec.id.in_(list(record_links))
            )
        )
    ).all()
    by_asset: dict[Any, Reacher] = {}
    asset_ids: list[uuid.UUID] = []
    for record_id, asset_id, name, external_id, fields, category in records:
        if asset_id is not None and asset_id not in by_asset:
            asset_ids.append(asset_id)
        holder = by_asset.setdefault(asset_id or record_id, Reacher(name=str(name)))
        holder.keys |= {k for k in (_key(name), _key(external_id), _key((fields or {}).get("hostname"))) if k}
        holder.products.update(_record_products(dict(fields or {}), category=category))
        if _is_repository(category, dict(fields or {})):
            holder.repository = True
        elif not holder.os_family:
            holder.os_family = str((fields or {}).get("os_family") or "").strip()
        for item, kind, through in record_links.get(record_id, []):
            entry = (item, kind, through or str(name))
            if entry not in holder.links:
                holder.links.append(entry)
    if asset_ids:
        soft = OrganizationAssetSoftware
        scope = [soft.organization_id == organization_id, soft.asset_id.in_(asset_ids)]
        if software is not None:
            refs = ProductIndex((r for r in software if r), values_are_refs=True)
            products = (await db.execute(select(soft.product_key, soft.product).where(*scope).distinct())).all()
            wanted = sorted({str(key) for key, product in products if product and refs.any_match(str(product))})
            scope.append(soft.product_key.in_(wanted))
        installed = (
            await db.execute(
                select(soft.asset_id, soft.product_key, soft.product, soft.version)
                .where(*scope)
                .distinct()
                .limit(MAX_SOFTWARE_ROWS + 1)
            )
        ).all()
        if len(installed) > MAX_SOFTWARE_ROWS:
            index.truncated, installed = True, installed[:MAX_SOFTWARE_ROWS]
        for asset_id, product_key, product, version in installed:
            owner = by_asset.get(asset_id)
            if owner is not None and product:
                owner.products.setdefault(str(product_key or _key(product)), str(product))
                if str(version or "").strip().lower() not in _UNKNOWN_VERSIONS:
                    owner.versioned.add(str(product))
    index.reachers = [r for r in by_asset.values() if r.links]
    return index


#: Largest page of supporting assets one read answers.
SUPPORTERS_PAGE_MAX = 200
#: The asset criticality scale, lowest first: a descending sort on criticality reads the highest first.
_CRITICALITY_RANK: tuple[str, ...] = ("unknown", "low", "medium", "high", "very_high")
#: The orders a page of supporting assets can be read in.
SUPPORTERS_SORTS: tuple[str, ...] = ("name", "criticality")


async def primary_asset_supporters(
    db: AsyncSession,
    organization_id: uuid.UUID,
    primary_asset: str,
    *,
    q: str = "",
    offset: int = 0,
    limit: int = 50,
    through: tuple[str, ...] = (),
    sources: tuple[str, ...] = (),
    sort: str = "name",
    descending: bool = False,
) -> dict[str, Any]:
    """The inventory records that support *primary_asset*, paged, searched, filtered and sorted on the server (#1980 S2).

    ``through`` keeps the rows reaching it that way (``asset``, ``asset_group``),
    ``sources`` the rows of those link sources; ``sort`` is ``name`` or
    ``criticality`` (``descending`` reads the highest first).

    One row per (record, link): a record linked directly (``through_kind``
    ``asset``) or as a member of a linked group (``asset_group``, ``through``
    the group's name), with the link's source, roles, inherited security needs
    and ``via``. A person's removal (``removed``) is left out. ``total`` is the
    count of every row the search keeps; ``counts`` splits the rows of the whole
    primary asset (search ignored) by how they reach it and by source.
    """
    from sqlalchemy import case, func, literal, or_, union_all

    from app.models.organization_asset_record import (
        OrganizationAssetGroup,
        OrganizationAssetGroupMember,
        OrganizationAssetRecord,
        OrganizationPrimaryAssetLink,
    )

    rec, link, member, grp = (
        OrganizationAssetRecord,
        OrganizationPrimaryAssetLink,
        OrganizationAssetGroupMember,
        OrganizationAssetGroup,
    )
    key = _key(primary_asset)[:200]
    limit, offset = max(1, min(int(limit or 50), SUPPORTERS_PAGE_MAX)), max(0, int(offset or 0))
    live = (link.organization_id == organization_id, link.primary_asset_key == key, link.removed.is_(False))
    direct = (
        select(
            rec.id.label("record_id"),
            rec.asset_id.label("asset_id"),
            rec.name.label("name"),
            rec.name_key.label("name_key"),
            rec.category.label("category"),
            rec.criticality.label("criticality"),
            literal("asset").label("through_kind"),
            literal("").label("through"),
            link.source.label("source"),
            link.roles.label("roles"),
            link.inherited_needs.label("inherited_needs"),
            link.via.label("via"),
        )
        .join(rec, rec.id == link.record_id)
        .where(*live, rec.organization_id == organization_id)
    )
    grouped = (
        select(
            rec.id.label("record_id"),
            rec.asset_id.label("asset_id"),
            rec.name.label("name"),
            rec.name_key.label("name_key"),
            rec.category.label("category"),
            rec.criticality.label("criticality"),
            literal("asset_group").label("through_kind"),
            grp.name.label("through"),
            link.source.label("source"),
            link.roles.label("roles"),
            link.inherited_needs.label("inherited_needs"),
            link.via.label("via"),
        )
        .join(grp, grp.id == link.group_id)
        .join(member, member.group_id == grp.id)
        .join(rec, rec.id == member.record_id)
        .where(*live, rec.organization_id == organization_id)
    )
    rows = union_all(direct, grouped).subquery()
    by = (
        await db.execute(
            select(rows.c.through_kind, rows.c.source, func.count()).group_by(rows.c.through_kind, rows.c.source)
        )
    ).all()
    counts: dict[str, dict[str, int]] = {"through": {}, "source": {}}
    for kind, source, count in by:
        counts["through"][str(kind)] = counts["through"].get(str(kind), 0) + int(count)
        counts["source"][str(source or "manual")] = counts["source"].get(str(source or "manual"), 0) + int(count)
    page = select(rows)
    wanted = _key(q)
    if wanted:
        like = f"%{wanted.replace('%', '').replace('_', '')}%"
        page = page.where(or_(rows.c.name_key.like(like), func.lower(rows.c.through).like(like)))
    if through:
        page = page.where(rows.c.through_kind.in_(list(through)))
    if sources:
        page = page.where(func.coalesce(rows.c.source, "manual").in_(list(sources)))
    total = (await db.execute(select(func.count()).select_from(page.subquery()))).scalar() or 0
    name_order: list[Any] = [rows.c.name_key, rows.c.through_kind, rows.c.through, rows.c.record_id]
    order: list[Any]
    if sort == "criticality":
        rank = case(
            {level: index for index, level in enumerate(_CRITICALITY_RANK)},
            value=func.coalesce(rows.c.criticality, "unknown"),
            else_=0,
        )
        order = [rank.desc() if descending else rank.asc(), *name_order]
    else:
        order = [c.desc() for c in name_order] if descending else name_order
    items = (await db.execute(page.order_by(*order).offset(offset).limit(limit))).all()
    return {
        "items": [
            {
                "record_id": str(r.record_id),
                "asset_id": str(r.asset_id) if r.asset_id else None,
                "name": str(r.name),
                "category": str(r.category or ""),
                "criticality": str(r.criticality or "unknown"),
                "through_kind": str(r.through_kind),
                "through": str(r.through or ""),
                "source": str(r.source or "manual"),
                "roles": [str(x) for x in r.roles or ()],
                "inherited_needs": {str(k): str(v) for k, v in (r.inherited_needs or {}).items()},
                "via": str(r.via or ""),
            }
            for r in items
        ],
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "counts": {"total": sum(counts["through"].values()), **counts},
    }


async def asset_primary_assets(
    db: AsyncSession, organization_id: uuid.UUID, asset_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The primary assets an inventory asset supports, with the role and how (#1591, S3).

    Each entry: ``{primary_asset, through, through_kind, source, roles, via}``;
    the agent tools and the asset page read it.
    """
    from app.models.organization_asset_record import (
        OrganizationAssetGroup,
        OrganizationAssetGroupMember,
        OrganizationAssetRecord,
        OrganizationPrimaryAssetLink,
    )

    rec, link, member, grp = (
        OrganizationAssetRecord,
        OrganizationPrimaryAssetLink,
        OrganizationAssetGroupMember,
        OrganizationAssetGroup,
    )
    direct = (
        await db.execute(
            select(link.primary_asset_key, link.source, link.roles, link.via, rec.name)
            .join(rec, rec.id == link.record_id)
            .where(link.organization_id == organization_id, rec.asset_id == asset_id, link.removed.is_(False))
        )
    ).all()
    grouped = (
        await db.execute(
            select(link.primary_asset_key, link.source, link.roles, link.via, grp.name)
            .join(grp, grp.id == link.group_id)
            .join(member, member.group_id == grp.id)
            .join(rec, rec.id == member.record_id)
            .where(link.organization_id == organization_id, rec.asset_id == asset_id, link.removed.is_(False))
        )
    ).all()
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for kind, rows in (("asset", direct), ("asset_group", grouped)):
        for primary_asset_key, source, roles, via, through in rows:
            out.setdefault(
                (str(primary_asset_key), str(through), str(source)),
                {
                    "primary_asset": str(primary_asset_key),
                    "through": str(through),
                    "through_kind": kind,
                    "source": str(source),
                    "roles": [str(r) for r in roles or ()],
                    "via": str(via or ""),
                },
            )
    return sorted(out.values(), key=lambda e: (e["primary_asset"], e["through_kind"], e["through"]))
