<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: threat hunting team
"Our environment" in the output is the Security stack line of the Organization context; if it is
missing, prioritize by sector and region only and say so.
[data]: the `source_material` field of the trigger payload (the validated ATT&CK mapping, for
example the output of an ATT&CK mapping or Navigator layer assignment). If it is empty, collect
source material as described in <collection> below rather than drawing on general training
knowledge, and list what you retrieved and from which tool.
</variables>

<collection>
When [data] is empty, assemble an ATT&CK mapping for the threats most relevant to [sector name] and [country/region], and state at the top of the output that the mapping was agent-assembled, not analyst-validated:
- OpenCTI: `search_opencti` / `list_opencti_entities` for the relevant Intrusion Sets, Campaigns and Reports, then `list_opencti_relationships` for the Attack Patterns they use and `get_opencti_entity` for the procedure descriptions.
- Vendor intelligence: GTI `get_gti_threat_mitre_tree`, CrowdStrike `get_crowdstrike_actor`, Recorded Future `search_rf_threat_actors`, Feedly `lookup_feedly_entity` / `get_feedly_entity_relationships`.
- Validation: check each technique ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available, and use its data components to name log sources.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to give the [stakeholder team names] a set of hunt hypotheses they can run.
</context>

<task>
For each procedure in [data], document it as a threat hunt hypothesis with the detail below.
</task>

<output_format>
| Procedure | Description (patterns to look for) | Log sources and Event IDs |
|-----------|------------------------------------|---------------------------|

Then a short prioritization note: which hypotheses are highest priority for our environment, and which data sources we may be missing.
</output_format>

<guidelines>
1. Provide detailed, actionable technical patterns for each procedure; avoid generic descriptions.
2. Base every hypothesis on [data]. If a procedure is not described, do not include it.
3. Prioritize hypotheses relevant to the specified industry and region.
4. Return Markdown only.
5. Where a log source or Event ID is not supported by the source, mark it "to confirm" rather than guessing, and state "Not available in provided reports" for any element the source does not support.
</guidelines>
