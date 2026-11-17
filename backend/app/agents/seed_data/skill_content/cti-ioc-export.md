# IOC Export

Export formats and procedures for indicators of compromise: CSV, STIX 2.1, OpenIOC and MISP. This skill covers format conversion, packaging and delivery, either as a downloadable file or as objects written to OpenCTI or MISP.

## Rules for tool use

- Build the export with `code_interpreter` (deduplication, validation, CSV / JSON / XML serialisation, UUID generation) and hand the result to the user with `generate_file`. Do not paste a large export inline.
- Validate every STIX 2.1 bundle with `stix_bundle_validator` before delivering or importing it.
- Writing to OpenCTI or MISP changes shared data: do it only when the user or the assignment asks for it, and prefer an OpenCTI draft so an analyst reviews before it reaches the knowledge graph.
- Use only the tools you actually have. When OpenCTI or MISP is not connected, deliver the file and say that the platform write-back was skipped.

## Supported export formats

### CSV

Standard tabular format. Most broadly compatible.

```csv
indicator,type,first_seen,last_seen,confidence,tlp,source,context,mitre_attack,tags
203.0.113.42,ipv4-addr,2026-01-15,2026-03-20,85,GREEN,"Mandiant report MAL-2026-001",C2 server for SUNBURST variant,T1071.001,"apt29;sunburst"
evil.example.com,domain-name,2026-02-01,2026-03-20,70,GREEN,"Internal analysis","Phishing landing page",T1566.002,"phishing;apt29"
```

**Column definitions:**

| Column | Required | Description |
|--------|----------|-------------|
| indicator | Yes | The IOC value |
| type | Yes | STIX indicator type: ipv4-addr, ipv6-addr, domain-name, url, file:hashes.SHA-256, file:hashes.MD5, email-addr |
| first_seen | Yes | ISO date first observed |
| last_seen | No | ISO date last observed |
| confidence | Yes | 0-100 confidence score |
| tlp | Yes | TLP marking |
| source | Yes | Source description |
| context | No | What the IOC represents (C2, phishing, etc.) |
| mitre_attack | No | ATT&CK technique IDs |
| tags | No | Semicolon-separated tags |

Values are exported refanged (machine-readable) by default. If the file is meant for humans or for an email body, ask whether to defang (`[.]`, `hxxp`) and say which form the file uses. Quote every field that may contain a comma, and neutralise spreadsheet formula injection (a value starting with `=`, `+`, `-` or `@` gets a leading apostrophe) when the CSV is meant to be opened in a spreadsheet.

### STIX 2.1 bundle

See the **STIX Bundle Authoring** skill for object templates, patterns, relationships and TLP marking definitions. For bundles meant for OpenCTI, the built-in **OpenCTI STIX Bundle Construction** skill holds the OpenCTI-specific rules. Always run `stix_bundle_validator` on the result.

### OpenIOC (XML)

Mandiant's legacy IOC format. Still used by some tools.

```xml
<?xml version="1.0" encoding="utf-8"?>
<ioc xmlns="http://schemas.mandiant.com/2010/ioc" id="[UUID]" last-modified="YYYY-MM-DDT00:00:00">
  <short_description>IOC Title</short_description>
  <description>Description</description>
  <authored_by>CTI Platform</authored_by>
  <authored_date>YYYY-MM-DDT00:00:00</authored_date>
  <definition>
    <Indicator operator="OR" id="[UUID]">
      <IndicatorItem id="[UUID]" condition="is">
        <Context document="Network" search="Network/DNS" type="mir"/>
        <Content type="string">evil.example.com</Content>
      </IndicatorItem>
      <IndicatorItem id="[UUID]" condition="is">
        <Context document="FileItem" search="FileItem/Md5sum" type="mir"/>
        <Content type="md5">abc123...</Content>
      </IndicatorItem>
    </Indicator>
  </definition>
</ioc>
```

Generate the UUIDs and escape XML special characters (`&`, `<`, `>`, `"`) in every value with `code_interpreter`.

### MISP format (JSON)

For import into a MISP instance, or as a file for partners who run MISP.

```json
{
  "Event": {
    "info": "IOC collection: [context]",
    "threat_level_id": "2",
    "analysis": "2",
    "distribution": "1",
    "Tag": [
      {"name": "tlp:green"},
      {"name": "misp-galaxy:mitre-attack-pattern=\"Phishing - T1566\""}
    ],
    "Attribute": [
      {
        "type": "ip-dst",
        "category": "Network activity",
        "value": "203.0.113.42",
        "to_ids": true,
        "comment": "C2 server"
      },
      {
        "type": "domain",
        "category": "Network activity",
        "value": "evil.example.com",
        "to_ids": true,
        "comment": "Phishing landing page"
      }
    ]
  }
}
```

