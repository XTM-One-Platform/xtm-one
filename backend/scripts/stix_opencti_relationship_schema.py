"""Regenerate ``tests/fixtures/extraction/opencti_relationship_schema.json``: the
relationships OpenCTI's schema accepts between the kinds the STIX extraction emits.

The extraction's ontology (``app/services/stix_extraction/ontology.py``) must only emit
relationships OpenCTI imports: its connectors drop any other without a word. The test
``tests/test_stix_extraction_ontology_conformance.py`` checks every relationship the
ontology can produce against this snapshot. Refresh it when OpenCTI's schema changes::

    git clone --depth 1 --filter=blob:none --sparse https://github.com/OpenCTI-Platform/opencti.git /tmp/opencti
    git -C /tmp/opencti sparse-checkout set --skip-checks \\
        opencti-platform/opencti-graphql/src/modules opencti-platform/opencti-graphql/src/database/stix.ts
    uv run --no-sync python scripts/stix_opencti_relationship_schema.py /tmp/opencti
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

#: Extraction kind → OpenCTI entity type constant.
_KINDS = {
    "intrusion-set": "ENTITY_TYPE_INTRUSION_SET",
    "threat-actor": "ENTITY_TYPE_THREAT_ACTOR_INDIVIDUAL",
    "campaign": "ENTITY_TYPE_CAMPAIGN",
    "incident": "ENTITY_TYPE_INCIDENT",
    "malware": "ENTITY_TYPE_MALWARE",
    "tool": "ENTITY_TYPE_TOOL",
    "channel": "ENTITY_TYPE_CHANNEL",
    "infrastructure": "ENTITY_TYPE_INFRASTRUCTURE",
    "attack-pattern": "ENTITY_TYPE_ATTACK_PATTERN",
    "vulnerability": "ENTITY_TYPE_VULNERABILITY",
    "sector": "ENTITY_TYPE_IDENTITY_SECTOR",
    "organization": "ENTITY_TYPE_IDENTITY_ORGANIZATION",
    "individual": "ENTITY_TYPE_IDENTITY_INDIVIDUAL",
    "country": "ENTITY_TYPE_LOCATION_COUNTRY",
    "region": "ENTITY_TYPE_LOCATION_REGION",
    "city": "ENTITY_TYPE_LOCATION_CITY",
    "ipv4-addr": "ENTITY_IPV4_ADDR",
    "ipv6-addr": "ENTITY_IPV6_ADDR",
    "domain-name": "ENTITY_DOMAIN_NAME",
    "url": "ENTITY_URL",
    "file": "ENTITY_HASHED_OBSERVABLE_STIX_FILE",
    "email-addr": "ENTITY_EMAIL_ADDR",
    "software": "ENTITY_SOFTWARE",
    "indicator": "ENTITY_TYPE_INDICATOR",
}
_OBSERVABLES = {"ENTITY_IPV4_ADDR", "ENTITY_IPV6_ADDR", "ENTITY_DOMAIN_NAME", "ENTITY_URL",
                "ENTITY_HASHED_OBSERVABLE_STIX_FILE", "ENTITY_EMAIL_ADDR", "ENTITY_SOFTWARE"}
#: Entity modules that declare relationships of their own, and their entity type.
_MODULES = {
    "organization/organization.ts": "ENTITY_TYPE_IDENTITY_ORGANIZATION",
    "channel/channel.ts": "ENTITY_TYPE_CHANNEL",
    "threatActorIndividual/threatActorIndividual.ts": "ENTITY_TYPE_THREAT_ACTOR_INDIVIDUAL",
}


def schema_pairs(src: Path) -> dict[tuple[str, str], set[str]]:
    text = (src / "database/stix.ts").read_text()
    body = text[text.index("export const stixCoreRelationshipsMapping"):text.index("export const checkStixCoreRelationshipMapping")]
    pairs: dict[tuple[str, str], set[str]] = {}
    for left, right, block in re.findall(r"\[`\$\{(\w+)\}_\$\{(\w+)\}`\]:\s*\[(.*?)\]", body, re.S):
        pairs.setdefault((left, right), set()).update(re.findall(r"name: RELATION_(\w+)", block))
    for module, left in _MODULES.items():
        for verb, block in re.findall(r"name: RELATION_(\w+),\s*targets: \[(.*?)\]", (src / "modules" / module).read_text(), re.S):
            for right in re.findall(r"name: (\w+), type", block):
                pairs.setdefault((left, right), set()).add(verb)
    return pairs


def main() -> None:
    checkout = Path(sys.argv[1])
    src = checkout / "opencti-platform/opencti-graphql/src"
    pairs = schema_pairs(src)
    allowed = []
    for left_kind, left in _KINDS.items():
        for right_kind, right in _KINDS.items():
            verbs = set(pairs.get((left, right), ()))
            if right in _OBSERVABLES:
                verbs |= pairs.get((left, "ABSTRACT_STIX_CYBER_OBSERVABLE"), set())
            if left in _OBSERVABLES:
                verbs |= pairs.get(("ABSTRACT_STIX_CYBER_OBSERVABLE", right), set())
            allowed += [[left_kind, verb.lower().replace("_", "-"), right_kind] for verb in verbs]
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    source = (f"OpenCTI-Platform/opencti@{commit}: opencti-platform/opencti-graphql/src/database/stix.ts "
              "(stixCoreRelationshipsMapping) and the organization, channel and threatActorIndividual modules; "
              "regenerate with backend/scripts/stix_opencti_relationship_schema.py")
    kinds = "threat-actor is Threat-Actor-Individual; file is StixFile; related-to and revoked-by are allowed between any two"
    lines = ",\n".join("  " + json.dumps(triple) for triple in sorted(allowed))
    out = Path(__file__).resolve().parents[1] / "tests/fixtures/extraction/opencti_relationship_schema.json"
    out.write_text(
        "{\n" f' "source": {json.dumps(source)},\n "kinds": {json.dumps(kinds)},\n "allowed": [\n{lines}\n ]\n' "}\n"
    )
    print(f"{len(allowed)} relationships written to {out}")


if __name__ == "__main__":
    main()
