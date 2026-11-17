# OpenCTI STIX Bundle Construction

Use this skill to build final STIX output for OpenCTI cyber threat intelligence tasks, both extraction (harvesting CTI from source documents) and transformation (rewriting an existing STIX bundle).

This skill is the source of truth for STIX construction, OpenCTI-oriented output style, object required fields, confidence scoring, relationship integrity, deduplication, validation, and final JSON quality.

## Applicability: Extraction vs Transformation

This skill serves two task modes:

- Extraction (harvesting): you read one or more source documents and build a STIX bundle from scratch. Every rule in this skill applies, including the container-shape constraints (non-empty `objects`, at least one `report`, one `report` per input document, and no `grouping`).
- Transformation: you receive an existing STIX bundle plus a user instruction and rewrite it. Every object-level rule in this skill still applies (required fields, `id` format, timestamps, confidence, references, deduplication, validation), but the container-shape constraints are subordinate to the user instruction. A transformation may legitimately produce an empty `objects` array (the instruction removed everything matching a criterion), a bundle with no `report` (the instruction filtered reports out), or a bundle that contains `grouping` objects (the instruction asked to wrap objects in a grouping).

Whenever this skill says the bundle "must contain at least one object", "must contain at least one `report`", "one `report` per input", or "must not contain any `grouping`", read those as extraction-mode requirements. In transformation mode, follow the user instruction instead.

## Output Contract

Return only valid JSON.

Do not return Markdown.

Do not return comments.

Do not return explanatory prose.

The expected root output is a STIX 2.1 Bundle:

```json
{
  "type": "bundle",
  "id": "bundle--UUID",
  "objects": []
}
```

Bundle constraints:

- root `type` must be `bundle`;
- root `id` must be a valid `bundle--UUID` STIX ID;
- `objects` must be an array;
- in extraction mode, `objects` must contain at least one object (in transformation mode an empty array is valid when the user instruction removes every matching object);
- in extraction mode, the bundle must not contain any object with `type` set to `grouping` (in transformation mode a `grouping` is allowed when the user instruction asks for one).

## Multi-Report Bundle Rules

These rules apply to extraction mode (building a bundle from one or more source documents). In transformation mode there are no input documents, so the report structure is whatever the user instruction requires - including no `report` at all.

The bundle is the transport container.

A STIX `report` is the analysis boundary.

For one input file or one logical source document, create exactly one `report`.

For multiple input files or logical source documents, create one `report` per input, all in the same bundle.

Do not create one bundle per input.

Do not create one global report for all inputs unless the user explicitly requests a consolidated global analysis.

Do not create STIX `grouping` objects.

Each `report.object_refs` must reference only objects and relationships extracted from the corresponding input source, except shared objects that are genuinely the same and supported by each relevant source.

Every object referenced in `report.object_refs` must exist in the same bundle.

A report must not reference itself.

## STIX 2.1 Baseline

Every STIX object must include:

- `type`
- `spec_version`: `2.1`
- `id` in `OBJECT_TYPE--UUID` format

For STIX Domain Objects and STIX Relationship Objects, include:

- `created`
- `modified`

For STIX Cyber-observable Objects, this skill uses the **OpenCTI-compatible enriched profile** by default. In this profile, also include:

- `created`
- `modified`
- `created_by_ref` when a source identity exists
- `object_marking_refs` when a marking is used
- `confidence` when the output schema allows it

These SCO enrichment fields may be rejected by strict STIX validators. If validation errors only concern these enriched SCO metadata fields, treat them as non-blocking for the final OpenCTI-oriented output.

Use UTC ISO 8601 timestamps in this format:

```text
YYYY-MM-DDTHH:mm:ss.sssZ
```

Use the source publication date for `published`, `created`, and `modified` when appropriate and available.

Use object-specific `first_seen` and `last_seen` only when those dates are explicitly present or directly supported by the source.

If no source date is available, use the current execution timestamp consistently across the bundle.

