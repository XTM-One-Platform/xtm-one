<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: technical security stakeholders
[product/service]: the `product` field of the trigger payload; if empty, Threat intelligence report.
[Threat Actor Name]: the `threat_actor_name` field of the trigger payload. If it is empty, use the
actor the [data] is about and say so.
[data]: the `source_material` field of the trigger payload. If it is empty, collect source
material on [Threat Actor Name] as described in <collection> below rather than drawing on general
training knowledge, and list what you retrieved and from which tool.
</variables>

<collection>
- Resolve the actor and its aliases first: OpenCTI `search_opencti` then `get_opencti_entity` (Intrusion Set or Threat Actor), Feedly `lookup_feedly_entity`.
- Relationships (targets, malware, tools, attack patterns, campaigns, indicators): OpenCTI `list_opencti_relationships`, `get_opencti_top_related`; Feedly `get_feedly_entity_relationships`.
- Vendor profiles: CrowdStrike `get_crowdstrike_actor`, GTI `get_gti_threat_profile` / `get_gti_threat_mitre_tree`, Recorded Future `search_rf_threat_actors`, Intel 471 `get_intel471_actor`, MISP `list_misp_galaxy_clusters`.
- Recent reporting: Feedly `search_feedly_articles`, OpenCTI reports via `list_opencti_entities`; ransomware groups: ransomware.live `get_ransomlive_group`.
- Techniques: MITRE ATT&CK `search_mitre_attack` / `get_mitre_attack_related`; indicators and YARA: OpenCTI `list_opencti_observables`, abuse.ch `search_abusech_iocs`, ransomware.live `get_ransomlive_yara`.
- Open web: `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with [product/service].
</context>

<task>
Generate a comprehensive Threat Actor Profile about [Threat Actor Name] using the [data].
</task>

<output_format>
## 1. Title
Threat Actor Profile: [Threat Actor Name]

## 2. Executive Summary
High-level overview of the threat actor and relevance to your organization.

## 3. Threat Actor Overview
1. Background and history
2. Known aliases and affiliations
3. Motivation and objectives
4. Estimated capabilities and resources

## 4. Targeting and Victimology
1. Geographic regions targeted
2. Industries and sectors targeted
3. Types of organizations targeted
4. Selection criteria and patterns

## 5. Tactics, Techniques, and Procedures (TTPs)
1. Attack vectors and initial access methods
2. Malware and tools used
3. Infrastructure characteristics
4. Operational security measures
5. MITRE ATT&CK mapping (check each ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available)

## 6. Attribution and Campaign Linking
1. Evidence linking to known campaigns or incidents
2. Confidence level in attribution
3. Connections to other threat groups or nation-states

## 7. Indicators of Compromise (IoCs)
1. Malicious IPs and domains
2. File hashes
3. YARA rules (if available)
4. Other detection artifacts

## 8. Risk Assessment
1. Potential impact to your organization
2. Likelihood of targeting based on sector and geography
3. Financial and reputational risk considerations

## 9. Recommendations
1. Detection and monitoring recommendations
2. Prevention and hardening measures
3. Response and containment guidance
</output_format>

<guidelines>
1. Build comprehensive profiles based on available intelligence
2. Include IOCs and TTPs that can be operationalized immediately
3. Assess relevance to the specified industry and region
4. Provide confidence levels for attribution claims
5. Balance depth with actionability for defenders
6. Include citations to source material
</guidelines>
