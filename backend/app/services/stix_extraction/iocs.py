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

"""Identifiers and observables found by pattern, never by the model.

ATT&CK technique ids, CVE ids, IP addresses, domains, URLs, e-mail addresses, file hashes
and registry keys have a grammar: a pattern finds every one, with exact offsets, at no
cost, and a model asked for them misses or mangles some. PDF extraction breaks long
tokens across lines (a SHA-256 cut by a line break, ``outlook-\\nweb.ddns.ne\\nt``), so the
patterns allow a line break inside them, and defanged forms (``hxxp``, ``[.]``, ``(dot)``)
are read and their canonical value refanged.

What the patterns find is *anchored*: the model's mentions never override it, and the
relationship step sees each one as an object it may link.

Matching stays permissive and each kind's validation decides (iocsearcher's design): a
domain ends on a real top-level domain (the public suffix list), an address read after
"version" or "build" is a version number, a digest of the empty file or one labelled
imphash, JA3 or serial is no file's, and in a report that defangs its indicators a live
link or address is a citation (GoodFATR, arXiv 2208.00042: context, not blocklists, takes
IOC precision from 0.31 to 0.83).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from functools import cache

_BR = r"(?:\r?\n)?"  # a line break PDF extraction may leave inside a token


def _hex(length: int) -> str:
    return rf"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{_BR}){{{length - 1}}}[0-9A-Fa-f](?![0-9A-Fa-f])"


# "evil\.com": a dot escaped as in a regular expression, a defanging some reports use.
_DOT = r"(?:\.|\[\.\]|\(\.\)|\{\.\}|\[dot\]|\(dot\)|\{dot\}|(?<=\w)\\\.(?=\w))"
_AT = r"(?:@|\[@\]|\(@\)|\{@\}|\[at\]|\(at\)|\{at\})"
_DASH = "[-\u2010\u2011\u2012\u2013\u2014]"  # CVE-2021-44228 typeset with any dash
# A label may be cut by a line break after any character (``outlook-\nweb``), never across a
# hyphen alone on its line: that is a list's bullet between two entries (``feed.php\n-\nevil.com``).
_LABEL = r"[A-Za-z0-9](?:(?:[A-Za-z0-9-]|\r?\n(?=[A-Za-z0-9]|-(?!\r?\n))){0,62}[A-Za-z0-9])?"
_TLD = r"[A-Za-z](?:\r?\n?[A-Za-z]){1,23}"
# A TLD must be letters; file extensions and sentence breaks are rejected in _canonical.
# Not under a wildcard or a placeholder label ("*.spdns.de", "[victim_id].duckdns.org"): the
# indicator is the zone's subdomains, and the zone's own name may be a provider's.
# An address that may end a sentence ("resolves to 192.0.2.10.").
_IPV4 = rf"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d){_DOT}){{3}}(?:25[0-5]|2[0-4]\d|1?\d?\d)"
_DOMAIN = rf"(?<![@\w.-])(?:{_LABEL}{_BR}{_DOT}{_BR})+{_TLD}(?![\w-])"

# File names worth reporting: executables, scripts, documents and archives an intrusion
# drops or delivers (the research pipeline's list, trimmed of image formats). Extensions
# that are also common top-level domains (.com, .pl, .so) are left to the domain pattern.
_FILE_NAME_EXTENSIONS = (
    "exe|dll|sys|scr|pif|cpl|ocx|msi|msp|bat|cmd|ps1|psm1|psd1|vbs|vbe|js|jse|wsf|hta|"
    "jar|lnk|iso|img|vhd|zip|rar|7z|cab|chm|doc|docx|docm|xls|xlsx|xlsm|ppt|pptx|pptm|rtf|"
    "pdf|sh|py|php|asp|aspx|jsp|elf|bin|dat|tmp|apk|dmg|pkg|dylib|sct|wsc|wsh|vb|swf|hta|"
    "htm|html|log|ini|cfg|conf|config|eml|msg|evtx|dmp|pem|key|crt|cer|pfx|p12|db|sqlite|"
    "tar|gz|tgz|xz|bz2|bak|old"
)
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # A technique table may set a sub-technique apart from its parent ("T1548 .002").
    ("attack-pattern-id", re.compile(rf"(?<![A-Za-z0-9])T\d(?:{_BR}\d){{3}}(?:(?:{_BR}| )\.(?:{_BR}\d){{3}})?(?![0-9])", re.IGNORECASE)),
    ("cve", re.compile(rf"(?<![A-Za-z0-9])CVE{_DASH}\d(?:{_BR}\d){{3}}{_DASH}\d(?:{_BR}\d){{3,6}}(?![0-9])", re.IGNORECASE)),
    # A URL wrapped by PDF extraction continues on the next line when that line starts
    # with a URL-like token (a path, a query, an encoded or hex run), never a word.
    ("url", re.compile(
        r"(?<![\w])(?:h(?:tt|xx|xt|tx|\*\*)ps?|ftp|fxp)(?:://|\[://\]|\[:\]//)"
        r"(?:[^\s<>\"')“”‘’]|\r?\n(?=[A-Za-z0-9_\-]*[/?=&%.#_-][^\s<>\"')]))+",
        re.IGNORECASE,
    )),
    # A host and a path written without a scheme ("evil[.]one/gate.php",
    # "174[.]34.164.106:11443/2/x"): a URL, as analysts record it; read like a bare domain,
    # never as a link the report cites. An address's path is not a network's prefix length.
    ("url", re.compile(
        rf"(?<![\w@./:-])(?:(?:{_LABEL}{_DOT})+{_TLD}|{_IPV4})(?::\d{{1,5}})?/(?!\d{{1,2}}(?![\w./-]))(?:{_DOT}|[^\s<>\"')\]“”‘’])+",
    )),
    ("mac-addr", re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}([:-]))(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")),
    ("autonomous-system", re.compile(r"(?<![\w])AS(?:N)?[ ]?(\d{1,10})(?![\w])")),
    ("email-addr", re.compile(rf"(?<![\w.+-])[A-Za-z0-9._%+-]+{_AT}{_BR}(?:{_LABEL}{_DOT})+[A-Za-z]{{2,24}}(?![\w-])")),
    ("sha512", re.compile(_hex(128))),
    ("sha256", re.compile(_hex(64))),
    ("sha1", re.compile(_hex(40))),
    ("md5", re.compile(_hex(32))),
    ("ipv4-addr", re.compile(rf"(?<![\d.]){_IPV4}(?:/\d{{1,2}})?(?!\d|\.\d)")),
    ("ipv6-addr", re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:/\d{1,3})?(?![\w:])")),
    ("file-name", re.compile(rf"(?<![\w.@-])[\w-][\w.-]{{0,120}}\.(?:{_FILE_NAME_EXTENSIONS})(?![\w-])", re.IGNORECASE)),
    ("windows-registry-key", re.compile(r"(?<![\w])(?:HKEY_[A-Z_]+|HK(?:LM|CU|CR|U|CC))\\[^\s\"'<>|]+", re.IGNORECASE)),
    ("domain-name", re.compile(_DOMAIN)),
)

_FILE_EXTENSIONS = frozenset(
    {
        "exe", "dll", "sys", "bat", "cmd", "ps1", "vbs", "js", "jar", "zip", "rar", "7z", "doc",
        "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "log", "png", "jpg", "jpeg", "gif",
        "lnk", "iso", "img", "msi", "py", "sh", "elf", "bin", "dat", "tmp", "ini", "cfg", "xml",
        "json", "html", "htm", "php", "asp", "aspx", "hta", "scr", "cpl", "ocx", "so", "apk",
    }
)


# Top-level domains that are also file extensions: a bare "README.md" or "invoice.one" is
# a file, a defanged one ("evil[.]one") a domain.
_EXTENSION_TLDS = frozenset({"md", "one", "app", "mov", "zip", "ps", "rs", "py", "sh", "so", "cab"})
# Digests of the empty file: every tool computes them, no sample has them.
_EMPTY_FILE_DIGESTS = frozenset(
    {
        "d41d8cd98f00b204e9800998ecf8427e",
        "da39a3ee5e6b4b0d3255bfef95601890afd80709",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce"
        "47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e",
    }
)
_HASHES = ("md5", "sha1", "sha256", "sha512")
# What a hex run read just after these words is: not a file's digest (MISP's hex types).
_NOT_A_FILE_DIGEST = re.compile(
    r"(?:imphash|ja3s?|jarm|authentihash|thumbprint|fingerprint|serial|licen[cs]e|guid|uuid|token|key)\W{0,12}$",
    re.IGNORECASE,
)
_VERSION = re.compile(
    r"(?:\bversion|\bver\.?|\bv|\bbuild|\brelease|\brev(?:ision)?)\s*$"
    # the end of a range of versions ("versions 11.4–19.0.0.207")
    r"|\b(?:versions?|ver\.?|v)\s*\d[\d.]*\s*[-\u2010-\u2014]\s*$",
    re.IGNORECASE,
)
_DEFANGED = re.compile(
    r"\[\.\]|\(\.\)|\{\.\}|\[dot\]|\(dot\)|\{dot\}|\[@\]|\(@\)|\{@\}|\[at\]|\(at\)|\{at\}"
    r"|\bh(?:xx|xt|tx|\*\*)ps?(?=:|\[)|\bfxp(?=:|\[)|\[:\]//|\[://\]|(?<=\w)\\\.(?=\w)",
    re.IGNORECASE,
)
# A link target in Markdown or HTML: what the report cites, not what it reports.
_LINK_TARGET = re.compile(r"\]\(\s*$|href\s*=\s*[\"']?\s*$", re.IGNORECASE)


def rendered_link(text: str, start: int, end: int) -> bool:
    """Whether the live URL at *text[start:end]* is written as a rendered hyperlink,
    "anchor text (URL)": how a page's or a PDF's links come out as text. The URL is where
    its anchor points (a tag page, a related article, an author), not a sign of a threat
    the anchor names."""
    return text[:start].rstrip(" ").endswith("(") and not _DEFANGED.search(text[start:end])


# Roots outside ICANN's that malware resolves through its own resolvers (Namecoin, I2P).
_ALTERNATIVE_ROOTS = frozenset({"bit", "i2p"})


@cache
def _top_level_domains() -> frozenset[str]:
    """The top-level domains of the public suffix list shipped with ``tld`` (offline):
    the last label of every public suffix. Empty when the list cannot be read, and every
    TLD-shaped label is then accepted."""
    from importlib.resources import files

    try:
        text = files("tld").joinpath("res/effective_tld_names_public_only.dat.txt").read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        return frozenset()
    labels = set()
    for line in text.splitlines():
        rule = line.strip().lstrip("!*.")
        if rule and not rule.startswith("//"):
            label = rule.rsplit(".", 1)[-1]
            labels.add(label.encode("idna").decode("ascii") if not label.isascii() else label.lower())
    return frozenset(labels)


@dataclass(frozen=True, slots=True)
class PatternMatch:
    """An identifier or observable found by pattern: kind, canonical value, offsets.

    ``cited`` marks a link or an address the report cites (a reference, a contact) rather
    than reports: it belongs in the report's references, not among its indicators.
    """

    kind: str
    value: str
    start: int
    end: int
    cited: bool = False


def refang(value: str) -> str:
    """The canonical form of a possibly defanged, line-broken token."""
    value = re.sub(r"\r?\n", "", value)
    value = re.sub(r"\[\.\]|\(\.\)|\{\.\}|\[dot\]|\(dot\)|\{dot\}|(?<=\w)\\\.(?=\w)", ".", value, flags=re.IGNORECASE)
    value = re.sub(r"\[@\]|\(@\)|\{@\}|\[at\]|\(at\)|\{at\}", "@", value, flags=re.IGNORECASE)
    value = re.sub(r"\[://\]|\[:\]//", "://", value)
    value = re.sub(r"^h(?:xx|xt|tx|\*\*)p", "http", value, flags=re.IGNORECASE)
    value = re.sub(r"^fxp", "ftp", value, flags=re.IGNORECASE)
    value = re.sub("[\u2010\u2011\u2012\u2013\u2014]", "-", value)
    return value


def _canonical(kind: str, raw: str) -> str | None:
    value = refang(raw).strip().rstrip(".,;:")
    if kind in _HASHES:
        # Case carries nothing: a PDF may typeset a digest in mixed case.
        value = value.lower()
        # A run of decimal digits ("2345678910111213...", an offset table) or a placeholder
        # ("aaaa...", "e1234567...") is no digest: a real one with at most one letter from a to
        # f, or fewer than six distinct characters, is a one-in-a-hundred-thousand chance.
        if sum(c in "abcdef" for c in value) <= 1 or len(set(value)) < 6:
            return None
        return None if value in _EMPTY_FILE_DIGESTS else value
    if kind in ("cve", "attack-pattern-id"):
        return value.replace(" ", "").upper()
    if kind == "domain-name":
        return _domain(value, bool(_DEFANGED.search(raw)))
    if kind == "mac-addr":
        return value.lower().replace("-", ":")
    if kind == "autonomous-system":
        digits = re.sub(r"\D", "", value)
        return digits if digits and int(digits) > 0 else None
    if kind == "ipv6-addr":
        try:
            address = ipaddress.ip_network(value, strict=False) if "/" in value else ipaddress.IPv6Address(value)
        except ValueError:
            return None
        if ":" not in str(address) or not _routable(address):
            return None
        return str(address)
    if kind == "file-name":
        # A template ("<NUM>\\-2024.xlsx") or an escaped fragment is no file's name.
        return None if value.startswith("-") else value
    if kind == "url":
        host = re.sub(r"^[a-z]+://", "", value, flags=re.IGNORECASE).split("/", 1)[0].split("?", 1)[0]
        host = host.rsplit("@", 1)[-1]
        host = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":", 1)[0]
        # A placeholder ("http://[url]/Bunny/[PHP endpoint]") or a masked host
        # ("*****.com") names no host.
        # A URL's host is a host: a scheme or a defanging says so, whatever its top-level
        # domain also names as a file extension ("hxxp://x-uc[.]a[.]run[.]app").
        named = bool(_SCHEME.match(raw) or _DEFANGED.search(raw))
        if _canonical("ipv4-addr", host) or _canonical("ipv6-addr", host) or (
            _HOSTNAME.fullmatch(host) and _domain(host, named)
        ):
            return value
        return None
    if kind == "ipv4-addr":
        try:
            address = ipaddress.ip_network(value, strict=False) if "/" in value else ipaddress.ip_address(value)
        except ValueError:
            return None
        if "/" not in value and value.endswith(".0.0.0"):
            return None  # a network ("4.0.0.0" in an assembly version), no host
        return str(address) if _routable(address) else None
    return value


def _domain(value: str, defanged: bool) -> str | None:
    """*value* as a domain, or None. A defanged one is an indicator its author marked: its
    case (a homoglyph, "go0gIe[.]com") and a top-level domain that is also a file extension
    ("transfer[.]sh") are then no reason to doubt it."""
    tld = value.rsplit(".", 1)[-1]
    # "actors.\nTechnical": a sentence ending on a word, then a capitalised one.
    if tld[:1].isupper() and not tld.isupper():
        return None
    # A label in random case ("WSCriPT.shELL") is obfuscated code: a domain is written
    # in lower case, upper case, or capitalised.
    if not defanged and any(_random_case(label) for label in value.split(".")):
        return None
    value = value.lower()
    tld = value.rsplit(".", 1)[-1]
    if not defanged and (tld in _FILE_EXTENSIONS or tld in _EXTENSION_TLDS):
        return None
    known = _top_level_domains()
    return value if not known or tld in known or tld in _ALTERNATIVE_ROOTS else None


_PASCAL = re.compile(r"(?:[A-Z][a-z]+)+")


def _random_case(label: str) -> bool:
    """A label none of whose hyphen-separated parts is lower, upper or capitalised."""
    def plain(part: str) -> bool:
        letters = "".join(c for c in part if c.isalpha())
        # lower, UPPER, Capitalised or PascalCase ("MicrosoftUpdate"); never camelCase or
        # random case ("naMe", "WSCriPT"), which only code writes.
        return not letters or letters.islower() or letters.isupper() or bool(_PASCAL.fullmatch(letters))

    return not all(plain(part) for part in label.split("-"))


def _out_of_context(kind: str, before: str, after: str = "") -> bool:
    """What the characters around a match say it is not: a version number read as an
    address, an imphash or a serial read as a file's digest, a variable's property or a
    method call read as a domain (``$drive.name``, ``shortcut.save()``)."""
    if kind == "domain-name":
        # A method call opens its parenthesis on the name, on code; "evil.ru (registrar:" or
        # "evil.ru(レジスター" is a remark on the domain.
        return before.endswith("$") or (after[:1] == "(" and after[1:2].isascii())
    if kind == "ipv4-addr":
        return bool(_VERSION.search(before))
    if kind == "file-name":
        return before.endswith(("<", ">"))
    if kind in _HASHES:
        return bool(_NOT_A_FILE_DIGEST.search(before))
    return False


def _routable(address: object) -> bool:
    """A private, loopback, link-local, multicast, reserved or documentation address
    (192.168.x.x, 127.0.0.1, 192.0.2.x, ::1...) names no attacker infrastructure: not an
    observable worth importing."""
    probe = getattr(address, "network_address", address)
    return bool(getattr(probe, "is_global", False))


_HEX_RUN = re.compile(r"[0-9A-Fa-f]+")
_DIGEST_LENGTHS = frozenset({32, 40, 64, 128})
_URL_CONTINUATION = re.compile(r"[A-Za-z0-9_\-]*[/?=&%.#_-]")


def _complete(token: str, file_names: bool = True) -> bool:
    """Whether *token* alone is a whole identifier or observable of some kind (a file's
    name counted or not)."""
    return any(
        pattern.fullmatch(token) and _canonical(kind, token)
        for kind, pattern in _PATTERNS if file_names or kind != "file-name"
    )


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _wraps(text: str, start: int, end: int) -> bool:
    """Whether the line break ``text[start:end]`` cuts a token (a PDF wrapping a long
    hash, a table cell wrapping a domain) rather than ends one: two hashes or two
    domains on consecutive lines of a list are two tokens."""
    before = re.search(r"\S+$", text[max(0, start - 200):start])
    after = re.match(r"\S+", text[end:end + 200])
    if not before or not after:
        return False
    previous, following = before.group(0), after.group(0).rstrip(".,;:)]")
    # A host's port or its root ("evil[.]org:51640", "evil[.]com/") leaves it whole.
    following = re.sub(r"(?::\d{1,5})?/?$", "", following) or following
    if "://" in previous:
        # A line that starts with an address, a domain, a link or a date starts a token
        # ("...payload.dat\n185.224.133.57", ".../VidT6cErs\n2020-04-20 - mshare").
        if previous[-1] not in "/:.-_=?&" and (_complete(following, file_names=False) or _ISO_DATE.match(following)):
            return False
        return "://" not in following and bool(_URL_CONTINUATION.match(following))
    # A digest is whole: what follows it on the next line, unless hex, is another token
    # ("<sha256>\n.NET #1: <sha256>").
    if _HEX_RUN.fullmatch(previous) and len(previous) in _DIGEST_LENGTHS and not _HEX_RUN.match(following):
        return False
    # A sentence that ends on an identifier ("tracked as CVE-2025-2857.\nREAD MORE") is no
    # domain cut after its dot, even when the next word is a top-level domain.
    if previous[-1] == "." and following[:1].isupper() and _complete(previous[:-1]):
        return False
    # A word that ends a sentence, then a line that is an indicator ("used by
    # administrators.\ndirwinstat.com", "Response Team.\nhjkdnd[.]duckdns[.]org"): a
    # domain cut after its dot has a dot before the cut, or the line goes on.
    if previous[-1] == "." and not re.search(r"[.\[\](){}]", previous[:-1]) and _complete(following, file_names=False):
        return False
    if previous[-1] in "-/_" or (previous[-1] == "." and not _complete(previous[:-1])):
        return True
    if _complete(following):
        return False
    # A complete token in lower case, then a capitalised word ("evil.org\nThe latter",
    # "evil[.]org\nIP addresses"): two tokens, a wrap keeps the token's case.
    if _complete(previous) and previous[-1].islower() and following[:1].isupper():
        return False
    # Two hex runs that together make a digest's length are one wrapped digest.
    if _HEX_RUN.fullmatch(previous) and _HEX_RUN.fullmatch(following) and len(previous) + len(following) in _DIGEST_LENGTHS:
        return True
    line = text[text.rfind("\n", 0, start) + 1:start]
    return not (_complete(previous) and len(line) < 60 and len(following) > 3)


def _scan_copy(text: str) -> str:
    """*text* with every line break that cuts no token turned into a space, at the same
    offsets: the patterns may then cross only the line breaks that wrap a token."""
    if "\n" not in text:
        return text
    chars = list(text)
    for match in re.finditer(r"\r?\n", text):
        if not _wraps(text, match.start(), match.end()):
            chars[match.start():match.end()] = " " * (match.end() - match.start())
    return "".join(chars)


_HOSTNAME = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:[A-Za-z]{2,24}|xn--[A-Za-z0-9-]{1,59})")
_SCHEME = re.compile(r"[a-z*]{3,5}(?:://|\[://\]|\[:\]//)", re.IGNORECASE)


def _unwrapped(raw: str) -> str:
    """A URL without the bracket that closes around it ("[hxxp://evil[.]one/a.exe]"): a
    closing bracket is the URL's only when it closes one the URL opens ("http://[::1]/").
    A comma ends the host and the path: in a delimited listing
    ("hxxp://evil[.]one/Panel/,2016-06-18,<sha256>") it starts the next field, and only a
    query or a fragment carries one."""
    path = re.split(r"[?#]", raw, maxsplit=1)[0]
    if "," in path:
        raw = raw[:path.index(",")]
    # A backslash (an escaped string's quote, a Windows path) or a "%" that encodes nothing
    # ("...dat&&%windir%\\system32") is outside the URL: a URL holds neither.
    raw = re.split(r"\\(?!\.)|%(?![0-9A-Fa-f]{2})", raw, maxsplit=1)[0].rstrip("&")
    while raw.endswith("]") and refang(raw).count("[") < refang(raw).count("]"):
        raw = raw[:-1]
    return raw


def find_pattern_entities(text: str) -> list[PatternMatch]:
    """Every identifier and observable in *text*, longest first where two overlap
    (a domain inside a URL or an e-mail address is the URL's or the address's)."""
    found: list[PatternMatch] = []
    # A report that defangs its indicators leaves live only what it cites.
    defanging = bool(_DEFANGED.search(text))
    scan = _scan_copy(text)
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(scan):
            raw = _unwrapped(match.group(0)) if kind == "url" else match.group(0)
            value = _canonical(kind, raw)
            if not value or _out_of_context(
                kind, text[max(0, match.start() - 40):match.start()], text[match.end():match.end() + 3]
            ):
                continue
            end = match.start() + len(raw.rstrip(".,;:"))
            # A bare domain is left out: reports list their indicators as bare domains
            # while defanging only some of them.
            cited = kind in ("url", "email-addr") and (kind == "email-addr" or _SCHEME.match(raw) is not None) and (
                bool(_LINK_TARGET.search(text[max(0, match.start() - 12):match.start()]))
                or (defanging and not _DEFANGED.search(match.group(0)))
            )
            found.append(PatternMatch(kind, value, match.start(), end, cited))
    found.sort(key=lambda m: (m.start, -(m.end - m.start)))
    # Sorted by start, a match overlaps a kept one exactly when it starts before the
    # furthest end kept so far.
    kept: list[PatternMatch] = []
    reach = -1
    for match in found:
        if match.start < reach:
            continue
        kept.append(match)
        reach = max(reach, match.end)
    # A digest inside a URL ("virustotal.com/file/<sha256>/analysis") is also the file's.
    urls = [m for m in kept if m.kind == "url"]
    kept += [
        m for m in found
        if m.kind in _HASHES and m not in kept and any(u.start <= m.start and m.end <= u.end for u in urls)
    ]
    kept.sort(key=lambda m: (m.start, -(m.end - m.start)))
    return kept


__all__ = ["PatternMatch", "find_pattern_entities", "refang"]