Do not invent dates.

## OpenCTI-Oriented Output Style

The output should be as close as possible to a clean OpenCTI-importable STIX bundle.

When the runtime injects a strict Output JSON Schema, schema compliance has priority over natural STIX sparsity.

The final JSON payload must exactly follow the injected output schema.

If the injected schema requires a property on every item of `bundle.objects`, every object in `bundle.objects` must include that property, even when the property is not semantically applicable to the object type.

This includes schema-level compatibility properties such as:

- `published`
- `report_types`
- `object_refs`
- `relationship_type`
- `source_ref`
- `target_ref`
- `pattern`
- `pattern_type`
- `valid_from`
- `value`
- `is_family`
- `aliases`
- `confidence`
- `labels`
- `country`
- `region`
- `latitude`
- `longitude`
- `hashes`
- `indicator_types`

When one of these fields is not applicable to the current object type, set it to `null`.

Setting a required schema field to `null` is not hallucination when the field is required by the output schema and not semantically applicable.

Do not omit required schema fields.

Do not try to produce a sparse STIX object if the strict output schema requires a flat object shape.

The object-specific rules below decide which required schema fields must be non-null for each STIX type.

## Generic Object Shape Required By The Output Schema

Every object inside `bundle.objects` must include the following fields when the injected schema requires this shape:

- `type`
- `spec_version`
- `id`
- `created`
- `modified`
- `name`
- `description`
- `published`
- `report_types`
- `object_refs`
- `relationship_type`
- `source_ref`
- `target_ref`
- `pattern`
- `pattern_type`
- `valid_from`
- `value`
- `is_family`
- `aliases`
- `confidence`
- `labels`
- `country`
- `region`
- `latitude`
- `longitude`
- `hashes`
- `indicator_types`

For each object, initialize all non-applicable fields to `null`.

Then fill the fields that are semantically required for the object type.

Example for a non-location object:

```json
{
  "type": "malware",
  "spec_version": "2.1",
  "id": "malware--uuid",
  "created": "2026-05-10T00:00:00.000Z",
  "modified": "2026-05-10T00:00:00.000Z",
  "name": "WalkerShell",
  "description": "Malware explicitly mentioned in the source.",
  "published": null,
  "report_types": null,
  "object_refs": null,
  "relationship_type": null,
  "source_ref": null,
  "target_ref": null,
  "pattern": null,
  "pattern_type": null,
  "valid_from": null,
  "value": null,
  "is_family": true,
  "aliases": null,
  "confidence": 80,
  "labels": null,
  "country": null,
  "region": null,
  "latitude": null,
  "longitude": null,
  "hashes": null,
  "indicator_types": null
}
```

Example for a country-level location object:

```json
{
  "type": "location",
  "spec_version": "2.1",
  "id": "location--uuid",
  "created": "2026-05-10T00:00:00.000Z",
  "modified": "2026-05-10T00:00:00.000Z",
  "name": "United States",
  "description": "Country explicitly mentioned in the source.",
  "published": null,
  "report_types": null,
  "object_refs": null,
  "relationship_type": null,
  "source_ref": null,
  "target_ref": null,
  "pattern": null,
  "pattern_type": null,
  "valid_from": null,
  "value": null,
  "is_family": null,
  "aliases": null,
  "confidence": 80,
  "labels": null,
  "country": "US",
  "region": null,
  "latitude": null,
  "longitude": null,
  "hashes": null,
  "indicator_types": null
}
```

Example for a region-level location object:

```json
{
  "type": "location",
  "spec_version": "2.1",
  "id": "location--uuid",
  "created": "2026-05-10T00:00:00.000Z",
  "modified": "2026-05-10T00:00:00.000Z",
  "name": "South Asia",
  "description": "Region explicitly mentioned in the source.",
  "published": null,
  "report_types": null,
  "object_refs": null,
  "relationship_type": null,
  "source_ref": null,
  "target_ref": null,
  "pattern": null,
  "pattern_type": null,
  "valid_from": null,
  "value": null,
  "is_family": null,
  "aliases": null,
  "confidence": 80,
  "labels": null,
  "country": null,
  "region": "southern-asia",
  "latitude": null,
  "longitude": null,
  "hashes": null,
  "indicator_types": null
}
```

