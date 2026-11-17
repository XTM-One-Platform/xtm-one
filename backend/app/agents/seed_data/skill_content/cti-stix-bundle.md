# STIX Bundle Authoring

STIX (Structured Threat Information Expression) 2.1 is the standard for representing and sharing cyber threat intelligence. This skill is a compact reference for writing STIX 2.1 bundles by hand or with `code_interpreter`: bundle structure, the common object types, patterns, relationships and TLP markings, plus the consumer-specific rules for MISP and OpenCTI.

## How this skill fits with the other STIX tools

- **OpenCTI-bound bundles**: load the built-in **OpenCTI STIX Bundle Construction** skill if it is attached. It is the source of truth for OpenCTI output (required fields per type, confidence and score rules, SCO enrichment, external references, report `object_refs`, deduplication, ID generation, the final validation checklist). Where the two skills differ, follow that one for OpenCTI.
- **Validation**: always run `stix_bundle_validator` on the final JSON (pass it as `bundle_json` or `bundle`). If it fails, fix the reported issues and validate again; if it returns `terminal: true`, stop retrying and follow its `next_step`. The validator normalises TLP marking-definitions to their spec form: never drop a marking to make validation pass.
- **Building**: use `code_interpreter` to generate UUIDs, timestamps and patterns and to serialise the JSON, instead of typing ids by hand. Escape single quotes and backslashes inside pattern values (`'` -> `\'`, `\` -> `\\`).
- **Delivering**: hand the validated bundle to the user with `generate_file` (`YYYY-MM-DD-CONTEXT.stix.json`). Write it to OpenCTI or MISP only when the user or the assignment asks for it (see "Delivering a bundle to a platform").

## Bundle structure

```json
{
  "type": "bundle",
  "id": "bundle--UUID",
  "objects": [
    // Array of STIX Domain Objects (SDOs), Cyber-observable Objects (SCOs) and Relationship Objects (SROs)
  ]
}
```

## STIX Domain Objects (SDOs)

### Indicator

Represents a pattern that can be used to detect suspicious activity.

```json
{
  "type": "indicator",
  "spec_version": "2.1",
  "id": "indicator--UUID",
  "created": "2026-01-15T00:00:00.000Z",
  "modified": "2026-01-15T00:00:00.000Z",
  "name": "Malicious IP used by APT28",
  "description": "C2 server observed in campaign targeting European government entities",
  "pattern": "[ipv4-addr:value = '203.0.113.42']",
  "pattern_type": "stix",
  "valid_from": "2026-01-15T00:00:00.000Z",
  "valid_until": "2026-04-15T00:00:00.000Z",
  "kill_chain_phases": [
    {
      "kill_chain_name": "mitre-attack",
      "phase_name": "command-and-control"
    }
  ],
  "confidence": 85,
  "labels": ["malicious-activity"],
  "object_marking_refs": ["marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da"]
}
```

`pattern_type` is not limited to `stix`: the STIX 2.1 open vocabulary also defines `pcre`, `sigma`, `snort`, `suricata` and `yara`, and OpenCTI's default vocabulary adds `tanium-signal`, `spl`, `eql`, `shodan` and `nova` (it is an open vocabulary, so an administrator may add others such as `kql`). A detection rule shipped as an indicator keeps its rule text in `pattern`.

### STIX pattern syntax

| IOC type | STIX pattern |
|----------|-------------|
| IPv4 | `[ipv4-addr:value = '1.2.3.4']` |
| IPv6 | `[ipv6-addr:value = '2001:db8::1']` |
| Domain | `[domain-name:value = 'evil.com']` |
| URL | `[url:value = 'https://evil.com/payload']` |
| SHA-256 | `[file:hashes.'SHA-256' = 'abc123...']` |
| MD5 | `[file:hashes.MD5 = 'abc123...']` |
| Email | `[email-addr:value = 'phish@evil.com']` |
| File name | `[file:name = 'malware.exe']` |
| Combined | `[file:hashes.'SHA-256' = 'abc...' AND file:name = 'payload.dll']` |

Patterns always carry the refanged value. A defanged value (`evil[.]com`) in a pattern never matches anything.

### Threat Actor

```json
{
  "type": "threat-actor",
  "spec_version": "2.1",
  "id": "threat-actor--UUID",
  "created": "2026-01-15T00:00:00.000Z",
  "modified": "2026-01-15T00:00:00.000Z",
  "name": "APT28",
  "description": "Russian state-sponsored threat actor also known as Fancy Bear, Sofacy",
  "aliases": ["Fancy Bear", "Sofacy", "Pawn Storm", "Forest Blizzard"],
  "threat_actor_types": ["nation-state"],
  "roles": ["agent"],
  "sophistication": "expert",
  "resource_level": "government",
  "primary_motivation": "political",
  "goals": ["espionage", "disruption"]
}
```

Named APT groups tracked by vendors are often better represented as an **intrusion-set** (the convention used by MITRE ATT&CK and OpenCTI); keep `threat-actor` for the people or organisation behind them. The OpenCTI skill states which type OpenCTI expects.

### Malware

```json
{
  "type": "malware",
  "spec_version": "2.1",
  "id": "malware--UUID",
  "created": "2026-01-15T00:00:00.000Z",
  "modified": "2026-01-15T00:00:00.000Z",
  "name": "SUNBURST",
  "description": "Backdoor trojanised into SolarWinds Orion software",
  "malware_types": ["backdoor", "trojan"],
  "is_family": true,
  "kill_chain_phases": [
    {"kill_chain_name": "mitre-attack", "phase_name": "initial-access"},
    {"kill_chain_name": "mitre-attack", "phase_name": "command-and-control"}
  ]
}
```

### Attack Pattern (MITRE ATT&CK technique)

```json
{
  "type": "attack-pattern",
  "spec_version": "2.1",
  "id": "attack-pattern--UUID",
  "created": "2026-01-15T00:00:00.000Z",
  "modified": "2026-01-15T00:00:00.000Z",
  "name": "Phishing: Spearphishing Attachment",
  "description": "Adversaries send spearphishing emails with malicious attachments",
  "external_references": [
    {
      "source_name": "mitre-attack",
      "external_id": "T1566.001",
      "url": "https://attack.mitre.org/techniques/T1566/001"
    }
  ],
  "kill_chain_phases": [
    {"kill_chain_name": "mitre-attack", "phase_name": "initial-access"}
  ]
}
```

Take the technique name, ID and tactic from the MITRE ATT&CK integration (`get_mitre_attack_object`) rather than from memory; the **MITRE ATT&CK Reference** skill explains the lookups.

### Campaign

```json
{
  "type": "campaign",
  "spec_version": "2.1",
  "id": "campaign--UUID",
  "created": "2026-01-15T00:00:00.000Z",
  "modified": "2026-01-15T00:00:00.000Z",
  "name": "Operation Fancy Storm",
  "description": "Targeted espionage campaign against European government entities",
  "first_seen": "2025-11-01T00:00:00.000Z",
  "last_seen": "2026-01-15T00:00:00.000Z"
}
```

### Identity (victim / target)

```json
{
  "type": "identity",
  "spec_version": "2.1",
  "id": "identity--UUID",
  "created": "2026-01-15T00:00:00.000Z",
  "modified": "2026-01-15T00:00:00.000Z",
  "name": "European Government Sector",
  "identity_class": "class",
  "sectors": ["government-national"]
}
```

## Relationship Objects (SROs)

```json
{
  "type": "relationship",
  "spec_version": "2.1",
  "id": "relationship--UUID",
  "created": "2026-01-15T00:00:00.000Z",
  "modified": "2026-01-15T00:00:00.000Z",
  "relationship_type": "uses",
  "source_ref": "threat-actor--UUID",
  "target_ref": "malware--UUID",
  "confidence": 85
}
```

Every `source_ref` and `target_ref` must point to an object present in the bundle (or known to the consumer). A dangling reference is the most common cause of a rejected import.

### Common relationship types

| Source | Relationship | Target |
|--------|-------------|--------|
| threat-actor | uses | malware, attack-pattern, tool |
| threat-actor | targets | identity, vulnerability |
| threat-actor | attributed-to | identity (sponsoring nation) |
| campaign | uses | malware, attack-pattern, tool |
| campaign | targets | identity, vulnerability |
| campaign | attributed-to | threat-actor |
| indicator | indicates | malware, threat-actor, campaign |
| malware | targets | identity, vulnerability |
| malware | uses | attack-pattern |

## TLP marking definitions (standard IDs)

STIX 2.1 freezes the IDs (and the `created` date `2017-01-20T00:00:00.000Z`) of the four TLP marking-definitions. Use them verbatim in `object_marking_refs`:

```json
"marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9"  // TLP:WHITE / TLP:CLEAR
"marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da"  // TLP:GREEN
"marking-definition--f88d31f6-486f-44da-b317-01333bde0b82"  // TLP:AMBER
"marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed"  // TLP:RED
```

- TLP 2.0 renamed WHITE to CLEAR without changing its meaning. STIX 2.1 knows no `clear` value: TLP:CLEAR is emitted on the WHITE id above with `definition: {"tlp": "white"}`.
- **TLP:AMBER+STRICT** has no STIX 2.1 core id. OpenCTI ships it as `marking-definition--826578e1-40ad-459f-bc73-ede076f81f37`. Other consumers may use another id or none; check with the recipient before relying on it, and fall back to TLP:AMBER plus a handling note in the description when the consumer has no AMBER+STRICT marking.
- Do not invent a TLP value the source does not support. Load the **TLP Guide** skill if attached for the sharing rules behind each colour.

### MISP-bound bundles: reference TLPs, do not inline them

For bundles that will be imported into MISP, **only reference TLP marking-definition IDs** in `object_marking_refs`. Do not include a `{"type": "marking-definition", ...}` object in the bundle's `objects` array.

The inline shape that some older examples show (`definition_type: "tlp"` with `definition: {"tlp": "clear"}`) fails MISP's STIX 2 importer with "Does not match any TLP Marking definition!". MISP resolves the canonical IDs from its own catalogue; supplying an inline copy triggers the spec-conformance check and the whole bundle is rejected.

Correct (MISP-compatible): reference only, the marking is resolved by the consumer:

```json
{
  "type": "bundle",
  "id": "bundle--...",
  "objects": [
    {
      "type": "indicator",
      "spec_version": "2.1",
      "id": "indicator--...",
      "created": "2026-04-26T00:00:00.000Z",
      "modified": "2026-04-26T00:00:00.000Z",
      "object_marking_refs": ["marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9"],
      "pattern": "[ipv4-addr:value = '203.0.113.42']",
      "pattern_type": "stix",
      "valid_from": "2026-04-26T00:00:00.000Z"
    }
  ]
}
```

Do not do this for MISP-bound bundles (the inlined TLP definition trips the importer):

```json
{
  "type": "marking-definition",
  "id": "marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9",
  "definition_type": "tlp",
  "definition": {"tlp": "clear"}
}
```

If you need a self-contained bundle for non-MISP consumers (offline ingest, archives, partners without a TLP catalogue), including the marking-definition objects is valid STIX 2.1: keep a separate non-MISP variant rather than tweaking the MISP one. If TLP must be applied inside MISP after import, apply it as an event tag (`add_misp_tag`), not as a STIX marking.

**OpenCTI is not affected**: it is STIX 2.1-native and imports bundles with inline TLP marking-definitions as-is. Only MISP-bound bundles need the reference-only treatment above.

## Authoring checklist

1. Every object has `type`, `spec_version: "2.1"` (SDOs and SROs), an `id` whose prefix matches the `type`, and RFC 3339 `created` / `modified` timestamps in UTC with milliseconds.
2. IDs are unique in the bundle and generated, not typed. SCO ids are deterministic in STIX 2.1 (UUIDv5 over the object's id-contributing properties); the OpenCTI skill gives the rule when SCOs are included.
3. Indicators carry `pattern`, `pattern_type` and `valid_from`; `valid_until` is set for infrastructure indicators that will age out.
4. `confidence` is 0-100 and consistent with the analytic confidence stated in the prose.
5. Every relationship endpoint and every marking reference resolves.
6. The TLP of every object is at least as restrictive as its source.
7. `stix_bundle_validator` returns valid.

## Delivering a bundle to a platform (only when asked)

- **File**: `generate_file` with the validated JSON. This is the default.
- **OpenCTI**: create the same knowledge as objects in a draft (`create_opencti_draft`, `use_opencti_draft`, then `create_opencti_entity`, `create_opencti_observable`, `create_opencti_indicator`, `create_opencti_relationship`, `add_opencti_to_container`), so an analyst reviews and validates it in OpenCTI. To keep the bundle itself with the knowledge, attach the file to the report with `upload_opencti_file`.
- **MISP**: there is no bundle-upload tool in XTM One; rebuild the content as an event (`create_misp_event`, `add_misp_attribute`, `add_misp_tag`), or deliver the reference-only bundle as a file for the MISP administrator to import.

## Related skills

- **OpenCTI STIX Bundle Construction**: OpenCTI-specific rules and the final validation checklist
- **IOC Export**: CSV, OpenIOC and MISP JSON exports of the same indicators
- **MITRE ATT&CK Reference**: technique names, IDs and tactics for attack-pattern objects
- **TLP Guide**, **Confidence Levels**: the marking and the confidence carried by every object
