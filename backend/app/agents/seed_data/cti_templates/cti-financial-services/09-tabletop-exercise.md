<variables>
[job role], [country/region] and [stakeholder team names] come from the "Organization context"
section of your instructions (and the Company Context knowledge base); for [stakeholder team names],
keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: CTI Analyst
[country/region]: Global
[stakeholder team names]: CTI, SOC, Fraud, Incident Response, Compliance/Legal
Use the Institution type, Payment infrastructure, Identity and authentication systems and Critical
third parties lines of the Organization context for the environment; anything the scenario needs
that is not there or in [data] goes to Section 6 as an assumption.
[data]: the `source_material` field of the trigger payload (threat intelligence). If it is empty,
collect intelligence on the threat named in the `threat_focus` field as described in <collection>
below rather than drawing on general training knowledge, and list what you retrieved and from which
tool. If both fields are empty, use the most active threat to financial institutions in the last 30
days from Feedly `query_feedly_cyberattacks` or OpenCTI, and say which you chose.
</variables>

<collection>
- Actor or campaign and its targeting: OpenCTI `search_opencti`, `get_opencti_entity`, `list_opencti_relationships`; CrowdStrike `get_crowdstrike_actor`; GTI `get_gti_threat_profile`; Recorded Future `search_rf_threat_actors`; Feedly `search_feedly_articles`, `query_feedly_cyberattacks`.
- Techniques: check each technique ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available; use only techniques the intelligence names.
- Existing exercises to build on: OpenAEV `search_openaev_scenarios` (read-only); do not create or change a scenario.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] at a financial institution in [country/region].
I need to produce a threat-informed tabletop exercise
for [stakeholder team names] grounded in current threat
intelligence, not a generic template, that tests realistic
response to active threats against BFSI infrastructure.
</context>

<task>
Using the threat intelligence in [data], produce a
tabletop exercise for a BFSI institution.

Ground the scenario in the specific TTPs, infrastructure
targets, and fraud dimensions present in the intelligence.
Map decision points to each participating team. Include
an inject timeline that can be run without additional
facilitation preparation.

Flag assumptions about the institutional environment
where the scenario requires details not present in
the source intelligence.

Before using this prompt, confirm the intelligence has
been sanitized in accordance with your institution's
data handling and TLP compliance requirements.

The threat intelligence is [data], the `source_material` field of the trigger payload.
</task>

<output_format>
## Threat-Informed Tabletop Exercise

### 1. Exercise Overview
| Field | Detail |
|-------|--------|
| Threat actor / campaign basis | |
| Primary target | Payment rails / Identity systems / Core banking |
| Exercise objective | What this exercise is designed to test |
| Participating teams | |
| Estimated duration | |

### 2. Exercise Narrative
Two to three paragraph description of the cyberattack incident
written for exercise participants, realistic, specific
to BFSI infrastructure, grounded in the source intelligence.

### 3. Inject Timeline
| Time | Inject | Team | Decision Required |
|------|--------|------|-------------------|
| T+0 | Opening scenario | All | Initial assessment and escalation decision |
| T+X | | | |
| T+X | | | |
| T+X | | | |
| T+X | Scenario conclusion | All | Resolution and lessons learned |

### 4. Team Decision Points
| Team | Key Decision | What Good Response Looks Like | What Poor Response Looks Like |
|------|-------------|-------------------------------|-------------------------------|
| CTI | | | |
| SOC | | | |
| Fraud | | | |
| Incident Response | | | |
| Compliance/Legal | | | |

### 5. Stress Test Questions
Questions the exercise should force each team to answer:
1. [Question]
2. [Question]
3. [Question]

### 6. Environmental Assumptions
Assumptions made about the institutional environment
where the exercise requires details not in the source
intelligence, flag for facilitator review before running.
</output_format>

<guidelines>
1. Ground every inject in the source intelligence,
   do not introduce attack vectors or TTPs not present
   in the threat intelligence
2. Decision points must reflect how the threat actor
   actually operates, not generic incident response process
3. Flag all environmental assumptions explicitly,
   facilitators need to validate these before running
   the exercise
4. Stress test questions should expose gaps in
   cross-team coordination, not just individual
   team process
5. Do not use em dashes anywhere in the output.
   Use a comma, colon, or period instead.
</guidelines>