## Critical Schema Shape Rule

The output schema can use a generic object shape for every item in `bundle.objects`.

Therefore, every object must include all schema-required fields, including fields that are only meaningful for specific STIX types.

Fields such as `country`, `region`, `latitude`, `longitude`, `hashes`, and `indicator_types` must be present on every object when required by the injected schema.

For object types where these fields are not applicable, set them to `null`.

Examples:

- For `malware`, set `country`, `region`, `latitude`, `longitude`, `hashes`, and `indicator_types` to `null`.
- For `identity`, set `country`, `region`, `latitude`, `longitude`, `hashes`, and `indicator_types` to `null`.
- For `relationship`, set `country`, `region`, `latitude`, `longitude`, `hashes`, and `indicator_types` to `null`.
- For `location`, set at least one of `country`, `region`, or both `latitude` and `longitude`.
- For `file`, set `hashes` when file hashes are available.
- For `indicator`, set `indicator_types` when the object is an indicator.

Do not omit these fields.

This rule exists to satisfy the strict Output JSON Schema and overrides the usual STIX preference to omit non-applicable optional fields.

When possible and source-supported, include:

- `created_by_ref`
- `object_marking_refs`
- `confidence`
- `external_references`
- `x_opencti_score`
- `x_opencti_main_observable_type`
- `x_opencti_content` on reports

Do not invent these fields.

Use them only when deterministic, supported, or explicitly required by the runtime.

## Source Identity Rules

If the report publisher, author, vendor, source organization, or producing organization is explicit in the input, create one STIX `identity` object for it.

Use:

```json
{
  "type": "identity",
  "spec_version": "2.1",
  "id": "identity--UUID",
  "created": "TIMESTAMP",
  "modified": "TIMESTAMP",
  "name": "SOURCE_ORGANIZATION",
  "identity_class": "organization"
}
```

Use that identity as `created_by_ref` on the report and on extracted analytical objects when appropriate.

Do not create a source identity if the source organization is not explicitly present.

If `created_by_ref` is used, the referenced identity must exist in the same bundle.

## Marking Definition Rules

If the source explicitly contains a TLP marking or another explicit marking, create or reference the corresponding STIX `marking-definition`.

Do not invent TLP values.

TLP marking IDs are frozen by the STIX 2.1 specification and must be used verbatim:

- `TLP:WHITE` / `TLP:CLEAR` -> `marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9`
- `TLP:GREEN` -> `marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da`
- `TLP:AMBER` -> `marking-definition--f88d31f6-486f-44da-b317-01333bde0b82`
- `TLP:RED` -> `marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed`

A TLP marking-definition must also carry `created: "2017-01-20T00:00:00.000Z"`, and STIX 2.1 knows no `clear` value: TLP:CLEAR is emitted as `definition: {"tlp": "white"}` on the white ID above. `stix_bundle_validator` normalizes these for you, so never drop a TLP marking-definition to make validation pass.

If `object_marking_refs` is used, the referenced marking definition must be valid.

For strict self-contained bundles, include the referenced marking-definition object in the same bundle.

If the target OpenCTI runtime accepts standard STIX marking-definition IDs, standard marking references may be used.

If the source does not contain a marking and the runtime does not require a default marking, omit `object_marking_refs`.

## Confidence Rules

Use the STIX `confidence` property as an integer from 0 to 100.

Include `confidence` for extracted analytical SDOs and SROs when the output schema allows it.

In this OpenCTI-compatible profile, include `confidence` on SCOs when the output schema allows it.

If a strict STIX validator reports only that `confidence` is not allowed on a SCO, ignore that validation error in the final validation decision.