## MISP attribute types

| IOC type | MISP attribute type | Category |
|----------|-------------------|----------|
| IPv4 address | ip-dst / ip-src | Network activity |
| IPv6 address | ip-dst / ip-src | Network activity |
| Domain | domain | Network activity |
| URL | url | Network activity |
| Email address | email-src | Payload delivery |
| SHA-256 hash | sha256 | Payload delivery |
| MD5 hash | md5 | Payload delivery |
| SHA-1 hash | sha1 | Payload delivery |
| Filename | filename | Payload delivery |
| Registry key | regkey | Persistence mechanism |
| Mutex | mutex | Artifacts dropped |

When MISP is connected, `describe_misp_attribute_types` returns the instance's own type and category list; use it when an indicator does not fit the table.

## Export workflow

1. **Collect** all IOCs from the investigation or analysis: the conversation, an uploaded file, an OpenCTI container (`get_opencti_container_full`), OpenCTI observables or indicators (`list_opencti_observables`, `list_opencti_entities`), or a MISP event (`get_misp_event`, `search_misp_attributes`).
2. **Deduplicate** (same indicator + same type = one entry).
3. **Validate** the format (IP parsing, hash length, URL format) and drop private or reserved addresses with a note. When MISP is connected, `check_misp_warninglists` flags likely false positives (public resolvers, CDNs, top sites) before they go into a blocklist.
4. **Apply the TLP marking** (inherit the most restrictive marking of the sources, or set it explicitly). Load the **TLP Guide** skill if attached for the sharing rules.
5. **Set confidence scores** (0-100, consistent with the **Confidence Levels** skill if attached).
6. **Generate** the export in the requested format with `code_interpreter`.
7. **Deliver** it (see below).

## Delivery

### As a file

Produce the file with `generate_file`, using these names:

| Format | File name |
|---|---|
| CSV | `YYYY-MM-DD-CONTEXT.csv` |
| STIX 2.1 | `YYYY-MM-DD-CONTEXT.stix.json` (validated with `stix_bundle_validator` first) |
| OpenIOC | `YYYY-MM-DD-CONTEXT.ioc` |
| MISP | `YYYY-MM-DD-CONTEXT.misp.json` |

State in the reply the TLP of the file, the number of indicators per type, and anything dropped during validation.

### Into OpenCTI (only when asked)

- Create a draft first (`create_opencti_draft`, then `use_opencti_draft`), so the import can be reviewed and validated in OpenCTI.
- Create the observables (`create_opencti_observable`) and the indicators (`create_opencti_indicator`, with pattern, `pattern_type`, score and valid-from), the entities they indicate (`create_opencti_entity`) and the relationships (`create_opencti_relationship`). Use `promote_opencti_observable` to create an indicator from an existing observable.
- Group them in a container (`create_opencti_container` for a report or grouping, `add_opencti_to_container`), and apply markings and labels (`add_opencti_marking`, `add_opencti_label`).
- To keep the original file with the knowledge, attach it to the container with `upload_opencti_file`.
- Check the result with `get_opencti_draft_info` and tell the user the draft must be validated in OpenCTI.

### Into MISP (only when asked)

- `create_misp_event` (info, threat level, analysis, distribution), then `add_misp_attribute` per indicator with the type and category from the table above and `to_ids` set deliberately (true only for indicators fit for detection).
- `add_misp_tag` for TLP and galaxy tags (TLP applied as a tag, not as a STIX marking).
- `publish_misp_event` only on explicit request: publishing pushes the event to the sharing community.

### Into a detection platform (only when asked)

Microsoft Sentinel `create_sentinel_threat_indicator`, Microsoft Defender `create_defender_indicator`, CrowdStrike `create_crowdstrike_ioc` and Splunk ES `add_splunk_es_threat_intel` accept indicators directly. These change alerting or blocking behaviour, so confirm the action (alert vs block), the expiry and the scope with the user before calling them.

## Related skills

- **STIX Bundle Authoring** and **OpenCTI STIX Bundle Construction**: the STIX 2.1 object model and the OpenCTI rules
- **IOC Enrichment Workflow**: enrich before exporting, so confidence and context columns are filled
- **TLP Guide**, **Confidence Levels**: the marking and the score carried by every exported indicator
- **Intelligence Sharing**: choosing the audience and channel for the export
