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
- Without supplied material, start from the actors that target [sector name] and [country/region]: OpenCTI `search_opencti`, `list_opencti_entities` (Intrusion Sets, Threat Actors, recent Reports) and `list_opencti_relationships` (uses attack-pattern); Feedly `search_feedly_articles`, `lookup_feedly_entity`, `get_feedly_trending_threats`.
- Per-actor technique sets: MITRE ATT&CK `get_mitre_attack_related` / `search_mitre_attack`; GTI `get_gti_threat_mitre_tree`; CrowdStrike `get_crowdstrike_actor`; Recorded Future `search_rf_threat_actors`.
- Open web: `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with [product/service].
</context>

<task>
Map threat actors and their corresponding Tactics, Techniques, and Procedures (TTPs) using the [data]. For each threat actor mentioned, create a separate table.
</task>

<output_format>
## 1. Threat Actor TTP Mapping

### [Threat Actor Name]
| MITRE Tactic | Technique ID & Name (linked to attack.mitre.org) |
|--------------|--------------------------------------------------|
| Initial Access | T1566 - Phishing |

(Repeat table for each threat actor)

## 2. Emerging TTP Analysis
1. Are there emerging TTPs that are becoming more prevalent?
2. Which techniques are shared across multiple threat actors?
3. What defensive gaps do these TTPs reveal?

## 3. Recommendations
1. Priority techniques to detect based on prevalence
2. Defensive improvements aligned to observed TTPs
</output_format>

<guidelines>
1. Create a separate table for each threat actor identified in the [data]
2. Link all MITRE ATT&CK technique IDs to attack.mitre.org; check each ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available
3. Identify patterns and emerging TTPs across multiple actors
4. Prioritize TTPs relevant to the specified industry and region
5. Provide actionable recommendations based on the analysis
6. Include citations to source material
</guidelines>
