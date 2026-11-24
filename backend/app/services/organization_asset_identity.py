# SPDX-License-Identifier: Apache-2.0

"""One real thing across sources: identity matching, field provenance and precedence (#1178, S1).

The organization context reads the same asset from several places (an
endpoint manager, an EDR, OpenAEV, OpenCRQ, a CSV, a person). Rows were keyed
on ``source`` + ``external_id`` (#1134), so the same laptop showed twice and a
second source named like the first was skipped. This module is the pure rule
that turns N source records into canonical things, with no I/O and no model
call; the store of later slices persists what it answers.

1. **Identity** (:func:`match_records`): each record carries identifiers by
   kind. Strong kinds merge, in the order of :data:`DEFAULT_KEY_ORDER`
   (hardware serial, cloud resource id, code repository path, managed device
   id, MAC address, FQDN, normalised hostname), each normalised by :func:`normalize_identifier`
   (placeholder serials and null MAC addresses are dropped). A weak kind (an
   IP address) never merges by itself: it is answered as a possible match.
   Two records are never merged when they come from the same source with
   different ids, when both sides already hold a higher-ranked key and those
   keys differ, or when an administrator split them. An administrator's merge
   wins over the keys. Every canonical thing says why it was merged
   ("merged on serial") and the order of the records never changes the answer.
2. **Fields** (:func:`resolve_fields`): each field keeps its value per
   source; the value shown follows a per-field precedence of source classes
   (:data:`DEFAULT_PRECEDENCE`, customisable). A local field (an enrichment a
   person sets) is taken from hand sources first, then from the synced record
   the person enriched (a sync never sends it), so no sync overwrites it.
3. **Software** (:func:`merge_software`): one entry per vendor, product and
   version across sources, with its sources and first and last seen; an
   unknown version is ``unknown``, never empty.
4. **Sections** (:func:`dedupe_section`): asset groups and the name-keyed
   sections (tech stack, crown jewels, AI systems, PIRs, tracked
   organizations) use the same matching with their own identifiers.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.services.organization_context import HAND_SOURCES, PLATFORM_SOURCES

# ── Identifiers ───────────────────────────────────────────────────────

#: Strong identifier kinds, the most reliable first: a key earlier in the list
#: overrules a later one (two records with different serials never merge on a hostname).
DEFAULT_KEY_ORDER: tuple[str, ...] = (
    "serial",
    "cloud_resource_id",
    "repository",
    "device_id",
    "mac",
    "fqdn",
    "hostname",
)
#: Identifier kinds that never merge by themselves (shared, recycled, translated).
WEAK_KINDS: frozenset[str] = frozenset({"ip"})
IDENTIFIER_KINDS: tuple[str, ...] = (*DEFAULT_KEY_ORDER, *sorted(WEAK_KINDS))
#: Plain-language name of each kind, for the merge explanation.
KIND_LABELS: dict[str, str] = {
    "serial": "serial",
    "cloud_resource_id": "cloud resource id",
    "repository": "repository",
    "device_id": "device id",
    "mac": "MAC address",
    "fqdn": "FQDN",
    "hostname": "hostname",
    "ip": "IP address",
    "external_reference": "external reference",
    "name": "name",
    "cpe": "CPE",
    "product": "vendor and product",
    "platform_id": "platform id",
}

#: Serial numbers firmware writes when none was set: they name no machine.
_PLACEHOLDER_SERIALS: frozenset[str] = frozenset(
    {
        "",
        "0",
        "00000000",
        "0000000000",
        "1234567890",
        "123456789",
        "NA",
        "N/A",
        "NONE",
        "NULL",
        "UNKNOWN",
        "DEFAULT",
        "DEFAULTSTRING",
        "TOBEFILLEDBYOEM",
        "SYSTEMSERIALNUMBER",
        "CHASSISSERIALNUMBER",
        "NOTSPECIFIED",
        "NOTAPPLICABLE",
        "INVALID",
    }
)
_HEX = re.compile(r"^[0-9a-f]{12}$")
_SPACES = re.compile(r"[\s\-_.]+")
_HOST_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
#: Apostrophes of every kind: a device name keeps them, the hostname derived from it drops them
#: (macOS: "Amani’s MacBook Pro" -> LocalHostName "Amanis-MacBook-Pro").
_APOSTROPHES = re.compile("['\u2018\u2019\u02bc\u0060\u00b4]")
#: Blanks and underscores, which a hostname spells as a hyphen.
_HOST_BLANKS = re.compile(r"[\s_]+")


def _serial(value: str) -> str:
    compact = _SPACES.sub("", value).upper()
    return "" if compact in _PLACEHOLDER_SERIALS or len(set(compact)) == 1 else compact


#: MAC prefixes of virtual interfaces (container bridges, hypervisors): the same value sits on many
#: machines, so it never names one and never merges two (Docker, VMware, VirtualBox, Hyper-V,
#: QEMU/KVM, Parallels, Xen).
_VIRTUAL_MAC_PREFIXES: tuple[str, ...] = (
    "0242",
    "005056",
    "000c29",
    "000569",
    "001c14",
    "080027",
    "0a0027",
    "00155d",
    "525400",
    "001c42",
    "00163e",
)


def _mac(value: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if not _HEX.match(digits) or digits in ("000000000000", "ffffffffffff"):
        return ""
    if digits.startswith(_VIRTUAL_MAC_PREFIXES):
        return ""
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def _is_ip_shaped(name: str) -> bool:
    """An address, or a dotted value whose last label is all digits (RFC 3696: never a top-level label)."""
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        return "." in name and name.rsplit(".", 1)[-1].isdigit()
    return True


def _fqdn(value: str) -> str:
    name = value.strip().lower().rstrip(".")
    if "." not in name or _is_ip_shaped(name):
        return ""
    return name if all(_HOST_LABEL.match(part) for part in name.split(".")) else ""


def _hostname(value: str) -> str:
    name = value.strip().lower().rstrip(".")
    if "\\" in name:  # DOMAIN\HOST
        name = name.rsplit("\\", 1)[1]
    if _is_ip_shaped(name):  # an address is the weak ip key, never a hostname
        return ""
    name = name.split(".", 1)[0]
    # A device name ("Amani’s MacBook Pro") and the hostname the OS derives from it
    # ("Amanis-MacBook-Pro") are one key: apostrophes dropped, blanks and underscores as hyphens.
    name = re.sub(r"-{2,}", "-", _HOST_BLANKS.sub("-", _APOSTROPHES.sub("", name))).strip("-")
    return name if _HOST_LABEL.match(name) and name not in ("localhost", "unknown") else ""


#: The host a bare ``owner/repo`` path names: the only one guessed, and only for two segments.
DEFAULT_REPOSITORY_HOST = "github.com"
_REPO_SEGMENT = re.compile(r"^[a-z0-9_.][a-z0-9_.-]{0,254}$")
_SSH_REMOTE = re.compile(r"^[a-z0-9_.-]+@([a-z0-9.-]+):(.+)$")


def _repository(value: str) -> str:
    """A code repository as ``host/owner/repo`` (GitLab ``host/group/subgroup/project``), or ``""``.

    Reads a web URL, a clone URL (``https``, ``ssh://``, ``git@host:path``) or a bare
    ``owner/repo``: lower-case, scheme, credentials, port, ``www.``, ``.git`` and the trailing
    ``/`` dropped, and a web sub-page (``/tree/main``, GitLab's ``/-/...``) cut. A bare path is
    read on :data:`DEFAULT_REPOSITORY_HOST` only with exactly two segments, so a GitLab
    ``group/subgroup/project`` without its host never takes a guessed one.
    """
    text = value.strip().lower()
    host = ""
    if ssh := _SSH_REMOTE.match(text):
        host, text = ssh.group(1), ssh.group(2)
    elif "://" in text:
        text = text.split("://", 1)[1]
        authority, _, text = text.partition("/")
        host = authority.rsplit("@", 1)[-1].split(":", 1)[0]
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [p for p in text.split("/") if p]
    if not host and parts and "." in parts[0] and len(parts) >= 3:
        host, parts = parts[0], parts[1:]  # github.com/owner/repo without a scheme
    if "-" in parts:  # GitLab web sub-page: /group/project/-/tree/main
        parts = parts[: parts.index("-")]
    host = host.removeprefix("www.")
    if not host:
        if len(parts) != 2:
            return ""
        host = DEFAULT_REPOSITORY_HOST
    elif host == DEFAULT_REPOSITORY_HOST:
        parts = parts[:2]  # github.com/owner/repo/tree/main, /pulls, ...
    if parts:
        parts[-1] = parts[-1].removesuffix(".git")
    if len(parts) < 2 or not all(_REPO_SEGMENT.match(p) and p not in (".", "..") for p in parts):
        return ""
    if not _fqdn(host) and host != "localhost":
        return ""
    return "/".join([host, *parts])


def _ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return ""
    return "" if address.is_loopback or address.is_unspecified or address.is_link_local else address.compressed


def _plain(value: str) -> str:
    return value.strip().lower()


_NORMALIZERS: dict[str, Callable[[str], str]] = {
    "serial": _serial,
    "mac": _mac,
    "fqdn": _fqdn,
    "hostname": _hostname,
    "ip": _ip,
    "repository": _repository,
}


def normalize_identifier(kind: str, value: Any) -> str:
    """*value* of identifier *kind* in its comparable form, or ``""`` when it names nothing."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return _NORMALIZERS.get(kind, _plain)(text)[:300]


# ── Records ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RecordRef:
    """A record's address: the source it comes from and its id there."""

    source: str
    external_id: str

    def label(self) -> str:
        return f"{self.source}:{self.external_id}"


@dataclass
class SourceRecord:
    """What one source says about one thing.

    ``identifiers`` maps a kind (:data:`IDENTIFIER_KINDS`, or a section's own
    kinds) to its raw values; ``fields`` holds the attributes as the source
    sends them; ``software`` the software it reports on the thing.
    """

    source: str
    external_id: str
    identifiers: dict[str, list[str]] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    software: list[dict[str, Any]] = field(default_factory=list)
    last_seen: datetime | None = None

    def __post_init__(self) -> None:
        self.source = (self.source or "").strip().lower() or "manual"
        self.external_id = str(self.external_id or "").strip()

    @property
    def ref(self) -> RecordRef:
        return RecordRef(self.source, self.external_id)

    def keys(self, kind: str) -> set[str]:
        values = self.identifiers.get(kind) or []
        if isinstance(values, str):
            values = [values]
        return {key for key in (normalize_identifier(kind, v) for v in values) if key}


@dataclass
class Canonical:
    """One real thing: its records, why they were merged, and the weak matches left apart."""

    id: str
    records: list[SourceRecord]
    #: How each record after the first joined: ``{"record", "kind", "value"}``, or kind ``manual``.
    merged_on: list[dict[str, str]] = field(default_factory=list)
    #: Weak-key matches with other canonical things, never merged: ``{"kind", "value", "with"}``.
    possible_matches: list[dict[str, str]] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return sorted({r.source for r in self.records})

    def explanation(self) -> str:
        """``merged on serial`` / ``merged on serial and hostname`` / ``merged by an administrator`` / ``one source``."""
        kinds = list(dict.fromkeys(m["kind"] for m in self.merged_on))
        if not kinds:
            return "one source"
        keys = [KIND_LABELS.get(k, k) for k in kinds if k != "manual"]
        manual = "manual" in kinds
        if not keys:
            return "merged by an administrator"
        return "merged on " + " and ".join(keys) + (" and by an administrator" if manual else "")


@dataclass(frozen=True)
class Decisions:
    """An administrator's merge and split decisions, applied over the keys."""

    #: Groups of records that are one thing whatever their keys say.
    merges: tuple[frozenset[RecordRef], ...] = ()
    #: Pairs of records that are never one thing.
    splits: frozenset[frozenset[RecordRef]] = frozenset()


def canonical_id(refs: Iterable[RecordRef]) -> str:
    """A stable id for a set of records: the same records give the same id, whatever their order."""
    first = min(refs, key=lambda r: (r.source, r.external_id))
    return hashlib.sha256(first.label().encode()).hexdigest()[:32]


class _Groups:
    """Union-find over record indexes, each group knowing its keys, sources and members."""

    def __init__(self, records: Sequence[SourceRecord], kinds: Sequence[str]) -> None:
        self.parent = list(range(len(records)))
        self.members: dict[int, list[int]] = {i: [i] for i in range(len(records))}
        self.keys: dict[int, dict[str, set[str]]] = {
            i: {kind: r.keys(kind) for kind in kinds} for i, r in enumerate(records)
        }
        self.ids: dict[int, dict[str, set[str]]] = {i: {r.source: {r.external_id}} for i, r in enumerate(records)}

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> int:
        ra, rb = sorted((self.find(a), self.find(b)))
        if ra == rb:
            return ra
        self.parent[rb] = ra
        self.members[ra] += self.members.pop(rb)
        for kind, values in self.keys.pop(rb).items():
            self.keys[ra][kind] |= values
        for source, ids in self.ids.pop(rb).items():
            self.ids[ra].setdefault(source, set()).update(ids)
        return ra


def match_records(
    records: Sequence[SourceRecord],
    *,
    key_order: Sequence[str] = DEFAULT_KEY_ORDER,
    weak_kinds: Iterable[str] = WEAK_KINDS,
    decisions: Decisions | None = None,
) -> list[Canonical]:
    """Group *records* into canonical things (deterministic, explained; see the module docstring).

    *key_order* is the configured list of strong kinds, most reliable first;
    a kind in *weak_kinds* only produces possible matches.
    """
    weak = [k for k in weak_kinds if k not in key_order]
    ordered = sorted(range(len(records)), key=lambda i: (records[i].source, records[i].external_id))
    groups = _Groups(records, [*key_order, *weak])
    index_of = {records[i].ref: i for i in ordered}
    position = {i: n for n, i in enumerate(ordered)}
    decisions = decisions or Decisions()
    #: Merge events in the order they happened; each names the first record of the group that joined.
    events: list[tuple[int, dict[str, str]]] = []

    def first(root: int) -> int:
        return min(groups.members[root], key=lambda m: position[m])

    def join(a: int, b: int, kind: str, value: str) -> None:
        later = max(groups.find(a), groups.find(b), key=lambda root: position[first(root)])
        joined = first(later)
        groups.union(a, b)
        events.append((joined, {"record": records[joined].ref.label(), "kind": kind, "value": value}))

    def split(a: int, b: int) -> bool:
        ra, rb = groups.find(a), groups.find(b)
        left = [records[i].ref for i in groups.members[ra]]
        right = [records[i].ref for i in groups.members[rb]]
        return any(frozenset((x, y)) in decisions.splits for x in left for y in right)

    def conflict(a: int, b: int, rank: int) -> bool:
        ra, rb = groups.find(a), groups.find(b)
        for source, ids in groups.ids[ra].items():
            other = groups.ids[rb].get(source)
            if other and source not in HAND_SOURCES and not (ids & other):
                return True  # one source, two different ids: two things
        for kind in key_order[:rank]:
            mine, theirs = groups.keys[ra][kind], groups.keys[rb][kind]
            if mine and theirs and not (mine & theirs):
                return True  # a more reliable key says they differ
        return split(a, b)

    for group in decisions.merges:
        present = sorted(index_of[ref] for ref in group if ref in index_of)
        for i in present[1:]:
            if groups.find(i) != groups.find(present[0]):
                join(present[0], i, "manual", "")

    for rank, kind in enumerate(key_order):
        owner: dict[str, int] = {}
        for i in ordered:
            for value in sorted(records[i].keys(kind)):
                j = owner.setdefault(value, i)
                if j == i or groups.find(i) == groups.find(j) or conflict(i, j, rank):
                    continue
                join(i, j, kind, value)

    roots = sorted({groups.find(i) for i in range(len(records))})
    canon: dict[int, Canonical] = {}
    for root in roots:
        members = sorted(groups.members[root], key=lambda m: position[m])
        mine = set(members)
        canon[root] = Canonical(
            id=canonical_id(records[m].ref for m in members),
            records=[records[m] for m in members],
            merged_on=[event for joined, event in events if joined in mine],
        )

    for kind in weak:
        seen: dict[str, int] = {}
        for i in ordered:
            for value in sorted(records[i].keys(kind)):
                j = seen.setdefault(value, i)
                ra, rb = groups.find(i), groups.find(j)
                if ra == rb:
                    continue
                for here, there in ((ra, rb), (rb, ra)):
                    hint = {"kind": kind, "value": value, "with": canon[there].id}
                    if hint not in canon[here].possible_matches:
                        canon[here].possible_matches.append(hint)

    return sorted(canon.values(), key=lambda c: (c.records[0].source, c.records[0].external_id))


# ── Fields: provenance and precedence ─────────────────────────────────

#: The class a source falls in when nothing names it: a person, an agent, a suite platform, else ``other``.
HAND_CLASS = "hand"
AGENT_CLASS = "agent"
OTHER_CLASS = "other"
#: Default precedence per field, by source class then source name; ``*`` is any other source.
#: ``mdm`` and ``edr`` are the classes an endpoint manager or an EDR source is registered under.
DEFAULT_PRECEDENCE: dict[str, tuple[str, ...]] = {
    "os_family": ("mdm", "edr", "openaev", HAND_CLASS, "*"),
    "os_version": ("mdm", "edr", "openaev", HAND_CLASS, "*"),
    "os_build": ("mdm", "edr", "openaev", HAND_CLASS, "*"),
    "software": ("mdm", "edr", "openaev", "*"),
    "criticality": ("opencrq", HAND_CLASS, "openaev", "opencti", "*"),
    "internet_facing": ("openaev", "opencrq", HAND_CLASS, "*"),
    "category": (HAND_CLASS, "openaev", "opencrq", "opencti", "mdm", "edr", "*"),
    "subcategory": (HAND_CLASS, "openaev", "opencrq", "opencti", "mdm", "edr", "*"),
}
#: The class each endpoint source is registered under (#1251): what ``DEFAULT_PRECEDENCE`` ranks.
BUILTIN_SOURCE_CLASSES: dict[str, str] = {
    "intune": "mdm",
    "tanium": "mdm",
    "defender": "edr",
    "crowdstrike": "edr",
    "servicenow": "cmdb",
    "github": "scm",
    "gitlab": "scm",
}
#: Precedence of a field not listed above: a person first, then the suite, then anything.
FALLBACK_PRECEDENCE: tuple[str, ...] = (HAND_CLASS, "openaev", "opencrq", "opencti", "*")
#: Fields a person sets on a synced thing (local enrichment, #1134): a hand record's value wins; without one, the
#: value kept on the synced record is shown (#1367: the person's edit of a synced asset lives there).
DEFAULT_LOCAL_FIELDS: frozenset[str] = frozenset({"environment", "group", "description", "business_service"})
#: Values that say nothing: never shown over a value another source knows.
_EMPTY: tuple[Any, ...] = (None, "", "unknown", [], {})


def source_class(source: str, classes: Mapping[str, str] | None = None) -> str:
    """The class *source* falls in: the configured one, else hand, agent, the platform's own name, else other."""
    source = (source or "").strip().lower()
    if classes and source in classes:
        return classes[source]
    if source in BUILTIN_SOURCE_CLASSES:
        return BUILTIN_SOURCE_CLASSES[source]
    if source in HAND_SOURCES:
        return HAND_CLASS
    if source.startswith("agent:"):
        return AGENT_CLASS
    if source in PLATFORM_SOURCES:
        return source
    return OTHER_CLASS


def _rank(source: str, order: Sequence[str], classes: Mapping[str, str] | None) -> int:
    cls = source_class(source, classes)
    for position, entry in enumerate(order):
        if entry in (source, cls):
            return position
    return order.index("*") if "*" in order else len(order)


@dataclass
class FieldValue:
    """A field of a canonical thing: the value shown, who set it, and every source's value."""

    value: Any
    source: str
    external_id: str
    candidates: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "external_id": self.external_id,
            "candidates": self.candidates,
        }


def resolve_fields(
    records: Sequence[SourceRecord],
    *,
    precedence: Mapping[str, Sequence[str]] | None = None,
    source_classes: Mapping[str, str] | None = None,
    local_fields: Iterable[str] = DEFAULT_LOCAL_FIELDS,
) -> dict[str, FieldValue]:
    """Each field of *records* with the value its precedence picks and every source's value.

    *precedence* overrides :data:`DEFAULT_PRECEDENCE` field by field. A value
    that says nothing (empty, ``unknown``) never wins over one that does; a
    field of *local_fields* is taken from hand sources first; when no hand
    record carries a value, from the other records, where a person's local
    enrichment of a synced row is kept (#1134, #1367). Ties go to the
    most recently seen record, then to the source name, so the answer never
    depends on the records' order.
    """
    table = {**DEFAULT_PRECEDENCE, **(precedence or {})}
    local = set(local_fields)
    names = sorted({name for r in records for name in r.fields})
    resolved: dict[str, FieldValue] = {}
    for name in names:
        order = tuple(table.get(name, FALLBACK_PRECEDENCE))
        offers = [r for r in records if name in r.fields]
        if name in local:
            hand = [r for r in offers if source_class(r.source, source_classes) == HAND_CLASS]
            if any(r.fields[name] not in _EMPTY for r in hand):
                offers = hand
        candidates = [
            {"source": r.source, "external_id": r.external_id, "value": r.fields[name]}
            for r in sorted(offers, key=lambda r: (r.source, r.external_id))
        ]
        known = [r for r in offers if r.fields[name] not in _EMPTY]
        pool = known or offers
        if not pool:
            continue
        best = min(
            pool,
            key=lambda r: (
                _rank(r.source, order, source_classes),
                -(r.last_seen.timestamp() if r.last_seen else 0.0),
                r.source,
                r.external_id,
            ),
        )
        resolved[name] = FieldValue(best.fields[name], best.source, best.external_id, candidates)
    return resolved


# ── Software ──────────────────────────────────────────────────────────

UNKNOWN_VERSION = "unknown"


def _spelling(current: str, other: str) -> str:
    """Of two spellings of one name, the cased one ("Chrome" over "chrome"), else the first in order."""
    if not current:
        return other
    if not other:
        return current
    return min(current, other, key=lambda s: (-sum(c.isupper() for c in s), s))


def _software_key(entry: Mapping[str, Any]) -> tuple[str, str, str] | None:
    product = _SPACES.sub(" ", str(entry.get("product") or "")).strip().casefold()
    if not product:
        return None
    vendor = _SPACES.sub(" ", str(entry.get("vendor") or "")).strip().casefold()
    version = str(entry.get("version") or "").strip() or UNKNOWN_VERSION
    return vendor, product, version.casefold()


def merge_software(records: Sequence[SourceRecord]) -> list[dict[str, Any]]:
    """The software of *records*: one entry per vendor, product and version, its sources and seen dates.

    The first source (by name) supplies the display spelling; CPE, PURL,
    install date and the bundling component (``bundled_in``, an SBOM's nesting)
    are filled from the first source that knows them; ``depends_on`` (``product
    version`` labels, an SBOM's dependencies) is the union of the sources; ``first_seen``
    is the earliest and ``last_seen`` the latest any source reported.
    """
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in sorted(records, key=lambda r: (r.source, r.external_id)):
        for raw in record.software:
            key = _software_key(raw)
            if key is None:
                continue
            entry = merged.get(key)
            if entry is None:
                entry = merged[key] = {
                    "vendor": str(raw.get("vendor") or "").strip(),
                    "product": str(raw.get("product") or "").strip(),
                    "version": str(raw.get("version") or "").strip() or UNKNOWN_VERSION,
                    "cpe": "",
                    "purl": "",
                    "install_date": None,
                    "bundled_in": "",
                    "type": "",
                    "depends_on": [],
                    "first_seen": None,
                    "last_seen": None,
                    "sources": [],
                }
            for attr in ("vendor", "product"):
                entry[attr] = _spelling(entry[attr], str(raw.get(attr) or "").strip())
            for attr in ("cpe", "purl"):
                if not entry[attr] and raw.get(attr):
                    entry[attr] = str(raw[attr]).strip()
            if entry["install_date"] is None and raw.get("install_date"):
                entry["install_date"] = raw["install_date"]
            if not entry["bundled_in"] and raw.get("bundled_in"):
                entry["bundled_in"] = str(raw["bundled_in"]).strip()
            # A declared type (an SBOM's component type) other than the default wins over none.
            if raw.get("type") and entry["type"] in ("", "application"):
                entry["type"] = str(raw["type"]).strip()
            for target in raw.get("depends_on") or ():
                label = str(target or "").strip()
                if label and label not in entry["depends_on"]:
                    entry["depends_on"].append(label)
            for attr, pick in (("first_seen", min), ("last_seen", max)):
                seen = raw.get(attr) or record.last_seen
                if seen is not None:
                    entry[attr] = seen if entry[attr] is None else pick(entry[attr], seen)
            if record.source not in entry["sources"]:
                entry["sources"].append(record.source)
    return sorted(merged.values(), key=lambda e: (e["vendor"].casefold(), e["product"].casefold(), e["version"]))


# ── CPE resolution of a software ─────────────────────────────────────

#: How sure a CPE is: reported by a source, found by a dictionary lookup, set by an administrator.
CPE_CONFIDENCES: tuple[str, ...] = ("override", "reported", "high", "medium", "low", "unresolved")

#: A CPE lookup (vendor, product, version) -> (cpe, confidence) or None; the store plugs a dictionary in.
CpeResolver = Callable[[str, str, str], tuple[str, str] | None]


def _cpe_with_version(cpe: str, version: str) -> str:
    parts = cpe.split(":")
    if len(parts) >= 6 and parts[0] == "cpe" and version and version != UNKNOWN_VERSION:
        parts[5] = version
    return ":".join(parts)


def resolve_cpe(
    vendor: str,
    product: str,
    version: str,
    *,
    reported: Iterable[str] = (),
    override: str = "",
    resolver: CpeResolver | None = None,
) -> dict[str, str]:
    """The CPE of a product and where it comes from; never guessed.

    An administrator's *override* wins (kept across syncs, its provenance
    ``override``); then a CPE a source *reported* (``reported``); then the
    *resolver* (a dictionary lookup on normalised vendor and product, its
    own confidence); else ``unresolved`` with an empty CPE.
    """
    if override.strip():
        return {"cpe": override.strip(), "confidence": "override"}
    for cpe in sorted({c.strip() for c in reported if c and c.strip()}):
        if _cpe_vendor_product(cpe):
            return {"cpe": cpe, "confidence": "reported"}
    if resolver is not None and product.strip():
        found = resolver(vendor.strip(), product.strip(), version.strip())
        if found and found[0] and found[1] in CPE_CONFIDENCES:
            return {"cpe": _cpe_with_version(found[0], version), "confidence": found[1]}
    return {"cpe": "", "confidence": "unresolved"}


# ── Sections ──────────────────────────────────────────────────────────


def _cpe_vendor_product(cpe: str) -> str:
    parts = cpe.strip().lower().split(":")
    if len(parts) >= 5 and parts[0] == "cpe" and parts[1] == "2.3":
        return f"{parts[3]}:{parts[4]}"
    return ""


def _name_ids(row: Mapping[str, Any], *, name_key: str = "name") -> dict[str, list[str]]:
    return {"name": [str(row.get(name_key) or "")]}


def _tech_ids(row: Mapping[str, Any]) -> dict[str, list[str]]:
    vendor = str(row.get("vendor") or "").strip()
    product = str(row.get("product") or "").strip()
    return {
        "cpe": [_cpe_vendor_product(str(row.get("cpe") or ""))],
        "product": [f"{vendor} {product}".strip()] if vendor else [],
        "name": [product],
    }


def _platform_ids(legacy: str) -> Callable[[Mapping[str, Any]], dict[str, list[str]]]:
    def ids(row: Mapping[str, Any]) -> dict[str, list[str]]:
        return {"platform_id": [str(row.get(legacy) or "")], "name": [str(row.get("name") or "")]}

    return ids


def _group_ids(row: Mapping[str, Any]) -> dict[str, list[str]]:
    return {"external_reference": [str(row.get("external_reference") or "")], "name": [str(row.get("name") or "")]}


def is_code_repository(row: Mapping[str, Any]) -> bool:
    """Whether an asset row is a code repository (category ``source_code``, subcategory ``code_repository``)."""
    return (
        str(row.get("category") or "").strip().lower() == "source_code"
        and str(row.get("subcategory") or "").strip().lower() == "code_repository"
    )


def _repository_ids(row: Mapping[str, Any]) -> list[str]:
    """The repository a code repository row names: its URL, else its name.

    GitHub, GitLab, OpenAEV, a CSV and a hand row spell the same repository differently
    (``https://github.com/Owner/Repo``, ``Owner/Repo``): each reads as one ``repository`` key.
    A row of any other category never carries one, so a host named ``a/b`` stays a host.
    """
    if not is_code_repository(row):
        return []
    for field_name in ("url", "name"):
        values = [v for v in _as_list(row.get(field_name)) if _repository(v)]
        if values:
            return values
    return []


def _asset_ids(row: Mapping[str, Any]) -> dict[str, list[str]]:
    # An empty value names nothing: a row stored with ``hostname=""`` still takes its name as hostname,
    # else its record carries no key and never merges with the same machine of another source.
    ids = {kind: [v for v in _as_list(row.get(kind)) if v.strip()] for kind in IDENTIFIER_KINDS}
    ids["repository"] = _repository_ids(row)
    if not ids["hostname"] and row.get("name"):
        ids["hostname"] = [str(row["name"])]
    return ids


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


#: section -> (identifier kinds in order, how a row gives its identifiers, weak kinds).
SECTION_IDENTITY: dict[
    str, tuple[tuple[str, ...], Callable[[Mapping[str, Any]], dict[str, list[str]]], frozenset[str]]
] = {
    "assets": (DEFAULT_KEY_ORDER, _asset_ids, WEAK_KINDS),
    "asset_groups": (("external_reference", "name"), _group_ids, frozenset()),
    "tech_stack": (("cpe", "product", "name"), _tech_ids, frozenset()),
    "crown_jewels": (("platform_id", "name"), _platform_ids("opencrq_asset_id"), frozenset()),
    "pirs": (("platform_id", "name"), _platform_ids("id"), frozenset()),
    "tracked_organizations": (("name",), _name_ids, frozenset()),
}


def section_record(section: str, row: Mapping[str, Any], *, index: int = 0) -> SourceRecord:
    """A profile row of *section* as a :class:`SourceRecord` (identifiers per :data:`SECTION_IDENTITY`).

    A row with no ``external_id`` (typed by hand) is addressed by its position.
    """
    if section not in SECTION_IDENTITY:
        raise ValueError(f"section must be one of {', '.join(SECTION_IDENTITY)}")
    _kinds, ids, _weak = SECTION_IDENTITY[section]
    source = str(row.get("source") or "manual")
    external_id = str(row.get("external_id") or "") or f"row-{index}"
    software = [dict(s) for s in row.get("software") or [] if isinstance(s, Mapping)]
    fields = {k: v for k, v in row.items() if k not in ("source", "external_id", "sync_id", "software")}
    return SourceRecord(
        source=source,
        external_id=external_id,
        identifiers=ids(row),
        fields=fields,
        software=software,
        last_seen=_row_last_seen(row.get("last_seen")),
    )


def _row_last_seen(value: Any) -> datetime | None:
    """A row's ``last_seen`` (ISO 8601, #1134 S4) as an aware datetime, ``None`` when empty or unreadable."""
    if not value or not isinstance(value, str):
        return None
    try:
        seen = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return seen.replace(tzinfo=UTC) if seen.tzinfo is None else seen


def dedupe_section(
    section: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    key_order: Sequence[str] | None = None,
    decisions: Decisions | None = None,
) -> list[Canonical]:
    """The rows of *section* grouped into real things, with the same rules as assets."""
    kinds, _ids, weak = SECTION_IDENTITY.get(section) or (None, None, frozenset())
    if kinds is None:
        raise ValueError(f"section must be one of {', '.join(SECTION_IDENTITY)}")
    records = [section_record(section, row, index=i) for i, row in enumerate(rows)]
    return match_records(records, key_order=tuple(key_order or kinds), weak_kinds=weak, decisions=decisions)