Use this scale:

- `90` to `100`: direct evidence in the source, no ambiguity, such as exact hashes, IPs, domains, URLs, CVEs, explicitly named malware, or explicitly stated relationships.
- `70` to `89`: strong analytical statement in the source, high confidence attribution, or strongly supported behavior mapping.
- `50` to `69`: medium confidence, likely attribution, or a statement presented as probable by the source.
- `30` to `49`: suspected, possible, alleged, or weakly supported statement.
- `0` to `29`: very weak or uncertain evidence. Prefer not extracting the object unless it remains useful and clearly uncertain.

If the source explicitly says `high confidence`, use `confidence: 80` unless a more precise value is provided.

If the source explicitly says `medium confidence`, use `confidence: 50` unless a more precise value is provided.

If the source explicitly says `low confidence`, use `confidence: 30` unless a more precise value is provided.

If confidence is not stated but the object is explicitly present in the source, use a conservative default of `confidence: 50` for analytical SDOs and relationships.

For indicators directly listed as malicious by the source, use `confidence: 80` unless the source says they are uncertain.

For SCOs directly listed as indicators or observables in the source, use `confidence: 50` by default, or `80` when the source explicitly presents the observable as malicious, suspicious, or part of the actor infrastructure.

Do not use confidence to make unsupported information look reliable.

## OpenCTI Score Rules

When the output schema and target platform allow custom OpenCTI fields, include `x_opencti_score` for indicators, malware, vulnerabilities, and other objects where a score is useful.

Use scores only as deterministic enrichment based on type and source certainty.

Do not invent severity beyond the source.

Recommended default scores:

- SHA-256, SHA-1, or MD5 file hash indicator: `x_opencti_score: 80`
- malicious domain indicator: `x_opencti_score: 70`
- malicious IPv4 or IPv6 indicator: `x_opencti_score: 50`
- malicious URL indicator: `x_opencti_score: 70`
- malware family or custom malware: `x_opencti_score: 70`
- open-source or commodity malware explicitly used maliciously: `x_opencti_score: 60`
- tool explicitly abused by an actor: `x_opencti_score: 50`
- critical vulnerability explicitly exploited in the campaign: `x_opencti_score: 90`
- vulnerability mentioned but not explicitly exploited: `x_opencti_score: 60`

If the source provides severity, priority, CVSS, or a score, prefer the source-supported value.

If scoring would be arbitrary, omit `x_opencti_score`.

## Allowed Types

Preferred STIX Domain Objects:

- `report`
- `identity`
- `threat-actor`
- `intrusion-set`
- `campaign`
- `malware`
- `tool`
- `attack-pattern`
- `indicator`
- `infrastructure`
- `location`
- `vulnerability`
- `note`
- `observed-data`

Preferred STIX Cyber-observable Objects:

- `domain-name`
- `ipv4-addr`
- `ipv6-addr`
- `url`
- `file`
- `email-addr`
- `software`
- `directory`
- `process`
- `mutex`

Preferred STIX Relationship Objects:

- `relationship`

Use custom types only when no standard type can represent the data and the target OpenCTI platform supports the custom type.

## SCO OpenCTI Enrichment Rules

This skill targets an OpenCTI-compatible enriched STIX output profile.

For SCOs, include OpenCTI-oriented metadata fields when they help the bundle match the expected output profile.

By default, SCOs should include:

- `type`
- `spec_version`: `2.1`
- `id`
- `created`
- `modified`

When applicable and supported by the source context, SCOs may also include:

- `created_by_ref`
- `object_marking_refs`
- `confidence`
- `labels`
- `external_references`

Use the same `created`, `modified`, `created_by_ref`, and `object_marking_refs` values as the source report and related extracted objects when appropriate.

If the built-in STIX validator returns errors only because these metadata fields are present on SCOs, ignore those errors for the final OpenCTI-oriented output.

