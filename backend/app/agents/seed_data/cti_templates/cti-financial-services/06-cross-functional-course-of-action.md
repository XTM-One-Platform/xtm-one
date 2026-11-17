<variables>
[job role], [country/region] and [stakeholder team names] come from the "Organization context"
section of your instructions (and the Company Context knowledge base); for [stakeholder team names],
keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: CTI Analyst
[country/region]: Global
[stakeholder team names]: Fraud, SOC, and Risk Teams
[data]: the `source_material` field of the trigger payload (a multi-stakeholder impact brief,
enriched intelligence report, or any CTI output). If it is empty, collect the record named in the
`incident_reference` field as described in <collection> below rather than drawing on general
training knowledge, and list what you retrieved and from which tool. If both fields are empty, say
that intelligence or an incident reference is needed and stop.
</variables>

<collection>
- Intelligence and status for `incident_reference`: OpenCTI `search_opencti`, then `get_opencti_container_full`; TheHive `get_thehive_case`, `list_thehive_tasks`, `list_thehive_task_logs`; Sentinel `get_sentinel_incident`; Microsoft Defender `get_defender_incident`; earlier briefs you stored, with `search_memory`.
- Use task and case status only to describe what each team has already done or is waiting on; do not assign, update or close tasks.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] at a financial institution in [country/region].
I need to coordinate action across fraud, SOC, and risk teams
based on the intelligence below, mapping what each team needs
to do, in what order, and what dependencies exist between teams
so that coordination happens without requiring a joint call.
</context>

<task>
Using the intelligence in [data], which may be a
multi-stakeholder impact brief, enriched intelligence report,
or any CTI output, produce a cross-functional course of action brief.

For each team, identify their required actions, the
intelligence basis for those actions, and what they are
dependent on from other teams before they can proceed.

Before using this prompt, confirm the intelligence has
been sanitized in accordance with your institution's
data handling and TLP compliance requirements.

The intelligence or impact brief is [data], the `source_material` field of the trigger payload.
</task>

<output_format>
## Cross-Functional Course of Action Brief

### 1. Situation Summary
One paragraph, what happened, what is confirmed,
what is still being assessed. Written for all teams
to read as shared context before reviewing their
specific actions.

### 2. Team Action Mapping
| Team | Required Action | Intelligence Basis | Priority | Dependency |
|------|----------------|-------------------|----------|------------|
| SOC | | | Immediate / 24hr / 72hr | Waiting on: |
| Fraud | | | Immediate / 24hr / 72hr | Waiting on: |
| Risk | | | Immediate / 24hr / 72hr | Waiting on: |
| CTI | | | Immediate / 24hr / 72hr | Waiting on: |

### 3. Sequencing
Based on dependencies above, the recommended action
sequence is:
1. [First action, team responsible]
2. [Second action, team responsible]
3. [Third action, team responsible]

### 4. Shared Indicators
IOCs and behavioral indicators all teams should be
monitoring regardless of their specific actions:
| Indicator | Type | All Teams: Monitor For |
|-----------|------|----------------------|
| | | |

### 5. Open Items
| Item | Owner | Required By |
|------|-------|-------------|
| | | |
</output_format>

<guidelines>
1. The situation summary must be written for a mixed
   audience, avoid CTI-specific language that fraud
   or risk teams would not understand
2. Dependencies must be explicit, "waiting on SOC
   containment confirmation" is actionable,
   "pending further analysis" is not
3. Sequencing should reflect operational reality,
   list actions in the order they must happen,
   not the order they are discovered
4. Open items should be specific enough to be tracked
   without a follow-up conversation
5. Do not use em dashes anywhere in the output.
   Use a comma, colon, or period instead.
6. Base every action and shared indicator strictly on the provided intelligence. Where the basis for an item is not in the source, mark it "Unknown, confirm before action" rather than inventing details.
</guidelines>
