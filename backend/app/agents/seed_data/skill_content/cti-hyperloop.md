# CTI Hyperloop Framework

The CTI Hyperloop (based on Google/Mandiant's practical implementation framework) extends the traditional intelligence cycle by running it at three levels at the same time, with continuous bidirectional feedback between them. It is the operational doctrine behind the CTI skills: use it to decide at which level a request sits, which lifecycle phases it goes through, and which roles and skills handle each phase.

## Core concept

Traditional intelligence cycle: linear, single-track, often stuck at the tactical level.
Hyperloop: three parallel tracks (strategic, operational, tactical) feeding each other continuously.

```
STRATEGIC  <->  OPERATIONAL  <->  TACTICAL
    |              |               |
Direction -> Collection -> Processing -> Analysis -> Dissemination -> Feedback
    ^                                                              |
    +--------------------------------------------------------------+
```

## Intelligence levels

### Strategic intelligence
- **Time horizon**: 6-24 months
- **Consumers**: CISO, Board, Risk Management, Security Architecture
- **Focus**: Threat landscape trends, emerging threats, geopolitical developments, sector risk posture
- **Products**: Threat landscape reports, strategic assessments, horizon scanning reports, maturity assessments
- **Example PIR**: "How will the ransomware threat landscape evolve over the next 12 months and what are the implications for our risk posture?"

### Operational intelligence
- **Time horizon**: Weeks to months
- **Consumers**: SOC Manager, IR Lead, Security Operations
- **Focus**: Active campaigns, threat actor profiling, TTP analysis, vulnerability exploitation trends
- **Products**: Campaign reports, threat actor profiles, operational assessments, hunt packages
- **Example PIR**: "What TTPs are currently being used by groups targeting our sector?"

### Tactical intelligence
- **Time horizon**: Hours to days
- **Consumers**: SOC Analysts, Detection Engineers, IR Analysts
- **Focus**: IOCs, detection rules, immediate response support, indicator enrichment
- **Products**: IOC packages, Sigma / YARA / KQL rules, flash reports, enrichment reports
- **Example PIR**: "What IOCs are associated with the current wave of attacks targeting our VPN appliances?"

## Roles

The phase tables below name roles. In XTM One a role is either the same agent with the matching skills attached, or a dedicated agent that the orchestrating agent delegates to (sub-agent task). Each skill is loaded by its display name; a run may load at most 8 distinct skills, so split long chains across roles or runs.

| Role | Does | Typical XTM One means |
|---|---|---|
| Orchestrator | Direction, routing, PIRs, feedback | **CTI Orchestrator** skill; OpenCTI PIRs (`manage_opencti_pir`) or agent memory |
| OSINT researcher | Open-source and landscape collection | `search_google_web`, `web_fetch`, `browser`, Feedly, RSS, NewsAPI integrations |
| Tool agents | Indicator lookups against intelligence sources | The threat-intel integrations (see the **Threat Intel Sources** skill) |
| IOC processor | Normalise, deduplicate, enrich, export | `code_interpreter`, OpenCTI observables / drafts, `stix_bundle_validator` |
| Analyst | Judgment, structured analytic techniques | Analytical and knowledge-cell skills |
| Report writer | Finished products | Production skills; `generate_report` |
| Detection engineer | Rules and hunts | Detection skills; Sentinel, Splunk, Defender, Elastic, Google SecOps integrations |
| Quality reviewer | Gate before dissemination | **Intelligence Quality Control** skill |

## Phase mapping

### Phase 1: Planning and direction
**What**: Define what intelligence is needed and set priorities.

| Level | Activity | Role | Skills |
|-------|----------|------|--------|
| Strategic | Set / review PIRs, horizon scanning briefs | Orchestrator | **PIR Management**, **CTI Stakeholder Management** |
| Operational | Define collection priorities for active investigations | Orchestrator | **PIR Management**, **CTI Standard Operating Procedures** |
| Tactical | Identify IOC collection gaps, detection coverage gaps | Orchestrator | **CTI Standard Operating Procedures** |

### Phase 2: Collection
**What**: Gather raw data from relevant sources.

| Level | Activity | Role | Skills |
|-------|----------|------|--------|
| Strategic | Trend monitoring, landscape research | OSINT researcher | **OSINT Methodology** |
| Operational | Threat actor tracking, campaign research, dark web monitoring | OSINT researcher, tool agents | **OSINT Methodology**, **Dark Web Collection** |
| Tactical | IOC lookups, bulk enrichment | Tool agents, IOC processor | **Threat Intel Sources**, **IOC Enrichment Workflow** |

### Phase 3: Processing
**What**: Normalise, deduplicate, and structure collected data.

| Level | Activity | Role | Skills |
|-------|----------|------|--------|
| All | IOC deduplication, format normalisation, source assessment tagging | IOC processor | **IOC Enrichment Workflow**, **Source Assessment (Admiralty Scale)** |

### Phase 4: Analysis
**What**: Apply human judgment to produce assessed intelligence.

| Level | Activity | Role | Skills |
|-------|----------|------|--------|
| Strategic | Horizon scanning, maturity assessment, trend analysis | Analyst | **Horizon Scanning**, **Threat Assessment**, **CTI Maturity Assessment** |
| Operational | ACH, threat actor profiling, campaign tracking | Analyst | **Analysis of Competing Hypotheses**, **Threat Actor Profiling**, **Campaign Tracking**, **Key Assumptions Check** |
| Tactical | IOC correlation, detection gap analysis, indicator pivoting | Analyst | **Indicator Pivoting**, **Vulnerability Intelligence** |

### Phase 5: Dissemination
**What**: Deliver finished intelligence to stakeholders.

| Level | Activity | Role | Skills |
|-------|----------|------|--------|
| Strategic | Landscape reports, annual assessments, board briefings | Report writer | **Intelligence Writing**, **Writing Assessments**, **CTI Stakeholder Management** |
| Operational | Actor profiles, campaign reports, operational assessments | Report writer | **Intelligence Writing**, **Writing Assessments** |
| Tactical | IOC packages, detection rules, flash reports | Detection engineer, IOC processor, report writer | **Sigma Rule Writing**, **YARA Rule Writing**, **KQL Query Writing**, **IOC Export**, **STIX Bundle Authoring** |

In XTM One, dissemination can be the chat / run output, a document produced with `generate_report`, an OpenCTI Report or Note (when asked), or a channel message from an assignment.

### Phase 6: Feedback
**What**: Assess effectiveness and refine direction.

| Level | Activity | Role | Skills |
|-------|----------|------|--------|
| All | Consumer feedback, source quality tracking, PIR refinement | Orchestrator | **Intelligence Feedback Loops**, **PIR Management** |

## Running the tracks continuously

The three tracks run in parallel, not one after the other. In XTM One, recurring work is an assignment on an agent with a trigger, for example:

- **Tactical** - a frequent assignment (or a webhook / event trigger) that enriches new indicators and flags detection gaps.
- **Operational** - a weekly assignment that reviews active campaigns and actors against the PIRs.
- **Strategic** - a monthly or quarterly assignment that produces a landscape or horizon-scanning product.

Each track records what it learned (OpenCTI entities when asked, or agent memory with `store_memory`) so the other tracks can pick it up.

## Bidirectional flow examples

**Tactical -> Operational**: IOC enrichment reveals an infrastructure pattern shared across multiple incidents -> triggers a campaign tracking investigation.

**Operational -> Strategic**: Campaign analysis identifies a nation-state actor shifting its targeting to a new sector -> feeds into the strategic landscape assessment and a PIR update.

**Strategic -> Operational**: Horizon scanning identifies AI-augmented social engineering as an emerging threat -> creates operational collection tasking for specific TTP monitoring.

**Operational -> Tactical**: Threat actor profiling reveals a preferred exploitation technique -> drives the creation of specific Sigma detection rules.

**Strategic -> Tactical**: Risk assessment identifies an unpatched VPN as a critical exposure -> prioritises vulnerability intelligence and IOC monitoring for VPN exploits.

**Tactical -> Strategic**: A spike in credential-stuffing alerts -> informs the strategic assessment of the underground economy and the infostealer trend.

## Orchestrator workflow mapping

When the orchestrator receives a task, it maps it to the Hyperloop:

| User request | Primary level | Hyperloop phases | Key roles |
|-------------|---------------|-----------------|------------|
| "Investigate this IP" | Tactical | 2 -> 3 -> 4 -> 5 | Tool agents -> IOC processor -> Analyst -> Detection engineer |
| "Profile this threat actor" | Operational | 2 -> 3 -> 4 -> 5 -> 6 | OSINT researcher -> IOC processor -> Analyst -> Report writer -> record new findings (OpenCTI / memory) |
| "Write a threat assessment on X" | Strategic / Operational | 1 -> 2 -> 3 -> 4 -> 5 -> 6 | Orchestrator (PIRs) -> OSINT researcher -> Analyst (SATs) -> Report writer -> Quality reviewer |
| "Enrich this IOC list" | Tactical | 2 -> 3 -> 5 | Tool agents -> IOC processor -> Export |
| "What's the current ransomware landscape?" | Strategic | 2 -> 4 -> 5 | OSINT researcher -> Analyst (knowledge cell) -> Report writer |
| "Create detection rules for APT28 TTPs" | Tactical | 4 -> 5 | Analyst (knowledge cell) -> Detection engineer |
| "Review our PIRs" | Management | 1 -> 6 | Orchestrator (PIR management, feedback loops) |