Do not remove SCO metadata fields just to satisfy strict STIX validation when the target output profile expects OpenCTI-enriched SCOs.

SCOs must not be empty.

A `file` SCO must contain at least one meaningful valid file property, such as:

- `name`
- `hashes`
- `size`
- `mime_type`

Example enriched file SCO:

```json
{
  "type": "file",
  "spec_version": "2.1",
  "id": "file--UUID",
  "created": "TIMESTAMP",
  "modified": "TIMESTAMP",
  "created_by_ref": "identity--UUID",
  "object_marking_refs": [
    "marking-definition--UUID"
  ],
  "confidence": 50,
  "hashes": {
    "SHA-256": "SHA256"
  }
}
```

A `domain-name` SCO must contain:

- `value`

An `ipv4-addr` SCO must contain:

- `value`

Example enriched IPv4 SCO:

```json
{
  "type": "ipv4-addr",
  "spec_version": "2.1",
  "id": "ipv4-addr--UUID",
  "created": "TIMESTAMP",
  "modified": "TIMESTAMP",
  "created_by_ref": "identity--UUID",
  "object_marking_refs": [
    "marking-definition--UUID"
  ],
  "confidence": 50,
  "value": "1.2.3.4"
}
```

An `ipv6-addr` SCO must contain:

- `value`

A `url` SCO must contain:

- `value`

An `email-addr` SCO must contain:

- `value`

If the required meaningful property is not available, do not create the SCO.

The target output profile is OpenCTI-enriched by default. Emit these SCO metadata fields when they are meaningful, even if a strict STIX validator reports them as non-standard. Only remove them if the user explicitly requests strict OASIS STIX output.

## Required Field Rules By Type

### Report

A `report` object must include:

- `type`: `report`
- `spec_version`: `2.1`
- `id`
- `created`
- `modified`
- `name`
- `published`
- `report_types`
- `object_refs`
- `description` when source context is available
- `created_by_ref` when a source identity exists
- `object_marking_refs` when a marking is used
- `confidence` when the output schema allows it

Use `report_types: ["threat-report"]` unless the source clearly supports another report type.

When the target OpenCTI import supports it, include `x_opencti_content` with the original source text or the closest available source-faithful extracted body.

Do not summarize inside `x_opencti_content`.

### Identity

An `identity` object must include:

- `name`
- `identity_class`

For a source publisher or vendor, use:

```json
"identity_class": "organization"
```

For sectors or victim classes, use the closest valid `identity_class`, such as `class`, and include `sectors` when source-supported.

Do not invent sectors.

### Threat Actor

A `threat-actor` object must include:

- `name`
- `threat_actor_types`

If actor type is unknown, use:

```json
"threat_actor_types": ["unknown"]
```

When supported by the source, include:

- `aliases`
- `description`
- `roles`
- `goals`
- `sophistication`
- `resource_level`
- `primary_motivation`
- `secondary_motivations`
- `first_seen`
- `last_seen`
- `confidence`

Do not infer government linkage unless the source states it or strongly supports it.

### Intrusion Set

An `intrusion-set` object must include:

- `name`

When supported by the source, include:

- `aliases`
- `description`
- `goals`
- `resource_level`
- `primary_motivation`
- `first_seen`
- `last_seen`
- `confidence`

Use `intrusion-set` for named activity clusters or APT group tracking names when the source presents them as an intrusion set or long-running activity set.

### Campaign

A `campaign` object must include:

- `name`

When supported by the source, include:

- `description`
- `objective`
- `first_seen`
- `last_seen`
- `aliases`
- `confidence`

Create a campaign only when the source describes a bounded operation, wave, activity cluster, or campaign.

Do not invent a campaign name if the source does not provide one. If a campaign is clearly described but unnamed, use a neutral source-grounded name based on the source title or target context.

### Malware

A `malware` object must include:

- `name`
- `malware_types`
- `is_family`

If malware type is unknown, use:

```json
"malware_types": ["unknown"]
```

