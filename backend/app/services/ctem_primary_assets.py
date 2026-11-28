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
from collections.abc import Iterable, Sequence
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
        "gateway",
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


#: #2711: a version token that closes a reference (``1.3.0``, ``v2``, ``1.0.0-1.0.4``, ``120``).
_VERSION_TOKEN = re.compile(r"v?\d[0-9a-z.+~:_-]*")
#: #2711: a reference of more name words than this (versions left out) is a long product title.
LONG_TITLE_WORDS = 3


def _compound_token(token: str) -> bool:
    """Whether *token* is a package name joined by ``-`` (two words or more) or ``_`` (three or more).

    ``mail-mcp-bridge``, ``mcp-dnstwist``, ``xcode_mcp_server``; a CPE-style two-word name
    (``nginx_plus``, ``internet_explorer``) and a version or architecture (``x86-64``) are not.
    """
    token = token.strip("()[]{},;")
    if "-" not in token and "_" not in token:
        return False
    words = [w for w in re.split(r"[-_]+", token) if any("a" <= ch <= "z" for ch in w)]
    return len(words) >= (2 if "-" in token else 3)


#: #2711 reopened: the operator that opens a version range of a reference (``hermes-agent >= 0.21.6, < 0.23.2``).
_RANGE_START = re.compile(r"\s*(?:<=|>=|==|!=|<|>|=)\s*v?\d")
#: One bound of a version range: an operator and the version it compares with.
_RANGE_BOUND = re.compile(r"(<=|>=|==|!=|<|>|=)\s*(v?\d[0-9a-z.+~:_-]*)")


def _name_part(value: str) -> str:
    """*value* without the version range that closes it: ``hermes-agent`` of ``hermes-agent >= 0.21.6, < 0.23.2``."""
    text = str(value or "")
    if text.strip().lower().startswith("cpe:"):
        return text
    match = _RANGE_START.search(text)
    return text[: match.start()].rstrip(" ,;") if match else text


@lru_cache(maxsize=65536)
def _name_tokens(value: str) -> tuple[str, ...]:
    """The name tokens of a software name: its version range and closing version tokens left out (#2711)."""
    tokens = _name_part(value).strip().lower().split()
    while len(tokens) > 1 and _VERSION_TOKEN.fullmatch(tokens[-1].rstrip(",")):
        tokens.pop()
    return tuple(tokens)


@lru_cache(maxsize=65536)
def _name_words(value: str) -> tuple[tuple[str, ...], bool]:
    """``(words, compound)`` of a software name, its version range and closing version tokens left out (#2711)."""
    tokens = list(_name_tokens(value))
    # Only a reference that is one joined package token is a package name (``mail-mcp-bridge``):
    # ``Microsoft Edge (Chromium-based)`` stays a product title.
    return tuple(_TOKEN.findall(" ".join(tokens))), len(tokens) == 1 and _compound_token(tokens[0])


def reference_names(product: str, ref: str) -> bool | None:
    """Whether a compound package name or long title *ref* names *product*; ``None`` for any other *ref* (#2711).

    A reference that is one joined package name (``mail-mcp-bridge 1.3.0``) names a product only by
    its full name: ``Mail`` is not ``mail-mcp-bridge``, nor ``mcp`` ``mcp-dnstwist``. A title of more
    than :data:`LONG_TITLE_WORDS` name words names a product by its full name, its words after the
    vendor (``AnyConnect Secure Mobility Client`` in ``Cisco AnyConnect Secure Mobility Client``), or a
    run of two words or more that opens it, vendor and product (``Microsoft Office`` in ``Microsoft
    Office 2019 for Mac``); never by one of its words (``analytics`` in a nine-word title) nor by words
    deep inside it. The words after the vendor and the opening run compare the label with its version
    kept (``Windows Server 2022`` is not ``Windows Server 2019 Standard Edition``), and a bare OS or
    vendor family label (:data:`FAMILY_WORDS`, ``Microsoft Windows``) or a generic word names a long
    title only by its full name (#2628, #2685). A CPE, or a short name (``Google Chrome 120``,
    ``nginx_plus``), answers ``None``: the whole-word rule applies.
    """
    text = str(ref or "")
    if text.strip().lower().startswith("cpe:"):
        return None
    words, compound = _name_words(text)
    if not compound and sum(1 for w in words if not w.isdigit()) <= LONG_TITLE_WORDS:
        return None
    label = str(product or "")
    named = _name_words(label)[0]
    if not named:
        return False
    label_full, ref_full = tuple(_TOKEN.findall(label.lower())), tuple(_TOKEN.findall(_name_part(text).lower()))
    if named == words or label_full == ref_full:
        return True
    if compound:
        return False
    needle = _needle(label)
    if needle in FAMILY_WORDS or needle in GENERIC_PRODUCT_WORDS:
        return False
    # The label keeps its version: ``Windows Server 2022`` never opens ``Windows Server 2019 ...``.
    size = len(label_full)
    for seq in (words, ref_full):
        if label_full == seq[1:]:
            return True
        if size >= 2 and (seq[:size] == label_full or seq[1 : 1 + size] == label_full):
            return True
    return False


