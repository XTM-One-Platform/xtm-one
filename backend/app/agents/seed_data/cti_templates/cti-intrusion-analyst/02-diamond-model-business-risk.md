<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: technical security stakeholders
[data]: the `source_material` field of the trigger payload (the validated extraction and report).
If it is empty, collect source material as described in <collection> below rather than drawing
on general training knowledge, and list what you retrieved and from which tool.
</variables>

<collection>
- The report and its extracted knowledge: OpenCTI `search_opencti`, `get_opencti_container_full`, `get_opencti_entity`, `list_opencti_relationships`; Feedly `get_feedly_article`, `search_feedly_articles`.
- Adversary context: CrowdStrike `get_crowdstrike_actor`, GTI `get_gti_threat_profile`, Recorded Future `search_rf_threat_actors`, Feedly `lookup_feedly_entity`.
- Infrastructure: VirusTotal `get_vt_domain_report` / `get_vt_ip_report`, DomainTools `get_domaintools_hosting_history`, Shodan `get_shodan_host_info`.
- Techniques (Enterprise v19): MITRE ATT&CK `get_mitre_attack_object` / `search_mitre_attack`.
- Open web: `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to give [stakeholder team names] a Diamond Model analysis of the intrusion.
</context>

<task>
Conduct a Diamond Model of Intrusion Analysis using all pertinent information in [data]: adversary, capabilities, infrastructure, and victim.
</task>

<output_format>
## 1.1 Adversary
Alias, suspected origin, motivation, attribution confidence, known group associations.

## 1.2 Capabilities
Malware and tools, exploited CVEs, observed ATT&CK techniques (v19), sophistication, delivery.

## 1.3 Infrastructure
C2 domains and IPs, hosting and ASNs, certificate fingerprints, sender domains, pivot points.

## 1.4 Victim
Targeted sectors, geography, victimology patterns, known compromised organizations.

## 2. Confidence and Intelligence Gaps
What is confirmed versus assessed, key unknowns, recommended pivots.
</output_format>

<guidelines>
1. Fill every section from [data] ONLY. Where information for an element is not available, state 'Not available in provided reports' rather than filling the gap with general knowledge.
2. Provide attribution with a confidence level; treat any actor naming as an assessment, not a fact.
3. Use estimative language (likely, probable, possible).
4. Map any techniques to MITRE ATT&CK Enterprise v19 or above; check each ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available.
5. Include inline citations to the source material.
6. Return Markdown only.
</guidelines>
