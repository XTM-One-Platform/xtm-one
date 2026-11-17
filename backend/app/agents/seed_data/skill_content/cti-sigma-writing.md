# Sigma Rule Writing

Sigma rules are vendor-agnostic detection rules written in YAML. They can be converted to platform-specific query languages (Splunk SPL, Elastic queries, Microsoft Sentinel KQL, etc.). Use this skill when the user asks for a Sigma rule ("write a Sigma rule for X"), or when a hash investigation or malware analysis surfaces behaviour worth a vendor-agnostic detection.

## Workflow

1. **Ground the behaviour.** Start from observed behaviour (process tree, command line, registry key, network pattern), not from a single IOC. A Sigma rule that only lists hashes or IPs belongs in an IOC export, not a detection.
2. **Map to ATT&CK.** With the MITRE ATT&CK integration: `search_mitre_attack` to find the technique, `get_mitre_attack_object` to confirm the ID, tactic and data sources, `get_mitre_attack_related` for related detections and mitigations.
3. **Check for existing rules** before writing a new one:
   - With the Recorded Future integration: `search_rf_detection_rules` (Sigma, YARA and Snort rules published by Insikt Group).
   - With the OpenCTI integration: `search_opencti` or `list_opencti_entities` for Indicators with `pattern_type` sigma that already cover the technique or family.
   - In the target SIEM: `list_sentinel_analytics_rules`, `list_splunk_saved_searches` / `list_splunk_es_correlation_searches`, `list_elastic_rules`, `list_secops_rules`.
4. **Write the rule** following the structure and quality checklist below. Generate the `id` UUID with `code_interpreter` (`uuid.uuid4()`) rather than making one up, and use `code_interpreter` to parse the YAML and catch syntax errors.
5. **Test the logic** against real telemetry (see "Testing against your SIEM"), then tune filters for false positives.
6. **Convert or store only when asked.** Rule text goes in the answer. For platform-native conversion, load the built-in **OpenCTI Detection Rule Converter Skill** if it is attached. Create an OpenCTI Indicator (`create_opencti_indicator` with `pattern_type` sigma) only when the user or the assignment asks for it.

Use only the tools you actually have; when an integration is not connected, say which source or test was skipped instead of guessing its result.

## Rule Structure

```yaml
title: Descriptive title of the detection
id: UUID                          # Generate a unique UUID
status: experimental|test|stable
description: >
    Detailed description of what this rule detects and why.
references:
    - https://reference-url.com     # Source intelligence, blog posts, CVE
author: CTI Platform
date: YYYY/MM/DD
modified: YYYY/MM/DD
tags:
    - attack.initial_access         # ATT&CK tactic (lowercase, dots)
    - attack.t1566.001              # ATT&CK technique
    - cve.2024.12345                # CVE if applicable
logsource:
    category: process_creation      # Log category
    product: windows                # OS/product
    service:                        # Optional: specific service
detection:
    selection:
        FieldName|modifier:
            - 'value1'
            - 'value2'
    filter_known_good:
        FieldName: 'legitimate_value'
    condition: selection and not filter_known_good
falsepositives:
    - Description of known false positive scenario
level: critical|high|medium|low|informational
```

## Log Source Categories

| Category | Description | Common fields |
|----------|-------------|---------------|
| `process_creation` | New process started | Image, CommandLine, ParentImage, User |
| `network_connection` | Network connection initiated | DestinationIp, DestinationPort, SourceIp |
| `file_event` | File created/modified/deleted | TargetFilename, Image |
| `registry_event` | Registry key/value change | TargetObject, Details |
| `dns_query` | DNS resolution | QueryName, QueryType |
| `image_load` | DLL/module loaded | ImageLoaded, Image |
| `pipe_created` | Named pipe created | PipeName |
| `ps_script` | PowerShell script execution | ScriptBlockText |
| `webserver` | Web server access logs | cs-uri-query, c-ip |
| `firewall` | Firewall logs | src-ip, dst-ip, dst-port, action |

## Field Modifiers

| Modifier | Description | Example |
|----------|-------------|---------|
| `contains` | Substring match | `CommandLine\|contains: '-enc'` |
| `startswith` | Starts with | `Image\|startswith: 'C:\Temp'` |
| `endswith` | Ends with | `Image\|endswith: '\powershell.exe'` |
| `re` | Regex match | `CommandLine\|re: '.*-e(nc)?.*'` |
| `base64offset` | Base64 encoded content | `CommandLine\|base64offset: 'IEX'` |
| `all` | All values must match | `CommandLine\|contains\|all:` |
| `cidr` | CIDR range match | `DestinationIp\|cidr: '10.0.0.0/8'` |

## Detection Logic