If the source does not clearly state whether the malware is a family, set:

```json
"is_family": false
```

When supported by the source, include:

- `aliases`
- `description`
- `first_seen`
- `last_seen`
- `operating_system_refs`
- `architecture_execution_envs`
- `implementation_languages`
- `capabilities`
- `confidence`
- `x_opencti_score`

### Tool

A `tool` object must include:

- `name`

When supported by the source, include:

- `tool_types`
- `description`
- `aliases`
- `confidence`

If the tool type is unknown but the source clearly identifies a tool, use:

```json
"tool_types": ["unknown"]
```

Do not classify a tool as malware unless the source supports it.

### Attack Pattern

An `attack-pattern` object must include:

- `name`

When the source provides a MITRE ATT&CK ID, include `external_references`.

For MITRE ATT&CK references, use:

```json
{
  "source_name": "mitre-attack",
  "external_id": "Txxxx",
  "url": "https://attack.mitre.org/techniques/Txxxx"
}
```

For sub-techniques, use this URL format:

```text
https://attack.mitre.org/techniques/Txxxx/yyy
```

Only include `kill_chain_phases` when the source explicitly provides the phase or when the MITRE ATT&CK technique ID is explicitly present and the mapping is unambiguous.

Do not infer kill chain phases from vague behavior descriptions.

### Vulnerability

A `vulnerability` object must include:

- `name`

For CVEs, use the CVE ID as `name`.

When a CVE is present, include:

```json
{
  "source_name": "cve",
  "external_id": "CVE-YYYY-NNNN"
}
```

If a stable public URL is deterministic and allowed by the target, include it.

When supported by the source, include:

- `description`
- `confidence`
- `x_opencti_score`

### Location objects

A `location` object must include the generic schema fields required on every object.

For `location` objects, at least one of the following fields must be non-null:

- `country`
- `region`
- both `latitude` and `longitude`

A `location` object with `country: null`, `region: null`, `latitude: null`, and `longitude: null` is invalid.

For country-level locations:

- set `country` to the ISO 3166-1 alpha-2 code;
- set `region` to `null` unless the region is explicitly useful and safely mappable;
- set `latitude` and `longitude` to `null` unless coordinates are explicitly available.

Examples:

- Russian Federation -> `country: "RU"`
- Bangladesh -> `country: "BD"`
- Pakistan -> `country: "PK"`
- United States -> `country: "US"`
- Japan -> `country: "JP"`
- China -> `country: "CN"`

For region-level locations:

- set `region` to a valid STIX region vocabulary value;
- set `country` to `null`;
- set `latitude` and `longitude` to `null` unless coordinates are explicitly available.

Examples:

- South Asia -> `region: "southern-asia"`
- Eastern Asia -> `region: "eastern-asia"`
- Eastern Europe -> `region: "eastern-europe"`
- Northern America -> `region: "northern-america"`

Do not create a `location` object if the location cannot be safely mapped to `country`, `region`, or both `latitude` and `longitude`.

For non-location objects, `country`, `region`, `latitude`, and `longitude` must still be present if required by the schema, but they must be set to `null`.

### Indicator

An `indicator` object must include:

- `name`
- `indicator_types`
- `pattern`
- `pattern_type`: `stix`
- `valid_from`

Only create indicators when a valid STIX pattern can be produced.

Use `indicator_types: ["malicious-activity"]` when the source presents the observable as malicious infrastructure, payload, malware, or activity.

The `pattern` must use valid STIX pattern syntax.

Examples:

```json
"[file:hashes.'SHA-256' = 'SHA256']"
```

```json
"[ipv4-addr:value = '1.2.3.4']"
```

```json
"[domain-name:value = 'example.com']"
```

```json
"[url:value = 'https://example.com/path']"
```

When applicable and supported by the output profile, include:

- `x_opencti_main_observable_type`
- `x_opencti_score`

Use these `x_opencti_main_observable_type` values:

- file hash pattern: `StixFile`
- IPv4 pattern: `IPv4-Addr`
- IPv6 pattern: `IPv6-Addr`
- domain pattern: `Domain-Name`
- URL pattern: `Url`
- email address pattern: `Email-Addr`

Do not create an indicator if the observable is not presented as malicious, suspicious, or operationally relevant by the source.

### Observed Data

Use `observed-data` only when the source describes an actual observation with a time window.

Required:

- `first_observed`
- `last_observed`
- `number_observed`
- `object_refs`

If no actual observation time is provided, prefer SCOs and indicators instead of `observed-data`.

### Relationship

A `relationship` object must include:

- `relationship_type`
- `source_ref`
- `target_ref`

Every `source_ref` and `target_ref` must point to an existing object in the same bundle.

Do not use `from_id`, `to_id`, or `relations`.

Place all relationship objects inside bundle `objects`.

Do not create a relationship unless the relationship is explicitly stated or directly and strongly supported by the source.

Do not create a relationship only because two objects appear in the same document.

Common relationship patterns:

- indicator `indicates` malware, infrastructure, tool, campaign, intrusion-set, or threat-actor
- intrusion-set or threat-actor `uses` malware, tool, infrastructure, or attack-pattern
- campaign `uses` malware, tool, infrastructure, or attack-pattern
- campaign or intrusion-set `targets` identity, sector identity, or location
- malware `uses` infrastructure only when explicitly supported
- threat-actor or intrusion-set `attributed-to` identity only when explicitly supported
- vulnerability may be related through `targets`, `uses`, or `exploits` only when the source clearly supports the relationship

Prefer fewer high-confidence relationships over many weak inferred relationships.

## External Reference Rules

Create `external_references` only when the source provides a stable external identifier or when the identifier itself provides a deterministic reference.

Allowed examples:

- MITRE ATT&CK technique ID explicitly present in the source, such as `T1003`
- CVE ID explicitly present in the source, such as `CVE-2020-1472`
- source report URL explicitly present in the source

For CVE identifiers, use:

```json
{
  "source_name": "cve",
  "external_id": "CVE-YYYY-NNNN"
}
```

For MITRE ATT&CK identifiers, use:

```json
{
  "source_name": "mitre-attack",
  "external_id": "Txxxx"
}
```

Do not invent external references for techniques, vulnerabilities, tools, campaigns, or malware when the source does not provide a stable identifier.

## Report Object References

Each report must reference all STIX objects extracted from its source, including:

- extracted SDOs;
- extracted SCOs when included;
- relationships extracted from the source;
- notes or observed-data if created.

Do not include duplicate IDs in `report.object_refs`.

Do not reference objects not present in the bundle.

Do not reference objects from another source report unless the same object is genuinely shared and supported by both sources.

## Deduplication

Deduplicate objects that represent the same entity or observable within the same source.

Deduplicate shared objects across sources only when they clearly represent the same entity or observable.

Do not deduplicate two objects only because they have similar names if the source context suggests they may be different.

Avoid duplicates by normalizing:

- named entities by normalized name and type;
- SCOs by normalized observable value;
- file SCOs by hash value;
- indicators by normalized STIX pattern;
- attack patterns by MITRE external ID when available;
- vulnerabilities by CVE ID when available.

Reuse existing object IDs in relationships.

If duplicate objects are merged, update all `object_refs`, `source_ref`, and `target_ref` values accordingly.

## ID Generation

All STIX IDs must follow:

```text
OBJECT_TYPE--UUID
```

The UUID must be valid.

The ID prefix must match the object's `type`.

Do not reuse the same ID for different objects.

Do not include the same object ID more than once in the bundle.

Do not include the same object ID more than once in a report `object_refs` array.

## Validation Workflow

Use the built-in tool `stix_bundle_validator` to validate the candidate bundle before returning the final answer when the tool is available.

Validation workflow:

1. Build a candidate STIX bundle JSON.
2. Call `stix_bundle_validator` with that bundle.
3. If `valid` is `false`, fix all blocking errors returned by the tool.
4. Treat the following validator errors as non-blocking in the OpenCTI-compatible enriched profile:
   - SCO contains `created`;
   - SCO contains `modified`;
   - SCO contains `created_by_ref`;
   - SCO contains `object_marking_refs`;
   - SCO contains `confidence`;
   - SCO contains `labels`;
   - SCO contains `external_references`;
   - SCO contains other OpenCTI-supported enrichment fields.
5. Do not remove enriched SCO metadata fields only because a strict STIX validator rejects them.
6. Re-run `stix_bundle_validator` only for blocking errors, with a maximum of 2 validation repair attempts (3 validator calls total: initial + two repairs).
7. If the validator answers `terminal: true`, the same error has been returned repeatedly and editing the bundle is not clearing it: stop calling `stix_bundle_validator` for that bundle and follow the `next_step` it returns.
8. If an object is invalid for a blocking reason and cannot be fixed without unsupported inference, remove that object and remove all references to it.
9. If remaining validation errors are only non-blocking OpenCTI-enriched SCO metadata errors, return the latest corrected bundle as the final output.
10. If `valid` is `true`, return only the final validated bundle JSON.

Do not skip this validation step when the tool is available.

## Final Validation Checklist

Before final output, verify:

1. JSON is valid.
2. Root object is a STIX bundle.
3. In extraction mode, bundle has non-empty `objects` (transformation mode may yield an empty bundle when the instruction removes everything).
4. In extraction mode, bundle contains at least one `report` (transformation mode may have no `report` when the instruction filters them out).
5. In extraction mode, bundle contains no `grouping` (transformation mode may include a `grouping` when the instruction asks for one).
6. Every object has valid `type`, `spec_version`, and `id`.
7. Every SDO and SRO has `created` and `modified`.
8. SCOs include OpenCTI-compatible metadata fields such as `created`, `modified`, `created_by_ref`, `object_marking_refs`, and `confidence` when appropriate.
9. Every relationship has `relationship_type`, `source_ref`, and `target_ref`.
10. Every relationship reference exists in the same bundle.
11. Every report `object_refs` entry exists in the same bundle.
12. No report references itself.
13. No `location` object exists unless it has `country`, `region`, or both `latitude` and `longitude`.
14. No `file` SCO exists without at least one meaningful file property such as `name` or `hashes`.
15. No `domain-name`, `ipv4-addr`, `ipv6-addr`, `url`, or `email-addr` SCO exists without `value`.
16. No object ID is duplicated in `bundle.objects`.
17. No object ID is duplicated inside a single `report.object_refs` array.
18. No object is created only to satisfy a relationship if the object is not grounded in the source.
19. No relationship is created only because two objects appear in the same document.
20. All fields required by the strict output schema are present on every object.
21. Fields required by the schema but not applicable to the object type are set to `null`.
22. For every non-location object, `country` is `null`, `region` is `null`, `latitude` is `null`, and `longitude` is `null`.
23. For every location object, at least one of `country`, `region`, or both `latitude` and `longitude` is non-null.
24. For every non-file object, `hashes` is `null`.
25. For every file object, `hashes` is non-null when a hash is available; otherwise the file object has another meaningful file property or is not created.
26. For every non-indicator object, `indicator_types` is `null`.
27. For every indicator object, `indicator_types` is non-null, `pattern` is non-null, `pattern_type` is `stix`, and `valid_from` is non-null.
28. Analytical objects and enriched SCOs include `confidence` when the output schema allows it.
29. Objects include `created_by_ref` only when the referenced identity exists in the bundle.
30. Objects include `object_marking_refs` only when the referenced marking definition is valid and available.
31. Indicators include `x_opencti_main_observable_type` when applicable.
32. OpenCTI custom fields such as `x_opencti_score` are only used when deterministic and useful.
33. No unsupported, fabricated, or dangling object or reference remains.
