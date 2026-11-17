# CTI Standard Operating Procedures

Seven standard operating procedures for a CTI function: daily triage, IOC processing, flash reports, threat actor profile updates, stakeholder briefings, quarterly PIR review and knowledge maintenance.

The **Owner** of each SOP is a role. In XTM One a role is played by an agent (or the analyst in the chat) and can be handed to a specialised agent through delegation when the platform offers it. Recurring SOPs (daily triage, quarterly PIR review, monthly knowledge maintenance) fit naturally as scheduled assignments on a CTI agent.

Use only the tools you actually have; when an integration is not connected, say which source or step was skipped instead of guessing its result. Never write to OpenCTI, MISP or a security control unless the user or the assignment asks for it.

## SOP-001: Daily Intelligence Triage

**Frequency**: Daily
**Owner**: Orchestrator (lead CTI agent)

1. Review overnight intelligence feeds and alerts.
   - OpenCTI: newest reports and indicators (`list_opencti_entities` sorted by creation date), notifications from triggers the team set up.
   - When connected: Feedly (`get_feedly_stream_articles`, `get_feedly_trending_threats`), RSS (`fetch_rss_feeds`), Recorded Future alerts (`list_rf_alerts`), GTI DTM alerts (`list_gti_dtm_alerts`), Intel 471 alerts (`list_intel471_alerts`), Flashpoint alerts (`list_flashpoint_alerts`), new KEV entries (`search_vulnintel_kev`), ransomware leak-site victims (`list_ransomlive_recent_victims`).
2. Check active PIRs - any new information relevant to Critical/High PIRs?
   - OpenCTI PIRs: `manage_opencti_pir` (action `list`), then `query_opencti_pir_entities` to see what was newly flagged, ranked by PIR score.
   - Without OpenCTI PIRs: the PIR set kept in agent memory (`search_memory`).
3. Triage new indicators:
   - Discard noise (known benign, low confidence, duplicate). MISP warning lists (`check_misp_warninglists`) catch common benign values.
   - Fast-track critical indicators (active exploitation, direct relevance).
   - Queue remaining for standard processing (SOP-002).
4. Dispatch OSINT research for any trending threats relevant to PIRs (see the **OSINT Methodology** skill).
5. Record significant new intelligence: in OpenCTI (note, report, relationship) when the assignment allows writes, otherwise in agent memory (`store_memory`) so the next run sees it.
6. Produce a daily intelligence brief if significant developments occurred (Intelligence Summary template, **Intelligence Writing** skill).

## SOP-002: IOC Processing

**Trigger**: New IOC list received (from internal detection, external sharing, or OSINT collection)
**Owner**: IOC Processor

1. Parse and classify all indicators (`code_interpreter` for extraction, refanging and type classification of large lists).
2. Validate format (reject malformed).
3. Deduplicate against existing active IOC collections: OpenCTI observables and indicators (`list_opencti_observables`, `search_opencti`), MISP attributes (`search_misp_attributes`).
4. Enrich via the enrichment workflow (load the **IOC Enrichment Workflow** skill if it is attached).
5. Apply source assessment to enrichment results (Admiralty rating per source).
6. Synthesise verdict (malicious / suspicious / benign / unknown).
7. Store when asked: OpenCTI observables / indicators (`create_opencti_observable`, `create_opencti_indicator`, or a draft with `create_opencti_draft` for review), or a MISP event. Otherwise return the verdict table in the output, or a file with `generate_file`.
8. If actionable: request detection rule creation (the **Sigma Rule Writing**, **YARA Rule Writing** or **KQL Query Writing** skills).
9. If significant: flag to the orchestrator for flash report consideration (SOP-003).
10. Export in relevant formats if sharing is appropriate (the **IOC Export** skill; TLP governs who may receive it).

## SOP-003: Flash Report Creation

**Trigger**: Time-critical intelligence requiring immediate stakeholder action
**Owner**: Orchestrator -> Analyst -> Report Writer -> Quality Reviewer

1. Orchestrator identifies time-critical situation.
2. Dispatch analyst for rapid assessment (30-minute time box).
3. Dispatch report writer with the flash report template (**Intelligence Writing** skill).
4. Quality reviewer performs expedited review (critical checks only):
   - TLP correct?
   - Key finding supported?
   - IOCs validated?
   - Actions clear?