#: #2711 reopened: the packaging suffixes a distribution adds to a product's own package name:
#: ``openssl-libs`` or ``curl-devel`` is still OpenSSL or curl, unlike ``hermes-estree``.
PACKAGE_SUFFIXES: frozenset[str] = frozenset(
    {"bin", "common", "data", "dev", "devel", "doc", "docs", "lib", "libs", "runtime", "utils"}
)
#: The ecosystem prefixes a distribution puts before a library's own name: ``python3-requests`` is ``requests``.
PACKAGE_PREFIXES: frozenset[str] = frozenset(
    {"golang", "lib", "node", "perl", "php", "py", "python", "python2", "python3", "ruby", "rust"}
)


def label_names(ref: str, label: str) -> bool | None:
    """Whether software *ref* names an inventory *label* that is one joined package name; ``None`` otherwise (#2711).

    ``hermes-estree`` or ``@apollo/server-gateway-interface`` is never reduced to one of its words:
    ``Hermes`` or ``Gateway`` names it only by its full name. A distribution package of the product
    itself (``openssl-libs``, :data:`PACKAGE_SUFFIXES`) keeps the whole-word rule only for the product
    part it packages: ``requests`` names ``python3-requests``, ``Python`` never names ``python-dateutil``.
    A CPE answers ``None``.
    """
    if str(label or "").strip().lower().startswith("cpe:") or str(ref or "").strip().lower().startswith("cpe:"):
        return None
    words, compound = _name_words(str(label or ""))
    if not compound:
        return None
    ref_words = _name_words(str(ref or ""))[0]
    if len(words) == 2 and (
        (words[-1] in PACKAGE_SUFFIXES and ref_words == words[:-1])
        or (words[0] in PACKAGE_PREFIXES and ref_words == words[1:])
    ):
        return None
    return ref_words == words


#: #2777: a parenthesised aside of an inventory label (``Krita (git bd6e8d0)``): a build note, not the product.
_ASIDE = re.compile(r"\([^()]*\)")


def label_names_word(word: str, label: str) -> bool:
    """Whether inventory *label* names product *word* outside an aside or a package path (#2777).

    ``Krita (git bd6e8d0)`` runs Krita, built from git: ``Git`` is only in its parenthesised aside.
    ``@npmcli/git`` is an npm package of that path: ``Git`` names it only by the whole label.
    A CPE label answers True (its product part is read by the whole-word rule).
    """
    text = str(label or "").strip().lower()
    if text.startswith("cpe:"):
        return True
    bare = " ".join(_ASIDE.sub(" ", text).split())
    if "/" in bare or bare.startswith("@"):
        return _needle(word) == bare
    return bool(bare) and contains_word(_needle(word), bare.replace("_", " "))


