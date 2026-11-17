# Intelligence Feedback Loops

Feedback is the final phase of the intelligence cycle (Phase 6 of the CTI Hyperloop). Without it, the cycle is open-ended and quality degrades over time. This skill covers four feedback types - consumer feedback, source quality tracking, analyst retrospectives and detection effectiveness - and how each one flows back into planning and direction.

## Types of Feedback

### 1. Consumer Feedback
Intelligence consumers (stakeholders) assess the value of products they receive.

**Collection points:**
- After every significant product delivery
- During quarterly PIR reviews
- During stakeholder briefings

**Questions:**
| Question | Response Options |
|----------|-----------------|
| Was this intelligence useful for your decision-making? | Very useful / Somewhat useful / Not useful |
| Was it delivered in time to act on? | Yes / Partially / No |
| Was the format appropriate for your needs? | Yes / Too technical / Too high-level / Wrong format |
| Was the confidence assessment helpful? | Yes / Unclear / Not included |
| What should we prioritise next? | Free text |

**Actions:**
- Useful + timely -> Continue current approach, reinforce collection sources
- Useful + late -> Improve detection/triage speed, adjust collection frequency
- Not useful -> Review PIR alignment, stakeholder needs, product format
- Format wrong -> Adjust per the **CTI Stakeholder Management** skill

Ask the questions at the end of the product or in the delivery message. Do not send a message to a stakeholder on an external channel unless the user or the assignment asks for it.

### 2. Source Quality Tracking
Track the reliability of intelligence sources over time.

**For each source used in products:**
- Was the information confirmed, partly confirmed, or disproven?
- Did the source's Admiralty rating need adjustment?
- Were there timeliness issues?

**Actions:**
- Consistently confirmed -> Upgrade reliability rating (e.g., C -> B)
- Inconsistent -> Maintain or downgrade rating
- Repeatedly disproven -> Downgrade to D or E
- Feed updates into source-assessment guidance (the **Source Assessment (Admiralty Scale)** skill gives the scale; your team's adjusted ratings are recorded as described under Tracking)

### 3. Analyst Retrospectives
After significant investigations or assessments:

**Questions:**
- What analytical techniques were applied? Were they effective?
- Were key assumptions validated or invalidated?
- Were there intelligence gaps that could have been filled?
- Were alternative hypotheses adequately considered?
- What would you do differently?

**Actions:**
- Update SOPs with lessons learned (record the proposed change; the built-in **CTI Standard Operating Procedures** skill is read-only, so team-specific changes are kept as described under Tracking)
- Adjust technique selection guidance
- Fill identified collection gaps
- Record validated/invalidated assumptions against the relevant knowledge

### 4. Detection Effectiveness
For detection rules produced from intelligence:

**Metrics:**
- True positive rate (did the rule catch real threats?)
- False positive rate (did it generate noise?)
- Coverage (did it miss known instances?)

**Actions:**
- High FP -> Refine rule, add exclusions
- Missed detections -> Expand rule, add variants
- No hits -> Verify rule logic, check data source availability

**Where the metrics come from** (use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result):
- Microsoft Sentinel: alerts and incidents raised by an analytics rule (`list_sentinel_analytics_rules`, `list_sentinel_incidents`, `run_sentinel_kql_query` over `SecurityAlert` / `SecurityIncident` with the incident classification for TP/FP).
- Microsoft Defender: alerts and their classification (`list_defender_alerts`), advanced hunting (`run_defender_hunting_query`).
- Splunk: fired alerts and notable events with their status (`list_splunk_fired_alerts`, `list_splunk_es_notable_events`, `run_splunk_search`).
- Elastic Security: alerts per rule (`list_elastic_alerts`, `get_elastic_rule`).
- Google SecOps: detections per rule (`list_secops_rule_detections`).
- OpenAEV: whether a simulated technique was detected or prevented (expectation results, `search_openaev_findings`, `query_openaev_analytics`) - the most direct test of coverage.

Compute rates with `code_interpreter` from the exported counts, and state the observation window.

## Feedback Integration

All feedback feeds back into Phase 1 (Planning & Direction):

```
Consumer says "not useful"       -> Review and adjust PIRs
Source proves unreliable         -> Update Admiralty ratings, adjust collection
Analyst retrospective finds gap  -> Create new collection tasking
Detection rule has high FP       -> Refine and redeploy
```

PIR adjustments follow the **PIR Management** skill.

## Tracking

Feedback state must survive between runs. Record it on the platform, not in the conversation only:

| Feedback | Where to record it in XTM One |
|----------|-------------------------------|
| Consumer feedback | Agent memory (`store_memory`, keyed by stakeholder and product), alongside the stakeholder register (see **CTI Stakeholder Management**); optionally a Note on the OpenCTI Report that was delivered |
| Source quality | Agent memory (one entry per source with its current team rating and the evidence); in OpenCTI, the source's Organization/Identity entity can carry a Note with the rating history |
| Retrospective outcomes | The run output or a report (`generate_report`); lessons to keep go to agent memory or a knowledge base (`store_knowledge`) when the team uses one |
| Detection effectiveness | Agent memory per rule; when the rule is stored as an OpenCTI Indicator, a Note on that indicator |

Write to OpenCTI only when the user or the assignment asks for it. Retrieve earlier feedback with `search_memory` / `search_knowledge` before running a review, so trends are visible ("third 'too technical' response from the CISO this quarter").
