<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: technical security stakeholders
[product/service]: the `product` field of the trigger payload; if empty, Threat intelligence report.
"Your environment" in the output is the Security stack line of the Organization context; if it is
missing, prioritize by sector and region only and say so.
[data]: the `source_material` field of the trigger payload. If it is empty, collect source
material as described in <collection> below rather than drawing on general training knowledge,
and list what you retrieved and from which tool.
</variables>

<collection>
When [data] is empty, collect recent reporting on intrusions, campaigns and malware relevant to [sector name] and [country/region]:
- OpenCTI: `search_opencti`, `list_opencti_entities` (Reports, Intrusion Sets, Campaigns, Malware), `get_opencti_container_full` for the report content, `list_opencti_relationships` for the attack patterns they use.
- Feedly: `search_feedly_articles`, `get_feedly_article`, `get_feedly_trending_threats`.
- Vendor intelligence: CrowdStrike `list_crowdstrike_intel_reports`, GTI `search_gti_threats` / `get_gti_threat_mitre_tree`, Recorded Future `search_rf_analyst_notes`, Sekoia `search_sekoia_intelligence`, Defender TI `list_defender_ti_articles`.
- Procedures and data sources: MITRE ATT&CK `get_mitre_attack_object` / `get_mitre_attack_related` (detections and data components for each technique).
- Web: `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with [product/service].
</context>

<task>
Create a table of attack procedures as threat hunt hypotheses. Go through the [data] and for each attack procedure, document the information specified below.
</task>

<output_format>
## 1. Threat Hunt Hypotheses Table

Create a table with the following columns:
| Procedure | Description | Logs |
|-----------|-------------|------|
| Short title of the procedure | Detailed description with patterns | Relevant logs and Event IDs |

## 2. Prioritization Notes
1. High-priority hypotheses based on relevance to your environment
2. Quick wins vs. comprehensive hunts
3. Required data sources and gaps
</output_format>

<guidelines>
1. Provide detailed technical information for each procedure
2. Include only actionable procedures for threat hunting
3. Focus on specific search patterns and detection logic
4. Avoid generic or ambiguous information
5. Prioritize hypotheses relevant to the specified industry and region
6. Include citations to source material
7. Where you cite an ATT&CK technique ID, check each ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available
</guidelines>