def tail_word_only(product: str, ref: str) -> bool:
    """Whether *product* is one generic word that only closes a vendor-qualified name *ref* (#2679 reopened).

    ``Gateway`` closes ``Panabit Smart Application Gateway`` and ``JetBrains Gateway``: a generic last
    word (:data:`GENERIC_PRODUCT_WORDS`) of a name of two words or more names a kind of thing, never
    the product. A CPE reference is read by its product part.
    """
    text = str(ref or "").strip()
    if text.lower().startswith("cpe:"):
        text = _haystack(text)
    word = _TOKEN.findall(_needle(str(product or "")))
    words = [w for w in _name_words(text)[0] if not w.isdigit()]
    return (
        len(word) == 1
        and word[0] in GENERIC_PRODUCT_WORDS
        and len(words) >= 2
        and words[-1] == word[0]
        and words[0] != word[0]
    )


def tail_refs(software: Iterable[str]) -> frozenset[str]:
    """The one-word references of *software* that only close a longer reference of the list (#2679 reopened).

    ``Gateway`` beside ``Panabit Smart Application Gateway`` is the generic last word OpenCTI keeps as
    a second Software name: it reaches nothing on its own (``JetBrains Gateway``, a ``Gateway`` app).
    """
    refs = [str(r) for r in software if str(r or "").strip()]
    return frozenset(ref for ref in refs if any(other != ref and tail_word_only(ref, other) for other in refs))


