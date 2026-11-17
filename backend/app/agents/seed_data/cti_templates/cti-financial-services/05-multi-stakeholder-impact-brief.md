<variables>
[job role], [country/region] and [stakeholder team names] come from the "Organization context"
section of your instructions (and the Company Context knowledge base); for [stakeholder team names],
keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: CTI Analyst
[country/region]: Global
[stakeholder team names]: Fraud, SOC, and Risk Teams
Assess payment, identity and core banking relevance against the Payment infrastructure and Identity
and authentication systems lines of the Organization context, and regulatory notification against
its Regulatory profile line; where they are placeholders, use generic BFSI infrastructure and say so.
[data]: the `source_material` field of the trigger payload (enriched intelligence, for example a
completed Third-Party and Ecosystem Risk Assessment or Fraud Impact Assessment). If it is empty,
collect the record named in the `incident_reference` field as described in <collection> below
rather than drawing on general training knowledge, and list what you retrieved and from which tool.
If both fields are empty, say that enriched intelligence or an incident reference is needed and stop.
</variables>

<collection>
- Enriched record for `incident_reference`: OpenCTI `search_opencti`, then `get_opencti_container_full` (Report, Case or Grouping); TheHive `get_thehive_case`, `list_thehive_observables`; earlier assessments you stored, with `search_memory`.
- IOC actions in Section B: confirm type and reputation with VirusTotal `get_vt_ip_report` / `get_vt_domain_report` / `get_vt_file_report`, GreyNoise `lookup_greynoise_ip`, Recorded Future `enrich_rf_indicators`.
- TTPs in Section B: check each ID named in the source with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available; never add techniques the source does not name.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] at a financial institution in [country/region].
I need to communicate the same enriched intelligence to
fraud, SOC, and risk teams simultaneously, each in their
own language and focused on what is relevant to their
function, without running separate briefings for each team.
</context>

<task>
Using the enriched intelligence in [data], which may
be a completed Third-Party and Ecosystem Risk Assessment
or Fraud Impact Assessment, produce a multi-stakeholder
impact brief with three stakeholder-specific sections.

Each section should be written for that team's priorities
and language. Do not repeat the same content across sections,
each team gets only what is relevant to them.

Before using this prompt, confirm the intelligence has
been sanitized in accordance with your institution's
data handling and TLP compliance requirements.

The enriched intelligence is [data], the `source_material` field of the trigger payload.
</task>

<output_format>
## Multi-Stakeholder Impact Brief

### BLUF
Two to three sentences maximum. What the intelligence
shows, what is confirmed, and the single most important
action required right now. Written for all three teams
to read as shared context before reviewing their
specific section.

### Section A, Fraud Team
*Focus: payment exposure, fraud signal types,
monitoring adjustments*

| Field | Detail |
|-------|--------|
| Payment types at immediate risk | |
| Fraud types implicated | BEC / ATO / Payment redirection / SIM swap |
| Identity systems at risk | |
| Recommended monitoring adjustments | |
| Immediate actions required | |

*Note: Flag institution-specific thresholds before
sharing this section.*

### Section B, SOC Team
*Focus: indicators of compromise, containment priorities,
detection opportunities*

#### IOCs, Action Immediately
| Indicator | Type | Action | Priority |
|-----------|------|--------|----------|
| | Hash / Domain / IP / URL | Block / Monitor / Hunt | Immediate / 24hr |

#### MITRE ATT&CK TTPs
| Tactic | Technique ID | Technique Name | BFSI Relevance |
|--------|-------------|----------------|----------------|
| | T#### | | Payment / Identity / Core Banking |

*Note: Only include TTPs confirmed in source material.
Do not map techniques not present in the intelligence.*

#### Containment Priorities
1. [First priority action]
2. [Second priority action]
3. [Third priority action]

#### Detection and Hunting
| Opportunity | Hypothesis | Owner |
|-------------|------------|-------|
| | | SOC Tier 1 / Tier 2 |

### Section C, Risk Team
*Focus: business impact, regulatory exposure,
escalation requirements*

| Field | Detail |
|-------|--------|
| Financial exposure estimate | |
| Customer impact | |
| Service disruption risk | |
| Regulatory notification required | Yes / No / Requires Review |
| Recommended escalation | |
</output_format>

<guidelines>
1. Write each section for that team's language and
   priorities, fraud language for Section A, technical
   language for Section B, business language for Section C
2. Do not repeat content across sections, each team
   receives only what is relevant to their function
3. Flag institution-specific thresholds and regulatory
   determinations for analyst review before sharing
4. If the intelligence does not support a finding for
   a specific field, mark as "Not confirmed in source
   material" rather than leaving blank. All outputs must
   be validated against source material before sharing.
5. Only include TTPs confirmed in source material, do
   not map techniques not present in the intelligence
   and do not invent MITRE T-code mappings
6. Do not use em dashes anywhere in the output.
   Use a comma, colon, or period instead.
</guidelines>