### Condition operators
- `and` - All conditions must match
- `or` - Any condition must match
- `not` - Negate a condition
- `1 of selection*` - Any of the named selections matching `selection*`
- `all of selection*` - All named selections must match

### Common patterns

**Process execution with specific arguments:**
```yaml
detection:
    selection:
        Image|endswith: '\cmd.exe'
        CommandLine|contains:
            - '/c whoami'
            - '/c ipconfig'
            - '/c net user'
    condition: selection
```

**Suspicious parent-child relationship:**
```yaml
detection:
    selection:
        ParentImage|endswith: '\outlook.exe'
        Image|endswith:
            - '\cmd.exe'
            - '\powershell.exe'
            - '\wscript.exe'
    condition: selection
```

**Network connection to suspicious destination:**
```yaml
detection:
    selection:
        DestinationIp|cidr:
            - '185.220.0.0/16'
    filter_internal:
        SourceIp|cidr: '10.0.0.0/8'
    condition: selection and not filter_internal
```

## MITRE ATT&CK Tag Format

```yaml
tags:
    - attack.tactic_name           # e.g., attack.initial_access
    - attack.tXXXX                 # e.g., attack.t1566
    - attack.tXXXX.XXX             # e.g., attack.t1566.001 (sub-technique)
```

Common tactic tags: `attack.initial_access`, `attack.execution`, `attack.persistence`, `attack.privilege_escalation`, `attack.defense_evasion`, `attack.credential_access`, `attack.discovery`, `attack.lateral_movement`, `attack.collection`, `attack.command_and_control`, `attack.exfiltration`, `attack.impact`

## Testing against your SIEM

Sigma itself does not execute. To test a rule, translate its detection logic into the query language of a connected SIEM and run it as a read-only hunt over a short time window first (24 hours), then widen. Keep the translated test query in the output so the reader can reproduce it.

| SIEM | Integration | Run a test hunt | Check existing coverage |
|------|-------------|-----------------|-------------------------|
| Microsoft Sentinel | Sentinel | `run_sentinel_kql_query` | `list_sentinel_analytics_rules` |
| Microsoft Defender XDR | Defender | `run_defender_hunting_query` (advanced hunting) | - |
| Splunk | Splunk | `run_splunk_search` (then `get_splunk_search_results` for long jobs) | `list_splunk_saved_searches`, `list_splunk_es_correlation_searches` |
| Elastic Security | Elastic Security | `run_elastic_esql_query`, `run_elastic_eql_query`, `search_elastic` | `list_elastic_rules` |
| Google SecOps | Google SecOps | `validate_secops_udm_query`, then `run_secops_udm_search` | `list_secops_rules` |

What to look for:
- **Known-bad**: the rule should fire on a known-malicious event (from the sample's sandbox behaviour, an incident, or a purple-team test).
- **Known-good**: run the selection without the filters over a normal week; every recurring benign hit becomes a `filter_*` block or a `falsepositives` entry.
- **Volume**: a rule that returns thousands of hits per day on a normal environment is a hunting query, not an alert. Lower the `level` or tighten the selection.
- **Field coverage**: confirm the log source actually exists in the SIEM (for Sentinel, `list_sentinel_data_connectors`; for Splunk, `list_splunk_indexes`). An empty result on a missing table is not a pass.

Report testing honestly: "tested on Sentinel over 7 days, 0 false positives, fired on the sandbox replay" or "not tested: no SIEM connected". Do not create or enable analytics rules, saved searches or SecOps rules unless the user asks for deployment.

## Quality Checklist

- [ ] Unique UUID generated for `id` field
- [ ] ATT&CK technique(s) mapped in tags
- [ ] False positives documented
- [ ] Level accurately reflects severity
- [ ] Log source correctly specified
- [ ] Detection logic tested against known-good and known-bad scenarios (or explicitly marked untested)
- [ ] References link to source intelligence
- [ ] Description explains WHAT is detected and WHY it matters

## Output

Return each rule as a fenced `yaml` block in the answer, preceded by a one-line summary (technique, log source, level, test status). Suggested file name when the user wants a file (`generate_file`): `TECHNIQUE_ID-SLUG.yml`. In OpenCTI, store a rule only when asked, as an Indicator with `pattern_type` sigma, the rule text as the pattern, and relationships to the related attack pattern, malware or intrusion set.

## Related skills

- **Hash Investigation**, **Malware Analysis** - produce the behavioural intelligence that informs the rule.
- **YARA Rule Writing** - static file-content counterpart; **KQL Query Writing** - native Sentinel / Defender queries.
- **OpenCTI Detection Rule Converter Skill** - conversion of a rule or STIX pattern into platform-native formats.
