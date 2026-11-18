"""Benchmark helper: the AZERG dataset (QCRI, Apache-2.0) as STIX references.

AZERG (RAID 2025, arXiv 2507.16576) annotates CTI report passages with STIX 2.1 entity and
relationship labels, by name, without offsets:
``{paragraph, entities: [{name, type}], relations: [{source_ref, target_ref,
relationship_type}]}``. Each passage becomes ``{"id", "text", "reference"}``, the
reference a STIX bundle built the way the extraction builds its own, so both are read
under one rule:

- types: THREAT_ACTOR → intrusion set, MALWARE, TOOL, IDENTITY → organization (AZERG does
  not tell a sector from an organization), LOCATION → country (nor a country from a
  region; see the ``coarse`` profile of the benchmark), ATTACK_PATTERN → ATT&CK id when it
  is one, VULNERABILITY → CVE when it is one, CAMPAIGN, INFRASTRUCTURE, INDICATOR → the
  observable its value is (hash, IP, domain, URL, e-mail, file name...);
  COURSE_OF_ACTION and an indicator that is no observable are left out;
- relationships: those the extraction produces ("is attributed to" and "use" read as
  such), through the same relationship matrix and OpenCTI schema, so an observable that
  indicates a malware becomes an indicator based on it, as in the extraction's bundle;
  others (has, mitigates, compromises, hosts, consists-of) are left out. ``--core`` keeps
  the six of the first benchmark (uses, targets, exploits, located-at, originates-from,
  attributed-to), for comparison with earlier runs.

Download (from ``backend/``): ``huggingface.co/datasets/QCRI/AZERG-Dataset/resolve/main/
consolidated/test.json``; the data stays out of this repository.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.stix_extraction.bundle import ExtractedObject, ExtractedRelation, build_bundle
from app.services.stix_extraction.iocs import find_pattern_entities
from app.services.stix_extraction.ontology import resolve_triple

_TYPES = {
    "THREAT_ACTOR": "intrusion-set",
    "MALWARE": "malware",
    "TOOL": "tool",
    "IDENTITY": "organization",
    "LOCATION": "country",
    "CAMPAIGN": "campaign",
    "INFRASTRUCTURE": "infrastructure",
}
_CORE = {
    "uses": "uses",
    "use": "uses",
    "targets": "targets",
    "exploits": "exploits",
    "located-at": "located-at",
    "originates-from": "originates-from",
    "attributed-to": "attributed-to",
    "is attributed to": "attributed-to",
}
_EXTENDED = {
    **_CORE,
    "authored-by": "attributed-to",
    **{verb: verb for verb in (
        "indicates", "variant-of", "communicates-with", "beacons-to", "exfiltrates-to", "drops", "downloads",
        "resolves-to", "has",
    )},
}
_RELATIONS = _EXTENDED
_TECHNIQUE = re.compile(r"^T\d{4}(?:\.\d{3})?$", re.IGNORECASE)
_CVE = re.compile(r"^CVE-\d{4}-\d{4,7}$", re.IGNORECASE)


def _kind_and_value(name: str, label: str) -> tuple[str, str] | None:
    if label == "ATTACK_PATTERN":
        return ("attack-pattern-id", name.upper()) if _TECHNIQUE.match(name) else ("attack-pattern", name)
    if label == "VULNERABILITY":
        return ("cve", name.upper()) if _CVE.match(name) else ("vulnerability", name)
    if label == "INDICATOR":
        matches = find_pattern_entities(name)
        if len(matches) == 1 and matches[0].end - matches[0].start >= len(name.strip()) - 1:
            return matches[0].kind, matches[0].value
        return None
    kind = _TYPES.get(label)
    return (kind, name) if kind else None


def passage_reference(record: dict) -> tuple[dict, dict[str, int]]:
    """The reference bundle of one AZERG passage, and what was left out."""
    objects: dict[str, ExtractedObject] = {}
    left_out = {"entities": 0, "relations": 0}
    for entity in record.get("entities", []):
        resolved = _kind_and_value(str(entity.get("name", "")).strip(), str(entity.get("type", "")))
        if resolved is None:
            left_out["entities"] += 1
            continue
        kind, value = resolved
        key = f"{kind}|{value.casefold()}"
        if key not in objects:
            objects[key] = ExtractedObject(
                ref=f"A{len(objects) + 1}", kind=kind, name=value,
                value=value if kind not in _TYPES.values() and kind not in ("attack-pattern", "vulnerability") else "",
            )
    by_name = {}
    for key, obj in objects.items():
        by_name.setdefault(key.split("|", 1)[1], obj)
    relations = []
    for relation in record.get("relations", []):
        verb = _RELATIONS.get(str(relation.get("relationship_type", "")).strip().lower())
        source = by_name.get(str(relation.get("source_ref", "")).strip().casefold())
        target = by_name.get(str(relation.get("target_ref", "")).strip().casefold())
        if not verb or source is None or target is None or source is target:
            left_out["relations"] += 1
            continue
        resolved = resolve_triple(source.kind, verb, target.kind)
        if resolved is None:
            left_out["relations"] += 1
            continue
        resolved_verb, swapped = resolved
        source, target = (target, source) if swapped else (source, target)
        relations.append(ExtractedRelation(source.ref, resolved_verb, target.ref))
    bundle = build_bundle(
        list(objects.values()), relations, document_name="", include_report=False,
        include_evidence=False, now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return bundle, left_out


def main() -> None:
    global _RELATIONS
    arguments = [a for a in sys.argv[1:] if a != "--core"]
    if "--core" in sys.argv:
        _RELATIONS = _CORE
    source, out = Path(arguments[0]), Path(arguments[1])
    records = json.loads(source.read_text())
    totals = {"entities": 0, "relations": 0}
    with out.open("w") as handle:
        for record in records:
            reference, left_out = passage_reference(record)
            for key in totals:
                totals[key] += left_out[key]
            handle.write(json.dumps({"id": record["record_id"], "text": record["paragraph"], "reference": reference}) + "\n")
    print(f"{len(records)} passages; left out: {totals}")


if __name__ == "__main__":
    main()
