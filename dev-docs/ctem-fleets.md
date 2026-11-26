# CTEM seeded fleets: sweep, fleet design and ROI model

Design reference of the program "CTEM seeded fleets" (epic #8975, this slice
#8976). Every later slice (S1 seed infrastructure, S2 reactive modules, S3
organization context, S4 ROI engine, F1 to F4 fleets, C1 command center)
implements what this document decides. XTM One mode only
(`platform_targets: ["xtm_one", "dev"]`); Copilot mode is out of scope.

Status: design. Nothing here is implemented yet unless the text says
"exists today". Counts and names are read from `main` at `b79164799f55`
(`backend/app/agents/builtin_agents.py`, `cti_templates.py`,
`seed_data/cti_templates/`).

## 1. Sources

| Source | Used for |
|---|---|
| Notion "CTEM End to End Use Case" (Marcus: "a published threat becomes an approved plan in days") | F1 story, top 3 actions, re-test loop |
| Notion "Q1 2027 CTEM use case: ideation" (themes A to E, A + C recommended) | F1 (A + C), F2 (B), F3 (E applied to AI) |
| Notion "OpenCRQ Use Cases" UC1 to UC11 | OpenCRQ role per fleet (UC5, UC6, UC7) |
| Notion EPIC "AI Threat Intelligence & AI Attack Surface Monitoring" | F3 scope: ATLAS mapping in OpenCTI, AI red team injectors in OpenAEV behind approval |
| Notion EPIC "CTEM Command Center" | F4 and C1 (no OpenCRQ, no usage metric today) |
| `dev-docs/ctem-gap-assessment.md` | Missing asset model, missing exposure object, weak mobilization |
| `dev-docs/cti-pack.md`, `dev-docs/intents-and-verticals.md` | Templates and intent routing |
| `backend/app/services/fleet_spec.py` | The `xtm-one/fleet/v1` envelope the drafts below follow |

JP's page "XTM Platform - CTEM Use Case Ideation" is not shared with Filigran
Copilot: its rows are not read here and not guessed.

## 2. What is seeded today (XTM One mode)

| Object | Count | Where |
|---|---|---|
| Built-in agents | 44 in `BUILTIN_AGENTS` + 5 CTEM stage analysts (`_STAGE_AGENTS`) = 49 | `builtin_agents.py` |
| Built-in assignments | 4 in `BUILTIN_ASSIGNMENTS` (monthly + manual exposure briefing, manual + hourly opt-in indicator decay) + 10 stage assignments (weekly + manual per stage) = 14 | `builtin_agents.py` |
| Agent templates (wizard) | 9 CTI templates, 42 manual assignments | `seed_data/cti_templates/` |
| Built-in skills | 13 in `BUILTIN_SKILLS` + 51 CTI skills (`cti_skills.py`) + FIMI skills (`fimi_skills.py`) | `builtin_agents.py`, `cti_skills.py`, `fimi_skills.py` |
| Built-in knowledge bases | 10 | `BUILTIN_KNOWLEDGE_BASES` |
| Seeded fleets | 0 | none: the seed has no fleet step |

