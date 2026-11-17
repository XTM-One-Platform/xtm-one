<variables>
[job role], [country/region] and [stakeholder team names] come from the "Organization context"
section of your instructions (and the Company Context knowledge base); for [stakeholder team names],
keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: CTI Analyst
[country/region]: Global
[stakeholder team names]: Risk and Operational Continuity Teams
[primary regulator]: the `primary_regulator` field of the trigger payload (e.g. OCC / FDIC /
Federal Reserve / State DFI); if empty, the Regulatory profile line of the Organization context; if
that is a placeholder, Not specified.
[applicable state(s)]: the `applicable_states` field of the trigger payload (e.g. NY, CA, for state
breach notification); if empty, the Regulatory profile line; otherwise Not specified.
[EU operations in scope]: the `eu_operations_in_scope` field of the trigger payload (Yes / No /
Under assessment); if it is "From Organization context", the Regulatory profile line; if that is a
placeholder, Under assessment.
[applicable frameworks]: the `applicable_frameworks` field of the trigger payload (e.g. FFIEC CAT,
NIST CSF, DORA, NY DFS Part 500, CA CCPA / breach notification, PCI DSS, MITRE ATT&CK / D3FEND,
Other); if empty, the frameworks in the Regulatory profile line. REQUIRED, no default: if neither
names a framework, do not produce the assessment; reply that [applicable frameworks] must be
provided and stop.
[control inventory]: the `control_inventory` field of the trigger payload (sanitized control
inventory or recovery plan); if empty, collect it as described in <collection> below and mark
"Countermeasure In Place?" as "Unknown, control inventory not provided" wherever you find no evidence.
[data]: the `source_material` field of the trigger payload (threat landscape summary). If it is
empty, collect source material as described in <collection> below rather than drawing on general
training knowledge, and list what you retrieved and from which tool.
</variables>

<collection>
- Threat landscape when [data] is empty: actors and campaigns targeting financial institutions in the last 90 days from OpenCTI `search_opencti`, `list_opencti_relationships`; Feedly `get_feedly_trending_threats`, `query_feedly_cyberattacks`; GTI `get_gti_threat_profile`; Recorded Future `search_rf_threat_actors`.
- ATT&CK: check each technique ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available.
- Controls and recovery plans when [control inventory] is empty: the Company Context knowledge base (`search_knowledge`); Microsoft Defender `list_defender_secure_score_controls`, `list_defender_security_recommendations`; OpenAEV `search_openaev_findings`, `query_openaev_analytics` (validated control outcomes).
- D3FEND has no XTM One integration: verify countermeasure names on d3fend.mitre.org with `web_fetch` when available, and otherwise flag them for verification as guideline 5 says.
- Regulatory deadlines: take them from the source, the Company Context knowledge base or the regulator's published text (`search_google_web` and `web_fetch`); never state a deadline you could not source.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] at a financial institution in [country/region],
regulated by [primary regulator], operating in [applicable
state(s)], with EU operations in scope: [EU operations in scope].
I need to identify where current controls and recovery plans
do not adequately address active or emerging threats against
BFSI infrastructure, producing a prioritized gap assessment
for [stakeholder team names] before an incident exposes those
gaps. Gap prioritization and remediation recommendations must
be grounded in [applicable frameworks].
</context>

<task>
Using the threat landscape summary and control inventory
or recovery plan below, produce a resilience gap
assessment.

For each gap identified:
- Map the offensive TTP to MITRE ATT&CK
- Identify the corresponding MITRE D3FEND defensive
  countermeasure and assess whether it is currently
  in place
- Assess potential impact if exploited and likelihood
  based on current threat actor behavior
- Assess whether the gap creates exposure under
  [applicable frameworks] and flag which framework
  drives remediation priority
- Where multiple frameworks apply to a single gap,
  identify the shortest applicable deadline as the
  controlling timeline

Prioritize gaps by potential impact to payment
infrastructure, customer access, and regulatory
compliance, not just technical severity.

Before using this prompt, confirm the inputs and intelligence has
been sanitized in accordance with your institution's
data handling and TLP compliance requirements.

The threat landscape summary is [data], the `source_material` field of the trigger payload.

The sanitized control inventory or recovery plan is [control inventory], the `control_inventory`
field of the trigger payload.
</task>

<output_format>
## Resilience Gap Assessment

### 1. Assessment Summary
| Field | Detail |
|-------|--------|
| Threat landscape basis | |
| Primary regulator | |
| Applicable states | |
| EU operations in scope | |
| Frameworks applied | |
| Controls or recovery plans assessed | |
| Total gaps identified | |
| Critical gaps requiring immediate action | |

### 2. Gap Analysis
| Gap | ATT&CK TTP | D3FEND Countermeasure | Countermeasure In Place? | Potential Impact | Likelihood | Regulatory Exposure | Controlling Framework | Priority |
|-----|------------|----------------------|--------------------------|-----------------|------------|--------------------|-----------------------|----------|
| | | | Yes / Partial / No | High / Med / Low | High / Med / Low | | Framework + deadline | Immediate / Near-term / Long-term |

### 3. Priority Gap Detail
For each Immediate priority gap:

**Gap:** [description]
- ATT&CK TTP: [tactic and technique]
- D3FEND countermeasure: [defensive technique]
- Countermeasure status: [in place / partial / absent,
  describe what exists and why it is insufficient]
- Threat basis: [which active threat actor behavior
  or campaign this gap exposes]
- Potential impact: [payment, customer, regulatory]
- Regulatory exposure: [applicable framework(s)
  and controlling deadline]
- Recommended remediation: [specific action, not
  general best practice]
- Owner: [which team is responsible]
- Target timeline: [driven by shortest applicable
  regulatory deadline]

### 4. Remediation Roadmap
| Gap | ATT&CK TTP | D3FEND Countermeasure | Recommended Action | Owner | Timeline | Controlling Framework |
|-----|------------|----------------------|--------------------|-------|----------|-----------------------|
| | | | | | Immediate / 30 days / 90 days | |

### 5. Residual Risk
Gaps that cannot be fully remediated in the near term
and the controls in place to manage residual risk:
| Gap | Residual Risk | Compensating Control | Framework Acknowledgment Required |
|-----|---------------|---------------------|----------------------------------|
| | | | Yes / No |
</output_format>

<guidelines>
1. Prioritize gaps by BFSI impact, payment infrastructure,
   customer access, and regulatory compliance take
   precedence over generic technical severity
2. Every gap must include both an ATT&CK TTP and a
   corresponding D3FEND countermeasure, if D3FEND
   does not map directly, note this explicitly rather
   than forcing an approximate match
3. Where multiple frameworks apply to a single gap,
   the shortest deadline controls the remediation
   timeline. Where deadlines are equivalent, apply
   this tiebreaker sequence: (1) the framework under
   which the institution is currently under examination
   or most recent audit finding, (2) the framework
   carrying the highest penalty exposure, (3) analyst
   judgment, document the basis for the decision
4. Residual risk must be documented for gaps that
   cannot be immediately remediated, unacknowledged
   residual risk is a compliance exposure under most
   applicable frameworks
5. D3FEND countermeasure mappings should be verified
   against the current D3FEND matrix at d3fend.mitre.org
   before sharing this assessment, taxonomy and technique
   identifiers are updated periodically and may not match
   what is generated here exactly
6. Do not use em dashes anywhere in the output.
   Use a comma, colon, or period instead.
</guidelines>