def product_matches(product: str, ref: str, *, family_exact: bool = True, ref_exact: bool = True) -> bool:
    """An installed *product* named by a software reference *ref* (a name, or a CPE's product part).

    The same rule as the gate's software match: equal, or *product* as a whole
    word of *ref* (``Nginx`` in ``nginx_plus``, never ``win`` in ``windows``).
    A bare OS or vendor family (:data:`FAMILY_WORDS`) matches only itself (#2628):
    a device whose OS family is ``Windows`` never runs ``Windows Server 2019`` by that word.
    *family_exact* False lets a family reference name a product of that family (a CVE
    naming only ``Windows`` reaches ``Windows 11``): a family-only match, never proven.
    A compound package name or long title *ref* names *product* only by its full name
    (:func:`reference_names`, #2711); *ref_exact* False skips that rule when *ref* is an
    inventory label and *product* the reference (the reverse direction of a match).
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
    if ref_exact:
        named = reference_names(str(product or ""), str(ref or ""))
        if named is not None:
            return named
    else:
        named = label_names(str(product or ""), str(ref or ""))
        if named is not None:
            return named
        if p != r and not label_names_word(p, str(ref or "")):
            return False
    if p in GENERIC_PRODUCT_WORDS:
        return p == r
    if family_exact and p in FAMILY_WORDS and p != r:
        return False
    return contains_word(p, r)


#: #2680: OS family strings an inventory import writes that name no platform word of
#: ``ctem_fixed_version._PLATFORM_WORDS`` (``Darwin`` is the kernel name macOS reports).
_OS_FAMILY_ALIASES: tuple[tuple[str, str], ...] = (("darwin", "macOS"),)


def device_platform(os_family: str) -> str | None:
    """The platform a device's reported *os_family* names, else ``None`` (#2680).

    The import's strings as they come (``Windows``, ``Windows NT``, ``Mac OS X``,
    ``Darwin``, ``macOS Sonoma``) are read to one of the platform names
    :func:`~app.services.ctem_fixed_version.platform_of_software` answers.
    """
    text = " ".join(str(os_family or "").split()).casefold()
    if not text:
        return None
    for word, platform in _OS_FAMILY_ALIASES:
        if contains_word(word, text):
            return platform
    from app.services.ctem_fixed_version import platform_of_software

    return platform_of_software(text, None, None)


def _platform_of_ref(ref: str) -> str | None:
    from app.services.ctem_fixed_version import platform_of_software

    text = str(ref or "")
    cpe = text if text.strip().lower().startswith("cpe:") else ""
    return platform_of_software("" if cpe else text, None, cpe or None)


def platform_compatible(ref: str, os_family: str) -> bool:
    """Whether software *ref* can run on a device of *os_family* (#2628).

    A reference built for one platform (the CPE ``target_sw``, else one OS named in
    its name: ``Office for Windows``, ``Windows Server``) never matches a device that
    reports another OS family (macOS, Linux). Unknown on either side: compatible.
    """
    if not os_family:
        return True
    built_for = _platform_of_ref(ref)
    device = device_platform(os_family)
    return not built_for or not device or built_for == device


def family_reach_allowed(ref: str, os_family: str, *, repository: bool = False, labels: Iterable[str] = ()) -> bool:
    """Whether a bare family reference *ref* (``Windows``) may reach an asset (#2680).

    A family names no product: it reaches only a device whose reported OS family is
    known and of that family. Never a code repository (a ``windows`` crate or npm
    package in its SBOM is not the OS), never a device of another OS family. A device
    that reports no OS family is of that family only when one of its own *labels*
    names a product of it (``Microsoft Windows 11``), never by a bare family label.
    """
    if repository:
        return False
    family = _platform_of_ref(ref)
    if not family:
        return False
    device = device_platform(os_family)
    if device:
        return device == family
    return any(not is_family_word(label) and _platform_of_ref(label) == family for label in labels)


def one_word_in_title(label: str, ref: str) -> bool:
    """Whether a one-word *label* only names one word of a multi-word title *ref* (#2780).

    ``analytics`` (an npm package) is a word of ``Citrix Analytics``, ``react`` of ``React Server
    Components``: a one-word label carries no vendor, so it never names a vendor-qualified title by
    one of its words. A CPE, a one-token package name (``nginx_plus``, the compound rules of #2711)
    or a title of one name word (``Safari 0``, its version left out) answers False.
    """
    text = str(ref or "").strip()
    if not text or text.lower().startswith("cpe:") or is_url_like(text):
        return False
    tokens = _name_tokens(text)
    if len(tokens) < 2:
        return False
    words = [w for w in _name_words(text)[0] if not w.isdigit()]
    named = _name_words(str(label or ""))[0]
    return len(named) == 1 and len(words) >= 2 and named[0] in words


def match_kind(ref: str, label: str) -> str:
    """How software *ref* named the inventory *label* (#2780): ``cpe``, ``vendor_product`` or ``name``.

    ``vendor_product``: the reference's name words, two or more, are the label's or open it after
    nothing but a vendor word (``Citrix Analytics`` names ``Citrix Analytics 2.1``). Any other name
    match is ``name``: one shared word (``analytics`` for ``Citrix Analytics``), a one-word name
    (``Safari 0``), or a reference met after ``for`` (``Office`` in ``Teams Meeting Add-in for
    Microsoft Office``): a possible exposure, never proof.
    """
    text = str(ref or "").strip()
    if text.lower().startswith("cpe:"):
        return "cpe"
    words = _name_words(text)[0]
    if len(words) < 2 or is_family_word(text):
        return "name"
    label_words = _TOKEN.findall(_name_part(str(label or "")).lower())
    size = len(words)
    for start in (0, 1):
        if tuple(label_words[start : start + size]) == words:
            return "vendor_product"
    return "name"


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
        # #2711: the reference is always the haystack the compound rule reads, never the label.
        if self._values_are_refs:
            return product_matches(value, text, family_exact=False, ref_exact=False) or product_matches(text, value)
        return product_matches(value, text) or product_matches(text, value, family_exact=False, ref_exact=False)

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
    #: #2711 reopened: label -> the versions its installed rows report, so a hop is verified only when one
    #: of them is the version or in the range the reference names. A label absent here has no version read.
    versions: dict[str, set[str]] = field(default_factory=dict)
    #: #2680 / #2685: a code repository record: a bare family reference never reaches it, and its
    #: SBOM dependencies never run a bare generic word (``client``).
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
    _label_cache: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)

    def _running(self, ref: str) -> frozenset[int]:
        """Indexes of the reachers running software *ref*, cached per reference."""
        hit = self._cache.get(ref)
        if hit is None:
            labels = self._labels_of(ref)
            holders = self._labels[1] if self._labels else {}
            hit = frozenset(i for label in labels for i in holders[label])
            self._cache[ref] = hit
        return hit

    def _labels_of(self, ref: str) -> frozenset[str]:
        """The inventory labels software *ref* names, cached per reference (#2711)."""
        hit = self._label_cache.get(ref)
        if hit is None:
            if self._labels is None:
                holders: dict[str, set[int]] = {}
                for i, r in enumerate(self.reachers):
                    for label in r.products.values():
                        holders.setdefault(str(label), set()).add(i)
                self._labels = (ProductIndex(holders), holders)
            hit = frozenset(self._labels[0].matching(ref))
            self._label_cache[ref] = hit
        return hit

    def _label(self, reacher: Reacher, ref: str) -> str:
        """The label *reacher* runs that *ref* names (#2711: the hop says what the asset runs), else ``""``."""
        if not ref:
            return ""
        named = self._labels_of(ref)
        return next((str(label) for label in reacher.products.values() if str(label) in named), "")

    def _matching(
        self, software: list[str], keys: list[str] | None, *, keys_only: bool = False
    ) -> list[tuple[Reacher, str]]:
        """The reachers *software* or subject *keys* name, each with the software reference it runs.

        A named product is preferred over a bare family reference; a family reference
        reaches only a device of that known OS family, never a repository (#2680).
        With *keys_only* (#2701: the evidence names its assets), only the reachers *keys*
        name are kept; *software* still says which reference each of them runs.
        """
        wanted = {k for k in (keys or []) if k}
        tails = tail_refs(software)
        refs = [(ref, is_family_word(ref)) for ref in software if ref and ref not in tails]
        out: list[tuple[Reacher, str]] = []
        for i, reacher in enumerate(self.reachers):
            if keys_only and not wanted & reacher.keys:
                continue
            named = family = ""
            for ref, is_family in refs:
                if (family if is_family else named) or i not in self._running(ref):
                    continue
                if is_family:
                    if family_reach_allowed(
                        ref, reacher.os_family, repository=reacher.repository, labels=reacher.products.values()
                    ):
                        family = ref
                elif platform_compatible(ref, reacher.os_family) and not (reacher.repository and is_generic_word(ref)):
                    named = ref
                    break
            matched = named or family
            if matched or wanted & reacher.keys:
                out.append((reacher, matched))
        return out

    def reach(
        self,
        software: list[str],
        keys: list[str] | None = None,
        *,
        keys_only: bool = False,
        fixed_version: str | Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """The primary assets *software* (names, CPEs, products) or subject *keys* reach.

        One entry per (primary asset key, through, source), best source first:
        ``{primary_asset_key, software, reference, version_verified, family_only, asset, through,
        through_kind, source, roles, via}``: ``software`` is the inventory label the asset runs,
        ``reference`` the software reference (a CVE's software) that named it (#2711).
        ``version_verified`` holds over every asset the chain reaches (a group of many devices
        included), not only the first one met: one versioned installed row is enough.
        ``family_only`` (#2680) holds when every asset of the chain is reached only by a bare OS
        family reference (``Windows``): a possible exposure, never a running product.
        *fixed_version* (#2747) is the CVE's fix, the only bound a reference naming no version is checked on.
        """
        if not self.reachers:
            return []
        generic = generic_refs(software)
        # The specific references first, so a device matched by both reports the specific one.
        ordered = [r for r in software if r not in generic] + [r for r in software if r in generic]
        found: dict[tuple[str, str, str], dict[str, Any]] = {}
        for reacher, matched in self._matching(ordered, keys, keys_only=keys_only):
            verified = matched not in generic and runs_versioned(reacher, matched, fixed_version=fixed_version)
            family_only = bool(matched) and is_family_word(matched)
            label = self._label(reacher, matched) or matched
            for link, kind, through in reacher.links:
                key = (link.primary_asset_key, through, link.source)
                if key in found:
                    if not found[key]["version_verified"] and verified:
                        found[key]["version_verified"] = True
                    if found[key]["family_only"] and not family_only:
                        found[key]["family_only"] = False
                        found[key]["software"], found[key]["reference"] = label, matched
                        found[key]["match"] = match_kind(matched, label) if matched else ""
                    elif found[key]["match"] == "name" and matched and match_kind(matched, label) != "name":
                        found[key]["software"], found[key]["reference"] = label, matched
                        found[key]["match"] = match_kind(matched, label)
                    continue
                found[key] = {
                    "primary_asset_key": link.primary_asset_key,
                    "software": label,
                    "reference": matched,
                    "version_verified": verified,
                    "family_only": family_only,
                    # #2780: how the reference named the label (cpe, vendor_product, name).
                    "match": match_kind(matched, label) if matched else "",
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

    def supporting(
        self, software: list[str], keys: list[str] | None, primary_asset_key: str, *, keys_only: bool = False
    ) -> list[dict[str, Any]]:
        """Every inventory asset *software* or *keys* name that reaches *primary_asset_key*, one entry per asset.

        Unlike :meth:`reach`, which keeps one chain per (through, source), this lists each
        supporting asset, its best link first: ``{asset, software, through, through_kind,
        source, roles}``, sorted by asset name. The loop trace pages it (#1979).
        """
        rank = {s: n for n, s in enumerate(SOURCE_ORDER)}
        out: list[dict[str, Any]] = []
        for reacher, matched in self._matching(software, keys, keys_only=keys_only):
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

    def totals(
        self, software: list[str], keys: list[str] | None = None, *, keys_only: bool = False
    ) -> dict[str, dict[str, int]]:
        """Per primary asset key, how many inventory assets reach it: ``{assets, direct, via_group}``.

        The decision record stores these next to the first chains, so a primary asset
        reached by 10,000 workstations reads 10,000, never the five chains kept (#1979).
        An asset reached only by a bare OS family reference is counted apart, in
        ``family_only``, never in ``assets`` (#2680): it inflates no scope.
        """
        out: dict[str, dict[str, int]] = {}
        for reacher, matched in self._matching(software, keys, keys_only=keys_only):
            family_only = bool(matched) and is_family_word(matched)
            kinds: dict[str, set[str]] = {}
            for link, kind, _through in reacher.links:
                kinds.setdefault(link.primary_asset_key, set()).add(kind)
            for primary_asset_key, seen in kinds.items():
                row = out.setdefault(primary_asset_key, {"assets": 0, "direct": 0, "via_group": 0})
                if family_only:
                    row["family_only"] = row.get("family_only", 0) + 1
                    continue
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


def runs_versioned(reacher: Reacher, ref: str, *, fixed_version: str | Sequence[str] | None = None) -> bool:
    """Whether *reacher* runs software *ref* from an installed row that reports its version (#2628).

    A bare family reference (``Windows``) is never verified: it names no product to version,
    nor is a bare generic word (``Client``, #2685). A reference with no version is compared
    with *fixed_version* (#2747).
    """
    if not ref or is_family_word(ref) or is_generic_word(ref):
        return False
    return any(
        (product_matches(label, ref) or product_matches(ref, label, ref_exact=False))
        and version_in_reference(label, reacher.versions.get(label), ref, fixed_version=fixed_version)
        for label in reacher.versioned
    )


def reference_versions(ref: str) -> tuple[tuple[str, str], ...] | None:
    """The version constraint a software reference names, ``None`` when it names none (#2711 reopened).

    ``(operator, version)`` bounds: ``hermes-agent >= 0.21.6, < 0.23.2`` is ``((">=", "0.21.6"), ("<", "0.23.2"))``;
    a closing version (``FFmpeg 8.0``) or a CPE version field is ``(("=", "8.0"),)``.
    """
    text = str(ref or "").strip()
    if text.lower().startswith("cpe:"):
        parts = text.lower().split(":")
        index = 5 if text.lower().startswith("cpe:2.3:") else 4
        version = parts[index] if len(parts) > index else ""
        return (("=", version),) if version not in {"", "*", "-"} else None
    rest = text[len(_name_part(text)) :]
    bounds = tuple((op, version.rstrip(",;")) for op, version in _RANGE_BOUND.findall(rest.lower()))
    if bounds:
        return bounds
    tokens = text.lower().split()
    if len(tokens) > 1 and _VERSION_TOKEN.fullmatch(tokens[-1]):
        span = _VERSION_SPAN.fullmatch(tokens[-1])
        if span:
            return ((">=", span.group(1)), ("<=", span.group(2)))
        return (("=", tokens[-1]),)
    return None


#: A closing version span ``1.3.0-1.3.3`` (both sides dotted, so ``1.0.0-1`` stays one version).
_VERSION_SPAN = re.compile(r"(v?\d+(?:\.[0-9a-z]+)+)-(v?\d+(?:\.[0-9a-z]+)+)")


_EPOCH_PREFIX = re.compile(r"^\d+:(.+)$")


def _release_tokens(value: str) -> list[str]:
    """The release tokens of a version, its epoch (``7:4.4.2-0ubuntu0``) and ``v`` prefix left out."""
    text = value.strip().lower()
    epoch = _EPOCH_PREFIX.match(text)
    if epoch:
        text = epoch.group(1)
    if text.startswith("v") and text[1:2].isdigit():
        text = text[1:]
    return _TOKEN.findall(text)


def _satisfies(installed: str, op: str, version: str) -> bool:
    from app.services.ctem_fixed_version import compare_versions

    if op in {"=", "=="}:
        # ``8.0`` names the 8.0 release line: 8.0 and 8.0.2, never 9.0 nor 8.01.
        want, have = _release_tokens(version), _release_tokens(installed)
        return bool(want) and have[: len(want)] == want
    cmp = compare_versions(installed, version)
    if cmp is None:
        return False
    return {"<": cmp < 0, "<=": cmp <= 0, ">": cmp > 0, ">=": cmp >= 0, "!=": cmp != 0}.get(op, False)


def version_in_reference(
    label: str, installed: Iterable[str] | None, ref: str, *, fixed_version: str | Sequence[str] | None = None
) -> bool:
    """Whether one *installed* version of *label* is the version or in the range *ref* names (#2711 reopened).

    #2747: a reference that names no version (``Firefox``, ``nginx_plus``) is verified only against
    the CVE's *fixed_version*: an installed version below it. With no fixed version known, nothing
    is compared, so nothing is verified (a KEV CVE never opens a P1 case on a version nobody placed).
    A label that names the reference's version itself (``Microsoft SharePoint Server 2019`` for
    ``SharePoint Server 2019``) is the release named. *installed* ``None``: no version read for the label.
    """
    bounds = reference_versions(ref)
    if bounds is None:
        fixes = _fix_list(fixed_version)
        if not fixes:
            return False
        if len(fixes) > 1:
            # #2777: one fix per release branch (``2.43.7 ... 2.50.1``): each installed version is
            # compared with the fix of its own branch, never with the smallest fix of all.
            return installed is None or any(below_branch_fix(str(v), fixes) for v in installed)
        bounds = (("<", fixes[0]),)
    if installed is None:
        return True
    if len(bounds) == 1 and bounds[0][0] == "=" and contains_word(bounds[0][1].lower(), str(label or "").lower()):
        return True
    return any(all(_satisfies(str(v), op, version) for op, version in bounds) for v in installed)


def _fix_list(fixed_version: Any) -> list[str]:
    """The fixed versions *fixed_version* holds: one string, or a list of one fix per branch (#2777)."""
    values = fixed_version if isinstance(fixed_version, (list, tuple)) else [fixed_version]
    return list(dict.fromkeys(str(v).strip() for v in values if str(v or "").strip()))


def below_branch_fix(installed: str, fixes: Iterable[str]) -> bool:
    """Whether *installed* is below the fix of its release branch (#2777).

    The branch is the longest leading run of release numbers *installed* shares with a fix
    (``2.45.1`` is on the ``2.45`` branch of ``2.45.4``). With no fix of its branch, it is
    affected only when it is below every fix (an older branch nobody fixed); above them, fixed.
    """
    from app.services.ctem_fixed_version import compare_versions

    have = _release_tokens(installed)
    known = [f for f in fixes if compare_versions(installed, f) is not None]
    if not have or not known:
        return False

    def shared(fix: str) -> int:
        want, n = _release_tokens(fix), 0
        while n < min(len(want), len(have)) - 1 and want[n] == have[n]:
            n += 1
        return n

    best = max(shared(f) for f in known)
    if best >= 2:
        branch = [f for f in known if shared(f) == best]
        return any((compare_versions(installed, f) or 0) < 0 for f in branch)
    return all((compare_versions(installed, f) or 0) < 0 for f in known)


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
                    owner.versions.setdefault(str(product), set()).add(str(version).strip())
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