Trigger types an assignment accepts today: `schedule`, `webhook`,
`integration_event`, `manual`, `work_item`, `platform_event`
(`agent_builder.ASSIGNMENT_TRIGGER_TYPES`). `platform_event` covers
`work_item.*`, `run.*`, `platform.release.deployed` and, for a module's
upgrade, `integration.version.changed` (a registered platform's heartbeat
reporting a new version, `services/integration_versions.py`; #584; and a
connector of a registered OpenCTI reporting another `version`, read every
5 minutes by the autonomous tick while a subscriber exists, as a
subscriber's run-as user, the payload adding `component_kind`,
`component_id`, `component_name`, `component_slug`:
`services/integration_component_versions.py`).
The version itself is read on demand by `manage_openaev_settings` action
`version`, `get_opencti_platform_version` (GraphQL `about { version }`) and
`get_opencrq_platform_version` (the public tRPC `health` procedure). The
source of a build baseline is the version XTM One registered, the string the
CTEM Command Center chips show (`PlatformRegistration.platform_version`, with
its `-dev.<sha>`): `get_platform_overview` returns it per module under
`registered_modules` (version, commit, status, healthy, `registered_at`,
`last_heartbeat_at`), and each of the three version tools names it beside the
module's live answer, labelled a fallback; when the two differ the answer
names both and infers no upgrade or downgrade (`core/module_versions.py`,
#888).
The OpenCTI integration exposes notifier and trigger management
(`manage_opencti_trigger`, `manage_opencti_notifier`) that S2 builds on; per
the program brief, the OpenCTI integration event is a create / update / delete
webhook and OpenAEV and OpenCRQ emit no event XTM One can subscribe to (not
re-read in this slice; S2 verifies it first).

### 2.1 Usage

No lab or telemetry figure was read for this slice: the last-30-day usage
column is left to S4, which adds per-agent run counts from `agent_runs`
(runs, units, cost) to the command center. Until then every verdict below rests
on the caller and the design, not on usage.

## 3. Inventory and verdicts

Verdicts: **ONE-OFF** (an intent or capability endpoint called by a module
button, the browser extension or a playbook; stays outside fleets),
**FOLD** (becomes a member of a seeded fleet, keeps its slug), **MERGE**
(absorbed by another agent), **RETIRE**, **NEW** (section 3.4).

### 3.1 Assistants and entry points

| Slug | Caller | Tools / integrations | Handovers | Verdict | Optimization |
|---|---|---|---|---|---|
| `ctem-assistant` | Chat, `global.assistant` (priority 10) | GA tools, OpenCTI + OpenAEV + OpenCRQ | opencti/openaev/opencrq/xtm-one assistants, deep-investigation | ONE-OFF (entry point), connected to the 4 fleets | Add handovers to the 4 fleet orchestrators; ground every answer in the organization context (section 6) |
| `xtm-one-assistant` | Chat, `global.assistant` (1) | web_fetch, agent_builder, media | module assistants, ctem-assistant | ONE-OFF | None |
| `opencti-assistant` | OpenCTI embedded chat, text intents | web_fetch, media, OpenCTI | dashboard, threat hunt, insights, fimi, ctem, xtm-one | ONE-OFF, connected to F1, F2, F3 | Hand over to `ctem-threat-relevance-analyst` for "are we at risk" questions |
| `openaev-assistant` | OpenAEV embedded chat, message and media generators | web_fetch, media, OpenAEV | scenario-creator, ctem, xtm-one | ONE-OFF, connected to F1, F2, F3 | None |
| `opencrq-assistant` | OpenCRQ embedded chat, control gap, risk scenario, assessment summary | web_fetch, media, OpenCRQ | ctem, xtm-one | ONE-OFF, connected to F1, F2, F3 | Becomes the read path of `ctem-risk-quantifier` until a dedicated tool set exists |
| `fimi-analyst` | OpenCTI FIMI vertical chat | fimi_analytics, workset, OpenCTI | opencti, dashboards, hunt, ctem, fimi-infra | ONE-OFF (vertical) | None in this program |
| `deep-investigation-agent` | Chat, flag `DEEP_INVESTIGATION` | deep_investigation | none | ONE-OFF, consulted by F1 and F3 | max_iterations 6 is right for a tool that loops itself |

### 3.2 Module capability endpoints (stay one-off)

| Slug(s) | Caller | Verdict | Optimization |
|---|---|---|---|
| `opencti-nlq` | `cti.nlq_search` | ONE-OFF | None |
| `opencti-entity-insights` | 6 `cti.*` insight intents | ONE-OFF | None |
| `opencti-dashboard-creator`, `opencti-dashboard-widget-creator` | Chat, handover | ONE-OFF | Widget creator consulted by F4 for the board dashboard |
| `opencti-detection-rule-converter` | Handover from threat hunt | FOLD into F1 and F2 as consulted member (detections drafted) | Shared member: a fleet may connect it rather than own it |
| `opencti-threat-hunt-creator` | Chat, handover | FOLD into F1 (hunt package for a relevant threat) | None |
| `openaev-scenario-creator` | Chat, handover | FOLD into F1 and F3 (scenario built) | Read the organization's assets and AI targets from the context contract before choosing targets |
| `openaev-payload-creator` | `aev.attack_path_additional_agent`, handover | FOLD into F1, F2, F3 as consulted member | None |
| `openaev-scenario-publisher` | Handover | ONE-OFF (catalogue PR) | None |
| `openaev-attack-path-orchestrator` | `aev.attack_path_orchestrator` | ONE-OFF (autonomous pentest) | None in this program |
| `aev-phishing-email-generator`, `aev-phishing-landing-page-generator` | AEV intents | ONE-OFF | max_iterations 5, no tools: correct |
| `aev-detection-rules-generator`, `remediation-rule-type-detector`, `remediation-crowdstrike-rule-generator`, `remediation-splunk-rule-generator` | `aev.detection_rules_generator` chain | ONE-OFF chain; consulted by F1 and F2 for "detections drafted" | max_iterations 100 on single-shot generators with no tools is a ceiling, not a need: lower to 5 (S1) |
| `cti-stix-harvester`, `cti-stix-harvester-filigran-ai` | `cti.stix_harvester` (cti 5 and cti 10) | ONE-OFF, kept as two: same intent, two engines (frontier model vs Filigran AI Orion) resolved by intent priority; not a duplicate | Document the priority rule in `intents-and-verticals.md` (S1) |
| `cti-ttp-harvester-filigran-ai` | `cti.ttp_harvester` | ONE-OFF; consulted by F3 for ATLAS extraction once ATLAS is in its prompt | None |
| `cti-stix-transformer`, `cti-stix-consumer`, `fimi-stix-transformer` | Playbook intents | ONE-OFF | None |
| `fimi-infrastructure-investigator` | Consulted by fimi-analyst | ONE-OFF | None |
| `browser-*` (8 agents) | Browser extension | ONE-OFF | max_iterations 1, no tools: correct |
| `template-designer`, `deck-importer` | Chat | ONE-OFF (presentation) | None |
| `ocr-benchmark-transcriber` | Evaluation | ONE-OFF (internal), `platform_targets: ["global"]` | None |

### 3.3 Scheduled analysts (fold into fleets)

| Slug | Today | Verdict |
|---|---|---|
| `exposure-briefing-analyst` | Monthly per user (`0 8 1 * *`) + manual | FOLD into F4 as the reporting member; also receives F1/F2/F3 outcomes |
| `ctem-scoping-analyst`, `ctem-discovery-analyst`, `ctem-prioritization-analyst`, `ctem-validation-analyst`, `ctem-mobilization-analyst` | Weekly per user (`0 8 * * 1`) + manual each | FOLD into F4; event-driven refresh (section 4.4) on top of the weekly run |
| `indicator-decay-guardian`, `indicator-decay-judge` | Manual + hourly opt-in | ONE-OFF pair (data hygiene); not a CTEM fleet |

No agent is retired: every one has a live caller (intent, button, extension or
schedule). No merge either: the two STIX harvesters are two engines behind one
intent, kept by design.

### 3.4 New agents

| Slug | Fleet | Unit of work | Why it is missing |
|---|---|---|---|
| `ctem-threat-response-orchestrator` | F1 | threat response closed | Nobody runs threat to plan to re-test today |
| `ctem-threat-relevance-analyst` | F1, F3 | threat triaged | No agent judges a new threat against the organization context |
| `ctem-risk-quantifier` | F1, F2, F3 | loss estimate updated | No agent writes OpenAEV results into OpenCRQ (UC7) |
| `ctem-remediation-coordinator` | F1, F2, F3 | action ticketed and re-tested | Mobilization is the weakest stage (gap assessment) |
| `ctem-vuln-prioritization-orchestrator` | F2 | CVE driven to verified fix | Theme B has no runner |
| `ctem-vuln-triage-analyst` | F2 | CVE triaged | KEV / EPSS / sighting against the tech stack |
| `ctem-ai-security-orchestrator` | F3 | AI threat closed | EPIC "AI Threat Intelligence" has no agent |
| `ctem-ai-threat-analyst` | F3 | AI threat mapped to ATLAS | ATLAS mapping exists only as a template assignment |
| `ctem-posture-orchestrator` | F4 | posture refreshed | The 6 analysts run independently |

Nine new agents, all `platform_targets: ["xtm_one", "dev"]`, company-managed,
built-in, JSON output schema per unit of work (section 5 reads it).

## 4. The four defined fleets

Since CTEM-31 (#1048, section 14) the defined set is the generic CTEM loop
(F1 core, F4 and M1 to M3 folded in as roles), F2 vulnerability management,
F3 AI security and F5 third-party risk. Only the CTEM loop is seeded until
the three use cases are tested (section 14). The per-fleet sections below
keep describing each role.

Common rules:

- Built-in, company-managed, seeded by S1 from a spec file under
  `backend/app/agents/seed_data/fleets/<slug>.yaml` (structural spec: members by
  `agent.ref.slug`), applied with `fleet_spec.apply` at boot, idempotent.
- Every write to a module that changes the organization's environment
  (launching a simulation, creating a ticket, deploying a rule) goes through a
  work item with `needs_human: true` (approval). Read and draft actions do not.
- Work-item keys `ctem:<fleet>:<object id>`; shared-state key per fleet
  `ctem:<fleet>:state` (open cases, last refresh, counters for ROI).
- **The loop fleets run on cases (#891, ownership #1390).** The CTEM loop
  owns every case on every trigger path (section 14). Its "Case opened or
  reopened" automation (platform_event, scope all, seeded off) hears
  `ctem_case.opened`, `ctem_case.reopened`, `ctem_case.sla_breached`,
  `ctem_case.validation_finished`, `ctem_case.retest_requested` and
  `ctem_case.stalled`. A case never waits on nobody: the hourly case sweep
  raises `ctem_case.stalled` on an open case with no open needs_human `ctem:`
  item of the case, no armed `ctem-loop-reminder:` timer and no case event for
  `CTEM_CASE_STALL_HOURS` (default 6), once per stall window, and the owning
  fleet re-runs its loop; every loop run ends on an ask to a named person with a
  deadline, an armed reminder, or a terminal state. A deferred or not-validated
  answer records when to ask again and arms `ctem-loop-reminder:<case id>`. A
  case opened from a signal starts with an owner (`services/ctem_case_owner.py`):
  the owner of an affected asset's records, else of one of their asset groups,
  else `CTEM_CASE_DEFAULT_OWNER`, matched to an active account by email only;
  else the CTEM decider (#2452): an active member of a group granting `decide`
  with notify on, else an active administrator, the seed account only when it
  is the sole one, the first by account creation then id; an `owner` case event
  records how it was found. The owner is set at the opening by this code, never
  by the loop agent; the case sweep gives the decider to an open case with no
  owner and no `owner` event (`backfill_owners`, idempotent, no notification),
  so an owner a person cleared stays cleared. Which fleet runs a case
  is declared on the fleet (#1966, `fleets.case_ownership`: `paths` null for
  every trigger path or a list, `key` its case item prefix), and counts only
  while a home member's "Case opened or reopened" automation is on. The loop
  declares every path, Vulnerability management `new_vulnerability`, seeded
  from `BUILTIN_FLEETS`; a fork carries its source's, an administrator sets
  one on any other fleet (section 14). The owner is computed when the event
  is published (`services/ctem_case_routing.py` stamps `owner_fleet`: a
  built-in fleet's slug, `fleet:<id>` for any other) and each automation
  filters on its own value, so exactly one fleet runs a case. Case events
  fire once: on fleet enable, Pause all, Resume, Back to not set up, an
  ownership change and the hourly case sweep, `catch_up` hands every open
  case its owner never ran (no open `<key>:case:<id>` item of any declared
  key, no `owner_requested` case event for that owner) one
  `ctem_case.opened`, P1 first, soonest SLA first, 100 per pass. An ask no
  case automation answered (no run of the case since it, no open case item)
  is made again after 15 minutes (`REASK_AFTER_SECONDS`) under a new event
  id, 3 asks in all (`MAX_ASKS`), so a delivery lost to an exhausted pool
  never leaves a case unowned (#2101).
  `GET /ctem-cases/{id}` answers `owner_fleet` {slug, name, active, fleet_id};
  the case page header shows "Owned by <fleet>" linked to the fleet page, or
  "No active fleet owns this case" when it is null or inactive.
  The run owns one work item `ctem:<fleet>:case:<case id>` (an open
  one gets a note, never a second loop), writes owner, state, validation
  (`record_ctem_case_validation`) and ticket (`record_ctem_case_ticket`) on the
  case, and closes on proof: a passing re-test resolves the case. Re-test items
  are keyed `ctem:<fleet>:retest:<case id>`. The event listeners record a
  signal (`record_ctem_signal`) instead of starting a loop; the scope gate
  opens or joins the case. `ctem:f1:state` and `ctem:f2:state` keep counters
  only; migration `c891ctemfleetcase1` dropped their open and closed lists
  with a dated note. The dropped open items are discarded: no fleet pass
  re-records them (the F2 daily pass re-ranks existing cases, the F1 weekly
  sweep covers the last 7 days); one reaches a case again only on a new
  signal, and the note lists their identifiers (first 50) so an operator can
  record a signal by hand. Gates are unchanged. Tests:
  `tests/test_ctem_fleets_on_cases.py`.
- **One writer per counter (#2250).** Each counter of `ctem:f1:state` and
  `ctem:f2:state` has exactly one seeded writer, named in
  `CTEM_F1_COUNTER_WRITERS` / `CTEM_F2_COUNTER_WRITERS` (`builtin_agents.py`)
  and in both personas: the F1 Weekly threat sweep writes `threats_reviewed`,
  `threats_relevant` and `last_refresh` once per week; the F1 Re-test due
  writes `cases_resolved` and `loss_avoided` once per resolved case (the note
  names the case id, so a resumed run adds nothing); the F2 Daily backlog pass
  sets `cve_triaged`, `cve_validated` and `last_pass` counted from the cases;
  the F2 Re-test due writes `ticket_closed_retested` once per case. The case
  listener (up to 5 runs at once) and the F2 triage listeners never write them.
  Tests: `tests/test_ctem_counter_writers.py`.
- **Bounded shared state (#10789).** Every seeded persona that keeps a list in
  shared state states its bound: a closed case leaves the open list and only
  bumps the counters, a history or closed list is appended with `keep_last`
  (50 by default, 200 for `ctem:f2:state` `closed`, 24 snapshots for
  `opencrq:risk-budget-desk:state`). The counters carry the totals. Pinned by
  `tests/test_seeded_shared_state_bounds.py`; the platform warns past
  `shared_state_value_soft_limit_chars` and refuses past the hard limit.
- The CTEM Assistant reaches each orchestrator by handover (`delegate`), only
  while that fleet is active (below).
- **Opt-in (#9160): a seeded automation never ships enabled; the customer opts
  in.** Agentic runs meter the customer's quota, so every fleet assignment
  spreads `SEEDED_FLEET_OPT_IN` (`app/agents/fleet_opt_in.py`): created
  disabled with the `created_disabled` stamp, never re-enabled by a boot, and
  enabling one anchors its schedule at that instant (no catch-up run). While
  disabled, the OpenCTI and OpenAEV provisioning keeps no trigger (the seed
  keeps the provisioning state keys so the tick can remove what it created).
  A built-in fleet is ACTIVE only while its orchestrator has an enabled
  assignment: inactive, its members run as playground agents (no organization
  block, no fleet run defaults, no fleet trace keys,
  `fleet_runs.home_fleet_defaults_of`) and the CTEM Assistant gets no handover
  to the orchestrator (`fleet_runs.active_handover_target_clause`). The 14
  dashboard and decay automations of 1.261102.0 are the only built-ins that
  ship enabled (`ENABLED_BY_DEFAULT_ALLOWLIST`); the tripwire
  `tests/test_ctem_fleets_opt_in.py` fails on any other, and on any seeded
  event, work_item or platform_event assignment enabled by default. Migration
  `c9160fleetoptin1` switched off the 12 rows main seeded on 5 and 6 Nov 2026.
  Seeding, profile drafting, ROI recording and trigger provisioning make no
  LLM call and create no run.
- **Run-as and pre-existing members (#9188).** Switching one of the 12 fleet
  automations on with no run-as stores the administrator who switches it on as
  `run_as_user_id` (an explicit run-as wins); the switch, and a run-as change
  on an enabled one, is refused (400, the provider named) when that user has
  no connected OpenCTI, OpenAEV or OpenCRQ integration the automation needs
  (`app/services/fleet_run_as.py`). Its OpenCTI and OpenAEV trigger
  provisioning never falls back to the seed owner: no run-as, no provisioning,
  the reason in its state. The hourly indicator decay re-check keeps its own
  rule. The 9 agents of 1.261102.0 folded into F1 and F4 as members
  (`PRE_EXISTING_FLEET_MEMBERS`) never take the fleet's budget or iteration
  defaults, and get the organization block only on a run their fleet started
  (the parent run's agent is in the same fleet); the 14 allowlisted
  automations never get it (`fleet_runs.assignment_fleet_context`). Their
  traces keep the fleet keys.
- **Seeded documentation (#9174 slice 6a).** Every built-in fleet ships its
  documentation in `backend/app/agents/builtin_fleet_docs.py`
  (`BUILTIN_FLEET_DOCS`, one entry per slug, every section of
  `fleet_docs.DOC_SECTIONS`); `seed_builtin_fleets` writes it through
  `seed_documentation` (a new version only when the text changed), read only
  on the built-in, copied by a fork. The text explains the fleet, never a
  run's result; the quota section starts with "Estimate:" and the triggers
  section names each seeded automation, pinned by
  `tests/test_builtin_fleet_docs.py`.

### 4.1 F1 Threat-informed exposure response

Story: Notion "CTEM End to End Use Case", themes A + C, OpenCRQ UC5 + UC7.

```yaml
apiVersion: xtm-one/fleet/v1
kind: Fleet
metadata: {slug: ctem-threat-response, name: Threat-informed exposure response, icon: Crosshair}
spec:
  orchestrator: ctem-threat-response-orchestrator
  members:
    - {key: ctem-threat-response-orchestrator, role_label: Orchestrator, parent: null, exposed: true,
       handovers: [{to: ctem-threat-relevance-analyst, type: delegate}, {to: openaev-scenario-creator, type: delegate},
                   {to: ctem-risk-quantifier, type: delegate}, {to: ctem-remediation-coordinator, type: delegate}]}
    - {key: ctem-threat-relevance-analyst, role_label: Relevance, parent: ctem-threat-response-orchestrator, exposed: false,
       handovers: [{to: opencti-threat-hunt-creator, type: consult}]}
    - {key: openaev-scenario-creator, role_label: Validation, parent: ctem-threat-response-orchestrator, exposed: false,
       handovers: [{to: openaev-payload-creator, type: consult}]}
    - {key: ctem-risk-quantifier, role_label: Risk, parent: ctem-threat-response-orchestrator, exposed: false}
    - {key: ctem-remediation-coordinator, role_label: Mobilization, parent: ctem-threat-response-orchestrator, exposed: false,
       handovers: [{to: opencti-detection-rule-converter, type: consult}, {to: aev-detection-rules-generator, type: consult}]}
    - {key: opencti-threat-hunt-creator, role_label: Hunt, parent: ctem-threat-relevance-analyst, exposed: false}
    - {key: opencti-detection-rule-converter, role_label: Detections, parent: ctem-remediation-coordinator, exposed: false}
  connected:
    - {agent: {slug: ctem-assistant}, collaboration_instruction: "Entry point; delegates 'are we at risk' questions"}
    - {agent: {slug: deep-investigation-agent}, collaboration_instruction: "Consulted for open-source context on a threat"}
```

Assignments (on the orchestrator):

| Name | Trigger | Does |
|---|---|---|
| New relevant threat | `integration_event` OpenCTI create on Intrusion-Set, Campaign, Report (S2), listen-only (#1580) | Provisions the OpenCTI trigger; the webhook route records the `relevant_threat` signal, no run |
| Threat activity flagged in a PIR | `integration_event` OpenCTI PIR playbook, listen-only (#1580) | Provisions the PIR playbook; the delivery records the `relevant_threat` signal, no run |
| Threat signal recorded | `platform_event` `ctem_signal.recorded` and `ctem_signal.triaged`, filter `trigger_path` `relevant_threat`, `source_provider` `opencti`, `pir_floor_met` true (#1678: the listener stamps it when a PIR score at delivery reaches the policy's `pir_min_score_threat`; below it the signal waits for the weekly sweep), verdict `to_validate` or none; `max_concurrent_runs` 5, ordered by `signal_id`; takes the switch of New relevant threat on upgrade | Triage of a signal the case loop does not own yet; a confirmed signal opens its case through the case loop |
| Weekly threat sweep | `schedule` `30 6 * * 1` (#1832) | Catches what events missed: records a signal per relevant entity created or updated in 7 days |
| Re-test due | `work_item` prefix `ctem:f1:retest:` | Re-runs the validated scenario after a fix |

Loop: relevance (score against sector, regions, tech stack, PIRs; one rule,
`CTEM_RELEVANCE_RULE` in `builtin_agents.py`, read by the analyst, the
orchestrator and the prompts (#1678): relevant at a score of 50 or more, or on
a PIR flag at or above `pir_min_score_threat`; otherwise the case closes as
"not relevant", with the reason) -> OpenAEV
scenario proposed (approval) -> results read -> OpenCRQ expected loss updated
-> top 3 actions, detections drafted, ticket (approval) -> re-test closes the
case. Units: threat triaged, scenario built, loss updated, action closed.

### 4.2 F2 Vulnerability prioritization to verified fix

Theme B, OpenCRQ UC6. Orchestrator `ctem-vuln-prioritization-orchestrator`;
members `ctem-vuln-triage-analyst` (Triage), `openaev-payload-creator`
(Validation, consulted), `ctem-risk-quantifier` (Risk),
`ctem-remediation-coordinator` (Mobilization, consults
`opencti-detection-rule-converter`); connected `ctem-assistant`,
`opencti-assistant`.

| Name | Trigger | Does |
|---|---|---|
| New KEV or EPSS jump | `integration_event` OpenCTI create / update on Vulnerability with KEV label or EPSS above threshold (S2) | Triage |
| Vulnerability sighted | `integration_event` OpenCTI sighting on Vulnerability (S2) | Triage at higher priority |
| Daily backlog pass | `schedule` `0 6 * * *` | Re-rank the open cases |
| Re-test due | `work_item` prefix `ctem:f2:retest:` | Re-validate after the fix |

Loop: asset match from the organization context (no matching asset: closed,
reason recorded) -> exploitability validated in OpenAEV (approval) -> finding
score (OpenCRQ UC6) -> remediation ticket (approval) -> re-test. Units: CVE
triaged, CVE validated, ticket closed and re-tested.

### 4.3 F3 AI security

EPIC "AI Threat Intelligence & AI Attack Surface Monitoring", theme E applied
to AI, MITRE ATLAS integration (`mitre_atlas` provider exists today), OpenAEV
AI red team injectors, template assignment `cti-adversary-analyst`
`04-mitre-atlas-mapping`. Orchestrator `ctem-ai-security-orchestrator`;
members `ctem-ai-threat-analyst` (ATLAS mapping, consults
`cti-ttp-harvester-filigran-ai`), `ctem-threat-relevance-analyst` (relevance
to the organization's AI targets), `openaev-scenario-creator` (AI tests:
prompt injection, jailbreak, excessive agency, tool misuse, MCP tool
poisoning), `ctem-risk-quantifier` (AI risk scenario in OpenCRQ),
`ctem-remediation-coordinator`; connected `ctem-assistant`,
`deep-investigation-agent`.

| Name | Trigger | Does |
|---|---|---|
| New AI-enabled report | `integration_event` OpenCTI create on Report labelled AI or with an ATLAS technique (S2) | Map, then relevance |
| Weekly AI threat watch | `schedule` `0 7 * * 2` | Summary of new AI campaigns with ATLAS techniques |
| AI target added | `work_item` prefix `ctem:f3:ai-target:` (an asset of category `ai_target` added to the inventory) | Baseline test proposal |

Every test of an AI target is behind approval; ATLAS entities are proposed in
an OpenCTI draft for analyst review, never written to the knowledge directly.
Units: AI threat mapped, AI test run, AI exposure fixed.

#### 4.3.1 Implementation (#9113)

`backend/app/agents/ctem_ai_security.py` holds the two new agents, their three
assignments and the `ctem-ai-security` entry of `BUILTIN_FLEETS`
(`builtin_agents.py` and `builtin_fleets.py` extend their lists with them):

- `ctem-ai-security-orchestrator` (Orchestrator, exposed): JSON case output
  (matched AI targets, proposed tests, status, missing context), tools
  `work_items` and `shared_state`, bindings `opencti` and `openaev`, delegates
  to the analyst and the four agents shared with F1. Each proposed test is a
  work item `ctem:f3:approval:` + system + test with `needs_human` true; the
  orchestrator never launches a simulation.
- `ctem-ai-threat-analyst` (ATLAS mapping, parent the orchestrator): JSON
  mapping output (techniques with evidence and confidence, the OpenCTI draft
  id), bindings `opencti` and `mitre_atlas`, consults
  `cti-ttp-harvester-filigran-ai`.
- Assignments on the orchestrator: "New AI-enabled report" (OpenCTI
  `integration_event`, provisioned trigger on Report labelled `ai`, S2a),
  "Weekly AI threat watch" (`0 7 * * 2`), "AI target added" (`work_item`,
  `correlation_prefix` `ctem:f3:ai-target:`; renamed from "AI system registered"
  by migration `r1844noaisys01`, #1844).
- ROI (section 5.4): the analyst counts `atlas_mapping` per technique in
  `atlas_techniques`, the orchestrator `ai_test_plan` per entry of
  `proposed_tests`.
- The agents shared with F1 (`ctem-threat-relevance-analyst`,
  `openaev-scenario-creator`, `ctem-risk-quantifier`,
  `ctem-remediation-coordinator`) are home in F1 and connected to F3, with
  `cti-ttp-harvester-filigran-ai`, `ctem-assistant` and
  `deep-investigation-agent`. A slug not seeded yet is skipped by the fleet
  and handover seeds, so F3 seeds alone and gains them when F1 lands.

Gaps, degraded cleanly: OpenCTI has no AI-system object. Since #1501 and
#1844 the AI targets are the inventory assets of category `ai_target` (with
their OpenAEV id); an AI target with no OpenAEV id gets the test proposal
only. The "AI target added" work item is created by whoever adds the asset
until the inventory emits it on save. The ATLAS
technique filter of the event trigger waits for an OpenCTI label convention;
the `ai` label is the filter today.

### 4.4 F4 CTEM posture and reporting

Orchestrator `ctem-posture-orchestrator`; members the five stage analysts
(roles Scoping, Discovery, Prioritization, Validation, Mobilization) and
`exposure-briefing-analyst` (Report); consulted
`opencti-dashboard-widget-creator`; connected `ctem-assistant`.

| Name | Trigger | Does |
|---|---|---|
| Weekly assessments | existing weekly stage assignments, kept | Unchanged |
| Event refresh | `platform_event` `work_item.completed` on keys `ctem:f1:`, `ctem:f2:`, `ctem:f3:` | Refreshes the stages the closed case touched, debounced to one refresh per stage per hour |
| Monthly board report | `schedule` `0 11 1 * *` on the orchestrator, after the monthly briefing and the OpenCRQ Board pack (#1832) | Board-ready report: stage scores and trend, the CTEM cases of the month (`ctem_read`), loss avoided, ROI |

The existing assignments keep their names (a renamed seed is swept and born
again). F4 adds one orchestrator and two assignments; it changes no stage
analyst persona beyond the organization-context section.

Implementation (#9112):

- Slice 1: `ctem-posture-orchestrator` in `BUILTIN_AGENTS` (xtm_one and dev,
  read-only on the modules, bound to OpenCTI, OpenAEV and OpenCRQ) and the
  fleet entry `ctem-posture` in `backend/app/agents/builtin_fleets.py`
  (`CTEM_POSTURE_FLEET`): the five stage analysts and
  `exposure-briefing-analyst` are home members under the orchestrator,
  `opencti-dashboard-widget-creator` is connected, `ctem-assistant` gets the
  entry handover. Every weekly, manual and monthly assignment keeps its name
  and its agent. Pinned by `tests/test_builtin_fleets.py`.
- Slice 2: two assignments on the orchestrator (`CTEM_POSTURE_ASSIGNMENTS`
  in `builtin_agents.py`), the orchestrator given the `shared_state` tool.
  - `Event refresh`: `platform_event` `work_item.completed`, filter
    `correlation_key` `starts_with` `ctem:f1:` / `ctem:f2:` / `ctem:f3:`,
    scope `all` (the seed owner is an administrator, and the F1-F3 items are
    filed by other agents). The run maps the closed case to the stages it
    changed, skips a stage refreshed less than an hour ago (shared state
    `ctem:f4:state`, path `refresh.<stage>.last_at`), delegates the others
    to their stage analysts and records the refresh. It runs as the
    assignment's owner: a `platform_event` trigger has no per-user fan-out,
    so each user's stage cards keep the weekly floor and their own manual
    refresh.
  - `Monthly board report`: an organization-level report, kept out of the
    per-user exposure briefing (F1-F3 outcomes and ROI are organization
    facts, not scoped to one user's module access). It reads the stage
    assessments and `ctem:f1:state` to `ctem:f3:state` and reports only the
    figures the fleets recorded, marked estimated unless the organization
    set its baselines.
  - Pinned by `tests/test_builtin_fleets.py` (the trigger validates, the
    filter takes F1-F3 keys and refuses the rest, the prompts name their
    state keys).

## 5. ROI model

Per run of an agent that produces units:

```
units            = count of the output schema's unit field (e.g. threats_triaged)
time_saved_min   = units x baseline_min(unit) - review_min(unit) x units
llm_cost         = measured cost of the run (agent_runs cost, exists today)
value            = time_saved_min / 60 x loaded_hourly_rate (organization context)
net_value        = value - llm_cost
```

Per fleet: the sum over its members' runs in the period, plus the cycle time
of a closed case (threat published to re-test passed) against the manual
baseline. Payback = setup cost (configurable, default 0) / net value per month.

| Unit | Agent(s) | Baseline source |
|---|---|---|
| threat triaged | `ctem-threat-relevance-analyst` | `research:ctem-roi-baselines` (Analyst item) |
| scenario built | `openaev-scenario-creator` | same |
| CVE triaged / validated | `ctem-vuln-triage-analyst`, `openaev-payload-creator` | same |
| loss estimate updated | `ctem-risk-quantifier` | same |
| action ticketed and re-tested | `ctem-remediation-coordinator` | same |
| AI threat mapped / AI test run | `ctem-ai-threat-analyst`, `openaev-scenario-creator` | same |
| stage assessment / briefing | stage analysts, `exposure-briefing-analyst` | same |
| one-off endpoints | each intent's unit (rule generated, bundle harvested, page described) | same |

Baselines are defaults an administrator overrides per organization; the
figures shown are labelled "estimated" until an override exists. S4 stores the
units per run (output schema), the baselines and the rate, and C1 shows them.

### 5.1 Implementation (S4, issue #8990)

`backend/app/services/roi.py` is the pure core:

- `BASELINES`: the 20 units of the analyst baseline table (key, fleet, role,
  manual minutes low / likely / high, review minutes). Every minute figure is
  labelled `assumption`, except the published low bound of `cve_assessed`.
- `HOURLY_RATES`: fully loaded cost per role (`analyst`, `ciso`) in USD and
  EUR, each with its source (BLS median / 0.70 ECEC share; Stepstone DE x 1.30
  on-cost).
- Unit declaration: an agent, built-in or custom, adds to its `output_schema`
  `"x-xtm-roi": {"unit": "cve_assessed", "count": "result.cves"}`.
  `count_units` reads the count from the structured output (array length or
  integer), never from the model's prose. `declaration_error` validates it.
- `compute_roi(measures, currency, period_days, setup_cost)`: minutes saved
  (`units x (likely - review)`, measured review when recorded), value at the
  role's rate, net value, ROI (`net / llm_cost`), payback in months, and the
  list of assumptions behind the figures. A rejected run saves nothing and
  keeps its cost; an unknown unit counts its cost only.

### 5.2 Storage (S4 slice 2)

Migration `r4oirec8990`, models `backend/app/models/roi.py`, helpers
`backend/app/services/roi_records.py`:

- `roi_run_records`: one row per assignment run whose agent declares
  `x-xtm-roi` (unique `run_id`), written by `record_run_units` when the run
  completes: unit, units counted from the structured output, LLM cost (sum of
  the run's `llm_traces.total_cost`), duration, the agent's home fleet,
  outcome `accepted`. Best effort in its own savepoint: a failure is logged,
  never fails the run. `set_run_outcome` records a review (`accepted`,
  `modified`, `rejected`) and the measured review minutes.
- `roi_baseline_overrides`: the organization's minutes for a catalog unit
  (low <= likely <= high, review under likely) with their source.
  `effective_baselines` is the catalog with them applied, each labelled
  `organization override`; `upsert_override` / `delete_override` are the
  admin writes the API of slice 3 exposes.

### 5.3 Report and API (S4 slice 3)

`backend/app/services/roi_report.py` and `backend/app/api/v1/roi.py`
(administrators only; the overrides stay editable with AI off):

- `build_report` groups the records of a window (`7d`, `30d`, `90d`, `all`)
  by agent and by home fleet and computes each group with `compute_roi`, the
  organization's baselines applied. `estimated` stays true until every unit
  used carries an organization override.
- `rates_for`: the organization profile's `loaded_hourly_rate` applies to
  every role when its `currency` is the one reported (label `organization
  override`); otherwise the catalog rates, in USD when the currency asked for
  has none.
- `GET /api/v1/roi?window=&currency=`: the organization, each fleet and each
  agent, with hours saved, cost, value, net value, ROI, payback and the
  assumptions.
- `GET /api/v1/roi/export?format=csv|pdf`: one CSV line per scope (cells a
  spreadsheet could read as a formula are quoted), or the one-page PDF "Value
  delivered by XTM One" rendered by `pdf_render` under the export rate limit.
- `GET /api/v1/roi/baselines`, `PUT` / `DELETE /api/v1/roi/baselines/{unit}`:
  the catalog with the overrides, and the audited admin writes.

### 5.4 Seeded declarations (S4 slice 4)

`backend/app/agents/builtin_roi_units.py` holds `BUILTIN_ROI_UNITS`, the unit
and count path of each built-in agent whose structured output carries a
countable artefact of a catalog unit; the seed writes it into the agent's
`output_schema` (`with_builtin_roi_unit`) on insert and on every full sync, so
the declaration lives in one place and the seed JSON files stay unchanged.

| Agent | Unit | Count path |
|---|---|---|
| `exposure-briefing-analyst` | `briefing_written` | `headline` |
| `cti-stix-harvester` | `stix_bundle_built` | `id` |
| `aev-phishing-email-generator` | `phishing_content_built` | `html_body` |
| `aev-phishing-landing-page-generator` | `phishing_content_built` | `html` |
| `remediation-crowdstrike-rule-generator` | `detection_rule_written` | `rules` |
| `remediation-splunk-rule-generator` | `detection_rule_written` | `spl_query` |
| `ctem-ai-threat-analyst` | `atlas_mapping` | `atlas_techniques` |
| `ctem-ai-security-orchestrator` | `ai_test_plan` | `proposed_tests` |

`count_units` counts an array by its length, an integer as is, and a
non-empty string or object as one artefact. Left out on purpose: the CTEM
stage analysts (a stage read is no catalog unit), the STIX transformers, the
detection-rules extractor and the indicator decay judge. The F1 to F4 fleet
agents add their row when they are seeded.

The extension of this engine to any agent or fleet, in Copilot mode and XTM
One mode (open unit catalog, declaration from the UI, outcome review, ROI
views), is designed in `dev-docs/roi-outcomes.md` (#9206).

The keyword never leaves the platform: `structured_output.strip_platform_keywords`
drops every `x-xtm-*` keyword before a schema goes to a provider
(`build_native_structured_output`) or into a prompt (`build_schema_prompt`).

## 6. Organization context contract

"Driven by the context of the organization" means these fields, held once
in the versioned store `organization_context_versions` (S3, #8987;
`services/organization_context.py` validates them and names the missing
ones). Every agent reads it with the core tool `get_organization_context`;
`GET /api/v1/organization-context` serves it, `PUT` (administrators) saves
the next version with its source (`form`, `draft`, `api`). A `PUT` carrying
`based_on_version` answers 409 when another version was saved since, and
every save answers `removed_rows` (list field -> the rows the version before
held and this one no longer holds). `GET /organization-context/versions/{n}`
reads a past version whole and `POST /organization-context/versions/{n}/restore`
(`fields`, `based_on_version`) saves the next version with those fields taken
from it, provenance `restored from v<n>` (administrators). The free-text
`company-context` knowledge base stays as background prose only:

| Field | Type | Read by |
|---|---|---|
| sector, sub-sector | vocabulary | F1, F2, F3 relevance |
| regions of operation | ISO country list | F1, F3 relevance |
| primary assets | assets with business service and owner | F1, F2 prioritization, OpenCRQ |
| tech stack | CPE / product list | F2 asset match, stage prioritization |
| AI targets (assets of category `ai_target`) | name, subcategory, provider, endpoint, model, owner, internet facing, OpenAEV id | F3 |
| PIRs | OpenCTI PIR ids (`manage_opencti_pir` exists today) | F1, F3 relevance |
| risk appetite | loss threshold per year, approval threshold | F1, F2, F3 approvals and ranking |
| loaded hourly rate, currency | number | ROI (section 5) |
| reference exchange rates (`fx_rates`, optional, not a contract field) | from currency, to currency, rate, date (`as_of`), source | `ctem-risk-quantifier`, `opencrq-risk-desk-orchestrator` |
| ticketing target | integration and project | Mobilization |

Currencies (#1383): OpenCRQ prices every loss in USD; the thresholds of the
risk appetite are in the profile's `currency`. A risk agent never compares
the two: it converts an OpenCRQ figure with the profile's reference rate
into the organization's currency (`reference_rate`, the latest `as_of`
wins) and cites the rate, its date and its source, or, with no such rate,
reports the comparison as unknown (`above_approval_threshold: null` in the
Risk Quantifier's output) and names the missing rate. The rates are set by
the API (`PUT /api/v1/organization-context` with `fx_rates`) or through an
agent (`update_organization_context` / a proposal on `fx_rates`); a form
save that does not send the key keeps the stored rates. They are shown in
the always-on summary next to the currency, at most ten, and are not a
category of the index.

Every new agent's persona carries a "Read the organization context first"
step; a missing field is reported in the run output, never guessed.

Fleet runs (slice 3b): the prompt of every assignment run of an agent that
has a home fleet ends with a `--- Organization context ---` block
(`services/organization_context.fleet_run_context`, appended after the run
context line): the current version, the filled fields quarantined and the
missing fields named, the text `get_organization_context` answers. An agent
in no fleet gets no block and keeps the tool; an unreadable profile never
fails the run.

Organization summary in every agent (#11533,
`services/organization_context_injection.py`): one cascade decides whether
a prompt carries the organization, the most specific explicit value
winning: `assignments.organization_context_mode` (unless `inherit`) >
`agents.organization_context_mode` (unless `inherit`) >
`platform_settings.organization_context_enabled` (default true). A chat
turn (any channel, a sub-agent or handover turn, a background copy) resolves
agent > platform in `agent_chat`; the general chat resolves the platform in
`general_chat`; an assignment run, whatever its trigger, resolves the three
in `assignment_engine` through `assignment_run_organization_context`.
Resolved on, the system prompt starts with a `--- Organization summary ---`
block, "basic + categories" (`render_summary`): the identity (name,
sector, regions, size, risk appetite, currency, the identity fields not
filled, quarantined) and the CATEGORY INDEX, one line per category of
`services/organization_context_categories.py` with its row count and the
call that reads it, at most `SUMMARY_MAX_CHARS` = 2,000 characters (about
500 tokens), never growing with a list's length. The categories are
generated from `OrganizationProfile`: every list field but `regions` and
`onboarding_skipped` (`pir_ids` read as `pirs`), plus
`tracked_organizations`; a list added to the model is a category without a
change there. `get_organization_context(category, query, offset, limit)`
reads one by page (at most 200 rows, `query` a case-insensitive substring
over the row's values); `category="*"` lists them with their sizes. The
categories preloaded after the block come from the same cascade on
`organization_context_categories` (platform JSON list, default `[]`; agent
and assignment NULL = inherit, a list, empty included, wins;
`resolve_categories`), each capped at `PRELOAD_MAX_CHARS` = 6,000 characters
with the call that reads the rest. The brief notice of an incomplete
profile ("Organization context incomplete: missing ...", #11586) and the
CTEM-only fields (`CTEM_ONLY_FIELDS`: PIRs, risk appetite thresholds,
loaded hourly rate, ticketing target) are CTEM-scoped (#1384): the summary,
the fleet block and the tool reading name them only when
`organization_context.ctem_work_for` holds (the agent holds CTEM tools, a
built-in CTEM case or signal agent or the `ctem_read` group, or its home
fleet is a seeded CTEM fleet or a fork of one). A run of a fleet member keeps the fuller
fleet block above instead, so the profile appears once; resolved off,
neither block. An explicit `off` on the agent or the assignment also removes
the tool (`tools_enabled_for`, in `agent_chat` and `assignment_engine`);
the platform switch removes the block only. The modes and the categories are
set in the agent and assignment forms, `PUT /api/v1/agents/{id}`,
`PUT /api/v1/assignments/{id}` and the platform tools (`create_agent`,
`update_agent`, `create_assignment`, `update_assignment`; `get_agent` and
`get_assignment` show the stored and the effective value); the platform
defaults are `GET|PUT /api/v1/organization-context/injection` (`enabled`,
`categories`, `available_categories` read-only). Every seeded agent
inherits (the seed never writes the mode, so a person's opt-out survives a
refresh); the CTEM vulnerability orchestrator, triage analyst and
remediation coordinator preload `tech_stack`, `primary_assets`,
`assets_exposure` and `prioritization_policy` (#11543).

The #11543 categories ride the same registry: `assets_exposure` and
`enterprise_knowledge` are list fields (generated), `prioritization_policy`
is the one object field declared in `_OBJECT_CATEGORIES` and read as one
row per rule set away from its default, per exclusion and per accepted risk
(`policy_rows`, the count `exposure_counts` gives). An agent writes a rule
the way it reads it, one dotted path per rule: `update_organization_context`
(administrator) and `propose_organization_context_change` take every scalar
rule of `POLICY_SETTABLE_PATHS` (`organization_context.py`: the scope
threshold, `cvss_min`, `epss_min`, `kev_always_in`,
`internet_exposure_required`, `min_primary_asset_criticality`,
`require_risk_scenario`, `sla_hours`, the PIR minimum scores and
`identity_criticality`; #2129); the exclusions and accepted risks stay with
Settings > Organization. `enterprise_knowledge`
is never paged as rows: without `query` the tool names each linked knowledge
base and whether the caller can search it; with `query` it answers passages
from the linked knowledge bases the person the turn acts for can read
(`organization_enterprise_knowledge.linked_knowledge_bases`,
`user_visible_filter`), a run acting for nobody gets none. The three
prompt builders pass that person to `build_organization_context_tool`.

OpenCRQ primary-asset sync (slice 3d,
`services/organization_context_opencrq.py`): an administrator calls
`POST /api/v1/organization-context/opencrq-sync` (`{"apply": false}` answers
the plan, `true` writes it) and the primary assets are reconciled with the
company-managed OpenCRQ's supporting assets, read with a token minted for
that administrator:

- a primary asset with no `opencrq_asset_id` whose name matches exactly one asset
  (case-insensitive) is linked to it; several matches are reported as
  `ambiguous`, never picked;
- profile to OpenCRQ: a linked primary asset's criticality is written to the asset
  (`low`, `medium`, `high`, `critical` -> `very_high`) when they differ;
- OpenCRQ to profile: a `very_high` asset no primary asset names joins the profile
  as a `critical` primary asset linked to it;
- a primary asset whose asset OpenCRQ no longer holds is reported
  (`not_in_opencrq`) and kept;
- a link never makes a typed primary asset an OpenCRQ row: it stays `manual` with
  `opencrq_asset_id` as a link (only a row with no `source` at all, saved
  before provenance, reads as an OpenCRQ row);
- a synced primary asset whose pushed criticality falls below the primary-asset sync's
  `min_criticality` becomes a typed row linked to its asset
  (`kept_below_sync_threshold`), so the sync never removes a primary asset for a
  criticality the reconcile wrote. A sync with `remove_gone` removes only the
  rows stamped with its own `sync_id`; the others are reported (`gone_kept`).

A changed profile is saved as the next version with source `api`.

Draft from the platforms (slice 2b, `services/organization_context_draft.py`):
an administrator calls `POST /api/v1/organization-context/draft` and gets a
proposed profile, nothing saved, read with a token minted for that
administrator. OpenCTI gives the platform organization's name and first
sector and the PIR ids; OpenAEV gives its security platforms as tech stack
entries; OpenCRQ gives its `high` and `very_high` supporting assets as primary
assets linked to their asset (`very_high` -> `critical`). Only an empty field
of the current profile is filled, each filled field names its platform
(`filled`), and a platform not connected or unreadable is listed in
`unavailable`, never guessed and never a failure. No model is involved. The
administrator reviews the draft and saves it with `PUT` and source `draft`.

### 6.1 Draft with AI (design, #9484)

Every organization definition (the primary profile, each tracked
organization of section 9, the first-login setup of #9173) can be drafted
with a model on top of the deterministic draft above. The deterministic
draft stays as it is: free, no model, the same `filled` and `unavailable`.

**Inputs**, all optional, chosen by the administrator in the draft panel:

| Input | Read when | Bound |
|---|---|---|
| Description | typed | 4,000 characters |
| Documents (annual report, CMDB or asset export CSV/XLSX, architecture doc, supplier list) | uploaded in the panel | 5 files, 10 MB each, text extracted with `chunking.extract_text_from_file_async`, 60,000 characters in total to the model |
| Public website | a URL is given | one page and at most 4 same-site links, `web_fetch.fetch_url_bytes` (module SSRF policy, private hosts refused) |
| Platform facts | the box is ticked (default on) | the facts `collect_facts` reads today |

Uploaded documents are held for the draft request only (in memory, never
written to a knowledge base or the file store) and never leave the
administrator's tenant; a website is fetched only when given.

**Output: a proposal, never a save.** `POST
/api/v1/organization-context/ai-draft` (administrators; the same under
`/api/v1/organizations/{id}/context/ai-draft` for a tracked organization)
answers:

```json
{
  "based_on_version": 3,
  "proposals": [
    {"field": "tech_stack", "value": [{"vendor": "Salesforce", "product": "Sales Cloud", "cpe": ""}],
     "source": "document:suppliers.xlsx", "confidence": "high", "basis": "row 12", "check": false,
     "current": [], "conflict": false}
  ],
  "not_supported": ["ticketing", "risk_appetite.approval_threshold"],
  "unavailable": ["OpenCRQ: not connected (Settings > Integrations > OpenCRQ)"],
  "usage": {"input_tokens": 18400, "output_tokens": 1200}
}
```

- One proposal per contract field (section 6) the inputs support; a field
  nothing supports is listed in `not_supported`, never guessed.
- `source` is one of `description`, `document:<name>`, `website:<url>`,
  `opencti`, `openaev`, `opencrq`; `confidence` is `high`, `medium` or
  `low`; `basis` quotes where the value was read.
- Values come out typed and validated by `OrganizationProfile` before they
  are answered: ISO regions and currency, the size band, tech stack entries
  with vendor, product and a CPE only when one is matched, primary assets
  linked to an OpenCRQ asset when the name matches exactly one (the rule of
  the OpenCRQ sync above), AI targets with kind and exposure, PIRs matched
  to existing OpenCTI PIR ids. A PIR the model suggests creating is answered
  under `suggested_pirs` and never created.
- Amounts (risk appetite thresholds, loaded hourly rate) carry their
  currency and `basis`; an inferred amount has `check: true` and shows
  "Check this".
- `conflict: true` when the current version already holds a value: the UI
  never pre-accepts such a proposal.
- Saving the accepted fields is the existing `PUT` with the new source
  `ai_draft`; the version stores the accepted proposals' `source` and
  `basis` (`sources` column of the version), shown in the version history.

**Quota and editions.** The model runs only on the administrator's click
(or a chat request, below): one call metered as one agentic request on that
user's quota (`enforce_agentic_quota`, source `organization_ai_draft`), a
refusal answered before any read. The panel shows the estimate ("about one
chat turn") before running. No model at page load, no background run, no
seeded automation. Community Edition or no LLM configured: the one "Draft"
button still opens the dialog, which drafts from the connected platforms
alone and says in one line that documents and AI need a model (#1991).

**Tracked organizations.** From a name alone the draft proposes sector,
regions, website, the OpenCTI organization matched by search (the
administrator picks when several match), relationship, criticality and
PIRs. In bulk, the input is a centred dialog (`components/ui/dialog.tsx`,
DialogTitle "Draft organizations with AI"): a `Textarea` with one name per
line, or a file picker for a supplier or customer file (CSV or XLSX, the
first column read as names), then the count of names read and the quota
line ("12 names, 2 agentic requests of your quota") before Draft. The N
proposals then open as a page section with its own URL
(`/settings/organization/tracked/ai-draft/<run id>`), never a dialog, in
the shared data table, the same table as the tracked organizations list:
- columns: name, match confidence, sector, relationship, criticality, the
  OpenCTI organization; sortable by name, match confidence and criticality;
  search on the name, filters on relationship, criticality and match
  confidence;
- the OpenCTI organization cell is a search-as-you-type picker when several
  match, empty with "No match" when none does;
- the shared `SelectionBox` selects rows for Accept selected or Reject
  selected; a row is accepted or rejected on its own from its actions;
- a row whose name already matches a tracked organization is a conflict
  row ("Already tracked", link to it): never created twice, never
  pre-selected;
- states: loading (one line per batch of 10), empty (the file held no
  organization name), no results for the search, and an inline error with
  Retry on a failed batch (the other batches kept);
- more than 50 names: "50 of 120 names drafted; run again for the next
  50", before running.
Every accepted organization is created with `monitoring` `off` (section
9.7). At most 50 rows per run, one metered call per 10 rows, the count
shown before running.

API (S-c1, backend). `POST /api/v1/organizations/tracked/ai-draft`
(multipart: `names` one per line, `relationship` used when the model is
unsure, `file` a CSV or XLSX whose first column is read, a header row
skipped; administrators, `require_agentic_ai`) drafts the rows with
`services/organization_tracked_ai_draft.py` and stores them as ONE pending
`new_tracked` `OrganizationContextProposal`; its id is the `<run id>` of
the review URL, read back with `GET /tracked/ai-draft/{proposal_id}`. A
row carries `name, relationship, criticality, sector, regions, website,
confidence, basis, opencti_matches, opencti_organization_id,
already_tracked_id, error, notes, status` (`pending`, `conflict`, `error`,
then `accepted` or `rejected`). The first batch is metered before any
model call (429 on a spent quota); each further batch is metered before
its call and a refusal fails that batch only. A name already tracked
costs no call. OpenCTI is searched by name (no model); the match is
pre-set only when exactly one Organization matches exactly. The decision
is `POST /api/v1/organization-context/proposals/{id}/decide` with
`accept` (names) and `opencti_ids` (name to the Organization picked);
a conflict or failed row is refused, a drafted sector and regions become
the organization's first profile version (source `ai_draft`).

UI (S-c2). "Draft with AI" sits next to "Add an organization" on the
Tracked organizations card (`TrackedOrganizationsAiDraftDialog.tsx`; the
licence CTA of #9173 when agentic AI is off). Draft navigates to the review
page `TrackedOrganizationsAiDraftReview.tsx`, route
`/settings/organization/tracked/ai-draft/:proposalId` (on the shared
`EntityPageHeader`, back to Organization). The page's rules (decidable rows,
the decide body, the run estimate, failed batches) live in
`lib/tracked-organizations-ai-draft.ts` with their tests. Retry on a failed
batch reopens the dialog with that batch's names, as a new run.

**Chat.** A built-in tool `propose_organization_context_change` (CTEM
Assistant and Platform Health; administrators only, refused otherwise) takes the
field changes or a new tracked organization from the conversation and
stores them as a pending proposal (`organization_context_proposals`: id,
organization, proposals, author, conversation, created_at, decided_at). It
answers an approval card in chat and the proposal appears in Settings >
Organization; accepting it is the same `PUT` with source `ai_draft`. The
tool never writes the profile. Shipped in S-a2: the service
`services/organization_context_proposals.py` validates every value against
the organization's contract before storing it (a value already set is
`conflict: true`, applied only when named), and the administrators review
with `GET /organization-context/proposals` and
`POST /organization-context/proposals/{id}/decide` (`accept`: the fields or
organization names kept; empty rejects; accepted organizations are created
with monitoring `off`). On the page, a pending proposal shows as one
status line above the form, "1 proposal from chat, 2 fields", with its
author, a link to its conversation and a Review link that opens the same
review diff. A proposal whose `based_on_version` is older than the current
version is re-diffed against the current values when opened (a field
changed since becomes a conflict row) and says "Made on version 3, the
profile is now version 5".

**The page header (read mode).** Edit stays the one primary action (on
main it renders `variant="secondary"`, `OrganizationSection.tsx`; S-b makes
it the filled primary). The
drafts are ONE secondary button "Draft" (#1991, Sam 24 Nov 2026; it
replaced the menu "From the platforms", "With AI", "From a connected
integration"), so the header is one row at 1024 in en, fr, es and ja. One
click runs one pass: the connected platforms (`POST
/organization-context/draft`, free, no model) always, and the AI (`POST
/organization-context/ai-draft`, metered) when a model is available; the
platform values come first, the AI completes the fields they left, and both
land in the one review below. The draft proposes only the fields of the two
Settings tabs (Organization; Risk and response): the frontend
`isSettingsDraftField` and the backend `OUT_OF_SCOPE_PATHS`
(`services/organization_context_ai_draft.py`) drop primary assets, software,
the inventory rows and the PIRs, which CTEM > Inventory > Sources and the
Prioritization policy card import. The fill agent of "From a connected
integration" (#11888) was removed: Inventory > Sources imports assets from
every platform and asset integration. The card of a tracked organization
(section 9.8) keeps its own "Draft with AI" button.

**The panel.** "Draft" opens a centred dialog (`components/ui/dialog.tsx`;
no drawer anywhere in the app, Sam 7 Nov 2026, #9502): the `DialogTitle`
"Draft", one line of description ("Draft reads your platforms and documents
and proposes values to review. Nothing is saved until you click Save."),
the connected platforms first; without a model, one line "Documents and AI
need a model: this draft reads the connected platforms only." in place of
the AI inputs, never a lock; then the inputs in a body that
scrolls inside the dialog, and a footer with Cancel then Draft (primary).
The review diff replaces the inputs in the same dialog, footer Cancel then
Save accepted (n). Draft is disabled while no input is given and no
platform is connected, with the reason beside it ("Add a description, a
document or a website, or connect a platform"). Each input is a
`SettingsField`: label
above the control at 14 px, one help line at 12 px with an example, and an
`InfoTip` saying what is read, the bound and what happens when it is empty:

| Input | Control | Help line | InfoTip |
|---|---|---|---|
| Description | `Textarea`, full width, character count | "What the company does, where, its main systems. Example: insurer, 2,000 staff, France and Spain, Guidewire." | read as typed, 4,000 characters; empty: not used |
| Documents | file picker; each file listed with its name, size and a remove button named "Remove <file name>" | "Used for this draft only, not stored." | 5 files, 10 MB each, 60,000 characters in total; a file that cannot be read is skipped with its reason; empty: not used |
| Website | URL `Input`, full size, inline error with `aria-invalid` ("Enter a public https URL") | "Example: https://www.example.com" | one page and at most 4 same-site links; private hosts refused; empty: nothing fetched |
| Platform facts | `Checkbox` per connected platform (only the connected ones are listed), default on | "What the connected platforms already know." | the facts "Draft from the platforms" reads; none connected: the row says so and links Settings > Integrations |

Under the inputs, before Draft: "About one chat turn of your quota" (the
estimate, section Quota below).

```
HEADER (read mode, one row at 1024)
+------------------------------------------------------------+
| Organization                       [Draft v]  [Edit]       |
|                                     From the platforms     |
|                                     With AI                |
+------------------------------------------------------------+

DRAFT WITH AI (centred dialog)
+------------------------------------------------+
| Draft with AI                                  |
| Proposes values from what you give; nothing    |
| is saved until you accept.                     |
|                                                |
| Description (i)                                |
| [............................................] |
| What the company does, where, its main systems |
|                                                |
| Documents (i)                                  |
| [Add files]                                    |
|  suppliers.xlsx  48 KB                    [x]  |
| Used for this draft only, not stored.          |
|                                                |
| Website (i)                                    |
| [https://www.example.com.....................] |
| Example: https://www.example.com               |
|                                                |
| Platform facts (i)                             |
| [x] OpenCTI   [x] OpenAEV                      |
| What the connected platforms already know.     |
|                                                |
| About one chat turn of your quota              |
|------------------------------------------------|
| [Cancel]                              [Draft]  |
+------------------------------------------------+
([x] is an icon button, accessible name "Remove suppliers.xlsx", tooltip
with the same text)

REVIEW (diff, same dialog)
+------------------------------------------------+
| Review: 3 proposals              [Accept all]  |
|  Tech stack   + Salesforce Sales Cloud  (high) |
|                         [Edit][Accept][Reject] |
|  Industry     Insurance -> Reinsurance         |
|               Replaces your value     (medium) |
|                         [Edit][Accept][Reject] |
|  Hourly rate  + 95 EUR   Check this     (low)  |
|                         [Edit][Accept][Reject] |
|  > Unchanged (1)                               |
|  Not supported: ticketing target, approval     |
|  threshold                                     |
|  OpenCRQ not connected: Settings > Integrations|
|------------------------------------------------|
| [Cancel]                 [Save accepted (1)]   |
+------------------------------------------------+
```

**The review diff.**
- A row shows the field, the proposed value and its confidence tag; the
  `source` and `basis` show in a hover card on the tag (keyboard focusable),
  not in the row.
- A conflict row (`conflict: true`) shows the current value and the
  proposed value side by side with the tag "Replaces your value"; it is
  never pre-accepted.
- A low-confidence proposal carries the tag "Low confidence" and an
  inferred amount (`check: true`) the tag "Check this"; neither is
  pre-accepted.
- No row is pre-accepted at all: the administrator accepts each one, or
  uses Accept all.
- Accept all accepts the proposals that are neither a conflict nor "Check
  this" nor low confidence, and says what it skipped ("Accepted 4, skipped
  2 to review").
- A value equal to the current one is not a row to accept: the API answers
  it with `current` equal to `value` and `conflict` false, and the review
  groups it under "Unchanged (n)", collapsed.
- Edit turns the proposed value into the field's own control (the same as
  in the form) before accepting; an edited value keeps its `source` and
  is saved with `basis` "edited by <user>".
- Save accepted (n) counts the accepted rows only and is disabled at 0.
- One rule for the save path: the dialog never saves over an open form.
  Opened from read mode (the header menu), there is no form open, so Save
  accepted (n) writes one new version with source `ai_draft` and the
  accepted rows' `source` and `basis`, which the page Save could not carry
  field by field. Opened from edit mode (the form toolbar) or from the
  first-login step, the footer reads Apply accepted (n): the accepted
  values fill the form and the form's own Save writes them, like "From the
  platforms" on main.

**States of the panel.** Every state is inline in the dialog, never a toast
only. The error of "From the platforms" (a toast only on main,
`OrganizationSection.tsx`) becomes inline with Retry in S-b, since S-b
rewrites that header.

| State | What the panel shows |
|---|---|
| Drafting | one progress line per source (description, each document, website, each platform): reading, read, or skipped and why; Cancel stops the request |
| Quota refused | before any read: "Your agentic quota is used up (n of n this month), it resets on <date>." and a link to Settings > Usage; Draft disabled, inputs kept |
| Model failed or timed out | an inline error under the inputs naming the cause ("The model did not answer in 60 s"), Retry, inputs and uploaded files kept |
| Document not extracted | its line in the progress list: "suppliers.pdf: no text could be extracted (scanned image)"; the draft goes on with the other sources |
| Website refused | its line: "www.example.local: refused, private or unreachable host"; the draft goes on |
| Zero proposals | the empty state "Nothing in what you gave supports a field", the `not_supported` list, and a hint to add a document or a description; Back to inputs |

In edit mode the summary cards leave the page and the form starts at the
top. The completeness line and the summary cards read the same
`missing_fields`, so a counted field is never listed as not filled. A
platform that could not be read always says why and links where to fix it.

**Slices.** S-a backend service, the two endpoints, the proposals table and
the chat tool; S-b the profile panel, the review diff and the three staging
defects above; S-c tracked organizations, single and bulk; S-d the hook in
the first-login setup (#9173). Each UI slice passes the UX gate.

**First-login hook (S-d).** In the organization step of the first-login
setup (#9173), the same "Draft" menu (From the platforms, With AI) is the
secondary action under the step's form, and "With AI" opens the same panel; the
review diff fills the step's form (Apply accepted (n)), nothing is saved
before the step's own Save. Community Edition or no LLM configured: "With
AI" stays in the menu with a lock and opens the single CTA of #9173, the
deterministic draft stays.

**Tests that gate S-a.** No model call without the request (the service is
never imported by the page load or a scheduler); a proposal on a filled
field is answered with `conflict: true` and a `PUT` of an unaccepted field
leaves it unchanged; no fetch without a URL and no document read without an
upload; an unsupported field is in `not_supported`; a document of one
tenant is never visible to another; `draft_profile` and the 14 built-in
automations unchanged.

**Tests that gate S-b** (frontend unit tests, no class-name assertion): a
conflict row is never pre-accepted; Accept all skips conflict, "Check
this" and low-confidence rows and announces the count it skipped; Save accepted (n) counts
only the accepted rows; an unchanged value is not a proposal row; the
quota refusal, the model error with Retry (inputs kept) and the zero
proposal empty state render inline, never as a toast only; the header
actions render on one row at 1024 in fr and es (the Draft menu button, not
two buttons); the panel is a dialog (role `dialog`, labelled by its title
"Draft with AI") and focus returns to the Draft menu button on close;
Draft is disabled with its reason when no input is given and no platform
is connected; from edit mode the footer is Apply accepted (n) and nothing
is saved before the form's Save; the "With AI" entry shows the lock and
the #9173 CTA in Community Edition or without an LLM.

## 7. Gaps, inputs of the next slices

| Slice | Gap |
|---|---|
| S1 seed infrastructure | Shipped in #8988: `fleets.is_builtin`, `BUILTIN_FLEETS` (`backend/app/agents/builtin_fleets.py`, entry shape in its docstring) seeded by `app.agents.fleet_seed.seed_builtin_fleets` at boot in xtm_one and dev modes (company-managed, by slug, members by agent slug, gated member skipped with reports moved up, workspace per fleet, `entry_handovers` to the orchestrator, stale ones swept); REST, `fleets` tools and `apply_fleet_spec` refuse writes except `BUILTIN_FLEET_WRITABLE_FIELDS`. Left: max_iterations 100 on single-shot generators |
| S2 reactive modules | No OpenAEV or OpenCRQ event; OpenCTI event filters by entity type and label to verify; debounce for F4 refresh |
| S3 organization context | Store, API and `get_organization_context` shipped (#8987 slice 1); S2 trigger filters derived from the profile's PIRs (section 8.3); injection in fleet runs (section 6, slice 3b); filters from sector and regions (section 8.3, slice 3c); OpenCRQ primary-asset sync (section 6, slice 3d); onboarding form in Settings > Organization (administrators: every contract field, the missing fields named, each save a version with source `form`, the OpenCRQ check as a dry-run plan then apply; slice 2a); draft from OpenCTI, OpenAEV and OpenCRQ (`POST /organization-context/draft`, section 6, slice 2b); "Draft from the platforms" in Settings > Organization opens the form on the draft, names the platform of each filled field and each platform not read, saves with source `draft` (slice 2c). S3 complete |
| S4 ROI engine | Units in output schemas, baselines, rate, aggregation per agent and fleet, last-30-day usage |
| F1 to F3 | F1 shipped in #9114: `ctem-threat-response` in `BUILTIN_FLEETS`, the orchestrator and the three shared role agents (`CTEM_FLEET_AGENTS`, F2 and F3 reuse them by slug, with their ROI units), the three orchestrator assignments (`CTEM_F1_ASSIGNMENTS`; the OpenCTI trigger provisioned from the profile's sector and regions). Approval work items are the orchestrator's persona (`needs_human`, keys `ctem:f1:`). Left: OpenCRQ write path for expected loss (UC7; the quantifier drafts, never writes); ticketing integration |
| F4 | Orchestrator, event refresh assignment |
| C1 | Fleets, coverage, stats and ROI in the command center (no OpenCRQ and no usage metric today) |

## 8. Reactive modules (S2, #8991)

### 8.1 Auto-provisioned OpenCTI triggers (S2a)

An `integration_event` assignment on `opencti` declares what it reacts to in
`trigger_config.provision`:

```json
{"provider": "opencti", "event": "create",
 "provision": {"fleet": "Threat response", "purpose": "New threats",
               "entity_types": ["Intrusion-Set", "Report"], "labels": ["ai"],
               "min_score": 50, "event_types": ["create"]}}
```

The scheduler tick (`services/trigger_provisioning.py`, provider registry;
OpenCTI pass in `services/opencti_trigger_provisioning.py`) creates in
OpenCTI, under the bound integration's identity, a Generic webhook notifier
posting to `/api/hooks/integrations/<integration>/assignments/<assignment>`
with `X-OpenCTI-Token` set to the integration's `webhook_secret` (no secret:
nothing is provisioned, the state says why; a platform-managed integration,
which the registration writes without one, gets a secret generated and stored
in its extra config by the first pass that needs it,
`trigger_provisioning.receiver_secret`, #970), and a live knowledge trigger with
the filters (`entity_type`, `objectLabel` by resolved label id,
`x_opencti_score` gte) and that notifier. Both are named
`XTM One - <fleet> - <purpose>`.

- Idempotent: the ids live in `trigger_config.opencti_provisioning`; a lost
  state adopts the objects by exact name instead of duplicating them.
- Repaired: every 15 minutes the objects are read back; a deleted one is
  recreated, an edited name, filter, notifier or configuration is patched, a
  change of event types replaces the trigger.
- Removed: when `provision` is dropped, the assignment is disabled or deleted.
  An unbound integration leaves the state `orphaned` with the reason.
- Bound: the assignment's binding, else its agent's; an agent that reaches the
  module only by template binding (`integration_template_bindings`, the seeded
  agents) gets the integration its provisioning run-as user resolves at run
  time (`trigger_provisioning.template_integration_id`: the user's own first,
  else a company-wide one, oldest first so the receiver URL stays stable; never
  the seed owner). None resolvable: the state says `template binding opencti:
  no OpenCTI integration resolvable for the run-as user`. The OpenAEV pass and
  the CTEM-08 live PIR score re-read (the provisioned `integration_id`) follow
  the same rule, and so does the event poller (`event_poller._poll_integration`,
  the OpenCRQ job events): a template-resolved integration is polled as the
  run-as user. A poll that reads nothing says why in `get_assignment`
  (`Poll skipped: <reason>`, `event_poll_skip:<assignment>`, a day).
- Visible: `opencti_provisioning` (status, trigger_id, notifier_id, error,
  checked_at) is returned with the assignment's `trigger_config`, next to
  `last_triggered_at`.
- De-duplicated: the notifier's body carries `notification_id` =
  trigger, entity, event, modification time; the receiver's existing
  `opencti:<notification_id>` slot drops a redelivery, never a new change.
- No filter at all is refused: a trigger on every change of the platform is
  never provisioned.
- Vulnerability signals (F2, #9120): `cisa_kev` (true or false,
  `x_opencti_cisa_kev`) and `min_epss_score` (0 to 1, gte on
  `x_opencti_epss_score`); both set, either one matches (one OR filter
  group). `from_types` keeps a relationship or sighting whose source is of
  these types (`fromTypes`; a sighting's `sighting_of_ref`), so
  `{"entity_types": ["stix-sighting-relationship"], "from_types":
  ["Vulnerability"]}` is a sighting of a vulnerability.
- Relationship target, type and CVSS (CTEM-08): `to_types` keeps a
  relationship whose target is of these types (`toTypes`, parent types
  included); `relationship_types` names the relationship type (`has`,
  `targets`...), matched on `entity_type`, which is the relationship type of
  a relationship in OpenCTI; `min_cvss` (0 to 10, gte on
  `x_opencti_cvss_base_score`) is read on the delivered object, so it applies
  to a Vulnerability, never to a relationship.
- Trigger rule "a vulnerability on software we run" (CTEM-08):
  `trigger_config.runs_in_profile: "source"` (beside `provision`, never
  sent to OpenCTI) keeps a relationship delivery only when its source
  (`source_value` of the OpenCTI extension) names a tech stack product
  (product name or CPE) or an asset (name or external id) of the primary
  organization profile. The receiver reads it before the trigger's filter,
  gate or run (`services/ctem_trigger_rules.py`); a drop is counted on the
  assignment with its reason. The F2 orchestrator's "Vulnerability on
  software we run" listener uses it on `has` from Software, Infrastructure
  or System to Vulnerability, whatever KEV or EPSS. The OpenCTI signal
  listener records the source in the signal's `evidence.software`, so the
  scope gate resolves the signal to that product.
- Checked at save time (#970): create and update (REST and the agent
  builder tools) run the provider's `read_provision_spec` on the trigger as
  it will be saved (`trigger_provisioning.provision_spec_error`) and refuse
  an unusable block with the pass's own message, never 15 minutes later.
  `organization_context` is a list of profile field names (`["pir_ids"]`,
  any of `pir_ids`, `regions`, `sector`): the values are read from the
  primary organization profile, so an object such as `{"pir_ids": [...]}` is
  refused. A subset of the profile PIRs is not supported: the playbook
  listens to every PIR of the profile; narrow the PIRs in the profile.
- Trigger rule "PIR score thresholds" (CTEM-08, per PIR since #2435):
  `trigger_config.pir_min_score: "policy"` (beside `provision`, never sent
  to OpenCTI) holds a PIR playbook delivery (below) to the minimum score of
  each delivered PIR on the Intelligence policy (`organization_pirs`, one row
  per OpenCTI PIR, refreshed hourly from every connected OpenCTI by the
  scheduler, `services/organization_pirs.py`). Only the PIRs enabled in
  signals count: the hydrated profile derives `pir_ids`, `pir_names`,
  `pir_details` and `pir_min_scores` from the enabled rows, so
  `organization_context: ["pir_ids"]` resolves to them and a PIR disabled on
  the policy leaves the playbook at the next provisioning pass. A PIR with no
  stored row falls back to the policy's former `pir_min_score_vulnerability`
  (default 70) or `pir_min_score_threat` (default 75), kept on the profile
  for one release. The delivered score lags one change, so the receiver
  re-reads the entity's live `pirInformation(pirId)` for each delivered PIR
  with the trigger's OpenCTI integration and starts the run when one PIR
  reaches its own minimum; a score it cannot read drops the delivery naming
  why (a narrowing rule never widens on an outage). A delivery whose entity
  is not of the listener's `provision.entity_types` is dropped too, since one
  playbook per listener delivers every flag of the enabled PIRs, and so is
  any type the policy has no threshold for (a report, an indicator): the rule
  fails closed. Seeded on "Vulnerability flagged in a PIR" (F2) and "Threat
  activity flagged in a PIR" (F1), both opt-in. The playbook carries one
  `pir_score` filter per distinct minimum (a nested group of the PIR ids that
  share it), so OpenCTI already drops a flag below its PIR's minimum; the
  receiver check stays for the lag. Migration `i2435pirpolicy01` carried the
  profile PIRs as enabled rows with their details and the former threat
  minimum, and removed the `pirs` organization syncs.
- PIR flags (CTEM-05): `provision.mode: "pir_playbook"` (with
  `organization_context: ["pir_ids"]`, required) provisions an OpenCTI
  playbook instead of a live trigger; `provision.pir_ids` (OpenCTI ids of
  profile PIRs) narrows it to a subset, an id outside the profile being an
  error that names it. OpenCTI flags an entity in a PIR
  through its internal `in-pir` relationship and writes the entity's
  `pir_information` without a stream event, so a live trigger with a
  `pir_score` filter never sees the flag. The playbook entry built for it,
  "Listen PIR events" (`PLAYBOOK_DATA_STREAM_PIR`), listens to the profile
  PIRs (enter and score change, `pir_score` gte `pir_min_score`), then
  "Send to notifier" posts to the same Generic webhook notifier and
  receiver, delivered to the integration's own OpenCTI user only (the
  notifier component checks that user's access to the entity, markings
  and organisations included). Same naming, state block, recheck and
  removal as a trigger; any drift (a node edited, the playbook stopped)
  rebuilds the playbook; switching mode deletes the other object. The
  delivery records a signal on the path the entity type gives
  (`relevant_threat`, `new_vulnerability`) with `evidence.pir`
  `[{pir_id, score_at_delivery}]`: ids and score only, the criteria and
  the causing relationship stay in OpenCTI. Read in a lab on OpenCTI
  master (21 Nov 2026): an entity entering a PIR is delivered once (type
  create); a repeated flag with the same score is not delivered; each
  score change is delivered once (type update) but carries the score
  before the change, because OpenCTI renders the entity before rewriting
  its PIR information. The delivery key therefore includes the delivered
  PIR information, and the fleet reads the live score from OpenCTI
  before acting. Leaving a PIR is not delivered (the delete toggle stays
  off).

### 8.2 Event enrichment

The notifier posts the whole STIX instance as `data`; the receiver's
`_opencti_event` now carries `stix_id`, `entity_id`, `score`, `labels`,
`created`, `modified` and `description` with `entity_type` and `entity_name`.

### 8.3 Filters derived from the organization profile (S3, #8987)

`provision.organization_context` names the profile fields (section 6) the
trigger derives its filters from, read again at every check, so a profile
save repairs the trigger at the next pass:

```json
{"provision": {"fleet": "Threat response", "purpose": "PIR threats",
               "entity_types": ["Report"], "organization_context": ["pir_ids"],
               "pir_min_score": 20}}
```

- `pir_ids` adds OpenCTI's composite `pir_score` filter (`score` gte
  `pir_min_score`, default 1, and `pir_ids` from the profile): an entity
  flagged in one of the organization's PIRs.
- A declared field the profile leaves empty puts the state in `error` naming
  it; nothing is provisioned wider than the fleet asked, nothing is guessed.
- `regions` and `sector` resolve, at every check, to OpenCTI's Country
  entities (the ISO alpha-2 code or its alpha-3 code among
  `x_opencti_aliases`) and Sector entity (exact name or alias,
  case-insensitive), and add one OR group: a container holding one of them
  (`objects`) or a relationship pointing at one (`toId`). Live stix
  filtering has no `regardingOf`, so an entity related to a country only
  through a relationship of its own does not match its creation event; the
  relationship's creation does.
- A region or sector OpenCTI holds no entity for puts the state in `error`
  naming it (`organization context not found in OpenCTI: regions ['JP']`).

### 8.4 Auto-provisioned OpenAEV triggers (S2c)

OpenAEV already has a notifications engine (`/api/notifiers`,
`/api/notification-triggers`: LIVE triggers on a resource type with
CREATE / UPDATE / DELETE and a filter group, WEBHOOK notifiers with url, verb
and static headers), so S2c needs no OpenAEV change.
`services/openaev_trigger_provisioning.py` is registered in `_PROVISIONERS`
and works like the OpenCTI one (idempotent, 15 min repair, removal on drop,
disable or delete, state in `trigger_config.openaev_provisioning`):

```json
{"provider": "openaev", "events": ["simulation_finished"],
 "provision": {"fleet": "Exposure validation", "purpose": "Results",
               "events": ["simulation_finished", "finding_created"]}}
```

| Event | OpenAEV trigger |
| --- | --- |
| `simulation_finished` | `SIMULATION` `UPDATE`, `exercise_status` eq `FINISHED` |
| `atomic_test_finished` | `INJECT` `UPDATE`, `inject_status` eq `EXECUTED`, `PARTIAL`, `ERROR` |
| `finding_created` | `FINDING` `CREATE` |

- One WEBHOOK notifier per assignment posts OpenAEV's default body to the
  assignment's receiver with the integration's `webhook_secret` as
  `X-OpenAEV-Token` (checked by `_verify_signature`, never persisted). No
  secret: the state is `error`, nothing is wired unauthenticated.
- One LIVE trigger per event, named `XTM One - <fleet> - <purpose> - <event>`;
  the receiver keys the delivery on that suffix (`_normalize_openaev_payload`)
  and records `_openaev_event` (event, operation, resource type and id,
  message, platform URL).
- Expectation results have no notification resource in OpenAEV
  (`NotificationResourceCatalog`): not provisioned, a remainder of S2.

### 8.5 OpenCRQ events from its job runs (S2d)

OpenCRQ has no outbound webhook or notifier: its `notifications` rows are
per-user UI entries and its `data_changed` channel is an internal Postgres
LISTEN. Every event the fleets need is the successful end of a background
job, and OpenCRQ already lists those runs through the tRPC procedure
`jobs.listAllRuns` (filters `kinds`, `statuses`, `finishedFrom`; sort
`finishedAt`), the read the `list_opencrq_jobs` tool makes. So S2d needs no
OpenCRQ change: `services/opencrq_job_events.py` maps the events to job
kinds and `event_poller` polls them every 60 s.

| Event | Job kinds (status `succeeded`) |
|---|---|
| `risk_scenario_recomputed` | `crq-simulation`, `crq-price-recompute` |
| `finding_score_changed` | `finding-score-recompute` |
| `control_validation_ingested` | `openaev-findings-sync`, `proven-posture-record` |

- The poll reads with the compound credential the OpenCRQ tools run on
  (`_get_token` for the assignment's acting user: an API key, or a token
  minted on a platform-managed integration).
- Tenant: the trigger filter's `tenant_id`, else the account's sole tenant
  (several tenants without a choice log an error and dispatch nothing).
- The cursor is the newest `finishedAt` dispatched; `finishedFrom` is
  inclusive, so the run at the cursor comes back and the seen set skips it
  (`message_id` = `opencrq:<tenant>:<run id>`). The first poll starts from now.
- The run receives `entity`: run id, kind, status, enqueued, started and
  finished times, processed and expected totals, progress message (OpenCRQ's
  `currentStep`), trigger, attempt, tenant and the instance URL.
- Moved scenarios (#1663): a `crq-price-recompute` run's progress message is
  a JSON summary; its `moved` ids, `movedCount` and, when OpenCRQ reports it,
  `movedAle` travel as `moved_scenario_ids`, `moved_count` and `moved_ale`
  (`risk_scenario_id`, `ale_before`, `ale_after`, `ale_delta`) on the payload
  top level and the entity, merged across a coalesced page: `moved_count` is
  the number of distinct moved scenarios (at least the largest count one run
  reported, since OpenCRQ caps the ids), never a sum over runs (#2117). The
  event declares `coalesce_merge` (union of the ids, `moved_ale` by scenario,
  the count kept whole), so a page folded into a waiting run keeps both
  pages' moves. The filter keys
  of every OpenCRQ event (`FILTER_KEYS`, declared in the catalog) are
  `tenant_id`, `kind`, `risk_scenario_ids`, `moved_scenario_ids` and
  `moved_count`, all payload fields, so `{"moved_scenario_ids": "<id>"}`
  runs the assignment when that scenario moved.
- Coalescing (#1417): a poll page is ONE event, not one per job. The newest
  run is the entity and the `message_id`; `covers` lists the runs of the page
  (their own `message_id`s, marked seen at dispatch), `run_count` counts them,
  and a run that reports progress and processed nothing (`expectedTotal`
  set, `processedTotal` 0) is no event. A kind that never reports progress
  (`crq-price-recompute`) ends at 0 whatever it re-priced, so its runs are
  kept. The `riskScenarioId(s)` of a job payload travel as
  `entity.risk_scenario_ids` and on each `covers` entry. The event carries
  `coalesce_key` = `opencrq:<tenant>:<event>`; the poller copies it into the
  trigger context and `trigger_or_queue_assignment` folds a later event with
  the same key into the run of the same assignment that has not started,
  queued or `pending` in a free slot (#2117; the executor re-reads the
  payload under the row lock before it marks the run running)
  (`services/event_coalescing.py`, status `coalesced`). The risk desk's three
  event assignments run with `max_concurrent_runs: 1`: a burst of 30 jobs
  starts one run and queues at most one more.

### 8.6 Known limit and the next slices

OpenCTI delivers to a service-account webhook notifier since
opencti #19665 (S2b). A finding score change is reported per recompute run,
not per finding: the run reads the changed findings itself.

### 8.7 The provisioning line on the assignment page (S2e)

The Trigger card of an `integration_event` assignment on OpenCTI or OpenAEV
shows what XTM One holds in that platform (`ProvisioningSummary`, read from
`trigger_config.<provider>_provisioning` and `trigger_config.provision`,
no new API field):

| State | When | Line |
| --- | --- | --- |
| Provisioned | `status` `provisioned` | the object name, the trigger count (one on OpenCTI, one per event on OpenAEV), last check, last event (`last_triggered_at`) or "No event yet" |
| Failed | `status` `error` | a catalogued sentence and the last check; the server's error text is never shown |
| Still held | `status` `orphaned` | the spec was dropped or the assignment disabled, the platform could not be reached to remove the objects |
| Waiting | `provision` declared, enabled, no state yet | created at the next check |
| Paused | `provision` declared, disabled | nothing is created until an administrator enables it (the opt-in of #9160) |

Saving the Trigger card on the same provider keeps `provision` and both
provisioning states, so an edit never removes what was created.

## 9. Multi-organization and third-party risk (#9157)

Status: decided (6 Nov 2026: D1 A, D2 A, D3 A, section 9.9). Built in
slices M1 to M6 (section 9.10); M1 (model, migration, API, the
`organization` argument of `get_organization_context`) and M2 (the tracked
organizations list and form under Settings > Organization,
`TrackedOrganizations.tsx`) are implemented, the rest is design. Read from `main` at `dcfbdcc597d9` (xtm-one),
OpenCTI `master` (`modules/pir/pir.graphql`, `vocabulary-utils.ts`) and
OpenCRQ `main` (`organizations`, `risk-scenarios`, `asset-dependencies`,
`supporting-assets` schemas).

Goal: the CTEM covers the platform's own organization (the primary one)
and the organizations it depends on or serves: key providers, key
customers, partners, subsidiaries. Each one gets its context, its PIR in
OpenCTI and opt-in monitoring, and a breach or a threat on a provider ends
in an expected loss for the primary organization and proposed actions.

### 9.1 What exists today

| Platform | Exists | Gap |
|---|---|---|
| XTM One | One profile, `organization_context_versions` (unique `version`, section 6); `get_organization_context`; the fleet-run block; trigger filters from `pir_ids`, `sector`, `regions` (section 8.3) | No notion of a second organization |
| OpenCTI | `Organization` entity with `x_opencti_organization_type` (vocabulary `organization_type_ov`: `constituent`, `csirt`, `partner`, `vendor`, `other`); `Pir` (`pir_type` `THREAT_LANDSCAPE`, `THREAT_ORIGIN`, `THREAT_CUSTOM`, `pir_criteria` of weighted filter groups, `pir_filters`); the PIR form builds `targets` criteria on locations and sectors | No `customer` or `subsidiary` type; nothing links an Organization to a PIR other than criteria |
| OpenCRQ | `organizations` are scopes (sector, country, revenue band, estate); `risk_scenarios` belong to one scope with an attack outcome and primary assets; `support_to_primary_links` and `support_to_support_links` model dependencies; supporting asset category `saas_application` (`saas_app`, `saas_tenant`) | No third-party scenario type and no supplier entity (a `vendor_dependency` reason exists only for finding dismissal) |

### 9.2 Model (XTM One)

- New table `organizations`: `id`, `name`, `role` (`primary` exactly once,
  else `tracked`), `relationship` (`provider`, `customer`, `partner`,
  `subsidiary`; null for the primary), `criticality` (`low`, `medium`,
  `high`, `critical`), `opencti_organization_id` (nullable),
  `monitoring` (`off` by default), `created_at`, `archived_at`. A partial
  unique index keeps one `primary`.
- `organization_context_versions` gains `organization_id` (FK, not null
  after the migration); `unique(version)` becomes
  `unique(organization_id, version)`. Versioning stays per organization:
  every save inserts the next version of that organization only.
- The profile contract (section 6) is kept for the primary. A tracked
  organization's profile is a subset plus two fields: `sector`, `regions`,
  `services` (what it supplies to or consumes from the primary: name,
  direction `supplies` or `consumes`, the primary assets it supports, the
  OpenCRQ supporting asset id when linked), and `contacts_owner` (the
  internal owner of the relationship, a user, never the third party's
  contact). Primary assets, AI targets, risk appetite, rate and ticketing
  stay primary-only: the loss is always the primary's.
- API: `GET /api/v1/organizations` (list), `POST` / `PATCH` / archive for
  tracked ones (administrators), and the per-organization profile at
  `/api/v1/organizations/{id}/context` with the same `PUT` and sources as
  section 6. `/api/v1/organization-context` stays and answers the primary.
- `get_organization_context` gains an optional `organization` argument
  (name or id); without it, the primary, as today. A new
  `list_tracked_organizations` is not needed: the tool answers the list of
  tracked names when asked with `organization="*"`.

### 9.3 Migration

One Alembic revision: create `organizations`, insert the primary with the
name of the latest profile (`organization_name`, else "Primary"), set
`organization_id` on every existing version, then the composite unique.
No behaviour change: every reader that does not name an organization
reads the primary exactly as before (the trigger provisioners, the
fleet-run block, the ROI report, the draft and the OpenCRQ sync).

### 9.4 OpenCTI: identity and PIR per organization

- Identity: a tracked organization links to an existing OpenCTI
  `Organization` (search by name and alias, the administrator picks; a
  missing one is created on the administrator's confirmation only).
  `x_opencti_organization_type` is set only when empty: `provider` to
  `vendor`, `partner` to `partner`, `subsidiary` to `constituent`,
  `customer` to `other`. It is never overwritten; the relationship of
  record lives in XTM One. No OpenCTI change is needed.
- PIR (decision D2 = A): one PIR per tracked organization of criticality
  `high` or `critical`; the others share one PIR named
  `XTM One - third parties` whose criteria list each of them. Created with
  `pirAdd`, `pir_type`
  `THREAT_CUSTOM`, criteria: `targets` towards the Organization entity
  (weight 3), towards its sector (weight 1), towards its regions
  (weight 1); `pir_filters` confidence gte the profile's threshold; name
  `XTM One - <organization> - third-party`. Its id is saved in that
  organization's profile `pir_ids`, so the section 8.3 filter works per
  organization unchanged. Reuses `manage_opencti_pir`.
- Built (M3, `services/organization_opencti.py`): `GET
  /api/v1/organizations/opencti-identities?search=` (exact name or alias
  first), `POST /api/v1/organizations/{id}/opencti-identity`
  (`identity_id`, or `create: true` to create it), `POST
  /api/v1/organizations/opencti-pirs` (`apply`, `min_confidence`; a dry
  run by default). Administrators only, through the company-managed
  OpenCTI integration with a token minted for the administrator.
  `pirFieldPatch` cannot edit criteria, so a PIR whose criteria changed is
  deleted and created again; a PIR of ours no organization needs is
  deleted; a PIR not named by this module is never touched. A tracked
  organization without an identity gets no PIR and is reported, as is a
  sector or region OpenCTI holds no entity for.
- Triggers per monitored organization, through the existing provisioner
  (section 8.1) with a new `provision.organizations` key (`primary`,
  `tracked`, or a list of relationships); the filter values are the union
  of the monitored organizations' ids, so the number of OpenCTI triggers
  does not grow with the number of organizations:
  - threat or vulnerability targeting the organization or its sector:
    `targets` relationship with `toId` in the organization and sector ids;
  - a sighting naming it: `stix-sighting-relationship` with `toId` (where
    sighted) in the organization ids;
  - a breach or a ransomware leak naming it: `Report` with `objects`
    holding an organization id (the leak-site connectors, e.g.
    `ransomlook`, import victims; whether they link an existing
    Organization is verified in M4);
  - a PIR flag: the `pir_score` filter on the tracked PIRs.
  The receiver resolves which organization an event concerns from the
  entity ids and puts it in the run's `--- Organization context ---`
  block (section 6) next to the primary's.
- Built (M4, `services/opencti_trigger_provisioning.py`,
  `services/organization_events.py`): `provision.organizations` takes
  `primary` (the default, nothing changes), `tracked` or a list of
  relationships; the scope keeps the tracked organizations with monitoring
  on and an OpenCTI identity, read again at every check. One OR group:
  `objects` or `toId` in the organizations' identity and sector ids, or
  `pir_score` on their PIRs. The provisioning state keeps, per
  organization, the internal and standard ids of its identity and sectors;
  the receiver matches the delivered instance's ids against them (an
  identity match wins over a sector match), records the organizations in
  the trigger payload (`_organizations`), and the run block appends their
  profiles after the primary's. `organization_context` (primary fields) is
  refused with a tracked scope. Leak-site victims are matched only when the
  connector links an existing Organization (to be read in the M4 lab).
- OpenCTI change: none for the first slices. If organization matching by
  alias proves weak, the change is a tiny filter key, auto-merged; a new
  object type would be heavy and team-reviewed, and is not proposed.

### 9.5 Fleets

- F1 to F4 take an organization scope in their assignments
  (`provision.organizations`, default `primary`): nothing changes for an
  existing installation.
- Third-party risk (decision D1 = A): a new fleet F5
  `ctem-third-party-risk`, enabled on its own (admin opt-in, off by
  default like every seeded fleet); it reuses the F1 role agents by slug
  and adds one orchestrator. A supplier-exposure case holds: the threat or breach
  (OpenCTI entity), the organization and its relationship, the dependency
  (the services it supplies and the primary assets behind them), the
  expected loss from OpenCRQ (section 9.6) and the proposed actions
  (contact the supplier, compensating control, contract clause, monitoring
  raised). Every action is an approval work item for a person
  (`needs_human`); no tool of the fleet writes to the third party, and the
  persona states it: never contact a third party automatically.
- Customer and partner events produce an awareness case (no loss figure
  unless a service they consume is named), never an action toward them.

Built (M5): `backend/app/agents/ctem_third_party_risk.py` holds the
orchestrator `ctem-third-party-risk-orchestrator` (bindings `opencti` and
`opencrq`, tools `work_items` and `shared_state` only, delegates to the
three F1 role agents connected by slug), its two assignments and the
`ctem-third-party-risk` entry of `BUILTIN_FLEETS`. "Tracked organization
named" is an OpenCTI `integration_event` on Report and Incident creation
with `provision.organizations` `tracked` (M4), so the run block names the
organizations concerned; "Weekly third-party review" runs `0 7 * * 3`.
Both spread `SEEDED_FLEET_OPT_IN` (off, admin opt-in); the opt-in tripwire
lists them. Approval items are keyed `ctem:f5:approval:`, the case state is
`ctem:f5:state`. The persona reads the loss through the supporting asset of
section 9.6 and states the never-contact rule. No migration (the two
assignments are seeded off, so `c9160fleetoptin1` does not name them), no
UI, no ROI unit yet (no catalog unit fits a supplier case).

### 9.6 OpenCRQ: expected loss of a supplier exposure (decision D3)

OpenCRQ has no third-party scenario. Recommended without an OpenCRQ
change: a provider's service is a supporting asset of the primary scope
(`saas_application` or `generic_asset`, named
`<organization> - <service>`), linked with `support_to_primary_links` to
the primary assets it supports; the case asks OpenCRQ for the scenarios
whose primary assets are reached through that asset and reads their loss
estimates. The loss stays on the primary organization, where the financial
profile lives. The alternative is one OpenCRQ organization scope per
tracked organization, which needs a financial profile per third party
that the primary does not own.

### 9.7 Quota and opt-in

- Every tracked organization starts with `monitoring` `off`; nothing is
  provisioned in OpenCTI and no run is metered until an administrator
  enables it (rule of item dev:ctem-fleets-opt-in).
- Before enabling, the setup flow shows the expected runs per month for
  that organization: the count of OpenCTI events over the last 30 days
  that the would-be filters match (a read-only count query per purpose),
  per fleet, next to the quota left.
- A per-organization daily cap (default 5 runs) holds the extra events
  as one digest run the next day; the cap is shown and editable.
- Built in M6 (backend): `organizations.daily_run_cap` (1 to 100,
  default 5, editable with `PATCH /api/v1/organizations/{id}`); the
  OpenCTI receiver counts an event against each tracked organization it
  concerns (UTC day, Redis) and holds it once every one of them used its
  cap; the autonomous tick runs the held events of one assignment and one
  organization the next day as one digest run (`_organization_digest` on
  the trigger payload, the organization in `_organizations`). A Redis
  outage runs the event: the cap never drops one. `GET
  /api/v1/organizations/{id}/run-estimate` answers the expected runs per
  month per enabled assignment whose `provision.organizations` serves the
  organization (one read-only `globalCount` per assignment over 30 days,
  bounded by the cap). Code: `services/organization_run_cap.py`.

### 9.8 Organization page and setup

Coordinated with dev:ctem-fleets-setup-experience: Settings > Organization
lists the primary organization first, then the tracked ones (relationship,
criticality, OpenCTI link, PIR, monitoring state, expected runs). The
first-login wizard sets up the primary only; tracked organizations are
added from the page.

### 9.9 Decisions

Decided by Sam on 6 Nov 2026: the recommendation of every row, so D1 = A
(F5 only), D2 = A for organizations of criticality `high` or `critical`
and one shared "third parties" PIR for the others, D3 = A (no OpenCRQ
change).

| | A | B | Decided |
|---|---|---|---|
| D1 third-party use case | New fleet F5 `ctem-third-party-risk`, enabled on its own | F1 extension with an organization scope | A: opt-in and quota stay separate from F1; the code reuses F1's role agents |
| D2 OpenCTI PIR granularity | One PIR per tracked organization | One shared "third parties" PIR whose criteria name every tracked organization | A for organizations of criticality `high` or `critical`, the others share one PIR (one PIR per org keeps the PIR's score readable per organization) |
| D3 OpenCRQ loss of a supplier exposure | Provider service as a supporting asset of the primary scope, loss read from the primary's scenarios | One OpenCRQ scope per tracked organization | A: no OpenCRQ change, the loss stays the primary's |

### 9.10 Slices

Each slice is one small PR with its acceptance in the XTM One mode lab.

| Slice | Content | Acceptance in the lab |
|---|---|---|
| M1 | `organizations` table, migration, API, `get_organization_context(organization=)` | The existing profile reads as the primary; every reader unchanged (tests); a tracked organization saved and versioned alone |
| M2 | Organization page: primary plus tracked list and form | Add, edit, archive a tracked provider |
| M3 | OpenCTI identity link and PIR per tracked organization | The PIR appears in OpenCTI with its criteria; its id in the profile |
| M4 | `provision.organizations` in the OpenCTI provisioner, the organization resolved in the run block | A report naming the provider fires one run whose prompt names it |
| M5 | F5 third-party risk fleet and its OpenCRQ read (after the seeded-fleet opt-in, dev:ctem-fleets-opt-in) | A supplier-exposure case with the loss and actions as approval items |
| M6 | Expected runs per organization and the daily cap in the setup flow | The estimate shows before enabling; the cap holds the extra events |

## 10. Fleet member configuration: defaults, bulk apply, organization scopes (#9162)

Status: design approved (UX Director round 3, #9165) and the four
decisions taken on 6 Nov 2026 (section 10.8); slices R1 to R5 are being
built, R6 is parked. Built: R1 (#9200), R2 (#9251), R3 backend
(organization scope model, API, tool, spec, fork, run block, #9345), the
R3 Organizations column (#9366), R4 presets (#9417, #9422, #9447) and the
R3 run estimate (`GET /fleets/{id}/run-estimate`, shown in the scope
editor before the save for the fleet default, one member or a selection;
not shown in the preset editor, which has no fleet). Read
from `main` at `54c20e31` (xtm-one). Designed with section 9: the
organization scope uses the `organizations` table of section 9.2.

Goal: a fleet's members are configured once for the whole fleet, then by
exception. Setting up a fleet of fifteen agents takes one screen and a
few actions, not fifteen cards.

### 10.1 What exists today

- `fleet_members` rows with role `connected` carry
  `collaboration_instruction` (text, 4000 characters at the API) and
  `context_inclusion` (`none`, `summary`, `full`, check constraint
  `ck_fleet_members_context_inclusion`). Home members (role `member`)
  carry neither.
- They are set one row at a time: `PATCH` on the member
  (`api/v1/fleets.py`), the `manage_fleet` tool's `connected` objects
  (`services/builtin_tools/fleets.py`), the fleet spec
  (`services/fleet_spec.py`) and the seeds (`agents/builtin_fleets.py`,
  `agents/fleet_seed.py`).
- The page (`frontend/src/components/fleets/FleetMembersPanel.tsx`,
  `ConnectedRow`) stacks one card per connected agent: a context picker
  ("No context", "A summary", "The full conversation") and a one-line
  instruction input saved on blur. No fleet default, no multi-select,
  no preset.
- Finding (grep of `backend/app` on `54c20e31`, to confirm with a test in
  R1): the two fields are referenced in 11 files, all of them storage,
  display, spec, seed or tool input. No reader was found in the
  consult, delegate or transfer path: the peer list of
  `services/agent_chat.py` renders the agent's description, not the
  row's instruction, and no code passes a summary or the conversation
  according to `context_inclusion`. Today the settings are recorded and
  shown, not applied (decision R-D2).
- There is no organization scope anywhere: one profile, section 6.

### 10.2 Two words, two notions

The page names them apart and never says "context" alone. Each field
has ONE label, used in the defaults, the column header, the bulk
actions, the side panel and the toasts (UX round 1, B6):

| Notion | Label (en) | Values (long / short in a cell) | Applies to |
|---|---|---|---|
| What a called member receives from the calling conversation (`context_inclusion`) | Conversation shared | Nothing / Nothing, A summary / Summary, The full conversation / Full conversation | Connected members |
| When and how the fleet works with the member (`collaboration_instruction`) | Collaboration instruction | free text (first line in a cell) | Connected members |
| Which organizations the agent reasons about (section 9) | Organizations | Own organization, a list of tracked organizations, All monitored organizations | Every member |

- Every label, long and short, comes from the one map
  `frontend/src/components/fleets/context-inclusion.ts` (`@i18n-keys`, read by the
  members panel, the "New fleet" dialog and the agent page's Fleet
  card); R2 adds the short set to it and no component hand-writes a
  value ("Full" alone is never shown).
- R2 renames "No context" to "Nothing" and the trigger aria-label
  "Context shared" to "Conversation shared" in that map and the four
  catalogs, so the three surfaces change together (A1). "Collaboration
  rule", "Fleet rule" and "Instruction" alone are not used.
- The French, Spanish and Japanese labels are added with the catalogs in
  R2; "fr" keeps the no-break space before ":" that the catalog test
  enforces.

### 10.3 Model

- `fleets` gains three columns, the fleet defaults:
  `default_context_inclusion` (`none` by default, same check),
  `default_collaboration_instruction` (text, empty by default) and
  `organization_scope` (JSON like the table's other settings,
  `{"mode": "primary"}` by default; `mode` is `primary`, `monitored` or
  `list` with 1 to 50 `organization_ids`, active organizations only).
- `fleet_members.context_inclusion` and `collaboration_instruction`
  become nullable: null means "inherits the fleet default". A new
  nullable `organization_scope` (same shape) on every member row; null
  inherits.
- One resolver, `services/fleets.effective_member_settings(fleet, row)`,
  answers the effective values and, per field, `inherited` or
  `overridden`. Every reader (the API, the graph, the tool, the spec, the
  run block) goes through it; nothing reads the raw columns.
- An agent's organizations for a run: the scope of the row of the fleet
  the run belongs to (its home fleet for an assignment run, the calling
  fleet's connected row for a consult), else the primary organization.
  An agent outside any fleet keeps the primary, as today. As built (R3):
  `fleet_runs.home_fleet_defaults` carries the effective scope of the
  home row, and the run's Organization context block lists the primary
  then each tracked organization of the scope (`monitored`: monitoring
  on; `list`: the ids still tracked and not archived), by name. The
  connected-row scope of a consult is stored, shown and exported; the
  consult run reads its home scope until the consult path carries the
  calling fleet (the same path R6 would change).

### 10.4 API and tools

- `PATCH /api/v1/fleets/{id}` takes `member_defaults`
  (`context_inclusion`, `collaboration_instruction`,
  `organization_scope`).
- `POST /api/v1/fleets/{id}/members/bulk`: `member_ids` and `set` (any of
  the three fields; `null` resets the field to the fleet default). One
  transaction, all or nothing, one audit event naming every row; the
  same rights as a single edit (`can_manage`). A refusal answers the
  reason and the rows it names; the page shows it in one toast and
  passes `suppressGlobalErrorToast` so the global handler does not add a
  second one.
- `GET /api/v1/fleets/{id}/run-estimate?organization_ids=...`: the
  expected runs per month per organization and per assignment of the
  fleet, from the read-only count query of section 9.7, next to the
  quota left. No model call, nothing saved.
- `POST /api/v1/fleets/{id}/members/{member_id}/instruction-draft`: an
  AI-drafted instruction, returned unsaved, only on the person's click,
  behind the AI gate (`REVIEWED_ESCAPES` not used), metered as that
  person's request. It is the `generate` callback of the shared
  `PromptGenerator` (10.5), never a page-local button. Shipped in R5a
  (`app/services/fleet_instruction_draft.py`): body `{brief, current_text?}`,
  answer `{text, truncated}`; 403 on a built-in fleet, 400 on a home member,
  502 when the model fails; the context names the fleet, its orchestrator,
  the connected agent or fleet and the fleet default, never an id.
- `manage_fleet` takes `member_defaults`; a `connected` object without
  `context_inclusion` or `collaboration_instruction` inherits. The fleet
  spec exports the defaults and only the overridden fields, and imports
  both, so an export round-trips. The tool's added arguments are counted
  against the built-in tools schema ceiling.

### 10.5 The page

Before (today, one card per connected agent in the 320 px column beside
the graph, repeated):

```
Connected agents
+------------------------------------------------+
| CTEM Risk Quantifier                         x |
| [ No context                              v ]  |
| [ When and how the fleet works with this...  ] |
+------------------------------------------------+
| Deep Investigation                           x |
| [ No context                              v ]  |
| [ When and how the fleet works with this...  ] |
+------------------------------------------------+
  ... fifteen times
```

After: a new "Members" view of the page-level `SegmentedFilter` of
`FleetPage.tsx` (Graph, Members, Schedule, Operations), drawn full width
(B3). `[#]` in `ListControlBar` is the select-all box (the only one,
round 2 B1), `[x]` a row's `SelectionBox`, `[<-]` the reset button (B4):

```
[ Graph | Members | Schedule | Operations ]

Fleet defaults                                     [ Apply a preset v ]
  Conversation shared        [ A summary v ]
  Organizations              [ Own organization, Acme (provider) v ]
  Collaboration instruction  "Answer the orchestrator with sources..." [Edit]
  Expected runs per month: Own organization 42, Acme 6.
  Nothing starts on save; monitoring stays opt-in per organization.

ListControlBar: [#] 3 selected  Clear | Conversation shared v | Organizations v
                | Apply a preset v | Reset to fleet defaults

+---+--------------------+-------------+----------------------+--------------------+---------------------------+-------------+
|   | Member             | Role        | Conversation shared  | Organizations      | Collaboration instruction | Automations |
+---+--------------------+-------------+----------------------+--------------------+---------------------------+-------------+
|[x]| CTEM Orchestrator  | Orchestrator| Not applicable       | Own organization   | Not applicable            | 3 enabled   |
|   |                    |             |                      | Fleet default      |                           |             |
|[x]| CTEM Risk Quantif..| Connected   | Summary              | Own organization   | Answer the orchestr...    | None        |
|   |                    |             | Fleet default        | Fleet default      | Fleet default             |             |
|[x]| Deep Investigation | Connected   | Full conversation    | Own, Acme +1       | Open-source context...    | None        |
|   |                    |             | Overridden    [<-]   | Overridden   [<-]  | Overridden          [<-]  |             |
+---+--------------------+-------------+----------------------+--------------------+---------------------------+-------------+
```

Line 1 of a cell is the value alone; line 2 is its muted caption: "Fleet
default" for an inherited value, "Overridden" plus the reset button for
an overridden one (round 2 B2). Every value stays on one line. The
orchestrator reads "Not applicable" in both Conversation shared and
Collaboration instruction, the same notion (A4 of round 2).

**Placement (B3).** The table is the "Members" view, not a tab or a
switch inside the members column. The "Graph" view keeps today's
hierarchy unchanged (the graph plus the 320 px side list: orchestrator,
parents, drag to re-parent, move, role label, exposure) and gains a
"Configure members" link that opens the Members view. R2 adds
`"members"` to `FleetView`, `readFleetView` and `fleetViewQuery`
(`frontend/src/lib/fleet-operations.ts`, which read only graph and
schedule from `?view=` today), so the link and a reload land on Members
(A2 of round 2). There is no inner
"Hierarchy / Configuration" toggle. In the Members view the
`ConnectedRow` cards are gone (R-D4).

**Fleet defaults.** The three defaults, the preset picker and the run
estimate, on top of the table. Before a default is saved the bar says
what it rewrites ("Applies to 12 members, 3 keep their override", A3);
the save gives the same Undo toast as a bulk action.

**One Members fold (#1387).** Under the Capabilities card (open first),
the fleet defaults, the toolbar, the selection bar and the table sit in
one "Members" section, folded by default: folded, one bar reads the home
and connected member counts and the current defaults (context, scope,
instruction) with a Show / Hide toggle; open, the defaults form shows
whole (no nested toggle), then the toolbar and the table. An unsaved
defaults edit or a selection keeps it open. A link with a members search,
filter or sort (`mq`, `mrole`, `mstate`, `msort`, `mdir`) or `mopen=1`
opens it; Hide over such a link writes `mopen=0`. No localStorage.

**Selection and bulk actions (B1).** The table uses the shared selection
of the product, never a page-local bar or a native checkbox:
- `SelectionBox` (`role="checkbox"`) on each row only; there is no
  header box (round 2 B1), as in `WorkItemsTab`, `SharedStateTab` and
  `BackgroundTasks`.
- `ListControlBar` draws the only select-all (with its mixed state),
  "N selected", Clear and the bulk actions. It hard-codes Delete and Approve today: R2 extends it with a
  generic bulk-actions slot (label, icon, handler, disabled reason) and
  the fleet page passes its four actions (Conversation shared,
  Organizations, Apply a preset, Reset to fleet defaults) through it.
- Keyboard: Tab reaches the row box, Space toggles the row, Shift+click
  and Shift+Space select the range from the last toggled row, Escape
  clears the selection. `ListControlBar` handles no Escape today: R2
  adds it to the shared bar (Escape with a selection runs Clear), with
  its unit test, so every list that uses the bar gains it (A5 of
  round 2).
- A gate test in R2 fails on a page-local "selected" bar (a "N
  selected" string or a bulk bar outside `ListControlBar`) under
  `frontend/src/components/fleets`.
- One call to the bulk endpoint per action, one toast with the count and
  an Undo (the previous values sent back through the same endpoint). A
  toast with an action is not force-removed (`toast-timing.ts`): its
  10 s is its `durationMs`, paused on hover and focus (A3).

**Cells (B4, A2).**
- An inherited cell shows the effective value with a muted "Fleet
  default" caption under it; the value is never only in a tooltip (A2).
- An overridden cell shows the value alone on line 1 and, on line 2,
  a muted "Overridden" caption (not a chip, no X glyph) with a 28x28
  icon button `RotateCcw`,
  aria-label and tooltip "Reset {field} to the fleet default". The X
  glyph stays reserved for removing a member from the fleet
  (`ConnectedRow`, "Remove {name}").
- A cell edits inline: the shared `Select` for Conversation shared, the
  Organizations editor below, the instruction dialog for the instruction.

**Collaboration instruction (B2, A4).** The cell shows the first line; a
click opens the shared centred `Dialog` (`components/ui/dialog.tsx`) with the
editor (4000 characters, the counter shown as "N / 4000"). It is a dialog,
not a drawer: the design standards (s2, Sam, 7 Nov 2026) allow no new
drawer, so the earlier `SlideOver` wording of this section is replaced
(UX round 1 of #9637, B1). The field carries an info bubble (what the
instruction is, an example, that the orchestrator reads it, that empty
inherits the fleet default); the reset sits beside the counter, so the
footer is one row, Cancel then a full-size Save:
- the AI first draft is the shared `PromptGenerator` with a `generate`
  callback calling `POST .../instruction-draft` (the pattern of
  `useDraftPresentationComment`) and a new `ContextType`
  `fleet_collaboration_instruction` with its `PLACEHOLDERS` entry (the
  Record in `prompt-generator.tsx` is exhaustive; A3 of round 2), so the AI-disabled tooltip and the
  "Improve with AI" path come with it; never a bespoke "Draft with AI"
  button, never automatic;
- "Use the fleet documentation" prefills from the fleet's documentation
  of dev:ctem-fleets-fork-and-docs when it exists, else from the seed's
  instruction;
- neither the prefill nor the AI draft overwrites typed text: they fill
  an empty field, else ask before replacing (A4).

Shipped in R5b (`components/fleets/MemberInstructionPanel.tsx`, pure parts
in `instruction-editor.ts`): the instruction cell is a button opening the
panel; below xl the `PanelRight` row action opens it. The documentation
prefill reads the lines of the documentation's `members` section that name
the member, else the fleet default instruction; Save with an empty text
resets the row to the fleet default.

**Organizations (A5).** The editor is a picker over runtime data, so it
uses the `SearchableSelect` / multi-select family, never the plain
`Select` (`searchable-lists.test.ts`): the primary first, then the
tracked organizations with their relationship, then "All monitored
organizations", which excludes a list (picking it clears the list and
disables it). The run estimate of the picked scope shows in the editor
before the save. In the cell, the list is the shared `TagList` with
"+N".

**Conversation shared is not applied yet (A9, R-D2 A).** Until R6, the
column header and the defaults field carry an `InfoTip` worded with the
product's copy (en: "Recorded for the fleet; applied to consults in a
later release"), so the page never implies the setting changes what a
member receives.

**Automations (A7).** The count of the member's enabled assignments with
an ICU plural (`{count, plural, one {# enabled} other {# enabled}}`), a
link to them; 0 reads "None" as plain muted text, never "Off", so it
does not read as a switch. Read-only here.

**States (A8).**
- A reader without `can_manage`: the view is read-only, with the reason,
  as the members panel today (no selection boxes, no bulk actions).
- An archived fleet: read-only, with the reason.
- A built-in fleet: the edit rights of today's Members card apply.
- A member the reader cannot see: the row reads "An agent not shared
  with you", without selection or edit.
- Loading: a skeleton of the defaults bar and five rows.
- No connected member: the shared `EmptyState` under the home members.
- A bulk refusal: one toast with the reason, `suppressGlobalErrorToast`
  set on the call.

**Widths (B5).** Every row stays on one line; the table never scrolls
sideways. Names and values use `min-w-0` + `truncate` + a tooltip with
the full text; Organizations is a `TagList` with "+N".

| Column | 1440 (content about 1130 px) | 1024 (content about 720 px) |
|---|---|---|
| Selection | 40 px | 40 px |
| Member | 220 px, truncate | 200 px, truncate |
| Role | 120 px | 110 px |
| Conversation shared | 190 px | 170 px |
| Organizations | 220 px, `TagList` +N | 200 px, `TagList` +N |
| Collaboration instruction | remaining (about 220 px), truncate | folded into the row's instruction dialog |
| Automations | 120 px | folded into the row's instruction dialog |

At 1024 each row opens its instruction dialog through a 28x28 row action at
the end of the Member cell (icon `PanelRight`, focusable, aria-label and
tooltip "Open {name} settings"); the dialog holds the collaboration
instruction with its reset and the automations. An overridden
instruction stays visible in the row: a muted "Instruction overridden"
caption under the member name (round 2 B2).

The reset button takes 28 px on line 2 of its cell, under the value,
so line 1 keeps the whole width for the value. R2's lab
acceptance measures fr, es and ja at 1024 (the longest labels), light
and dark from the design tokens.

### 10.6 Presets

A preset is a named set of values for any of the three fields
(`fleet_member_presets`: name, values). Decision R-D3 A: company-wide
presets managed by administrators, no personal preset. Three built-ins,
named with the words of 10.2 so a name says what it writes (A6), live in
code (`app.services.fleet_member_presets.BUILTIN_PRESETS`, ids
`builtin:<key>`), read only and never stored:

| Built-in | Conversation shared | Organizations |
|---|---|---|
| "Own organization, a summary" | A summary | Own organization |
| "All monitored organizations, nothing" | Nothing | All monitored organizations |
| "Own organization, the full conversation" | The full conversation | Own organization |

Built-ins carry the shared `CompanyManagedBadge` / `BuiltinBadge` in the
picker. A preset applies to the fleet defaults or to the selected rows
through the bulk endpoint, with the same Undo; it writes values, it keeps
no link (editing a preset changes no fleet).

**Built (R4 backend, #9162):** table `fleet_member_presets` (migration
`r9162presets01`), `GET /api/v1/fleet-member-presets` for every user (the
built-ins first, then the company's by name), `POST`, `PATCH /{id}`,
`DELETE /{id}` for administrators only (a built-in answers 403, a name is
unique without regard to case, a value is checked as a fleet default is, a
`list` scope must name active organizations). Applying a preset is no route
of its own: the picker sends its values through `PATCH /fleets/{id}`
(`member_defaults`) or `POST /fleets/{id}/members/bulk` (`set`).

**Built (R4 UI, #9162):** the Members view carries "Apply a preset" on the
fleet defaults bar and "Preset..." in the selection bar
(`components/fleets/ApplyPresetDialog.tsx`). The picker lists the built-ins
first (their names translated by id, `BUILTIN_PRESET_NAMES`), then the
company's presets as typed, and shows the chosen preset's values before
Apply. On the fleet defaults: one `PATCH /fleets/{id}` with the fields the
preset sets, a toast with an Undo sending the previous defaults back. On a
selection (`presetCalls`): one bulk call per kind of row, a connected row
taking every value, a home row its organization scope alone, one toast and
one Undo restoring each row's previous value or inheritance.

**Built (R4 admin, #9162):** Settings > Agents and automations > Fleets
carries a "Member presets" card under the fleet list
(`pages/settings/FleetMemberPresetsSection.tsx`). The Settings Fleets
section shows in XTM One mode only (`automationsSection`); the presets
themselves apply in every mode, and a Copilot-mode place to manage them is
open in the R4 remainder of #9162. Every user reads the presets with the values
each writes (the built-ins with their badge, read only). An administrator
creates, edits (name, and the values replaced whole) and deletes company
presets through the admin routes above: the editor turns on each setting
the preset writes (at least one), a name is required and unique among the
built-ins and the company's without regard to case (checked before the
call, the server's refusal said in a toast otherwise). Deleting asks first
and says that fleets keep the values the preset wrote.

### 10.7 Migration, quota and what does not change

- One Alembic revision, no `CONCURRENTLY`: the new columns with their
  defaults, then per fleet the defaults and the overrides of decision
  R-D1. Every effective value after the migration equals the stored
  value before it, asserted by a migration test over the seeded fleets.
- No run is started, no trigger provisioned and no assignment changed by
  a save: the 14 dashboard and decay assignments of the seeded fleets
  are untouched, and an assignment reads an organization scope only once
  its `provision.organizations` names one (section 9.4, default
  `primary`).
- The AI draft runs on the person's click only; the estimate is a count
  query. Zero automatic quota consumption.

### 10.8 Decisions for Sam

Decided by Sam on 6 Nov 2026 ("go with the recommended options"):

| Decision | Answer | What it means for the slices |
|---|---|---|
| R-D1 | B | The R1 migration sets, per fleet and per field, the most common member value as the fleet default; only the rows that differ keep an override. The migration test asserts that every effective value is identical before and after. |
| R-D2 | A | Conversation shared is stored and shown only, with the `InfoTip` of 10.5; consult and delegate do not apply it. R6 is out of scope until its own go. |
| R-D3 | A | Company-wide presets managed by administrators, plus the three built-ins of 10.6; no personal presets. |
| R-D4 | A, amended by UX B3 | One page-level "Members" view (a table for every member, home and connected); the graph keeps the hierarchy. |

The options as they were proposed:

| | A | B | Recommended |
|---|---|---|---|
| R-D1 migration of the existing values | Every existing row value kept as an override (every row shows the marker) | Per fleet, the most common value becomes the fleet default and only the rows that differ keep an override | B: effective values identical (tested), the page opens clean |
| R-D2 conversation shared, not applied today (10.1) | The redesign stores and shows it, as today, with the `InfoTip` of 10.5; applying it is a later slice with its own go | Slice R6 applies it to consult and delegate (nothing, a summary, the full conversation) | A now, R6 proposed separately: applying it changes the behaviour of every fleet whose rows say summary or full |
| R-D3 presets | Company-wide presets managed by administrators, plus the three built-ins | Personal presets per user, plus the built-ins | A: one vocabulary across the fleets of a company (the UX Director's note: the built-in names of 10.6 depend on it) |
| R-D4 the members view | One table for every member, home and connected, as a page-level "Members" view of the `SegmentedFilter`; the graph and its side list keep the hierarchy | The table for connected agents only, the tree unchanged above it | A, as amended by the UX Director (B3): one place to set a value, home members get the organization scope too, no inner toggle |

### 10.9 Slices

Each slice is one small PR, merged at green after the UX Director's
APPROVE for the UI ones, with its acceptance in the XTM One mode lab
(light and dark, 1440 and 1024, en, fr, es, ja; the captures come with
each UI PR, the wireframes above are ASCII).

| Slice | Content | Acceptance in the lab |
|---|---|---|
| R1 | Fleet defaults, nullable member fields, resolver, migration, bulk endpoint, tool and spec | Every seeded fleet reads the same effective values before and after; a bulk call of 10 rows is one transaction |
| R2 | Members view of the `SegmentedFilter`, table, defaults bar, `ListControlBar` bulk-actions slot and its gate test, Overridden marker and reset, Undo, the label map and catalogs of 10.2, the states of 10.5 | Set the conversation of 10 members in one action with Shift+click; reset one; fr, es, ja rows on one line at 1024 |
| R3 | Organization scope on fleets and members, run estimate, organizations in the run block (needs section 9 M1) | A fleet scoped to the primary and one provider shows both estimates; a run's prompt names both |
| R4 | Presets and the three built-ins | Apply "All monitored organizations, nothing" to a selection, then Undo |
| R5 | Instruction dialog, fleet documentation prefill, `PromptGenerator` draft on request | Prefill from the documentation; a draft appears only after the click and never replaces typed text without asking |
| R6 | Only on R-D2 B: conversation shared applied to consult and delegate, the `InfoTip` removed. Parked: R-D2 is A (10.8), R6 needs its own go | A summary-row consult receives a summary, a nothing-row nothing |

## 11. Fleet control: autonomy level, Enable, Pause all, Resume, Back to not set up (#9769, #11060)

**CTEM gate catalog (#916, CTEM-34).** The four CTEM loop fleets
(threat response, vulnerability prioritization, AI security, third-party
risk) declare one gate per loop stage, from
`app.agents.builtin_fleet_docs.CTEM_GATE_KEYS`:

| Key | Stage | Impact |
|---|---|---|
| `ctem.validation.non_readonly` | Validate: create or launch an OpenAEV scenario or atomic test | destructive (high on third-party risk, which has no OpenAEV step) |
| `ctem.analyst_review` | Review of the threat package before its security coverage is requested | low |
| `ctem.remediation.decision` | Decide the remediation option and record it as an OpenCRQ treatment plan | high |
| `ctem.mobilize` | Tickets, owners notified, prepared change handed over: one decision per case | destructive |
| `ctem.production_change` | A change applied on production through an integration: indicators blocked in the EDR in prevention mode, a rule deployed, a configuration changed (#2061) | high |
| `ctem.accept_risk` | Residual risk accepted beyond the policy cap | high |
| `ctem.close_without_retest` | A case closed with no passing re-test | high |

Mobilize is not only a ticket (#2061, Sam, 25 Nov 2026): each action of the
Remediation Coordinator declares its `mode`, `ticket`, `applied_change` or
`both`. An applied change runs through an integration bound to the Mobilization
slots (EDR/XDR, SIEM, beside ticketing and notification), under
`ctem.production_change`, and is written on the case with `update_ctem_case`
`applied_change` {integration, kind (`ioc_prevention`, `detection_rule`,
`configuration`, `control_restored`), pushed, ids, rollback}: one
`applied_change` event in the remediation phase that moves an open case to
`remediating`. Sigma stays detection (converted and deployed as a rule); only
indicators and the EDR custom rules that support it go to prevention mode. The
re-test still closes the case.

The coverage scenario starts after the validation decision (#2102, Sam, 25 Nov
2026). OpenAEV builds a scenario from every security coverage it receives and
schedules it two minutes later only when the coverage carries a periodicity.
When `ctem.validation.non_readonly` waits at the fleet's level (or is pinned
`wait`), the orchestrators create the coverage without periodicity (auto
enrichment left on), so the scenario exists unscheduled; the validation decision
shows it (link, injects, targets, read-only or not) with a daily-repeat option,
and the approval launches it (`manage_openaev_scenario` `launch`, then
`schedule_recurrence` when the repeat is chosen). The run's integration context
carries the gate keys that wait (`fleet_autonomy.waiting_gate_keys`, context key
`fleet_waiting_gates`); with the validation gate among them,
`manage_opencti_security_coverage` refuses a periodicity on `create` and
`update`, naming the gate, so a repeat is never set through the coverage. When
the gate runs, the coverage keeps `P1D` and starts on its own.

Launch scope (#2482, Sam, 26 Nov 2026). Every validation decision that
launches a scenario on asset groups (the initial validation and the re-test of
the threat response and vulnerability prioritization fleets) offers two scopes,
the default first and always the recommended one: **Default**, the scenario's
own targets, the whole asset group(s) as built (`manage_openaev_scenario`
`launch` without `asset_ids`, as before); **Sample**, a few assets of the group
(3 unless the person says otherwise, never more than the group holds; one per
platform present, agents online first, the case's own asset included when the
case names one), named on the card, which says the result measures only those
assets. `launch` with `asset_ids` duplicates the scenario, retargets every
inject of the copy to exactly those assets (`inject_assets`, no
`inject_asset_groups`, content targets aligned) and launches the copy; the
original scenario is never written, and a copy that cannot be fully retargeted
is deleted with nothing launched (`simulations._retargeted_copy`). OpenAEV's
own launch targets (`ScenarioTargetsInput`) are not used: they add targets to
the injects instead of replacing the group, behind a preview feature flag. The
daily repeat keeps the scope chosen; the Gate answered run launches the scope
the answer chose (a plain approve is the default) and writes the scope, the
assets and the scenario launched on the case. A sample that leaves out the
case's own asset says it does not close the case. A gate that runs without a
decision always launches the default scope.

No seeded gate names a tool or a grant (#1675, Sam, 24 Nov 2026): every tool
is offered and runs when called; a gate that waits is one decision item the
orchestrator files between two steps (`needs_human`, its ask card), and the
Gate answered run reads the answer and sets the case's next state itself. The
analyst review still offers `apply_opencti_draft` with Live writes off, by its
key (`builtin_fleet_docs.GATE_OFFERED_TOOLS`), never paused.
Decisions the loop raises itself (#1828, prompts of `_case_assignment` and
`_gate_answered_assignment`, no code enforcement): a re-plan answer on the
review gate is followed, once the new draft is attached, by the same
`ctem:review:<case_id>` decision filed again with the new draft link; an SLA
breach, or two answered decisions of one case that contradict, files one
`ctem:remediation:<case_id>` decision with options and a recommendation; an
answer in the person's own words that picks no option and decides nothing is
answered on the case and in the item's result, and the same decision is filed
again. Each phase (scoping, validation, mobilization) ends on one case note
`Verdict PHASE: proven affected | proven not affected | unproven` with its
evidence; the newest verdict of a phase holds (no new column: the note is the
canonical verdict until a product choice asks for a typed field).
At the seeded level (Low) two gates wait per case (Sam, 23 Nov 2026): the
OpenAEV validation (on the three fleets with the OpenAEV validation step) and
`ctem.mobilize`; every other gate runs and the case records it as passed
automatically. Reads and drafts are never a decision. One decision per
validation: its ask names the scenario or atomic test, its targets, schedule
and what it will do to them, and the answer covers creating and launching exactly that; a
new target, schedule or object is a new decision, by the persona. The coverage
request is under `ctem.analyst_review` alone (#1467). Full human gates: every
gate waits. Full autonomous: nothing waits. A pin (`wait` or `run`) moves one gate
either way on a built-in fleet too. The former keys (`ticket.create`,
`openaev.*.launch`, `supplier.action`) are gone from these fleets: a ticket is
the Mobilize action, so one Mobilize decision covers every ticket of its case.
The review step (CTEM-16): the vulnerability and threat orchestrators draft the
threat package as an OpenCTI Grouping (the vulnerability or threat, software,
attack patterns, a note on the exploitation conditions) and request its
security coverage, which OpenAEV builds its scenario from; the coverage
follows the run's OpenCTI draft. Out of the box the request runs and the case
records "analyst review skipped by policy"; pinned `wait`, the request pauses
as a decision until a person answers, or until the analyst sets the mark label
on the Grouping in OpenCTI (`analyst-reviewed`, or the policy's
`analyst_review_label`): the vulnerability fleet's listen-only assignment
"Analyst review mark" provisions the Grouping update trigger, and the webhook
route (`services/ctem_analyst_mark.py`) approves the coverage requests waiting
on that Grouping and resumes their runs, the resolution naming the mark. A
pause holding any other call is left to a person.
The personas name no approval of their own: they apply the catalog the run
prompt states (`tests/test_ctem_gate_catalog_916.py` pins it). A gate that
waits is a decision of the unified system (`dev-docs/human-gates.md`), never a
CTEM-specific card.

**Not set up stays Not set up (#11060).** A person's (`PUT /assignments/{id}`)
or an agent's (`update_assignment`) switch-off of an automation that has never
run (`execution_count` 0 and no `last_triggered_at`,
`assignment_pause.has_never_run`) is stamped `created_disabled`, not
`user_toggle` / `agent_tool`: a fleet still being built never reads Paused
because of a toggle. Once it has run, the toggle keeps its own stamp.

One compact control per fleet, on its header, plus bulk on the Fleets list.
The routes (`backend/app/api/v1/fleets.py`) and their service
(`backend/app/services/fleet_control.py`):

| Route | What it does | Who | AI off |
|---|---|---|---|
| `PATCH /fleets/{id}` `autonomy_level` | `full_gates` (default of every new and seeded fleet, #2279), `medium`, `low`, `autonomous`; audit `autonomy_level_change` | lower: whoever manages the fleet; raise: an administrator | allowed (no model call) |
| `GET /fleets/{id}/approval-gates` | The approval gate catalog the level acts on (`Fleet.approval_gates`, else the documentation's gates; #10000), the gates proposed from the members' configuration (`services/fleet_gate_proposals.py`: one gate per bound integration and impact class naming its write tools, read-only bindings and tool scoping honoured; impact table: read never gated, mark or draft `low`, send, reply, forward, delete, isolate, payment or signature `destructive`, every other write `high`) and the approvals that exist outside the level (agent and automation human in the loop, tool-call approval). Also `autonomy_level`, `effective_catalog` (every gate the level acts on with `source` stored, documentation or computed, `pin`, `tools`, `gated` and `by_level`: the cursor's before-click preview) and `computed_catalog_on` (#10153). No model call, no quota (#10014) | whoever sees the fleet | allowed |
| `PUT /fleets/{id}/autonomy` | One click of the cursor (#10153): `{level, pins?}` applied in one transaction, `pins` = `{gate key: "wait" \| "run"}` (per-action overrides, `Fleet.approval_pins`; a built-in fleet takes them too since #916, kept across seed upgrades: the seed never writes them). On a custom fleet it turns the computed catalog on. Audit `autonomy_level_change` with level and pins before and after | lower the level, pin "wait", drop a "run" pin: whoever manages the fleet; raise, pin "run", drop a "wait" pin: an administrator | allowed |
| `PUT /fleets/{id}/approval-gates` | Stores a custom fleet's catalog (accepted proposals or edited gates, validated as `DocGate`, unique keys). On a built-in fleet (#916) the body is the gates added to the seeded catalog, stored in `Fleet.customer_gates` (customer layer, never written by the seed, so kept across upgrades; read after the seeded gates, source `customer`, pinned like any gate); a seeded key is refused (422: pin it instead). Audit `approval_gates_change` | add: whoever manages the fleet; remove a gate, a tool of one or lower its impact: an administrator | allowed |
| `POST /fleets/{id}/enable` | Enable fleet (S4b): every automation that is off switches on (seeded, paused by a person, by Pause all), never one an emergency stop holds; the seeded fleet automations run as `run_as_user_id` (the caller by default, another user an administrator's choice). All or nothing: 400 while the run-as user misses a module an automation needs (`fleet_run_as.missing_providers`), 409 when nothing is off or during an emergency stop. `dry_run` answers the plan (each automation, its modules, what is missing, its `runs_per_day` from its schedule and `event_driven`) for the setup dialog, plus `estimate` (#1538): the picked automations' scheduled runs a day, how many run once per event, the mean cost of one of their runs over 30 days (`llm_traces` joined on `assignment_runs`, null without a run) and a cost a day, an order of magnitude the dialog re-sums over the person's pick. Audit `fleet_enable` | whoever manages the fleet; a built-in fleet an administrator | refused (`require_agentic_ai`): it starts new AI work |
| `POST /fleets/{id}/pause` | Pause all: every automation that is on switches off, stamped `fleet_pause`. Audit `fleet_pause` | as Enable | allowed |
| `POST /fleets/{id}/resume` | Resume: exactly the `fleet_pause` ones switch back on; 409 during an emergency stop. Audit `fleet_resume` | as Enable | allowed |
| `POST /fleets/{id}/unset` | Back to not set up (#11060): every automation that is on switches off, and every off one (any stamp but `panic_stop`) is re-stamped `created_disabled`, so the fleet reads Not set up, 0 on, no Resume. Nothing deleted (ids, prompts, triggers, run history kept); an automation an emergency stop holds is left as it is and named (`held_by_stop` in the audit). Idempotent. Agent tool: `set_fleet_automations(action="unset")`, a dry-run count without `confirm=true`. Audit `fleet_unset` plus one `pause` row per assignment | as Enable | allowed |
| `POST /fleets/{id}/reset` | Reset a built-in fleet to its factory state (#1508; `services/fleet_reset.py`), administrators only, `dry_run` for the dialog's counts, `confirm_name` (the fleet's name) for the real call. Every automation off and `created_disabled` (as unset) with its run-as and schedule anchor cleared; the level back to the seeded one; `approval_pins`, `customer_gates` and every fleet customer setting (tool-level enforcement, instructions position, confirmed prerequisites, health dismissals, check and enrolment, activity visibility, budget, iteration cap, run budget, member defaults, organization scope) back to their column default; a local documentation version replaced by the shipped one; each home member's customer layer cleared (bindings and their scoping, lending, MCP servers, knowledge bases, skills, custom tools, organization notes, model pin, approval switches), the seed's own built-in integrations, knowledge bases and skills kept, and its definition rewritten from the seed now. Kept: ids, run history, cases and findings, the audit trail, the workspace, and the home members' open work items (decision items included), counted in the answer's `open_gates` and `open_items`. Refused (422) on a custom fleet and (409) during an emergency stop. Idempotent; audit `fleet_reset` with `before` (the fleet as it stood: effective level, home members, connected agents, pins, added gates, open decision items and open items) and `changes` (the counts), plus `autonomy_level_change` (reason `fleet_reset`) when the level moves (#2134); the next boot changes nothing. | administrators | allowed |
| `POST /fleets/bulk` | pause, resume or set_autonomy on up to 100 fleets, or `all_fleets` (administrators, pause only); each fleet follows its single-fleet rule, refused fleets reported; `dry_run` counts | per fleet | allowed |

**What the level acts on (#10153).** A custom fleet whose `approval_pins` is
stored (every new custom fleet, every custom fleet the migration
`p10153fltpin1` found gating nothing, every fleet a person applied a level to
with `PUT /fleets/{id}/autonomy`; since #10168 the fleets the migration moved
are back on `full_gates` with no pins) acts on its effective catalog: its declared
gates (stored, else documented) plus the gates computed from its members'
configuration (`fleet_gate_proposals.fleet_effective_gates`, a declared gate
wins on the same key), each gate waiting when its impact reaches the level's
threshold (`full_gates`: low and up, `medium`: high and up, `low`:
destructive, `autonomous`: none) unless a pin says `wait` or `run`. The run
enforcement, the run prompt and the page read the same catalog. A new custom
fleet starts at `autonomous` (nothing waits until a person moves the cursor).
An existing fleet keeps its stored level and its run prompt on rollout:
`q10168fltinst1` (#10168) restores `full_gates` and NULL pins on the custom
fleets `p10153fltpin1` had moved (created before #10164 merged, no level
change in the audit log), so no member's run prompt changes. A custom fleet that declares gates and has no pins stored keeps acting on its
declared gates alone until a person applies a level. A built-in fleet acts on
its seeded catalog alone.

**Step badges follow the catalog (#1074).** A step of the structured
documentation may name the gate it stands for (`DocStep.gate_key`, a catalog
key such as `ctem.mobilize`). `GET /fleets/{id}/documentation` derives that
step's `gate` (the About page's "A person approves first" badge) from the
same effective catalog it returns as `autonomy_catalog`
(`fleet_autonomy.with_live_step_gates`), level and pins applied, so the About
page and the Approval tab never disagree at any level or pin. A step without a
key, or naming a gate the catalog does not hold, keeps its stored flag. The
built-in steps name their keys and their stored flags equal the seeded level
(Low: the OpenAEV validation and `ctem.mobilize` wait), checked for every built-in fleet by
`test_builtin_fleet_docs.py`.

**Set by instructions (#10168, B2-I).** A fleet whose approvals live in its
personas, guardrails and skills (no platform gate) has a second position on
the same scale, `Fleet.instructions_position`, between-step values allowed
(`full_gates`, `full_gates_medium`, `medium`, `medium_low`, `low`,
`low_autonomous`, `autonomous`; `fleet_autonomy.INSTRUCTIONS_POSITIONS`). It
is never enforced: no tool call pauses because of it. `PUT
/fleets/{id}/instructions-position {position, announce}` stores it (audit
`instructions_position_change`; toward more autonomy: an administrator;
lowering: whoever manages the fleet; a built-in fleet is refused) and, unless `announce` is false (an initial reading),
sets `instructions_position_announced`, which adds one line to the autonomy block
of the members' run prompt. A position not announced is display only: the
block stays byte-identical. `GET /fleets/{id}/approval-gates` returns both
fields.

On the Approval tab, "Tell the members" (a switch next to the position,
on by default) sends `announce`; off, the move is recorded for display only
and the toast says the members' instructions are unchanged (#10255). An
agent sets the same field through `update_fleet` (`instructions_position`,
`none` clears it, and `announce_instructions`, false by default): for an
administrator only, whatever the direction, audited
`instructions_position_change` with `via: agent`.

Opening a page never calls a write route: the setup dialog reads the
`dry_run` plan and the run estimate (`GET /fleets/{id}/run-estimate`) and
switches nothing on before Confirm.

### Seeded and default level: Full human gates (#2279)

Every fleet created or seeded without a level starts at `full_gates` (Sam,
25 Nov 2026; built-in fleets shipped at `low` from #10005 to #2279, custom
fleets without a declared catalog at `autonomous` from #10153 C2): every gate
waits for a person, as one decision item (a needs_human work item with its ask
card) the orchestrator files between steps. Full human gates never turns on
tool approval: `enforce_gates_at_tool_level` stays off (#1467) and is never
honoured on a built-in fleet (#1675), so no tool call pauses inside a run and
the fleet's tools stay authorized. `create_fleet`
(`backend/app/services/fleets.py`) writes `DEFAULT_AUTONOMY_LEVEL` when the
caller passes no level (REST, the `create_fleet` tool, a spec without
`autonomy_level`); a fork keeps its source's level. The seed
(`backend/app/agents/fleet_seed.py`, `_apply_seed_autonomy`) moves an
existing built-in fleet to `SEEDED_FLEET_AUTONOMY_LEVEL` at the next boot
unless a person ever chose its level (an `autonomy_level_change` audit row on
the fleet other than a factory reset's, `details.reason` `fleet_reset`): a
person-set level is never overwritten. The move is audited as
`autonomy_level_seed_default` under `system:fleet-seed`, never attributed to a
user. A factory reset puts the fleet back at `full_gates`. Fleets stay
disabled by default, so the level changes nothing until someone enables the
fleet. An administrator raises the level.

What waits and what runs alone at Low (a level an administrator picks), per
seeded fleet
(`backend/app/agents/builtin_fleet_docs.py`):

| Fleet | Still waits for a person | Runs alone, logged |
|---|---|---|
| Threat-informed exposure response | Launch an OpenAEV scenario (`destructive`) | Create a remediation ticket (`low`) |
| Vulnerability prioritization | Validate exploitability in OpenAEV (`destructive`) | Create a remediation ticket (`low`) |
| AI security | Run a test against an AI system (`destructive`) | none declared |
| Third-party risk | Any proposed action on a supplier (`destructive`) | none declared |
| Posture | none declared | none declared |

Reclassified with #10005: the three OpenAEV launches (`high` to
`destructive`) because an executed inject runs payloads on the
organization's endpoints or AI targets, which the fleet cannot tell from
production, and cannot be undone; the supplier action (`high` to
`destructive`) because contacting a third party or changing a contract
clause leaves the platform irreversibly. Agents lower a level freely and
raise it only for an administrator, in a chat, never in an assignment run,
and only once that administrator confirmed (#10439): `update_fleet` answers
"Not applied" with the change first, applies it on a second call with
`confirm=true`, and writes the same `autonomy_level_change` audit row as the
fleet page, its summary naming the raise by an agent on the administrator's
instruction (details `raised_by_agent_on_instruction_of`). The About status
chip shows the enforced level only when the catalog holds a gate; a fleet
that gates nothing reads its instructions position ("Between Medium and Low,
by instruction") or "No gated actions". The Fleets list card says the same
(#10457): `GET /fleets` carries `gated_action_count` (the length of the
effective catalog, computed for the whole list in two reads by
`fleet_gate_proposals.fleets_gated_action_counts`) and `instructions_position`,
and the card reads them through `autonomyStatusLabel`, the helper behind the
About chip. The agent reads it from `get_fleet` (and every fleet write answer,
`_describe`): an "Instructions position" line under the autonomy level, with
its label, stored value, whether it was announced to the members and "not
enforced by the platform" (`_instructions_text`; "not set" when unset), so
no audit log read is needed to learn it.

### Where the gates are enforced (#10140, opt-in since #1467)

By design XTM One enables no programmatic tool-level gate by default (#1467,
Sam, 23 Nov 2026): a fleet's gates are instructions. The run prompt states
the level and what waits (`fleet_autonomy_block`), the member asks once
through a decision item with its ask card, and once a person answered it the
run acts with no second pause. A built-in (seeded) fleet never pauses a tool
call, on any deployment (#1675, Sam, 24 Nov 2026): the seed forces
`Fleet.enforce_gates_at_tool_level` off at every boot, `PUT
/fleets/{id}/autonomy` refuses `enforce_at_tool_level: true` on it (422, fork
the fleet to enforce), `fleet_autonomy.fleet_enforces_at_tool_level` answers
false for it whatever is stored, and a built-in member of a built-in fleet
never gets an in-run approval card (`hitl_whitelist.seeded_fleet_member`, read
by `resolve_approve_tool_calls`). The platform pauses tool calls only for a
custom fleet an administrator opted in (`enforce_at_tool_level`, administrators
only, audited in `autonomy_level_change`). On an
opted-in fleet, a gate that names tools pauses those calls wherever a member of the fleet
acts, not only in its automations: `execute_assignment_run` wraps its gate
with `make_fleet_autonomy_gate`, and `run_agent_chat` (chat, channel threads
of agent accounts, consults, sub-agents, work-item turns) calls
`fleet_autonomy.install_fleet_autonomy_gate` for the agent's home fleet after
the agent's own gate. The fleet gate only adds pauses: the agent's own
approval settings keep deciding every other call, an evaluation that skips
approvals skips it, the platform approval switch turned off wins, and a
person's approval of the exact call lets it run. A pre-existing built-in
member folded into a seeded fleet (`is_pre_existing_fleet_member`) keeps its
behaviour of 1.261102.0. A gate without tools is stated to the agent in its
assignment prompt only.

## 12. Modify with AI: a chat bound to the fleet (#10162)

The fleet page docks a conversation next to the fleet (UX decision A on
item 5dcb88e3: the shared StudioRail on the right). `conversations.fleet_id`
(migration `f10162fltconv1`, no foreign key, no index, the twin of
`custom_dashboard_id`) binds it: `POST /conversations` with `fleet_id`,
`PUT /conversations/{id}/fleet` (owner only; one subject at a time, so it
unbinds a deck, a video or a dashboard, and they unbind it), and
`GET /conversations?fleet_id=` for the rail's history. A fleet the caller
does not see as themselves, or an archived one, answers 404.

Each turn `services/fleet_studio.load_fleet_studio_context` resolves the
binding against the caller's own visibility (no admin bypass) and puts a
`[Fleet]` block in the system prompt: the fleet id, workspace, member count,
autonomy level, orchestrator and roster (30 members named, the rest
counted), READ-ONLY when the caller may not manage it, the built-in rule
when it is seeded. The turn carries the `platform` group, which holds the
fleet functions. What each fleet tool checks, confirms and audits stays the
tool's own contract (#10165: autonomy lowered only, pause applied only with
`confirm=true`). Slices: S1 #10165 (tools), S2a this binding, S2b the header
button and the rail.

## 13. Single-product fleets (M1 OpenCTI #10542, M2 OpenAEV, M3 OpenCRQ)

Since CTEM-31 (section 14) M1, M2 and M3 are roles of the generic CTEM loop
fleet, no longer fleets of their own: their agents, automations, triggers and
ROI below are unchanged; their gates map onto the loop's catalog.

F1 to F5 each need two or three modules. A customer who buys one product
only gets one seeded fleet that works end to end with that product alone,
under the same rules: XTM One mode and dev only (`platform_targets`), every
automation `SEEDED_FLEET_OPT_IN` (off, admin opt-in, run as the enabling
administrator, no quota while off), seeded at the Low autonomy level, ROI
on catalog units only, the existing agents connected by slug and never
moved, renamed or changed.

### 13.1 M1: PIR intelligence desk (`opencti-pir-desk`, #10542)

Outcome: every PIR of the organization gets a ranked, current answer the CTI
team can act on. Module `app/agents/opencti_pir_desk.py`.

| Agent | Role | ROI unit |
|---|---|---|
| `opencti-pir-desk-orchestrator` (new) | Orchestrator, the loop | exempt (orchestrator) |
| `opencti-pir-triage-analyst` (new) | Scores an entity against each PIR | `threat_report_triaged`, per match |
| `opencti-landscape-reporter` (new) | Weekly PIR answer, half-year landscape | `briefing_written` |
| `opencti-threat-hunt-creator` (connected) | Hunt package for a top threat | exempt (utility) |
| `opencti-detection-rule-converter` (connected) | Detections of the hunt package | exempt (utility) |
| `indicator-decay-guardian`, `indicator-decay-judge` (connected) | Stale indicator re-check | `indicator_decay_reviewed` (judge) |
| `opencti-assistant`, `opencti-entity-insights`, `opencti-dashboard-creator` (connected) | Chat, context, dashboards | exempt |

Automations (all opt-in, off): `PIR match created` (provisioned OpenCTI
trigger: create of Report or Vulnerability with
`organization_context: ["pir_ids"]`, so only entities flagged in one of the
organization's PIRs reach it; threat activity, Intrusion-Set and Campaign, is
the CTEM loop's `Threat activity flagged in a PIR`, one owner per entity type,
and each STIX id is triaged once, #1832), `Weekly PIR answer` (Mondays 07:00),
`Half-year threat landscape` (1 January and 1 July), `Stale indicator
clean-up` (monthly). One gate: `indicator.revoke`, impact destructive, so it
waits at Low: the orchestrator files one approval work item per indicator
(`opencti:pir-desk:approval:<id>`) and never revokes.

PIR criteria: today's PIRs carry sectors and regions; the personas use any
other criteria type a PIR carries and never require one, so the fleet gets
richer when the Q1 2027 PIR types ship without a code change.

The 14 dashboard and decay automations are unchanged (golden test in
`tests/test_ctem_fleets_opt_in.py`): the fleet connects the decay agents and
calls them through a handover, it never touches their own assignments.
Tests: `tests/test_opencti_pir_desk_fleet.py`.

### 13.2 M2: Detection validation desk (`openaev-detection-desk`, #10575)

Outcome: measured prevention, detection and logging coverage; every gap
closed and proven closed by a re-test. For SOC and detection engineering
teams that run OpenAEV alone. Module `app/agents/openaev_detection_desk.py`.

| Agent | Role | ROI unit |
|---|---|---|
| `openaev-detection-desk-orchestrator` (new) | Orchestrator, the loop | exempt (orchestrator) |
| `openaev-coverage-gap-analyst` (new) | Sorts one simulation: blocked, detected, logged, missed; root cause | `simulation_result_analysed`, per simulation |
| `openaev-coverage-reporter` (new) | Weekly coverage report, gaps open and closed | `retest_cycle`, per gap proven closed |
| `remediation-rule-type-detector`, `remediation-splunk-rule-generator`, `remediation-crowdstrike-rule-generator`, `aev-detection-rules-generator` (connected) | The rule chain for a `no_rule` gap | `detection_rule_written` (generators) |
| `openaev-scenario-creator`, `openaev-scenario-publisher`, `openaev-payload-creator` (connected) | Build what the weekly plan lacks | their own |
| `openaev-assistant` (connected) | Chat | exempt |

Automations (all opt-in, off): `Simulation finished` (provisioned OpenAEV
trigger, event `simulation_finished`, `services/openaev_trigger_provisioning.py`),
`Weekly validation plan` (Mondays 06:00), `Weekly coverage report`
(Fridays 08:00), `Re-test due` (work item keyed
`openaev:detection-desk:retest:`). One gate: `openaev.simulation.launch`,
impact destructive, so it waits at Low in the tool path
(`manage_openaev_simulation` to RUNNING, `manage_openaev_atomic_testing`
launch); the orchestrator also proposes every launch as a `needs_human`
work item and never starts one itself.

Gap sorting: the strongest control met per inject and asset; the root cause
is `no_rule`, `no_log`, `broken_collector` (a collector with no result for
the simulation or for weeks), `matching_defect` or `unknown`. Only `no_rule`
gets a drafted rule; the others get a remediation naming the log source or
the collector. The findings data model and the logging validation are read
when the connected OpenAEV exposes them and never required. A gap closes on
a re-test result only, never on a remediation marked done.

Tests: `tests/test_openaev_detection_desk_fleet.py`.

### 13.3 M3: Risk budget desk (`opencrq-risk-budget-desk`, #10593)

Outcome: a risk register priced in money that stays current, and the
controls to fund ranked by the expected annual loss each removes per
dollar. For CISOs and risk managers who run OpenCRQ alone and defend the
budget to the CFO, the Board or an insurer. Module
`app/agents/opencrq_risk_budget_desk.py`. OpenCRQ's own threat catalogue is
the source; OpenCTI is never required.

| Agent | Role | ROI unit |
|---|---|---|
| `opencrq-risk-desk-orchestrator` (new) | Orchestrator, the loop | exempt (orchestrator) |
| `opencrq-register-analyst` (new) | What moved in the register and why | `risk_scenario_quantified`, per scenario re-priced |
| `opencrq-investment-analyst` (new) | What-if, one case per control, by loss removed per dollar | `investment_case_written`, per case (new unit) |
| `opencrq-board-reporter` (new) | Monthly Board pack | `briefing_written` |
| `ctem-risk-quantifier` (connected) | Drafts a scenario for a new exposure | its own |
| `opencrq-assistant` (connected) | Chat | exempt |

Automations (all opt-in, off): `Scenario re-priced`, `Finding re-scored`
and `Control validation ingested` (the OpenCRQ job-run events of
`services/opencrq_job_events.py`, polled every 60 s, no provisioning),
`Weekly investment review` (Mondays 07:30), `Monthly Board pack` (the 1st
at 10:00; staggered off the PIR answer and after the briefing, #1832). One gate: `opencrq.register.write` (create or update a scenario,
its loss estimates, the risk tolerance), impact high, so it runs logged at
Low and waits at the higher levels; the personas propose such a change as a
`needs_human` work item and never make it.

Known limits, stated in the fleet documentation: OpenCRQ is in beta until
its general availability (February 2027); XTM One reads budget reviews only
(`list_opencrq_budget_reviews`) and writes a treatment plan only through the
`opencrq.register.write` gate (`create_opencrq_treatment_plan` and the move
tools, CTEM-18, `ctem-cases.md` 9quater), so the fleet recommends (work items keyed
`opencrq:risk-budget-desk:recommendation:`) and a person approves the plan write. The `investment_case_written` minutes are a desk assumption
(`LABEL_ASSUMPTION`) until a sourced figure replaces them.

Tests: `tests/test_opencrq_risk_budget_desk_fleet.py`.

## 14. Four defined fleets: one generic CTEM loop and three use cases (CTEM-31, #1048)

Sam, 21 Nov 2026: at most four built-in fleets, one generic loop end to end and
three use cases, each one understandable out of the box. `BUILTIN_FLEETS`
(`backend/app/agents/builtin_fleets.py`) defines exactly:

| # | Slug | Name | What it carries |
|---|---|---|---|
| 1 | `ctem-loop` | CTEM loop | Every trigger path on a case: F1 core roles, the posture role (F4 orchestrator, the five stage analysts, the briefing), the Intelligence desk (M1), the Validation desk (M2), the Risk desk (M3) |
| 2 | `ctem-vuln-prioritization` | Vulnerability management | Path 1, a vulnerability on software we run, to a verified fix (F2) |
| 3 | `ctem-ai-security` | AI security | F3 |
| 4 | `ctem-third-party-risk` | Third-party risk | Paths 10 and 11 (F5) |

A fifth entry fails `tests/test_builtin_fleets.py`: adding one is a product
decision. An agent has one home fleet: the use-case fleets reach the loop's
role agents (risk quantifier, remediation coordinator) as connected agents.

### Seeded set: the CTEM loop only (#2105)

Sam, 25 Nov 2026: the three use cases are defined but not seeded until they
are tested. `SEEDED_BUILTIN_FLEET_SLUGS` is the switch (`{"ctem-loop"}`);
`SEEDED_BUILTIN_FLEETS` is what `fleet_seed.seed_builtin_fleets` seeds.

- Agents: `UNSEEDED_FLEET_AGENT_SLUGS`, derived from the definitions (the
  orchestrators and home members of the unseeded fleets that no seeded fleet
  names), are withheld by the agent seed with their assignments. Shared agents
  (risk quantifier, remediation coordinator, the product assistants) stay.
- Upgrade: the stale-fleet sweep removes the three company-managed fleets
  (members back to the playground), the stale-agent sweep their exclusive
  agents. A person's fork is kept; `RETIRED_FLEET_SUCCESSORS` names the CTEM
  loop as its successor. With the F2 declaration gone, `new_vulnerability`
  cases fall back to the loop (`paths` null).
- Derived maps (`BUILTIN_FLEET_LEADS`, `BUILTIN_FLEET_LEAD_SLUGS`) cover the
  seeded set only; the definitions, their documentation and tests stay.
- Re-enabling a use case is adding its slug to the switch: it seeds again
  exactly as its definition.

### Custom case ownership (#1966)

Any fleet can run CTEM cases: a customer forks a CTEM fleet, or builds one,
and gives it the case work. `fleets.case_ownership` declares the trigger
paths it takes (`paths` null = every path) and its case item key; the seed
writes it on the built-in fleets (`case_ownership` of `BUILTIN_FLEETS`).

- Fork: the copy carries the declaration (its own key
  `ctem:fleet-<fork id>`), its copied "Case opened or reopened" automations
  (off, like every copied automation) are pointed at the fork
  (`owner_fleet` eq `fleet:<fork id>`), and every copied agent gets the
  `ctem` tool group, so the forked orchestrator reads and manages cases
  with no manual change.
- Any other fleet: `PUT /fleets/{id}/case-ownership` {paths, key, clear},
  administrators only, refused on a built-in fleet (fork it). It points the
  home members' case automations at the fleet and runs the catch-up.
- Precedence, among the fleets whose case automation is on and whose
  declaration holds the path: a list of paths beats every path; then a fleet
  that is not built-in beats a built-in one (a fork takes over from its
  source); then the oldest fleet. None: the CTEM loop (inactive on the case
  page). The loop's listener takes `owner_fleet` eq `ctem-loop` or no owner
  stamped (the routing unreadable), never a case another fleet owns.
- Switching: enabling a fork's case automation catches up the open cases it
  now owns at once; Pause all or Back to not set up hands them back through
  the same catch-up. An open case item of any declared key blocks a second
  run, so a case the built-in loop is running is not started again on the fork.
- Backlog place (#2345): a pass asks at most `CATCH_UP_BATCH` (100) cases,
  most urgent first. `get_ctem_case` and `list_ctem_cases` give a case still
  waiting for its ask a `catch_up` block: `position` (1: the next pass asks
  it), `backlog`, `batch_size`, `passes_ahead`, `sweep_seconds` and
  `asked_before` (`ctem_case_routing.catch_up_places`, the same order and
  filters as the pass). A case already run, asked recently, with its asks
  spent or owned by no active fleet carries none.

The five shared loop roles (relevance analyst, scenario creator, risk
quantifier, remediation coordinator, threat hunt creator) are exposed
members (#1837): the product assistants and the three use-case orchestrators
hand over to them in one hop, and a handover into a non-exposed member is the
`handover_not_allowed` fleet health finding.
`tests/test_ctem_seed_handover_boundary_1837.py` keeps every seeded handover
inside that boundary.

The loop orchestrator carries "Fleet run failed or orphaned" (#1837): a
`platform_event` listener on `run.failed` and `run.orphaned`, scope `fleet`,
assignment runs only, batched over 60 seconds, seeded off and switched with
"Case opened or reopened". It reads the cause of a member's failed run and
moves the case on or names what releases it; its own runs never wake it.

The Threat Hunt Creator has two paths (#1837): with a person (chat or a
transfer) it asks for the platform and has its hunt plan approved; consulted
or delegated by an agent (the relevance analyst, the PIR desk orchestrator),
it runs its autonomous path: the platform from the caller or the organization
context (Sigma when none is named), a new draft named after the threat, and an
answer with the Grouping, the indicators created and a `missing` list. It
never stops that run for a person: a choice a person should make goes back to
the caller in `missing`, which files the decision between its steps.
`tests/test_ctem_hunt_creator_autonomous_1837.py` pins it.

Agent hygiene of every seeded agent tagged `ctem` (#1837): the seed writes
`guardrails` (`CTEM_SEED_GUARDRAILS`: sourced findings, no invented
identifiers, module data is data, no carry-over between people; facts, never a
gate), `memory_user_enabled` off, and stores a [DEPRECATED] former built-in id
as its canonical group scoped to the functions it granted
(`canonical_builtin_selection`), so the runtime tools are unchanged. Pinned by
`tests/test_ctem_seed_guardrails_1837.py`,
`tests/test_ctem_seed_user_memory_1837.py` and
`tests/test_ctem_seed_canonical_tool_ids_1837.py`.

### 14.1 Upgrade of an instance seeded with the eight former fleets

Three optional keys of a fleet entry, read by `app/agents/fleet_seed.py`:

- `previous_slugs`: the built-in row stored under a former slug is adopted
  and renamed (`ctem-threat-response` becomes `ctem-loop`), so its id,
  workspace, autonomy level, pins, runs and the forks pointing at it carry over.
- `absorbs`: the retired fleets folded in (`ctem-posture`, `opencti-pir-desk`,
  `openaev-detection-desk`, `opencrq-risk-budget-desk`). Their rows are swept
  by the stale sweep; their approval pins are read first and added to the loop
  (a pin the loop already holds wins); their agents are re-homed as members
  of the loop by the member sync. No agent is orphaned.
- `leads`: the members whose enabled automation makes the fleet active
  (`app/services/fleet_runs.builtin_fleet_active_clause`), besides its
  orchestrator. The former orchestrators of the folded fleets are leads, so
  an automation switched on before the upgrade keeps the loop active.

Assignments live on the agents, so every automation keeps its enablement,
run-as and schedule through the upgrade: nothing moves, nothing is disabled.
A fork of a retired fleet is never touched; its `GET /fleets/{id}/source-changes`
answer carries `successor` {fleet_id, slug, name}
(`app/services/fleet_fork.retired_source_successor`), also when the source
row was adopted under its new slug.

### 14.2 Gates

Out of the box one gate waits per case, `ctem.mobilize` (CTEM-34). The folded
desks' tool gates map onto the loop's catalog: the M2 simulation launches are
`ctem.validation.non_readonly` tools, the M3 register writes are
`ctem.remediation.decision` tools (impact high, logged), and the M1 indicator
revocation joins the action text of `ctem.mobilize`. All of it is configurable
by the autonomy cursor and pins.

### 14.3 Embedded module chats

The desks' agents keep `platform_targets` `["xtm_one", "dev"]` and their
`disable_chat`; no module vertical reads a fleet slug (checked on main at the
time of the change), so the embedded OpenCTI, OpenAEV and OpenCRQ chats are
unaffected. The posture lead keeps answering people in chat
(`chat_members` of the loop entry).

### 14.4 First-connection setup of the loop (CTEM-33, #1087)

The first-connection proposal of #9173 is versioned
(`organization_setup_prompt.SETUP_VERSION`, `ctem-loop-1`): shown to an
administrator of an XTM One platform at install and once after an upgrade that
carries a new version, never in Copilot mode. Its six steps (organization,
modules, ecosystem, gates, preview, enable) each save on their own through
`PUT /api/v1/organization-context/setup-prompt/steps`.

`GET /api/v1/fleets/setup/ctem-loop` (administrators, read only) answers each
step's state and the step to resume on (`app/services/fleet_setup_status.py`):
`done` or `skipped` as recorded for the current version, else `ready` when the
platform already holds it (a saved profile with primary assets and a tech stack;
one CTEM module registered; one filled ecosystem slot of the loop fleet; the
loop fleet seeded, for gates and preview; every automation on, for enable),
else `todo`. The Modules step names what the loop loses without each module.
Nothing is enabled before the enable step, which calls `POST /fleets/{id}/enable`.

Frontend: "Set up now" of `OrganizationSetupPrompt` opens
`components/fleets/CtemLoopSetupDialog.tsx` in place (no navigation). It opens on
`resume_on`, records each step with Continue (`done`) or Skip (`skipped`), and
reuses the existing pieces rather than new forms: the Organization step links to
Settings > Organization in setup mode, Ecosystem embeds
`FleetCapabilitySlotsCard`, Gates embeds `FleetAutonomyStrip`, and Enable opens
the fleet setup wizard on its "Run as and automations" step (dry-run plan, run
estimate, Confirm; see "One guided setup for every fleet" below). The step order, copy
and fact readers are pure functions in `components/fleets/ctem-loop-setup.ts`.

The end (slice 4): after the last step the dialog shows "The CTEM loop is set
up" with two actions. "Send a test signal" records one signal through the real
intake (`POST /api/v1/ctem-signals`, path `new_vulnerability`, provider
`manual`, evidence `{test: true, origin: "ctem-loop-setup"}`, a dedupe key per
send: `testSignal` in `ctem-loop-setup.ts`), so reconcile and the scope gate run
on it, then opens the Signals page on its loop trace (`/ctem/signals?signal=<id>`).
"Open the command center" goes there without a signal. The setup is always
reachable again through `CtemLoopSetupEntry` ("Set up the CTEM loop") in the
command center hero, and on the CTEM loop fleet page (slug `ctem-loop`) through
the header's "Set up" and the menu's "Run setup again" (`FleetSetupDialog`),
for an XTM One administrator only, never while impersonating
(`showsCtemLoopSetupEntry`).

On the command center the entry is a nudge (#1379, `dashboardSetupEntryShown`
in `ctem-loop-setup.ts`, `useDashboardSetupEntry`): it is drawn only while no
CTEM work runs, that is the loop fleet's enable step reads
`automations_enabled` 0 and every fleet the page shows (the CTEM fleets, or the
person's choice, custom fleets included) reads 0 automations on in its health;
it is not drawn while either is loading. Its close control writes
`entry:ctem-loop-setup` into `users.organization_context_notice_dismissed`, so it
never comes back on its own for that person. The fleet page entries and the
case card's "Set up the CTEM loop" choice (a case no automation works) are
contextual and always shown.

### One guided setup for every fleet

A fleet has one setup path: "Set up" on its card and on its page header while
no automation is on, "Run setup again" in the page's menu once one runs. Both
open `FleetSetupDialog`, which routes the CTEM loop fleet (XTM One
administrator) to `CtemLoopSetupDialog` and every other fleet to
`FleetSetupWizard`: What it does (purpose, triggers, monthly run estimate),
Integrations (the capability slots, skippable with "I will do it later",
dropped for a fleet without slots), Autonomy (`FleetAutonomyStrip` compact),
Run as and automations (the `dry_run` plan; a row that misses a module starts
unpicked with "Needs <module>" because the backend refuses a pick that misses
one), Review and enable (one `POST /fleets/{id}/enable`). Nothing is enabled
before Confirm. Progress is kept per fleet in the browser
(`fleet-setup-wizard.ts`, key `xtm.fleet-setup.<fleet id>`), cleared on
enable; no endpoint was added. The end screen names the automations left off
and the slots not bound, with a link to the Members view.

## 15. Module account preflight of per-user stage runs (#2042)

A per-user CTEM stage run (the five `ctem-*-analyst` stage agents and
`exposure-briefing-analyst`) acts as one member through that member's own
OpenCTI / OpenAEV identity. A member who never signed in to a module has no
account there, and every call answers 401 (OpenCTI) or 403 (OpenAEV): expected,
but the run used to spend several hundred thousand tokens before completing
"degraded".

`app.services.ctem_module_preflight` reads the member's identity once per
module before the run is charged and before any model call
(`assignment_engine._skip_by_module_preflight`, right after the activation
check): OpenCTI `query { me { id } }`, OpenAEV `GET /api/me`, with the same
per-user platform JWT the run's tools send (module auth is unchanged). Answers
are cached per member and integration (present 30 min, missing 10 min;
`forget(user_id)` drops them).

- `STAGE_NEEDS` maps each stage slug to its need: groups that must all be met,
  each met by any of its modules. Every stage today needs OpenCTI or OpenAEV
  (`ANY_CTEM_MODULE`), since each falls back to the module it has.
- A need not met ends the run `skipped`, `error = "no_module_account"`, zero
  tokens, its result naming the module(s) and telling the member to sign in to
  them once. The states read are kept on `trigger_context.module_accounts`.
- Anything that is not a clear no-account answer (timeout, 5xx, unreadable
  body, no credential) is `unknown` and the run goes ahead: the preflight never
  blocks a run on a guess.

### 15.1 Mid-run guard

A member who has an account on one module and is refused by the other during
the run (the preflight reads the identity, a tool still answers 401 / 403) is
guarded inside the run. `assignment_engine` binds
`guarding_module_accounts(True)` around the agent loop of a per-user stage run
(the agents `STAGE_NEEDS` names); every other run binds it off.

- The first tool error of a module that reads as a missing account (the
  401 / 403 wording of `integrations.http.format_http_error`, OpenCTI
  `AUTH_REQUIRED` / "You must be logged in") marks the module refused for the
  run (`note_tool_refusal`). A capability denial (OpenCTI `FORBIDDEN_ACCESS`)
  or a 5xx does not.
- Every later call to a tool of that module in the run is answered without
  being sent (`not_run = "module_refused"`, `refusal_answer`): no retry across
  the module's tools.
- That refusal is an expected outcome: `agent_loop._log_tool_result` records
  it as `expected_error`, disposed `platform_acceptable` / `accepted` with the
  reason `no_module_account: <module>`, never as an undisposed `tool_error`.

The stage card (#2042): `GET /ctem-metrics/stages/{stage}/assessment` reads the
user's newest run for the vertical; when it was skipped `no_module_account` and
no assessment can be shown, it answers `status: "no_module_account"` with
`no_module_account: [modules]` (the `missing` ones of
`trigger_context.module_accounts`) and never `stale`, so the page does not start
another run that would be skipped again. The card's AI strip draws
`StageSignInHint` (`frontend/src/pages/xtm-one/ctem/stage-sign-in-hint.tsx`):
"Sign in to <modules> once, then regenerate".
