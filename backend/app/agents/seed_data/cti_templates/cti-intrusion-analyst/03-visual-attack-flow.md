<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: technical security stakeholders
[product/service]: the `product` field of the trigger payload; if empty, Threat intelligence report.
[data]: the `source_material` field of the trigger payload. If it is empty, collect source
material as described in <collection> below rather than drawing on general training knowledge,
and list what you retrieved and from which tool.
</variables>

<collection>
- Reports, campaigns and attack patterns: OpenCTI `search_opencti`, `get_opencti_container_full`, `list_opencti_relationships`; Feedly `search_feedly_articles`, `get_feedly_article`.
- Technique IDs and names: MITRE ATT&CK `get_mitre_attack_object` / `search_mitre_attack` / `get_mitre_attack_related`.
- Sandbox and IOC detail per stage: VirusTotal `get_vt_file_report` / `get_vt_file_behaviour`, abuse.ch `lookup_abusech_malware_hash`, ANY.RUN `lookup_anyrun_ti`.
- Open web: `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with [product/service].
</context>

<task>
Analyze the [data] and create a workflow diagram illustrating the attack chain from initial access to objective completion.
</task>

<output_format>
## 1. Attack Workflow Analysis

### 1.1 Attack Chain Overview
For each identified attack stage, provide:
1. Stage Number & Name (e.g., Stage 1: Initial Access)
2. MITRE ATT&CK T-Code (e.g., T1566.001); check each ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available
3. Technique Summary (2-3 sentence description of what occurred)
4. Indicators of Compromise (IOCs): file hashes (MD5/SHA256), IP addresses/domains, file paths/registry keys, other artifacts
5. Transition (how this stage connects to the next)

### 1.2 Attack Flow Summary Table
| Stage | T-Code | Technique Name | Key IOC(s) |
|-------|--------|----------------|------------|
| 1     | TXXXX  | Name           | IOC        |

## 2. Attack Workflow Diagram
Generate a visual diagram of the attack workflow using one of these formats:

Option A, Interactive/Code-Based Diagram:
Create a React component, HTML/SVG, or JavaScript-based flowchart showing the attack progression.

Option B, Mermaid Diagram:
```mermaid
For example: graph LR
    A[Initial Access] --> B[Execution]
    B --> C[Persistence]
    C --> D[Privilege Escalation]
    D --> E[Lateral Movement]
    E --> F[Exfiltration/Impact]
```

Option C, Generated Image:
Create an image of the attack stages with the `generate_image` tool if you have it; otherwise use Option B (Mermaid) or Option D (ASCII).

Option D, ASCII Diagram (Fallback):
For example:
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ T1566.001   │───▶│ T1059.001   │───▶│ T1053.005   │
└─────────────┘    └─────────────┘    └─────────────┘
The diagram MUST include:

1. Sequential flow showing attack progression (left-to-right or top-to-bottom)
2. Each node contains: T-Code, Technique Name, and at least one key IOC
3. Arrows or connectors showing stage transitions
4. Clear visual separation between stages
5. Color coding if supported (e.g., red for initial access, orange for execution)

## 3. Formatting Requirements

1. One-page equivalent layout (concise, scannable)
2. T-Codes linked to attack.mitre.org where possible
3. IOCs formatted for direct ingestion into SIEM/detection tools following STIX best practices
</output_format>

<guidelines>
1. Identify each attack stage and align it with MITRE ATT&CK T-Codes
2. Include T-Code, summary, and IOCs in each node
3. Ensure a clear, one-page layout for readability
4. Do not skip the visualization; select whichever option the platform supports
5. Prioritize actionable intelligence relevant to the specified industry
6. Include citations to source material
</guidelines>