5. Disseminate via appropriate TLP channel (see the dissemination matrix in the **CTI Stakeholder Management** skill).
6. Link to relevant PIRs (cite the PIR names in the product; in OpenCTI, the report's relationships to the PIR's entities make it surface in the PIR).
7. Schedule follow-up analysis if warranted.

**Expedited review**: For flash reports, quality review focuses on CRITICAL issues only. MAJOR and MINOR issues are noted but don't block publication. Speed matters.

## SOP-004: Threat Actor Profile Update

**Trigger**: Significant new intelligence about a tracked threat actor
**Owner**: Orchestrator -> Analyst

1. Identify which knowledge applies: the actor's intrusion set in OpenCTI (`search_opencti`) and the matching knowledge-cell skill if one is attached (for example **China Cyber Espionage** or **Ransomware Ecosystem**).
2. Load the current knowledge: `get_opencti_entity` and `list_opencti_relationships` for the intrusion set; the knowledge-cell skill with `load_skill`.
3. Dispatch analyst to review new intelligence against existing knowledge.
4. Analyst determines:
   - What is genuinely new?
   - What confirms existing knowledge?
   - What contradicts existing knowledge?
5. Record the update (built-in knowledge-cell skills are read-only, so the update goes to the platform):
   - Add new campaign entries (OpenCTI Campaign + `attributed-to` relationship, when asked)
   - Update TTP tables (`uses` relationships to Attack Patterns)
   - Revise the profile summary if the landscape changed (the **Threat Actor Profiling** template)
   - Add sources (Admiralty-rated) and the date of the update
   - Note the change in an OpenCTI Note on the intrusion set, or in agent memory (`store_memory`) when writes are not allowed
6. If significant change: flag for stakeholder communication.

## SOP-005: Stakeholder Briefing

**Trigger**: Quarterly or ad-hoc stakeholder request
**Owner**: Orchestrator -> Analyst -> Report Writer

1. Review active PIRs for the stakeholder.
2. Gather satisfaction history since last briefing.
3. Dispatch analyst to prepare briefing content:
   - Key developments since last briefing
   - PIR satisfaction status
   - Emerging threats relevant to stakeholder
   - Recommended PIR adjustments
4. Dispatch report writer to produce the briefing document (`generate_report` for a printable brief).
5. Tailor format to stakeholder (per the **CTI Stakeholder Management** skill).
6. Quality review.
7. Deliver via appropriate channel.
8. Collect feedback (questions in the **Intelligence Feedback Loops** skill).
9. Update PIRs based on feedback.

## SOP-006: Quarterly PIR Review

**Frequency**: Quarterly
**Owner**: Orchestrator

1. List all active PIRs (`manage_opencti_pir` action `list`, or agent memory).
2. For each PIR:
   - Review satisfaction history
   - Check stakeholder is still active/relevant
   - Assess if priority should change
   - Determine if scope needs adjustment
   - Decide: maintain / modify / retire
3. Archive retired PIRs.
4. Create new PIRs based on stakeholder feedback and emerging threats.
5. Update collection plans for modified PIRs.
6. Document review outcomes.

The full PIR lifecycle and the OpenCTI PIR mapping are in the **PIR Management** skill.

## SOP-007: Knowledge Cell Maintenance

**Frequency**: Monthly review, continuous updates
**Owner**: Orchestrator

Built-in knowledge-cell skills are read-only and refreshed by the platform; their content states a "last reviewed" date. Maintenance therefore means keeping the team's live knowledge current around them.

1. List the knowledge cells the team relies on (the attached knowledge-cell skills and the matching OpenCTI intrusion sets, malware and campaigns).
2. For each cell:
   - Check its "last reviewed" date and the `updated_at` of the matching OpenCTI entities - flag any not updated in 60+ days
   - Review intelligence gaps - any now answerable?
   - Check if active campaigns have concluded -> mark them historical
   - Verify the summary still reflects the current landscape
3. For stale cells: dispatch OSINT research for current intelligence.
4. For cells with resolved gaps: record the answer (OpenCTI Note, or agent memory) and update the gap list.
5. Consider: are new cells needed for emerging threats? Propose them to the team rather than inventing one.
