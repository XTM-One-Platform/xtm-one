# MITRE ATT&CK Reference

How to query MITRE ATT&CK in XTM One (techniques, tactics, groups, software, campaigns, mitigations, data sources and detection strategies) and how to use it in analysis: mapping observed behaviour to technique IDs, cross-referencing threat-actor profiles with ATT&CK groups, and finding detection coverage gaps.

## Dataset

ATT&CK is served by the **MITRE ATT&CK integration**, not by a local file. The integration loads the official STIX 2.1 collections, keeps them cached, and covers three domains: `enterprise-attack` (default), `mobile-attack` (Android, iOS) and `ics-attack` (industrial control systems), as enabled by the administrator. Pass `domain` on a call to leave Enterprise.

- `get_mitre_attack_status` reports the enabled domains, the pinned or loaded ATT&CK version and the latest published release. Cite the ATT&CK version in any product that maps techniques, and check it when an ID seems missing or renamed.
- Use only the tools you actually have. When the MITRE ATT&CK integration is not connected, say so and do not produce technique IDs, names or tactic assignments from memory without flagging them as unverified. As a partial fallback, OpenCTI usually holds the ATT&CK attack patterns imported by its MITRE connector (`search_opencti`, `list_opencti_entities`).

For AI and machine-learning systems, use the **MITRE ATLAS integration** instead (see "MITRE ATLAS" below).

## Querying ATT&CK

| Need | Tool | Example |
|---|---|---|
| You only have a name, alias or keyword | `search_mitre_attack` | `query="Cozy Bear"` finds APT29; `query="credential dumping"`; restrict with `object_types` |
| Full detail of one object | `get_mitre_attack_object` | `attack_id="T1566"`, `"T1059.001"`, `"TA0002"`, `"G0016"`, `"S0154"`, `"M1038"`, `"C0028"`, or an exact name such as `"Mimikatz"` |
| The tactics, in matrix order | `list_mitre_attack_tactics` | returns TA IDs and the shortnames used as the tactic filter |
| All techniques for a tactic or platform | `list_mitre_attack_techniques` | `tactic="initial-access"`, `platform="Windows"`, `subtechniques="include"`, `modified_since="2026-01-01"` |
| All objects of a type | `list_mitre_attack_objects` | `object_type="groups"`, `name_filter="APT"`; also software, campaigns, mitigations, data sources, data components, detection strategies, analytics, assets (ICS) |
| Relationships (the core pivot) | `get_mitre_attack_related` | see the recipes below |

### Find a technique by ID

`get_mitre_attack_object(attack_id="T1566")` returns the description, tactics, platforms, detection guidance, the attack.mitre.org URL and a relationship summary (sub-techniques, mitigations, groups and software that use it, detection strategies).

### List all techniques for a tactic

`list_mitre_attack_tactics()` first, to get the current shortnames, then `list_mitre_attack_techniques(tactic="SHORTNAME")`. Enterprise tactic shortnames have long been: `reconnaissance`, `resource-development`, `initial-access`, `execution`, `persistence`, `privilege-escalation`, `defense-evasion`, `credential-access`, `discovery`, `lateral-movement`, `collection`, `command-and-control`, `exfiltration`, `impact`. Releases can rename or split tactics, so trust the tool's list over this one.

### Find a threat group

`search_mitre_attack(query="APT28", object_types=["groups"])` matches the group name and its aliases (Fancy Bear, Sofacy, Forest Blizzard...), then `get_mitre_attack_object(attack_id="G0007")` for the detail. ATT&CK models groups as STIX `intrusion-set` objects.

### Map a group to techniques

`get_mitre_attack_related(attack_id="G0007", relationship="uses", direction="outgoing", related_type="techniques")` lists the techniques the group uses, with the procedure examples ATT&CK documents (how exactly the group used each technique). Use `related_type="software"` for its malware and tools, and `relationship="attributed-to", direction="incoming", related_type="campaigns"` for the campaigns ATT&CK attributes to it.

### Reverse pivots

- Which groups use a technique: `get_mitre_attack_related(attack_id="T1566", direction="incoming", related_type="groups")`.
- What mitigates a technique: `get_mitre_attack_related(attack_id="T1059", relationship="mitigates", direction="incoming")`.
- Which detection strategies detect it: `get_mitre_attack_related(attack_id="T1059", relationship="detects", direction="incoming")`.

### Find software or malware

Software objects are STIX `malware` or `tool`: `search_mitre_attack(query="Mimikatz")` or `list_mitre_attack_objects(object_type="software", name_filter="...")`, then `get_mitre_attack_related` for the techniques it implements and the groups that use it.

### What changed in a release

`list_mitre_attack_techniques(modified_since="YYYY-MM-DD")` and `list_mitre_attack_objects(object_type="groups", modified_since="YYYY-MM-DD")` triage what a new ATT&CK release touched.

## Key object types

| STIX type | ATT&CK concept | ID prefix | Key fields |
|------|----------------|-----------|-----------|
| `attack-pattern` | Technique / sub-technique | T#### / T####.### | name, description, kill_chain_phases, external_references (technique ID) |
| `x-mitre-tactic` | Tactic | TA#### | name, x_mitre_shortname |
| `intrusion-set` | Group | G#### | name, aliases, description |
| `malware` | Malware (software) | S#### | name, description |
| `tool` | Tool (software) | S#### | name, description |
| `campaign` | Campaign | C#### | name, first_seen, last_seen |
| `course-of-action` | Mitigation | M#### | name, description |
| `x-mitre-data-source` / `x-mitre-data-component` | Data source / component | DS#### / DC#### | name, description |
| `x-mitre-detection-strategy` / `x-mitre-analytic` | Detection strategy / analytic | DET#### / AN#### | name, description |
| `relationship` | Links objects | | source_ref, target_ref, relationship_type (`uses`, `mitigates`, `subtechnique-of`, `detects`, `attributed-to`, `targets`, `revoked-by`) |

