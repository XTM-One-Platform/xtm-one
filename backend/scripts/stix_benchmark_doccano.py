"""Benchmark helper: span-annotated ground truth (doccano JSONL) as the reference STIX bundle it describes.

Only the offline benchmark (``stix_extraction_benchmark.py``) reads doccano: the platform's
ground truth is a STIX bundle (an evaluation dataset entry's expected answer), and this
adapter is how the research datasets become one.

The research datasets (MAR, VOSC, ...) annotate character spans and links between spans.
An evaluation of a STIX extraction compares objects, so a document's annotation becomes
one object per annotated entity, whatever the number of its mentions (two spans
``WalkerShell`` and ``walkerShell`` are one malware), and one relationship per linked pair
of objects (the same link annotated on two mentions is one relationship). The guideline
choices the extraction follows apply to the reference too, so both sides are read under
one rule: a threat actor annotated as located somewhere originates from it.

Both label vocabularies of the datasets are read: the STIX-path one
(``Intrusion-Set``, ``Attack-Pattern.x_mitre_id``, ``File.hashes.SHA-256``) and the
flat one (``intrusion set name``, ``attack pattern mitre id``, ``sha 256``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime

from app.services.stix_extraction.bundle import ExtractedObject, ExtractedRelation, build_bundle
from app.services.stix_extraction.grounding import surface_key
from app.services.stix_extraction.iocs import refang
from app.services.stix_extraction.ontology import resolve_relation

_LABEL_KINDS: dict[str, str] = {
    # STIX-path vocabulary
    "intrusion-set": "intrusion-set",
    "threat-actor": "intrusion-set",
    "threat-actor-group": "intrusion-set",
    "malware": "malware",
    "tool": "tool",
    "software": "tool",
    "channel": "channel",
    "campaign": "campaign",
    "sector": "sector",
    "organization": "organization",
    "individual": "individual",
    "country": "country",
    "region": "region",
    "city": "city",
    "attack-pattern.x_mitre_id": "attack-pattern-id",
    "attack-pattern": "attack-pattern",
    "vulnerability": "cve",
    "vulnerability.name": "cve",
    "file.name": "file-name",
    "file.hashes.md5": "md5",
    "file.hashes.sha-1": "sha1",
    "file.hashes.sha-256": "sha256",
    "file.hashes.sha-512": "sha512",
    "domain-name.value": "domain-name",
    "ipv4-addr.value": "ipv4-addr",
    "ipv6-addr.value": "ipv6-addr",
    "url.value": "url",
    "email-addr.value": "email-addr",
    "windows-registry-key.key": "windows-registry-key",
    # flat vocabulary
    "intrusion set name": "intrusion-set",
    "threat actor": "intrusion-set",
    "threat actor group": "intrusion-set",
    "malware name": "malware",
    "campaign name": "campaign",
    "attack pattern mitre id": "attack-pattern-id",
    "attack pattern name": "attack-pattern",
    "vulnerability cve": "cve",
    "vulnerability name": "vulnerability",
    "file name": "file-name",
    "md5": "md5",
    "sha 1": "sha1",
    "sha 256": "sha256",
    "sha 512": "sha512",
    "domain name": "domain-name",
    "ipv4": "ipv4-addr",
    "ipv6": "ipv6-addr",
    "url": "url",
    "email address": "email-addr",
    "windows registry key": "windows-registry-key",
}

_VALUE_KINDS = frozenset(
    {"md5", "sha1", "sha256", "sha512", "domain-name", "ipv4-addr", "ipv6-addr", "url",
     "email-addr", "windows-registry-key", "cve", "attack-pattern-id"}
)


def label_kind(label: str) -> str | None:
    """The extraction kind of a dataset label, ``None`` for a label it does not extract."""
    return _LABEL_KINDS.get(label.strip().lower())


def _value(kind: str, surface: str) -> str:
    value = refang(surface).strip()
    if kind in ("md5", "sha1", "sha256", "sha512", "domain-name"):
        return value.lower()
    if kind in ("cve", "attack-pattern-id"):
        return value.upper()
    return value


def doccano_to_stix(document: dict, *, now: datetime | None = None) -> dict:
    """The reference STIX bundle of one doccano document (``text``, ``entities``,
    ``relations``), without a report (the envelope is not the knowledge)."""
    text = document["text"]
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    entity_group: dict[object, tuple[str, str]] = {}
    for entity in document.get("entities", []):
        kind = label_kind(str(entity.get("label", "")))
        if kind is None:
            continue
        surface = text[entity["start_offset"] : entity["end_offset"]]
        key = _value(kind, surface).casefold() if kind in _VALUE_KINDS else surface_key(surface)
        if not key:
            continue
        groups[(kind, key)].append(entity)
        entity_group[entity["id"]] = (kind, key)

    objects: list[ExtractedObject] = []
    refs: dict[tuple[str, str], str] = {}
    for index, ((kind, _key), members) in enumerate(sorted(groups.items()), start=1):
        raw = [text[m["start_offset"] : m["end_offset"]] for m in members]
        # A value (hash, domain...) cut by a line break is one token; a name is words.
        surfaces = [_value(kind, r) if kind in _VALUE_KINDS else " ".join(r.split()) for r in raw]
        name = Counter(surfaces).most_common(1)[0][0]
        ref = f"G{index}"
        refs[(kind, _key)] = ref
        objects.append(
            ExtractedObject(
                ref=ref,
                kind=kind,
                name=name,
                aliases=sorted(set(surfaces) - {name}) if kind not in _VALUE_KINDS else [],
                spans=[(m["start_offset"], m["end_offset"]) for m in members],
                value=name if kind in _VALUE_KINDS else "",
            )
        )
    kinds = {obj.ref: obj.kind for obj in objects}
    relations: list[ExtractedRelation] = []
    for relation in document.get("relations", []):
        source = refs.get(entity_group.get(relation.get("from_id")))  # type: ignore[arg-type]
        target = refs.get(entity_group.get(relation.get("to_id")))  # type: ignore[arg-type]
        verb = str(relation.get("type", "")).strip().lower()
        if not source or not target or source == target:
            continue
        resolved = resolve_relation(kinds[source], verb, kinds[target]) or verb
        relations.append(ExtractedRelation(source, resolved, target))
    # Case variants of one name are one object: no alias is the name in another case.
    for obj in objects:
        obj.aliases = [a for a in obj.aliases if surface_key(a) != surface_key(obj.name)]
    return build_bundle(
        objects,
        relations,
        document_name=str(document.get("id", "")),
        include_report=False,
        include_evidence=True,
        now=now or datetime(2026, 1, 1, tzinfo=UTC),
    )


__all__ = ["doccano_to_stix", "label_kind"]