Deprecated and revoked objects are hidden by default; `search_mitre_attack` can include them (`include_deprecated`) when a report cites an old ID. A revoked technique points to its replacement: map to the replacement, and say so.

## Common ATT&CK technique references

| ID | Name | Common use |
|----|------|-----------|
| T1566 | Phishing | Initial access via email |
| T1566.001 | Spearphishing Attachment | Malicious attachment |
| T1566.002 | Spearphishing Link | Malicious URL |
| T1059 | Command and Scripting Interpreter | Execution via scripts |
| T1059.001 | PowerShell | PowerShell execution |
| T1078 | Valid Accounts | Using legitimate credentials |
| T1071 | Application Layer Protocol | C2 over HTTP/HTTPS/DNS |
| T1055 | Process Injection | Code injection for evasion |
| T1053 | Scheduled Task/Job | Persistence via scheduled tasks |
| T1547 | Boot or Logon Autostart Execution | Persistence via autostart |
| T1562 | Impair Defenses | Disabling security tools |
| T1486 | Data Encrypted for Impact | Ransomware encryption |
| T1190 | Exploit Public-Facing Application | Vulnerability exploitation |

Confirm names and tactics with `get_mitre_attack_object` before publishing: names are occasionally revised between releases.

## Usage in analysis

- **Map observed TTPs to technique IDs.** Describe the behaviour first (what the actor did, with evidence), then search for the technique (`search_mitre_attack` with a behaviour keyword), read its description, and choose the most specific sub-technique the evidence supports. Do not map a technique the evidence does not show; a sandbox tag alone is a candidate, not a mapping.
- **Cross-reference threat-actor profiles with ATT&CK groups.** Resolve the vendor name through aliases (`search_mitre_attack`), then compare the techniques you observed with the group's documented set (`get_mitre_attack_related`). Technique overlap is weak evidence for attribution: many groups use the same techniques. Use it to support, never to establish, an attribution.
- **Identify detection coverage gaps.** Build the technique set that matters (from the PIRs, a threat profile or a group), list your detection rules from the connected SIEM / EDR integrations (for example `list_sentinel_analytics_rules`, `list_splunk_saved_searches`, `list_elastic_rules`, `list_secops_rules`) and the techniques they claim to cover, and report the techniques with no rule. `get_mitre_attack_related(..., relationship="detects", direction="incoming")` gives the ATT&CK detection strategies and data components to aim new rules at. Testing coverage with simulations is an OpenAEV task.
- **Prioritise mitigations.** For the gap techniques, `get_mitre_attack_related(..., relationship="mitigates", direction="incoming")` lists the mitigations; count how many priority techniques each one covers.

### ATT&CK Navigator layer

XTM One has no Navigator integration. To give the user a visual heatmap, build a Navigator layer JSON with `code_interpreter` and deliver it with `generate_file` (`CONTEXT-layer.json`) for import into ATT&CK Navigator:

```json
{
  "name": "APT28 observed vs documented",
  "domain": "enterprise-attack",
  "versions": {"layer": "4.5"},
  "description": "Generated from the MITRE ATT&CK integration, ATT&CK version [from get_mitre_attack_status]",
  "techniques": [
    {"techniqueID": "T1566.001", "score": 2, "comment": "Observed in incident 2026-04 and documented for G0007"},
    {"techniqueID": "T1059.001", "score": 1, "comment": "Documented for G0007, not observed"}
  ],
  "gradient": {"colors": ["#ffe766", "#ff6666"], "minValue": 0, "maxValue": 2}
}
```

Set the layer version to the one the user's Navigator expects if it differs.

## Recording the mapping (only when asked)

In OpenCTI, link the entities to attack patterns with `create_opencti_relationship` (intrusion set / malware / campaign `uses` attack-pattern), inside a draft (`create_opencti_draft`) so an analyst reviews it; look the attack pattern up first (`search_opencti`) rather than creating a duplicate. In STIX output, follow the **STIX Bundle Authoring** skill for attack-pattern objects with the ATT&CK `external_id`.

## MITRE ATLAS (AI and machine-learning systems)

For attacks on AI or ML systems (model evasion, poisoning, prompt injection, model theft, LLM-agent abuse), use the **MITRE ATLAS integration**:

- `search_atlas` for a keyword, `get_atlas_technique`, `get_atlas_tactic`, `get_atlas_mitigation` for details, `get_atlas_related_objects` for relationships.
- `list_atlas_tactics`, `list_atlas_techniques`, `list_atlas_mitigations` and `get_atlas_matrix` for the layout.
- `list_atlas_case_studies` and `get_atlas_case_study` for documented real-world incidents.
- `map_attack_to_atlas` to find the ATLAS counterpart of an ATT&CK technique when an intrusion moves from IT infrastructure to an AI system.
- `get_atlas_status` for the loaded ATLAS version.

## Related skills

- **Threat Actor Profiling**, **Campaign Tracking**: profiles that cite ATT&CK groups and techniques
- **Malware Analysis**: behaviour-to-technique mapping for a sample
- **Sigma Rule Writing**, **KQL Query Writing**, **YARA Rule Writing**: detections for the gap techniques
- **STIX Bundle Authoring**: attack-pattern objects in STIX output
