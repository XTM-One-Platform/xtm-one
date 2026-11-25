# Platform Health: the Concierge reworked into a built-in health suite

Issue: #101. Status: S0 (this spec). Read on main `7674eb87`, 18 Nov 2026. S14 (Module
Analyst, section 12) added on main `e1b19ef`, 21 Nov 2026.

Platform Health replaces the Platform Concierge (`dev-docs/concierge-*.md`; its former
flag document is retired, section 2.4). It is built in, has no feature flag, and its checks never
consume agentic quota. It reports, inside XTM One, the issues, recommendations and gaps
of the agents and fleets an admin enrols, of XTM One itself, and of every registered
OpenCTI, OpenAEV and OpenCRQ platform.

Name (decided by Sam, 18 Nov 2026, section 11): **Platform Health** everywhere: page and
nav label, routes, settings section, i18n, docs and the seeded fleet. No configurable name.

## 1. The three scope rules

These rules win over anything below.

### 1.1 Scope by mode (follows `dev-docs/mode-split.md`)

| Mode | Agents and fleets | XTM One platform | OpenCTI, OpenAEV, OpenCRQ modules |
|---|---|---|---|
| Copilot (`copilot`, Copilot-like) | yes (enrolled only) | no | no: no card, check, setting, route or nav entry; module API answers 404 |
| XTM One (`xtm_one`) | yes (enrolled only) | yes | yes |
| `ai_disabled` | no (agents do not run) | yes, deterministic | yes, deterministic |

- Agent and fleet health is **generic AI**: one code path in both modes, never gated on
  `is_xtm_one`, `useIsXtmOne()` or the `xtmOne` nav key.
- Module and platform health is **domain content**: gated on the mode, and listed with its
  reason in the mode-split guard allowlists (`frontend/src/lib/mode-split-guard.test.ts`
  `MODE_READERS`, `backend/tests/test_mode_split_guard.py`, S5 #9766).
- The Health page itself is generic (it exists in both modes); only its module cards,
  module findings, module settings and module routes are mode-gated.

### 1.2 Agents and fleets are opt-in, never on by default

- The capability is built in and on; **no agent and no fleet is checked until it is
  enrolled**. Enrolment: per agent (agent page), per fleet (fleet page: covers its members,
  a member added later is covered while it stays in the fleet), or several at once by an
  admin from the Health page.
- Default after the release: **zero agents enrolled**. An instance with about 900 agents
  starts nothing on upgrade: no signal evaluated, no check run, no cost.
- Signals, scheduled checks and the analysis layer read enrolled agents and fleets only.
  This replaces the Concierge `scope_mode = "all"`.
- Migration: an agent or flow whose `concierge_improvement_opt_in` is true keeps an
  enrolment (an owner's explicit choice); nothing else is enrolled. The migration logs the
  count per table; the count on Filigran Copilot is read on the slice S1 PR from the live
  database by the deploying admin, not guessed here.
- Conversation-content reads (memory consolidation, quality diagnosis) stay a **second,
  separate opt-in** inside an enrolled agent, with the user opt-out kept
  (`users.concierge_opt_out` renamed, values carried).

### 1.3 Modules are automatic

In XTM One mode, XTM One platform health and OpenCTI, OpenAEV and OpenCRQ module health
are on by default for every registered platform from the release that ships them, with no
enrolment and no admin action. Settings let an admin tune thresholds or switch off
individual checks. "Automatic" covers the deterministic checks of section 4 and, from S14,
the agentic Module Analyst of section 12 (on by default under its own spend ceiling).

### 1.4 Acceptance pinned by tests

- (a) Upgrade test, 900 seeded agents: zero agent checks, zero signals evaluated on them,
  zero analysis cost until one is enrolled.
- (b) Copilot mode: no module surface (route, nav, settings, API: 404 or absent).
- (c) XTM One mode: a registered OpenCTI and OpenAEV show findings after the first pass,
  no admin action, AI switched off included.
- (d) A full day of health passes changes no agentic quota counter (per user and
  `_global`) and no agentic execution count.
- (e) A forced analysis over-spend is refused at the ceiling.
- (f) No `CONCIERGE` flag; no "Concierge" or "Alfred" string in UI, nav, docs or API, the
  documented aliases excepted.
- (g) to (k): the Module Analyst acceptance tests of section 12.9.

## 2. Inventory and disposition

Every Concierge surface, with its fate. Nothing is removed without a row here.
"Rename" means the new name in UI, API, docs and tests, with the old API path kept as an
alias for one minor release (section 2.3).

### 2.1 Backend services (`backend/app/services/concierge/`, 49 files, about 620 KB)

| Surface | Files | Disposition |
|---|---|---|
| Availability, flag gate | `availability.py` | Keep and rename (`services/health/availability.py`): `enabled` answers true; the per-agent gate becomes "enrolled" |
| Settings model | `settings.py` (`ConciergeSettings`: enabled, name, scope_mode, scope ids, budget caps, dry_run, schedules, signals, proposal/flow durations) | Rewrite as `HealthSettings` (section 7); name, cron strings, durations and dry_run leave the UI and become constants or env; scope becomes the enrolment tables |
| Naming ("Alfred") | `naming.py`, `frontend/src/lib/concierge.ts` | Remove: fixed names per mode, no setting |
| Consent rule | `consent.py`, `access.py`, `cycle_access.py` | Keep and rename: scope = enrolled agents and fleets; content = second opt-in minus opted-out users; access rule unchanged (admin all, manager what they manage) |
| Budget | `budget.py` (`concierge_cycles`, `concierge_budget_months`, `concierge_budget_work`) | Keep as the analysis ceiling ledger (section 6); no longer an "extra cap" on the agentic tiers: the analysis spend leaves the customer quota |
| Signals | `signals.py` (feedback drop, failure spike, cost drift, model change) | Keep as deterministic agent checks of the engine (section 4.1), reading enrolled targets only |
| Findings | `findings.py`, `concierge_findings` | Merge into the engine's finding store (`health_findings`) |
| Cycles and digest | `cycles.py`, `digest.py`, `concierge_cycle_runs`, `concierge_schedule_marks`, `concierge_signal_states` | Keep and rename: a cycle becomes a health pass; the digest becomes the Health page history and the optional daily email |
| Fleet seed (5 agents) | `fleet_seed.py`: `concierge`, `concierge-memory-steward`, `concierge-quality-analyst`, `concierge-flow-lab`, `concierge-experimenter` | Keep and rename the fleet "Platform Health", orchestrator "Platform Health"; the migration renames rows by slug and leaves a hand-edited persona as written (compare to the seeded hash). Done in S1 (part 3): the default name is "Platform Health" in every mode (`naming.DEFAULT_NAME`; the copilot "Alfred" default is gone), the former defaults stay in `DEFAULT_NAMES` so the next seed renames an orchestrator, fleet and workspace still carrying "Concierge" or "Alfred" and keeps a hand-set name; seeded assignment descriptions point at Settings > Automations > Platform Health. Assignment names and slugs (seed keys) change with the migration. Done in S1b (#437, Sam, 19 Nov 2026): the fleet, its five agents, their assignments and its workspace are built in and read-only for everyone in every mode (`read_only.py`); the seed rewrites their definition and the name "Platform Health" on every boot, a saved Settings name no longer renames them, and duplicate and fork are refused |
| Memory steward | `memory_steward*.py`, `memory_history.py` | Keep, content opt-in only (rule 1.2), runs only for enrolled agents with the second opt-in |
| Quality analyst, diagnosis | `diagnosis.py`, `quality_deps.py`, `text_kinds.py`, `turns.py` | Keep, content opt-in only |
| Evaluations, datasets | `evaluations.py`, `evaluation_tools.py`, `datasets.py` | Keep (generic AI, already shared with `EVALUATION`) |
| Experiments, variants | `experiments.py`, `variants.py` | Keep, admin on demand only, never scheduled on upgrade |
| Flow Lab (shadow, comparison, proposals, switch-over) | `flow_*.py` | Keep, enrolled flows only, admin on demand |
| Sandbox | `sandbox/*` | Keep unchanged (it isolates experiments) |
| Improvement proposals | `api/v1/improvement_proposals.py`, `improvement_proposals` | Keep; served while `EVALUATION` is on or Health is (always) |
| Companion and pets | `companion.py`, `pets.py`, `concierge_pets`, `scripts/generate_alfred_pet.py`, profile companion card | Keep as a profile option (Sam, 18 Nov 2026, section 11): renamed with the rest, tables and uploaded pets carried by the migration, nothing dropped |
| Identity | `identity.py` (`concierge_role`, principal) | Keep and rename (`health_role`), values carried |

### 2.2 API, frontend, docs

| Surface | Today | Disposition |
|---|---|---|
| `/api/v1/concierge/*` (opt-in, opt-out, name, settings, budget, cycles, signals, schedules, improvements, pets, companion, activity) | 3 routers in `main.py` | Rename to `/api/v1/health/*`; `name` and `activity` removed, `pets` and `companion` kept (profile option); old paths aliased (2.3). Done in S1: every route served under `/api/v1/health`, `/api/v1/concierge` kept as a deprecated alias (hidden from OpenAPI, `Deprecation` and `Link` headers, `tests/test_health_api_alias.py`); the frontend calls the new prefix |
| Nav entry | `nav-model.ts:367` `aiLeaf("/concierge", Compass, "Concierge")`, `named: "concierge"` | Leaf "Platform Health" at `/health`, no `named`; `/concierge` redirects. Done in S1: nav leaf, page title, Settings section `/settings/automations/health` (old `concierge` segment opens it), `/concierge[/tab]` redirects to `/health[/tab]` |
| Page | `pages/Concierge.tsx` (Improvements, Cycles, Signals) | `pages/Health.tsx` (section 8). Done in S1: the page file and its component are `pages/Health.tsx` / `Health`, lazy-loaded at `/health`; the content is reworked in S8 |
| Settings | `pages/settings/ConciergeSection.tsx`, `ConciergeTriggerBlocks.tsx`, `ConciergePetLibrary.tsx`, `concierge-block.tsx` | One compact `HealthSection` (section 7) |
| Agent page | `pages/agent-detail/OverviewTab.tsx` opt-in card and Improvements block | One compact row at the bottom of the overview, after the configuration: enrolment switch, content opt-in, last finding count, link to Health. Done in S10a: `components/health/HealthImprovementRow.tsx` replaces the opt-in card and the Improvements card (switch, count of proposals awaiting approval, Details dialog with the consent notice, Improve with, proposals and cycles); `HealthTargetCard.tsx` removed |
| Flow page | `pages/flow-builder/ConciergePanel.tsx`, toolbar button | Same compact row in the flow side panel |
| Profile | `ConciergeOptOutRow.tsx`, `ConciergeCompanionCard.tsx` | Opt-out kept and renamed; companion kept as a profile option and renamed |
| i18n | about 577 occurrences in 5 catalogs | Renamed in every language. Done in S1 (part 2): the 47 user-facing keys the frontend asks for (opt-in card, consent notice, profile opt-out, Settings section and trigger blocks, assignment pause reason) read "Platform Health" in en, fr, es and ja; the keys the backend also emits follow in part 4: built-in tool group titles and details ("Health Sandbox", "Health Evaluation", "Health Diagnosis", "Health Experiments", "Health Flow Lab", "Platform Health"), the audit entity labels "Platform Health cycle" and "Platform Health pet", the orchestrator tool results and descriptions, the budget refusal name and the switch-over pause reason |
| Docs | `docs/docs/administration/concierge.md`, 7 `dev-docs/concierge-*.md` | `administration/health.md`, `usage/health.md`; dev-docs folded into this file and `platform-health-*.md` slices. Done in S1: both pages renamed (`administration/concierge.md` to `administration/health.md`, `usage/improvements.md` to `usage/health.md`), rewritten to Platform Health and published in `mkdocs.yml` (Administration, and Use the platform > Monitor activity) |
| Flag | `CONCIERGE` in `backend/app/core/feature_flags.py` and `frontend/src/lib/utils.ts` | Removed per "Removing the flag"; `ENABLED_FEATURES=CONCIERGE` becomes a no-op logged once |

### 2.3 Aliases and migration

- Old API paths answer the new handlers for one minor release with a `Deprecation` header,
  then 410. The list lives in `dev-docs/reference/api-routes.md`.
- One Alembic migration renames columns (`agents.concierge_improvement_opt_in` to
  `health_enrolled` plus a new `health_content_opt_in` carrying the same value,
  `flows.*` likewise, `users.concierge_opt_out` to `health_content_opt_out`,
  `agents.concierge_role` to `health_role`) and tables (`concierge_*` to `health_*`), and
  carries `platform_settings.concierge_settings` into `health_settings` (thresholds kept,
  `scope_mode = all` dropped, never turned into enrolments).
- Seeded assignments: the five fleet assignments are named "Platform Health ..." (memory
  consolidation, quality review, flow review, investigation, weekly digest). Done in S1: the
  former "Concierge ..." name stays in `FleetAssignmentSpec.former_names`; the next seed
  renames the row still carrying it in place (schedule, enabled state and history kept, no
  second row) and `fleet_assignment` finds either name meanwhile, so no migration is needed
  for the names (`test_a_reseed_renames_an_assignment_still_under_its_former_name`).
- Audit entity types: new entries are written `health_cycle`, `health_pet` and
  `health_settings`. Historical rows keep `concierge_cycle`, `concierge_pet` and
  `concierge_settings` (the audit log is never rewritten); the frontend
  `AUDIT_ENTITY_LABELS` maps both forms to the same label.
- Audit actions: new entries are written `health.dataset_build`, `health.evaluation_launch`,
  `health.dry_run_write` and `health.sandbox_run` (`services/health/audit_actions.py`).
  Historical rows keep their `concierge.*` action; `LEGACY_AUDIT_ACTIONS` names each former
  action and `action_names()` gives a reader both forms (`test_health_audit_actions.py`).
- Tool group gate: the Platform Health groups are `gate="health"` (formerly
  `gate="concierge"`), in the backend `BuiltinToolGate` and the frontend `BuiltinToolGroup`
  type. The gate is computed from the registry on every read, never stored, so no migration.
- LLM tool names: the orchestrator reads are `list_health_signals`, `list_health_cycles`,
  `get_health_cycle` and `request_health_cycle`. The former `*_concierge_*` names stay in
  `RENAMED_BUILTIN_FUNCTIONS` (`builtin_tool_aliases.py`): each definition declares them as
  `ToolDefinition.aliases`, the executor runs them, and a stored `builtin_allowed_tools` or
  deny list naming one keeps its meaning (`test_health_orchestrator_renames.py`). The
  evaluation tools follow the same path: `run_health_evaluation`, `get_health_evaluation` and
  `compare_health_runs`, formerly `run_concierge_evaluation`, `get_concierge_evaluation` and
  `compare_concierge_runs`; the MCP read-only list names both forms of the comparison
  (`test_health_evaluation_renames.py`).
- Built-in tool group ids: `concierge_diagnosis`, `concierge_experiments`,
  `concierge_sandbox`, `concierge_orchestrator` and `concierge_evaluation` are now
  `health_diagnosis`, `health_experiments`, `health_sandbox`, `health_orchestrator` and
  `health_evaluation`. The former ids stay accepted as [DEPRECATED] aliases in
  `RENAMED_BUILTIN_GROUPS` (`builtin_tool_aliases.py`, a pure rename, unlike the merged
  ids of `BUILTIN_GROUP_ALIASES`): an API call, an import or a `builtin_allowed_tools` key
  naming one is stored under the current id (`canonical_builtin_selection`), never
  advertised. Migration `h119healthgrp` rewrites `agents` and `assignments`
  (`builtin_tool_ids`, `builtin_allowed_tools`); its downgrade writes the former ids back
  (`test_health_group_renames.py`). The flow-run source marker `concierge_sandbox`
  (`SANDBOX_RUN_SOURCE`, stored on `flow_runs.trigger_context`) is not a group id and
  keeps its value.
- Index and constraint names: the ten `health_*` tables declare `ix_health_*`, `uq_health_*`
  and `ck_health_*` (formerly `*_concierge_*`). Migration `h120healthidx` renames, from the
  catalog, every constraint (primary, foreign, unique, check) and index of those tables whose
  name holds the token `concierge_`, so a migrated database matches a fresh install; it is
  catalog-only (no index rebuilt), idempotent, and its downgrade writes the former names back
  (`test_health_index_names.py`).
- Seeded skill slugs: `concierge-quality-analyst`, `concierge-experimenter` and
  `concierge-flow-lab` are `health-quality-analyst`, `health-experimenter` and
  `health-flow-lab` (content files and the `health` tag follow). The former slugs stay in
  `RENAMED_BUILTIN_SKILL_SLUGS` (`builtin_agents.py`): the skill seed adopts a row still
  tagged `slug:<former>` under its current slug and retags it in place, so the row keeps its
  id and every agent's `skill_ids` keeps pointing at it; no Alembic migration is needed. A
  row already on the current slug wins and the former one is removed as stale
  (`test_health_skill_renames.py`).
- Fleet agent slugs: `concierge`, `concierge-memory-steward`, `concierge-quality-analyst`,
  `concierge-flow-lab` and `concierge-experimenter` are `platform-health`,
  `health-memory-steward`, `health-quality-analyst`, `health-flow-lab` and
  `health-experimenter` (persona files follow; the seed tag is `health`). Each
  `FleetAgentSpec` keeps its former slug in `former_slugs` (`RENAMED_FLEET_AGENT_SLUGS`): the
  next seed renames the company-managed row still on it in place, so its id, handovers
  (stored by agent id), fleet membership, assignments, enrolment and history stay with it;
  no Alembic migration is needed. A row already on the current slug wins and a former one
  beside it is left as it is (logged, never deleted); a personal agent on a former slug is
  never renamed. The organization proposal offer follows the orchestrator's slug
  (`test_health_agent_slug_renames.py`).
- Built-in pet: "Alfred" (slug `alfred`) is the Butler (slug `butler`, files under
  `health_pets/butler/`, `scripts/generate_butler_pet.py`). The former slug stays a read
  alias in `RENAMED_BUILTIN_PETS` (`pets.py`): a stored preference, a `PUT` or a spritesheet
  URL naming it answers the Butler and no upload may take it. Migration `h121healthpet`
  rewrites `users.concierge_companion` (`pet` only, other keys kept); its downgrade writes
  `alfred` back (`test_health_pets.py`, `test_health_companion_postgres.py`).
- Deployments: FiligranHQ/platform and the helm charts are searched for
  `ENABLED_FEATURES=CONCIERGE` at slice S1; any hit is removed in the same slice (Copilot
  values) with its helm-xtm-suite mirror when it exists. Searched 18 Nov 2026 (S1): no
  hit in FiligranHQ/platform nor in the helm charts, so no deployment change.

### 2.4 The former `CONCIERGE` flag

The flag is gone from `FeatureFlag` / `KNOWN_FEATURE_FLAGS` (backend and
`frontend/src/lib/utils.ts`). `ENABLED_FEATURES=CONCIERGE` left in an environment is an
unknown id: logged and ignored at startup like any unknown flag, never an error.
`services/health/availability.health_enabled` answers `True`; whether the fleet
works is the admin switch alone (`concierge_settings.enabled`, off by default,
`health_active`). Every gate the flag held is now open: the identity, consent reads
(enrolled targets only, section 1.2), the budget, `/api/v1/concierge/*`, the agent, flow
and profile cards, the Settings section, the fleet seeding, the scheduler, the
`gate="health"` tool groups and `/api/v1/improvement-proposals`. Kept as they were:
`platform_settings.concierge_settings`, the opt-ins and opt-outs, `agents.concierge_role`,
the budget ledger, cycle digests, signal states and schedule marks (renamed by the S1
migration, 2.3). `backend/tests/test_health_built_in.py` pins the built-in behaviour.

## 3. Engine

- **Deterministic first.** A check is a Python function over database rows, Redis
  counters, a registration snapshot or a probe. No model call. Runs in every edition and
  mode, `ai_disabled` included.
- **Scheduling.** A health pass every 5 minutes for module and platform checks (aligned on
  the registration heartbeat), every 15 minutes for agent and fleet checks on enrolled
  targets, daily for slow checks (version behind, certificate expiry). Built-in constants,
  env overrides for support, no cron editor.
- **Not metered.** A pass is not an assignment run, not an agent run, holds no
  `RunContext`, and calls nothing in `services/quota.py`. Test (d) reads every quota
  counter (per user and `_global`) and the agentic execution count before and after a
  simulated day of passes.
- **Finding.** `id` (stable: check id + subject), `check_id`, `module`
  (`agents`, `fleets`, `xtm_one`, `opencti`, `openaev`, `opencrq`), `subject` (ids),
  `category` (availability, performance, capacity, configuration, security, licence,
  coverage, cost), `severity` (critical, warning, info), `status` (open, resolved,
  dismissed; `pending` internally), `evidence` (values, threshold, read time, source),
  `recommendation`, `doc_link`, `first_seen`, `last_seen`, `resolved_at`, `opened_at`,
  `alerted_at`. Dedupe on `id`; opens when the check fails twice in a row
  (`OPEN_AFTER_FAILS` = 2, `fail_streak`; before, the row is `pending`: not scored, not
  listed, never alerted, deleted by a passing read); auto-resolve when the check passes
  twice in a row; dismiss with a reason and an expiry, reusing the
  `fleet_health_dismissals` pattern.
- **Restart grace.** A failing `availability` read during a restart is marked `in_grace`
  and never opens a finding (it still refreshes one already open): the XTM One checks for
  `STARTUP_GRACE` (10 minutes) after the process starts (`PROCESS_STARTED_AT`), a
  module's checks for `RESTART_GRACE` (10 minutes) after its registration reads as a
  restart (version changed, or a heartbeat more than 8 minutes after the previous one:
  `snapshots.registration_restarted`, stamped `platform_health_snapshots.restarted_at`).
- **Score.** Per module: `100 - 25 x critical - 8 x warning - 1 x info`, floored at 0,
  open findings only; overall = the lowest module score. The formula is shown on the page.
- **History.** Daily rollup of score and counts per module, kept 90 days (7/30/90 d views).
- **Existing surfaces read the engine.** Command center product pills, Dashboard notices
  (model configuration, knowledge base health, shared OAuth credentials) and
  `get_fleet_health` read the same findings; no second implementation.

**Implementation (S2).** `backend/app/services/health/engine.py` holds the check registry
(`register_check`), `record_results` (dedupe on `finding_key`, auto-resolve after
`RESOLVE_AFTER_PASSES` = 2, dismissal with expiry), the score (`score_from_counts`,
`module_scores`), the daily rollup (`rollup_day`, `health_score_days`, 90 days) and
`enrolled_agent_ids` (an agent enrolled one by one, or a home member of an enrolled fleet,
`fleets.health_enrolled`). Findings live in `health_check_findings`
(`models/health_check.py`, migration `h122healthengine`), apart from the analysis layer's
`health_findings`. The scheduler runs `health_pass_tick` at step 9c bis of the autonomous
tick, whatever the AI switch. Tests: `tests/test_health_engine.py` (ids, score, no
quota import) and `tests/test_health_engine_postgres.py` (dedupe, resolve, dismissal,
history, test (a) with 900 agents, test (d) with 288 passes).

## 4. Check catalogue

Default thresholds are starting points, editable where marked (E).

### 4.1 Agents and fleets (enrolled only, both modes, not in `ai_disabled`)

| Check | Source | Default |
|---|---|---|
| Assignment failing | `fleet_health_signals.assignment_is_failing` | last 3 runs failed |
| Schedule silent | `schedule_is_silent` | 14 days |
| Failure spike | `signals.read_failures` | 5 % over last 20 runs |
| Feedback drop | `signals.read_feedback` | -10 points over 3 days, 10 ratings min |
| Cost drift | `signals.read_costs`, `is_cost_spike` | +20 % over 7 days, 10 runs min; 3x day spike, 200k tokens min |
| Model change | `signals.read_models` | any change of the resolved model |
| Owner disabled | agents, users | owner inactive |
| Integration of the agent broken | connectivity probe result, OAuth refresh failures | last probe failed or token expires within 7 days |
| Channel bot broken | channel config, last delivery error | last delivery failed |
| Work items | `_work_item_findings` (orphaned claims, blocked without blocker, stale) | as today |
| Fleet findings | `build_fleet_health_report` | as today |

### 4.2 XTM One platform (XTM One mode and `ai_disabled`)

| Check | Source | Default |
|---|---|---|
| Worker and scheduler alive, tick lag | scheduler heartbeat key | lag > 2 min warning, > 10 min critical |
| Queue depth | Redis queues | > 1,000 warning |
| Postgres, Redis, trace store, object storage reachable | probes | any failure critical |
| Sandbox create latency | `xtmone_sandbox_create_seconds` | p95 > 30 s warning |
| LLM providers | provider probe, error rate and latency from traces | error rate > 5 % warning; default or embedding model unset critical |
| Quota near ceiling | `services/quota.py` read-only | > 90 % warning |
| Licence | licence state | invalid critical; expiry < 30 days warning |
| Version behind | latest release read daily | one minor behind info, two warning |
| Integrations | connectivity probes, OAuth expiry, refresh failures | failure warning |
| MCP servers | MCP health | unreachable warning |
| Knowledge bases | indexing jobs, embedding model change | stalled > 1 h warning; stale embeddings warning |
| Security audit | `api/v1/security_audit.py` findings | mapped severity |
| Registration token | `registration_token_state()` | published default critical |
| SMTP configured | `email.is_email_configured()` | unset info |
| XTM Hub registration | hub registration state | unset info |
| Certificates | TLS of the public URL | expiry < 21 days warning |

**Implementation (S4a).** `backend/app/services/health/platform_checks.py` registers the
configuration checks `xtm_one.registration_token`, `xtm_one.licence`,
`xtm_one.licence_expiry`, `xtm_one.smtp` and `xtm_one.hub_registration` (5 minutes),
gated on the mode (nothing recorded when `settings.is_copilot_like`, AI switch not read).
Tests: `tests/test_health_platform_checks_postgres.py`.

**Implementation (S4b).** The same module registers four per-subject checks:
`xtm_one.integration_broken` (company-managed integrations in `error` or `reauthorize`,
or a failed connection test), `xtm_one.mcp_server_broken` (enabled company-managed MCP
servers whose OAuth refresh failed), `xtm_one.kb_indexing_stalled` (documents in
`processing` untouched for over 1 hour, one finding per knowledge base) and
`xtm_one.kb_stale_embeddings` (a knowledge base whose embedding model is not the
platform's). Each closes out the open findings whose subject it no longer reads through
`engine.close_out` (moved out of `agent_checks.py`, which now calls it), so a fixed or
deleted subject resolves after two passes. Personal integrations and servers stay out:
their owner sees them on the agent checks.

**Implementation (S4c).** Three more configuration and capacity checks:
`xtm_one.default_model` and `xtm_one.embedding_model` (critical when unset: agents with
no model of their own cannot run, knowledge bases cannot index) and
`xtm_one.quota_near_ceiling` (warning, one finding per capped period, when a platform-wide
agentic ceiling is 90 % used). The quota read is `services/quota.platform_agentic_usage`,
the same counters the gates read, no counter moved (pinned by
`test_the_platform_usage_read_moves_no_counter`).

**Implementation (S4d).** Two availability and capacity checks:
`xtm_one.dependency_unreachable` (critical, one finding per dependency: the readiness
probe of `/health/ready` on Redis and the object storage, each bounded at 4 s; PostgreSQL
needs none, the pass runs on it) and `xtm_one.trace_backlog` (warning when the tracing
overflow list `tracing:overflow` holds over 1,000 traces; nothing recorded when Redis
cannot be read, that is the dependency finding).

**Implementation (S4e).** Two more availability checks: `xtm_one.worker_absent`
(critical when no worker presence in Redis says `running`, from
`services/worker_presence.count`; nothing recorded when Redis cannot be read) and
`xtm_one.provider_error_rate` (warning, one finding per model, when over 5 % of its traces
of the last hour failed, 20 traces at least; read from PostgreSQL `llm_traces`, which every
trace store writes; closed out when the model no longer fails). The scheduler tick runs the
pass itself, so a stopped scheduler records nothing new and its findings age on the page.
The rows still owed (sandbox latency, version, certificates) follow in S4f.

**Implementation (#768).** `xtm_one.maintenance_stalled` (warning when the hourly
maintenance of the scheduler tick, retention sweeps and purges included, has not completed
in two hours since it was first scheduled; read from the persisted wall-clock schedule
`services/wall_clock_schedule.py`, Redis keys `schedule:hourly_maintenance:*`; nothing
recorded before the first schedule or when Redis cannot be read). Evidence: `last_completed_at`
(left out when no pass completed) and `overdue_since` (last completed pass, or first schedule,
plus two intervals); no English word is ever sent as a value.

**Implementation (S4f).** Two daily checks (`DAILY_CHECK_INTERVAL`, 1 day), the only
platform checks that reach the network: `xtm_one.version_behind` (info when one newer
release than the running `x.y.z` core is published, warning from two; read from the public
release list of `XTM-One-Platform/xtm-one` on api.github.com, drafts and pre-releases left
out; a local `0.0.0` build or an unreadable list records nothing) and
`xtm_one.certificate_expiry` (warning, subject the host, when the TLS certificate of
`FRONTEND_URL` expires within 21 days or fails verification; verified with
`fetch_ssl_context`, administrator-trusted certificates included; an `http` URL or an
unreachable host records nothing for itself, and the finding of a former host, once
`FRONTEND_URL` moved, goes through `close_out` and resolves after two passes; an
unreachable host keeps its own open finding). The module is listed in `PLATFORM_INTERNAL` of
`tests/test_egress_funnel.py`. Sandbox create latency is still owed: the
`xtmone_sandbox_create_seconds` histogram is per process, so the check needs a shared store
first (a later slice).

### 4.3 OpenCTI (XTM One mode, automatic)

| Check | Source | Default |
|---|---|---|
| Registration heartbeat | `PlatformRegistration.is_healthy` | missed 15 min critical |
| Version and licence | registration | licence invalid critical; behind XTM One support matrix warning |
| Connector status (E) | snapshot: connectors (active, last heartbeat, works in error, queue) | stale after 30 min warning; works in error > 10 warning |
| Connector version (E) | snapshot | behind platform info |
| RabbitMQ (E) | snapshot of `rabbitMQMetrics` | queue depth > 100,000 warning; no consumer critical |
| Elasticsearch/OpenSearch (E) | snapshot of `elasticSearchMetrics` | yellow warning, red critical; disk > 85 % warning |
| Workers, ingestion lag (E) | snapshot | lag > 15 min warning |
| Feeds (TAXII, RSS, CSV) | snapshot | last run in error warning |
| Playbooks failing | snapshot | last run failed warning |
| Retention rules | snapshot | none on a large instance info |
| Coverage gaps | organization context tech stack vs connectors present | missing connector for a listed product info |

Built in S5 (`services/health/opencti_checks.py`, one result per registered OpenCTI,
subject `{registration_id}`, connectors `{registration_id, connector_id}`):
`opencti.heartbeat` (critical, `is_healthy`, 15 min), `opencti.connector_stale` (warning,
active connector silent 30 min), `opencti.connector_errors` (warning, > 10 works in error),
`opencti.queue_depth` (warning, > 100,000), `opencti.queue_consumers` (critical, messages
waiting with no consumer), `opencti.search_engine` (yellow warning, red critical),
`opencti.search_disk` (warning, > 85 %), `opencti.ingestion_lag` (warning, > 15 min). Every
check but the heartbeat reads the latest snapshot only while it is fresh (15 min), and a key
the snapshot does not carry records nothing; each check closes out the subjects it no longer
reads. The (E) thresholds are `HealthSettings.opencti` (S10b), read once per pass. Owed:
connector version, feeds, playbooks, retention and coverage (their snapshot keys come with
the OpenCTI push PR), and the version and licence row (read from the registration).

### 4.4 OpenAEV (XTM One mode, automatic)

| Check | Source | Default |
|---|---|---|
| Registration heartbeat, version, licence | registration | as OpenCTI |
| Platform healthchecks | snapshot of `io.openaev.healthcheck` (types SMTP, IMAP, AGENT_OR_EXECUTOR, SECURITY_SYSTEM_COLLECTOR, INJECT, TEAMS, NMAP, NUCLEI, INJECTOR_CONTRACT, ASSETS, ...; statuses ERROR, WARNING) | ERROR critical, WARNING warning |
| Injectors, collectors, executors (E) | snapshot | silent > 60 min warning; version behind info |
| Agents offline or outdated (E) | snapshot | offline > 24 h warning |
| Simulations failing, expectations stuck (E) | snapshot | stuck > 24 h warning |
| Security platform collectors silent (E) | snapshot | > 24 h warning |
| Coverage gaps | scenarios vs organization context | info |

Built in S6 (`services/health/openaev_checks.py`, one result per registered OpenAEV,
subject `{registration_id}`, healthchecks `{registration_id, type}`, integrations
`{registration_id, integration_id}`, the snapshot read by the OpenCTI helper `_registrations`):
`openaev.heartbeat` (critical, `is_healthy`, 15 min), `openaev.healthcheck` (one per type
OpenAEV reports, ERROR critical, WARNING warning), `openaev.integration_silent` (warning,
injector, collector or executor silent 60 min), `openaev.agents_offline` (warning, over half
of the agents offline). Snapshot keys added to format 1 (`snapshots.py`): `healthchecks`
(type, status ERROR or WARNING, count; 100 rows at most), `integrations` (id, name, kind
injector, collector or executor, updated_at; 500 at most), `agents` (total, offline). Owed:
version behind, simulations failing and expectations stuck, coverage (their snapshot keys come
with a later OpenAEV PR), and the version and licence row. Acceptance (c) of section 1.4 is
pinned by `tests/test_health_openaev_checks_postgres.py`.

### 4.5 OpenCRQ (XTM One mode, automatic)

Read live on OpenCRQ-Platform/opencrq main, 18 Nov 2026: OpenCRQ exposes process states
(`back/src/utils/health-check.ts`: FORKING ... RUNNING ... ERROR, sent to the parent
process only) and a job health (`module/src/modules/jobs/health.ts`: running, queued,
never_ran, healthy, failing). It does not register with XTM One today (no registration
client found by code search). Checks: registration heartbeat, version, licence once it
registers; process states and failing jobs once the snapshot carries them. Missing on the
OpenCRQ side: the registration itself and a snapshot of those two states.

Re-read 20 Nov 2026: OpenCRQ now registers (`back/src/features/xtm-one/xtm-one-registration.ts`,
every 5 minutes, `platform_identifier` `opencrq`), so its heartbeat is read like the others.
Built in S7 (`services/health/opencrq_checks.py`, one result per registered OpenCRQ, subject
`{registration_id}`, jobs `{registration_id, job}`): `opencrq.heartbeat` (critical,
`is_healthy`, 15 min) and `opencrq.job_failing` (warning, one per job kind whose last run
reached the dead-letter queue, the `failing` health of `deriveJobHealth`). Snapshot key added
to format 1 (`snapshots.py`): `jobs` (kind, health running, queued, never_ran, healthy or
failing; 200 at most). The process states stay owed: they reach the parent process only and
are not a registration-time read. The OpenCRQ push PR adds `health` to
`buildRegistrationBody`. Owed: version and licence row.

## 5. Data acquisition

| Option | For | Against |
|---|---|---|
| **Push**: each platform adds a `health` snapshot to the registration it already sends every 5 minutes (`POST /api/v1/platform/register`, OpenCTI `XTM_ONE_SCHEDULE_TIME`, OpenAEV `XtmOneClient`) | No credential stored in XTM One; works through internal URLs; zero extra cost; the heartbeat already proves liveness | Needs a PR in opencti, openaev and opencrq; a platform older than that release sends no snapshot (only heartbeat, version and licence checks then) |
| Pull: XTM One calls a dedicated health endpoint with a platform-scoped identity | Fresher on demand | A credential per platform, network reachability from XTM One, a new endpoint anyway |

**Decision: push** (confirmed by Sam, 18 Nov 2026, section 11), with automatic refresh:
the Platform Health page re-reads on its own interval, so fresh data shows without a
manual pull. An admin may still force a refresh: "Refresh" sets a flag the
next registration answer carries (`health_refresh_requested`), and the platform sends its
next snapshot at once. `RegisterRequest` gains an optional bounded `health` object
(schema versioned, size capped at 256 KB, unknown keys ignored); the latest snapshot is
stored per registration (`platform_health_snapshots`, one row, replaced). Module PRs on
OpenCTI, OpenAEV and OpenCRQ are opened on team approval (skill section 16).

Built in S5 (`services/health/snapshots.py`): format version 1 reads `connectors` (id,
name, active, updated_at, works_in_error, queue_messages; 500 at most), `rabbitmq`
(queue_messages, consumers), `search_engine` (status green/yellow/red, disk_used_percent)
and `ingestion_lag_seconds`; other keys are dropped, numbers clamped, strings cut. A
snapshot of another version, over 256 KB or that does not read is ignored and logged, never
a 422 (it rides on the heartbeat), and nothing is stored under the published default
registration token. The `health_refresh_requested` answer comes with the Health page (S8).

## 6. Analysis layer and cost control

- Where AI runs only (licensed, switched on, not `ai_disabled`). The deterministic engine
  never depends on it.
- Runs only when the finding set of a module changed (hash of open finding ids and
  severities), at most once a day per module, plus on demand by an admin.
- Platform routing (light) model, no conversation content in the prompt, prompt caching
  on the static part.
- What one finding sends (`analysis.build_prompt`, #1638): a deterministic finding its check
  id, severity, category, subject, `evidence.values` and `evidence.threshold`; an agentic
  finding of a module checker (`evidence.source` `module_checker`, section 13.6) its check
  name without the `agentic` segment, `checked_by: module_checker`, the tool, object ids and
  values of its first 5 evidence reads (values over 1,200 characters cut) and the checker's
  recommendation (400 characters). The static instruction says a check id is never a
  component name and that a component is named only when the values name it, so an id such
  as `opencti.agentic.managers_running` is never read as an "agentic manager".
- Hard ceiling, tokens and dollars per day and per month, enforced by reservation before
  the call (the `budget.py` ledger, renamed). Over the ceiling the call is refused and the
  page says so. Test (e) forces an over-spend.
- Traces carry their own source (`platform_health`), excluded from customer quota and
  from agentic execution counts, visible in observability; the spend is shown in
  Settings > Platform Health, next to the ceilings, never on the Health page (#961).
- Where the outcome shows (#961, 21 Nov 2026): on each module card of the Health page, one
  "AI summary" block (`components/health/ModuleAnalysisSummary.tsx`, the `StatCard` footer):
  the first line of `summary` (the headline, the first fix), clamped to two lines, with
  "As of <time>"; Show more opens the whole text and the order of fixes in the shared
  `InfoPopover`, never in place, so the cards of a grid row keep one height. Show more is
  offered when the summary holds more than its headline OR the clamped headline is cut
  (measured after layout and on resize, `scrollHeight > clientHeight`), so a one-line
  summary too long for two lines still opens in full. The footer
  follows the subtitle in normal flow, never pinned to the card bottom: the dividers of one
  grid row share one y and the spare height falls below the footer content. A module with no open finding, or the analysis off, shows
  no block and its card no footer (`analysisRowFor` decides it in the overview); a refused or failed analysis shows one muted line with its
  reason in plain words, never a success badge. Analyse now is the item of the card's
  actions menu, disabled with its reason while AI is off, the analysis is off or a ceiling
  is cleared. Administrators only (overview scope `all`). The former full-width Analysis
  panel (`HealthAnalysisPanel.tsx`) is removed. One AI block per card: the Module Analyst
  line of section 12.7 joins this block, never a second one.
- As built (S9a): `services/health/analysis.py`, tables `health_module_analyses` and
  `health_analysis_spend` (periods `D:YYYY-MM-DD`, `M:YYYY-MM`, row-locked month then day),
  `HealthSettings.analysis` (off, four ceilings, any cleared refuses `ceiling_not_set`), a
  call without a price row refused `no_price`, routes `GET /health/analysis` and
  `POST /health/analysis/{module}/run` (`require_ai_enabled`), tick after `health_pass_tick`.
- **Default ceiling, measured (S9b, 20 Nov 2026).** Lab `lab-live` (live model through the
  Copilot LLM proxy, `claude-opus-5-5`), one health pass then one analysis per module:
  only `xtm_one` had open findings (33); the other modules hold none and complete with no
  call. Billed: 3,963 input + 800 output = 4,763 tokens, 0.031852 USD (6.7 USD per
  million tokens blended). The prompt was 8,709 characters: 2.2 characters per token, so
  the reservation estimate moved from 3 to 2 characters per token (3 under-reserved by a
  third). Worst case per analysis at `MAX_FINDINGS` 60: about 7,200 input + 800 output, so
  8,000 tokens. Defaults: 6 modules x 2 analyses a day x 8,000 = 96,000, set to
  **100,000 tokens and 1 USD a day, 3,100,000 tokens and 31 USD a month**; the analysis
  stays off by default. The lab model quota counter moved only by the analysis calls (9
  before, 11 after two runs of the measurement); no agentic quota counter is involved.
- **Spend ceilings locked by default (#1202, decided by Sam, 22 Nov 2026).** This spend
  runs outside the agentic quota, so its ceilings are the only bound on it; an admin who
  could raise them would buy model usage outside the licence quota. Every spend ceiling
  (`SPEND_FIELDS` in `services/health/settings.py`: `budget.*`, the four of `analysis`,
  the five of `module_analyst`) resolves to the built-in defaults above
  (`apply_spend_lock`, inside `load_health_settings`, so every reader sees the value in
  force) unless `PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED` is true. While locked, a PUT of
  the settings that changes a ceiling from the value in force is refused (403,
  `SpendLimitsLockedError`, nothing written); one that sends the values in force saves its
  other fields and the row keeps the stored ceilings (`load_stored_health_settings`), so
  unlocking finds them again. Unlocked, today's behaviour holds, a cleared ceiling still
  refusing (`ceiling_not_set`). The switches (`analysis.enabled`,
  `module_analyst.enabled`) stay editable: turning them off only lowers the spend.

## 7. Settings

One compact "Platform Health" section (Settings > Agents and automations > Platform Health):

- Enrolled agents and fleets (list, bulk enrol, bulk remove).
- Per module (XTM One mode only): check toggles and the (E) thresholds of section 4.
- Analysis: on or off, daily and monthly ceiling, and under them today's and this month's
  analysis spend against each ceiling (`AnalysisSpendLines`, from `GET /health/analysis`;
  moved from the Health page by #961).
- Spend lock (#1202, section 6): `GET /health/settings/spend-limits` answers `locked`,
  `locked_reason` (`environment`), `env_var` and, per ceiling, `value` (in force),
  `default`, `stored` and `source` (`default` | `database`). While locked, the ceilings of
  the Budget, Analysis and Module Analyst blocks render read-only with a lock and one line
  naming the variable, in edit mode too; the spend lines and the switches are unchanged.
- Alert emails: off by default; recipients, minimum severity, immediate for a critical
  finding open for the alert window plus a daily digest, one email per finding and open
  episode. Stored as `HealthSettings.alert_emails` (S12, `services/health/alerts.py`):
  after each pass commits, `send_alerts` mails every critical finding open continuously
  for `alert_after_minutes` (default 15, 0 to 1440, the "Send after" field) and not
  alerted in this episode (`alerted_at` stamped; Redis `SET NX` on
  `health:alert:<finding id>:<opened_at>` against a second replica), then the digest
  once per UTC day (mark `alert:digest` in `health_schedule_marks`, unchanged). A
  finding that resolves within the window never mails, so a restart sends nothing
  (#958, with the open-after-two-reads rule and the restart grace of section 3); a
  reopened finding, or a dismissed one restored, is a new episode. Nothing is sent with
  the emails off, SMTP unset or no recipient (or immediate off), but the due findings are
  still stamped `alerted_at` (`stamp_due_alerts`), so turning the emails on later never
  mails a burst of the findings already open; a failed send never fails the pass.
- Layout (S13, `pages/settings/HealthSection.tsx` `GROUPS`): one section, a horizontal
  `SectionIndex` ("On this page") jumping to four groups, anchors `#health-settings-<key>`:
  status (enable and name, scope), spend (budget, analysis), checks (schedule, signals,
  module thresholds, durations, dry run), alerts (alert emails), then the companion pets.
  A block names itself; a group adds no second header. Pinned by `HealthSection.test.tsx`.
  The schedule, durations, dry-run and name blocks stay (the S0 plan to drop them was not
  applied: they are admin settings the fleet reads).

## 8. Health page

- Left menu "Platform Health" (`/health`), generic AI leaf, both modes.
- Overview: one card per module present (agents, fleets; plus XTM One, OpenCTI, OpenAEV,
  OpenCRQ in XTM One mode) with score, counts by severity and trend; for an administrator,
  a card with open findings carries the AI summary of section 6 and an actions menu
  (Analyse now). No spend and no analysis panel on the page (#961).
- Findings list filterable by module, severity, category, status.
- Finding detail: evidence, recommendation, doc link, history, dismiss with reason,
  "Ask AI" (where AI runs).
- History: 7, 30, 90 days.
- Access: an admin sees all; an agent manager sees the findings of what they manage
  (`cycle_access` rule).

### 8.1 Access matrix (S13, pinned by `backend/tests/test_health_role_mode_matrix_postgres.py`)

The same in Copilot, XTM One and `ai_disabled`; only the module cards of an administrator
follow the mode (section 1.1). A group manager does not manage its members' agents: it
reads Platform Health as a plain user unless it owns or is shared an agent.

| `GET /api/v1/health...` | Administrator | Group manager, agent owner, plain user |
|---|---|---|
| `/name`, `/me/opt-out`, `/pets` | 200 | 200 |
| `/overview`, `/findings`, `/findings?agentic=` | 200, every module of the mode | 200, `agents` and `fleets` of the agents they manage |
| `/history` | 200 | 200, empty |
| `/improvements`, `/cycles`, `/signals` without a target | 200 | 404 (one managed target only, `?agent_id=` or `?target_type=&target_id=`) |
| `/settings`, `/budget`, `/schedules`, `/analysis`, `/me/companion`, `/me/activity` | 200 | 403 |

`POST /analysis/{module}/run` is refused `ai_disabled` (403, `detail.code`) while AI is off.

Module Analyst (S14e, section 12): a module card's `last_investigation` is filled for an
administrator only, on OpenCTI, OpenAEV and OpenCRQ, so never in Copilot mode (no module
card there); every other reader gets `null` on every card. `POST /modules/{module}/investigate`
is an administrator's (403 for the three other roles in every mode); for an administrator it
answers 404 in Copilot mode (the module is not present), 403 `ai_disabled` while AI is off,
and in XTM One mode the analyst's own refusals of section 12.7 (409 `module_analyst_off`,
`no_registration`) or 202.

Module checks (S15f-2, section 13.8): `GET /modules/{module}/checks` is an administrator's
(403 for the three other roles in every mode); for an administrator it answers 404 in
Copilot mode (the module is not present) and 200 in XTM One mode and while AI is off (stored
reports, no model call).

### 8.2 Link guard (S13, pinned by `frontend/src/lib/health-link-guard.test.ts`)

Visible <=> openable on the frontend: `/health` and `/settings` are `AdminRoute`s, so every
Health surface that links there is drawn for administrators only, either by its own
`isAdmin` gate (companion, profile companion card, Dashboard critical findings notice) or
because only an administrator page renders it (Overview, Signals, analysis panel, the
Settings blocks). The surfaces open to everyone (agent improvement row, flow panel,
improvements inbox, cycles list, profile opt-out) link only to the target's own page
(`/agents/<id>`, `/flows/<id>/builder`, `/assignments/<id>`), which has no route guard and
checks access itself. The guard lists every Health file with its audience: a new file fails
until it is classified, a link to an `AdminRoute` from a surface open to everyone fails with
its file and line, and a `/health?tab=` link fails (the page reads its tab from the path).
The Module Analyst surfaces are classified there as administrators' (S14e):
`ModuleAnalystParts.tsx` through the Overview, `HealthModuleAnalystBlock.tsx` through the
Settings section. The module check report (S15e, S15f-2) is classified the same way:
`ModuleCheckReport.tsx` through the Overview; it links to no `AdminRoute` of its own.

## 9. Platform email (SMTP) integration

- Built-in company-managed integration "Platform email (SMTP)", present only when
  `email.is_email_configured()`; connectivity probe registered with
  `integrations/connectivity.register_connectivity`.
- Tool `send_email`: to, cc, bcc, reply_to, subject, markdown or HTML body in the Filigran
  layout (`services/email.py`), attachments by file id (20 MB total, the existing cap).
- Guards: recipient domain allowlist (empty = refuse all), per-agent daily send limit,
  tool approval on by default, audit log entry per send, From fixed to the platform sender.
- Health alert emails (section 7) use `deliver_email` directly, not the agent tool: the
  immediate email of a critical finding goes out once it has been open for
  `alert_after_minutes` (default 15), at most once per open episode; a restart of XTM One
  or of a module sends none.

## 10. Slice plan

One issue (#101), one PR per slice, `Refs #101 (S<k> of the plan)`; the last slice closes
it. Each slice carries tests, docs (`docs/docs/administration`, `docs/docs/usage`) and
i18n in every language. A slice touching `frontend/src` merges on the UX Director's
APPROVE on its head.

| Slice | Content | UI |
|---|---|---|
| S0 | This spec | no |
| S1 | Rename and de-flag: flag removed, routes, nav, i18n, docs, seeded fleet, migration (columns, tables, settings), aliases, companion kept as a profile option, deployment values checked | yes |
| S2 | Engine: finding store, passes, score, history, dismissals, not-metered test (d), enrolment model and the 900-agent upgrade test (a) | no |
| S3 | Agent and fleet checks (4.1) on enrolled targets, existing signals and fleet health moved onto the engine | no |
| S4 | XTM One platform checks (4.2), mode gate and guard allowlist entries, test (b) | no |
| S5 | Registration `health` snapshot in XTM One, OpenCTI checks (4.3); OpenCTI push PR | no |
| S6 | OpenAEV checks (4.4); OpenAEV push PR; test (c) | no |
| S7 | OpenCRQ: registration and snapshot PR on OpenCRQ, checks (4.5) | no |
| S8 | Health page, command center pills and Dashboard notices on the engine | yes |
| S9 | Analysis layer and ceiling, the lab measurement, test (e) | yes |
| S10 | Agent and flow page compaction, Health settings section | yes |
| S11 | Platform email (SMTP) integration and `send_email` | yes |
| S12 | Health alert emails | yes |
| S13 | Consistency and polish pass (Sam, 19 Nov 2026): role matrix (admin, group manager, agent owner, plain user) x mode (Copilot, XTM One, `ai_disabled`) for every Health surface written here and each cell pinned by a test (visible <=> openable <=> API 200); an automated guard walking every in-app link of the Health surfaces per role; one compact Settings section; display quality (empty states, score formula, trends, evidence, links, vocabulary, i18n, dark and light, a11y); UX Director full-journey review per role in the lab, APPROVE closes the program; matrix and before/after captures in the PR body | yes |
| S14 | Module Analyst (section 12, Sam, 21 Nov 2026): agentic read-only investigation of every registered OpenCTI, OpenAEV and OpenCRQ, recommendations on findings, outside agentic quota under its own ceiling; sub-slices S14a to S14e (section 12.10) | yes |
| S15 | Agentic module health checks (section 13, Sam, 22 Nov 2026): one health checker per module with its check playbook skill and module tooling, a daily check round independent of findings delegated by the orchestrator, a check report per module on the Health page, agentic findings per failed check; sub-slices S15a to S15f (section 13.10) | yes |

Companion (Sam, 19 Nov 2026, ahead of S8): the companion, its profile card and
`/health/me/{companion,activity}` exist only for who can open Platform Health. While
the scoped manager view of section 8 is not built that is an administrator: a plain
user gets no pet, no card and a 403 from the three routes. The bubble says the open
findings first (most severe kind and its count, or "No open finding."), then the
current activity; no "standing by" filler. When S8 builds the scoped view, a manager
of an enrolled agent or fleet gets the companion and its link opens that view.

## 11. Product decisions (decided by Sam, 18 Nov 2026)

- Name: **Platform Health** (not "Health", not "Health Assistant"), in routes, nav,
  settings, i18n, docs and the seeded fleet from S1.
- Companion and pets: **kept** as a profile option; not removed in the rename.
- Acquisition: **push** in the heartbeat, with automatic refresh of the UI (section 5).
- Analysis ceiling: **go**, set from the S9 measurement (section 6).

Decided by Sam, 21 Nov 2026:

- Module Analyst (section 12): **go**. In XTM One mode the fleet runs agentic checks on
  the registered modules, outside agentic quota, under its own daily and monthly ceiling
  in Spend limits. This reverses the "extra cap, never a bypass" rule of
  `services/health/budget.py` for this member only (section 12.5).

Decided by Sam, 22 Nov 2026:

- Agentic module health checks (section 13): **the check is the product**. "People don't
  need an analysis of the findings, it's useful but we need agentic health checks for the
  modules, so the fleet needs tooling to actually go into OpenCTI / OpenAEV / OpenCRQ and do
  checks beyond the findings and the health the modules are declaring." Section 13
  supersedes the finding-centric framing of section 12: a recommendation on a declared
  finding stays, as one output of a check round among others. What S14 shipped (the member,
  the triggers, the quota exemption, the ceiling ledger, the recommendation and agentic
  finding store) is kept and reused; nothing is reverted.
- Agentic module checks scope: **modules and their own data only**. "Don't put any third
  party or connector checks like drata or cve etc. It's focused on the modules. It's only
  health of the modules and the data itself, + integrations but 'generic' like logs etc.
  Remove everything related to third parties." Third-party and connector checks are out of
  scope of section 13 (13.4).

## 12. Module Analyst: agentic module checks (S14)

Decided by Sam, 21 Nov 2026 (section 11). The deterministic checks of sections 4.3 to 4.5 say
*what* is wrong from the pushed snapshot; the one-call analysis of section 6 orders the
fixes from the findings alone. Neither reads the module itself. The Module Analyst is a
sixth member of the Platform Health fleet that investigates each registered OpenCTI,
OpenAEV and OpenCRQ through its integration, read-only, and attaches recommendations to
the findings it explains. It adds to the engine, it never replaces a check: a module with
no open finding still gets its daily look, and the deterministic pass runs whatever the
analyst does.

### 12.1 Scope and switches

- **XTM One mode only** (section 1.1): domain content, gated on the mode, listed with its
  reason in the mode-split guard allowlists. In Copilot mode the member is seeded but its
  assignments never start, its routes answer 404 and no surface draws it.
- **Where AI runs only**: the member is a model run, so `ai_disabled`, an unlicensed
  edition or the AI switch off start nothing (`agentic_gate.agentic_verdict`, read where
  the cycle opens, as `budget.open_cycle` does). The deterministic checks keep running.
- **The Platform Health on/off switch** (`availability.health_active`) stops it like the
  rest of the fleet.
- **On by default** in XTM One mode under its default ceiling (section 1.3, modules are
  automatic); `HealthSettings.module_analyst.enabled` switches it off, and a module can be
  excluded one by one.
- **Built in and read-only** (`read_only.py`): seeded by `fleet_seed.seed_health_fleet`
  with `health_role` `module_analyst` (a new value of `identity.HealthRole`), rewritten on
  every boot, never edited by anyone.

### 12.2 Triggers

One cycle per registration (target type `module`, target id = the registration id; one
open cycle per target, the partial unique index of `concierge_cycles` extended to it):

- **Finding set change.** After each health pass, for every registration whose open
  finding set changed since the analyst's last cycle on it (new finding, resolved finding,
  severity change: the `analysis.finding_set_hash` of that registration's findings, kept
  per registration), a `signal` cycle starts. Debounce: at most one change-triggered
  cycle per registration per `change_min_interval` (default 2 hours), so a flapping
  connector does not start a cycle every 5 minutes; the next allowed cycle reads the set
  as it is then.
- **Daily.** One `schedule` cycle per registration per UTC day (schedule key
  `module_analyst` in `cycles.SCHEDULE_KEYS`, mark in `health_schedule_marks`), skipped
  for a registration a change-triggered cycle already covered that day.
- **On demand.** An administrator may start one from the module card ("Investigate
  now"), refused at the ceiling like the others.
- A registration that is not active (unregistered, heartbeat missed: `opencti.heartbeat`
  and its equivalents critical) gets no cycle: there is nothing to read; the finding
  already says so.

### 12.3 What it reads (read-only, by allow-list)

The member reads the module through the integration its registration created
(`PlatformRegistration.integration_id`), never a user's own binding and never a
credential stored for the purpose (company-managed agent, company-managed integration
only). Two locks, both enforced where the tools are built, never by a prompt sentence:

1. The binding is read-only (`services/integration_read_only.py`): every write tool is
   withheld from the model and refused by the executor.
2. An explicit allow-list of read tools per provider (`MODULE_CHECKER_TOOLS` since S15b-1, one tuple
   per provider in `services/health/module_analyst.py`), so a read tool added later to a
   module integration is not reachable until it is listed. A tool name outside the list
   is refused with the read-only reason.

What it looks at (whatever the module API exposes; each S14a tool names its query):

| Module | Reads |
|---|---|
| OpenCTI | connectors (state, last heartbeat, works in error, queue), workers and queues, feeds (TAXII, RSS, CSV) and their last run, playbooks and their last executions, background tasks, retention rules, platform version vs the connectors' versions |
| OpenAEV | injectors, collectors and executors (state, last seen, version), agents online, simulations and scenarios in error, health checks, background jobs, platform version drift |
| OpenCRQ | registration status, scheduled computations and their last result, data sources, version drift |

Plus the platform's own data, read through the existing health tools: the open findings
of that registration with their evidence, the latest snapshot, the previous analyst
recommendations (so a repeated cause is said once), the organization context tech stack
(coverage gaps of section 4.3).

No conversation content, no user data beyond the module's own configuration objects. A
read that fails (module down, timeout, a tool the module version lacks) is recorded in
the cycle digest and the cycle goes on with what it has; it never retries in a loop.

### 12.4 What it writes

Only inside XTM One, never to a module:

- **Recommendations on findings.** A new `health_finding_recommendations` row per
  (finding, cycle): the finding id, the text (bounded, 1,500 characters), the evidence it
  read (tool, object ids, values, read time), a confidence (`high`, `medium`, `low`), the
  cycle id. The deterministic `recommendation` field of the finding is kept as is; the
  agentic one is shown next to it, labelled as such. A finding resolved by the engine
  keeps its recommendations as history.
- **Findings it discovers.** A cause the checks do not cover (a playbook failing on a
  removed connector, a feed pointing at a dead URL) is recorded through the engine
  (`record_results`) under check id `<module>.agentic.<slug>`, `source` `module_analyst`,
  category from the section 3 list, tagged agentic on the page. It follows the engine's
  life (dedupe, dismiss with expiry); since no deterministic pass re-checks it, the
  analyst's next cycle on that registration resolves it when it no longer sees the cause,
  and it auto-expires after 7 days without a confirming cycle.
- **Proposals.** A change the analyst recommends inside XTM One (a threshold, a check to
  switch off) goes through the existing improvement proposals with administrator approval.
  A change inside the module is a recommendation text only: the analyst has no path to
  write it.

### 12.5 Quota: outside agentic quota, under its own ceiling

This is the one exception to "an extra cap, never a bypass" (`budget.py`), for this
member only, and the docstring of `budget.py` says so.

Why: the modules are automatic (section 1.3) and the analyst is part of the product's
own health, not work an admin asked an agent to do. Metering it on the action tiers would
make customer agents compete with the platform's own diagnosis, and a customer at its
tier limit would lose the investigation exactly when its modules degrade. The
section 6 analysis already runs outside quota on the same reasoning; the analyst extends
it to an agentic loop, with a ceiling of its own instead of the tiers.

How, so the exception cannot leak:

- **Exemption decided by the server only.** The quota check skips a run only when its
  assignment is the seeded module analyst assignment of the built-in fleet
  (`fleet_assignment("module_analyst")`, agent `health_role` `module_analyst`,
  company-managed), read from the database at run start. No trigger payload, setting or
  run field a person or a tool can write turns it on. Every other fleet member keeps
  counting in the tiers.
- **Own trace source** `platform_health` (`LLM_TRACE_SOURCES`), excluded from customer
  quota and from agentic execution counts, visible in observability and LLM analytics.
- **Own ceiling**, `HealthSettings.module_analyst`: tokens and dollars per day and per
  month, plus a per-cycle token cap, shown as one row "Module Analyst" in Settings >
  Platform Health > Spend limits, next to the analysis row. A cleared ceiling refuses
  (`ceiling_not_set`), as section 6.
- **Enforced by reservation before every model call** of the loop (the worst case of the
  next turn: its estimated input plus the output cap), on day and month rows of
  `health_module_analyst_spend` locked month then day, the `health_analysis_spend`
  pattern; real usage replaces the reservation after the call, a failed call books its
  reserved worst case. Over any ceiling the turn is refused, the run stops with what it
  has written, and the cycle digest and the page say so.
- It does not touch the fleet budget of `budget.py` (caps of the other members) either:
  one ceiling, one ledger, no double count.

### 12.6 Default ceiling

The default is set from a measured cost per module per cycle, as section 6 did.

**Measured (S14b-3, 21 Nov 2026).** Lab `lab-ootb-live` (live model through the LLM
proxy, one OpenCTI registered through the real path, one OpenAEV and one OpenCRQ seeded
registrations, no open module finding), three daily cycles, one per module, on main with
the per-call output cap of `module_analyst_ceiling.py` (`MAX_CALL_OUTPUT`, 8,192 tokens
reserved and sent to the provider, #935). Per run: 21,427 to 21,534 input and 1,038 to
1,161 output tokens, 22 to 23 s; the ledger day row reads 67,804 tokens and 0.32488 USD
for the three runs, so about **22,600 tokens and 0.108 USD per module per cycle with no
open finding**. The first pass of the measurement found that reserving the loop's whole
output allowance put the first call over the 80,000 per-cycle cap (every run stopped
`budget_exhausted` with 0 tokens); #935 fixed it before these figures were taken.

A cycle on a module with open findings reads more (the findings and the module state that
explains them) and is not measured in the lab, which had none to open; it is bounded by
the per-cycle cap and measured at the production retest of S14 (section 12.9 (g)).

Defaults, kept from the provisional estimate: per-cycle cap **80,000 tokens**,
**500,000 tokens and 4 USD a day, 10,000,000 tokens and 60 USD a month**. Why they stay:
the quiet case (3 modules, one daily cycle each) costs about 68,000 tokens and 0.33 USD a
day, 10 USD a month, well under them; the bad day (3 modules, 3 cycles each, cycles with
findings costing up to twice the quiet one, about 45,000 tokens) is 9 x 45,000 = 405,000
tokens and about 2 USD, under the daily ceiling with headroom for a cycle that reaches its
cap. Lowering the defaults to the quiet figure would stop the analyst on the day it is
needed. The production retest replaces the findings estimate; if a cycle with findings
lands over 55,000 tokens on average, the daily default moves with it.

These ceilings are locked at these defaults unless `PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED`
is true (#1202, section 6): a stored value is kept, not applied, and the reservation
(`module_analyst_budget.reserve`) reads the value in force.

Model: the platform routing model, as section 6, unless the measurement shows it cannot
hold the loop (then the platform default model, priced in the same ledger, and the
decision written here).

### 12.7 Where it shows

- **Health page.** On a finding: a "Module Analyst" block under the recommendation with
  the agentic recommendation, its confidence, the evidence it read and when; agentic
  findings carry an "Agentic" tag and a category filter value. On the module card: the
  last investigation (time, outcome) and "Investigate now" for an administrator.
- **Cycles.** Each cycle is a `HealthCycle` of the member (target type `module`, its
  registration's title), listed in the Cycles tab with its digest (what it read, what it
  attached, what it could not read, its spend).
- **Weekly digest.** A "Modules" part: cycles run, recommendations attached, agentic
  findings opened and resolved, spend against the ceiling.
- **Spend limits.** The "Module Analyst" row (section 12.5), with the day and month spend.
- Access follows section 8.1: module surfaces are for administrators (an agent manager
  manages no module); the access matrix and the link guard of section 8.2 gain the new
  routes and files (S13 tests extended).

### 12.8 Fleet and implementation map

- `identity.HealthRole` gains `module_analyst`; `fleet_seed.FLEET_ASSIGNMENTS` and
  `AssignmentKey` gain `module_analyst`; `cycles.TargetType` gains `module`;
  `digest.py` gains the member's sections.
- `services/health/module_analyst.py`: triggers, the tool allow-list, the quota exemption
  predicate (`is_module_analyst_run`), the ceiling ledger. The triggers (S14a-2b) are
  `module_analyst_tick`, run by `cycles.health_tick` after the signals: every 5 minutes
  (mark `module_analyst` in `health_schedule_marks`) it hashes each active module
  registration's open findings (`analysis.finding_set_hash`) and `due_trigger` decides
  `change` (the hash differs from the one last taken, outside the debounce) or `daily`
  (no cycle on it this UTC day); the hash taken, the last firing and its refusal are a
  `health_signal_states` row of kind `module_findings`, target type `module`. At most 6
  cycles a pass, changes first; a passing refusal (budget, licence, AI off) is retried
  on the next pass. Settings: `HealthSettings.module_analyst` (`enabled`, on by default;
  `change_min_interval_minutes`, 120, 15 to 1,440; `excluded_registration_ids`).
- Built-in tools of the health group for the member: `list_module_findings`,
  `record_finding_recommendation`, `record_agentic_finding`; the module reads are the
  registration integration's own read tools behind the allow-list. On OpenCTI the
  `manage_*` tools that reach connectors, feeds and playbooks classify as writes, so three
  read-only state tools serve the analyst (`providers/opencti/health_reads.py`, S14a-2b-3):
  `list_opencti_connector_states` (active flag, type, last update, queued messages),
  `list_opencti_feed_states` (RSS, CSV, JSON, TAXII: running flag, last execution) and
  `list_opencti_playbook_states` (running flag, queued messages). The first two ship in
  the `health_modules` group (`builtin_tools/health_modules.py`, S14a-2b): health only,
  agent keyed, wired in XTM One mode only; `list_module_findings` reads for the Module
  Analyst or an administrator, `record_finding_recommendation` writes for the Module
  Analyst alone, inside one of its cycles and on a finding of the registration that cycle
  investigates (one row per finding and cycle, a second call revises it).
- The module reads reach the run through `health_modules.add_module_read_tools`
  (S14a-2b-4), called by the assignment engine for every run carrying the group: the
  registration is read from the run's own cycle row (target type `module`, same agent),
  never from the payload; its integration is built with the read-only binding rule, then
  every tool outside `MODULE_CHECKER_TOOLS` is withheld and refused by name. Any other
  run, Copilot mode or an inactive registration gets no module tool. The reads act as the
  **"Run checks as" user** alone (`HealthSettings.module_run_as_user_id`,
  `app/services/health/run_as.py`, #1351): a cycle run has no triggering user, a
  platform-managed module integration needs one to sign its JWT, and the XTM One
  administrator generally has no access to OpenCTI or OpenAEV. There is no other
  fallback (no service account, no seed owner, no administrator by default). Not set, or
  the user cannot reach the registration's integration (granted and connected for them,
  the `fleet_run_as.missing_providers` rule of #9188), no read is wired and the run's
  checks carry "Checks need a run-as user with <Module> access"; `GET
  /health/modules/{module}/checks` answers `run_as: not_set | no_access` and the module
  card shows that one state with a link to the setting instead of a "not run" line per
  check. `PUT /health/settings` (a changed user) and `PUT /health/settings/run-as` (the
  CTEM setup's Modules step) refuse with a 400 naming the modules a user cannot read,
  nothing written: `detail` is `{code: run_as_missing_module_access, modules, message}`
  (or `{code: run_as_inactive, message}`), worded by the client from its catalogs and
  shown under the picker, never in a toast. The cards' "Set the run-as user" link lands
  on the block's own anchor `#health-settings-run-as`. A wiring that yields no tool for a usable user is logged as an error
  and named on the run ("Module tools not wired", a platform-side defect), never blamed
  on the allow-list.
- The orchestrator does not delegate to the analyst; it starts only from its own
  triggers. **Superseded by section 13.2 (S15):** the orchestrator delegates the module
  round to the module health checkers.

### 12.9 Acceptance pinned by tests

- (g) XTM One mode, Platform Health on, a registered OpenCTI with an open finding: after
  the pass a Module Analyst cycle starts with no admin action, its run calls the module's
  read tools, and at least one recommendation is attached to the finding and served by
  `GET /health/findings` (backend test with a stubbed model and integration; lab retest
  at deploy).
- (h) A full analyst cycle changes no agentic quota counter (per user and `_global`) and
  no agentic execution count; another fleet member's run still counts (test (d) extended).
- (i) A forced over-spend stops the run at the module analyst ceiling, day and month;
  a cleared ceiling refuses.
- (j) Copilot mode, Platform Health off, `ai_disabled`: no cycle starts, routes 404 or
  absent as section 8.1 says.
- (k) No write reaches a module: the member's tool set holds only allow-listed read
  tools, a write tool called by name is refused, and an allow-list entry the classifier
  reads as a write fails the test.

### 12.10 Sub-slices (one PR each, `Refs #101 (S14x of the plan)`)

| Slice | Content | UI |
|---|---|---|
| S14 spec | This section | no |
| S14a | Member, role, assignment, target type `module`, triggers and debounce, tool allow-list and read tools, recommendation and agentic finding store (migration), tests (g) backend, (j), (k); shipped in two PRs: S14a-1 the role, the target type, `module_analyst.py` (allow-list, refusal, `registration_in_scope`) and `health_finding_recommendations` (`tests/test_health_module_analyst.py`), S14a-2a the member's seed (`health-module-analyst`, no handover, assignment "Platform Health module investigation" that nothing starts yet), S14a-2b its triggers, tools and the OpenCTI read tools | no |
| S14b | Quota exemption and own ceiling ledger, settings model and defaults, the lab measurement and the default ceiling written in 12.6, tests (h), (i); shipped in two PRs: S14b-1 the server-only predicate `is_module_analyst_run` and the engine exemption (`exempt_module_analyst` on the run's quota trace metadata), `HealthModuleAnalystSettings` ceilings at the provisional defaults of 12.6, the `health_module_analyst_spend` ledger (`module_analyst_budget.py`, migration `h128healthmaspend01`), tests in `tests/test_health_module_analyst_budget*.py`; S14b-2 the reservation before every model call of the loop (`module_analyst_ceiling.py`: the run's provider wrapped by the engine when `is_module_analyst_run` holds; a refused call answers `budget_exhausted` with no tool call), the release from the fleet budget of `budget.py` (`open_cycle` gates the member's cycle on the switches and the licence only, `cycles._settle_cycle` marks its runs booked with nothing charged and never stops it at the fleet caps), tests in `tests/test_health_module_analyst_ceiling.py`; S14b-3 the lab measurement and the default ceiling of 12.6 | no |
| S14c | Health page: recommendations on findings, agentic tag and filter, module card last investigation and "Investigate now"; shipped in two PRs: S14c-1 the API (`FindingResponse.agentic` and `analyst_recommendation`, the newest `health_finding_recommendations` row; `GET /findings?agentic=`; `ModuleCard.last_investigation` for an administrator; `POST /modules/{module}/investigate`, administrators, `require_agentic_ai`, one on-demand cycle per active registration, `module_analyst_off` and `no_registration` refusals; `tests/test_health_module_analyst_page_api_postgres.py`), S14c-2 the page | S14c-1 no, S14c-2 yes |
| S14d | Cycles entry, weekly digest part, Spend limits row, docs (`docs/docs/administration`, `docs/docs/usage`) and i18n; shipped in two PRs: S14d-1 the backend (`digest.py` sections `recommendations` and `agentic_findings` of a module cycle, its `budget` read from its run's traces against `module_analyst.cycle_tokens` with `ledger: module_analyst`; `GET /analysis` gains `module_analyst`, the Spend limits row's ceilings and day and month spend; the weekly digest prompt gains the Modules part; `tests/test_health_module_analyst_digest_postgres.py`), S14d-2 the UI, docs and i18n (`pages/settings/HealthModuleAnalystBlock.tsx`, the Module Analyst block of Spend limits, XTM One modes only, with its five ceilings in the section's draft and its day and month spend from `GET /analysis`; `CycleDigestView` shows a module cycle's recommendations by confidence, its agentic findings and its spend against its own ceiling; `module_analyst` in `HEALTH_SPECIALIST_LABELS`; `docs/docs/administration/health.md`) | S14d-1 no, S14d-2 yes |
| S14e | Access matrix and link guard extended (S13 tests), UX Director journey review in the lab | yes |

## 13. Agentic module health checks (S15)

Decided by Sam, 22 Nov 2026 (section 11). Section 12 built a member that explains the
findings a module declares; with no finding it has nothing meaningful to do, and its ten
read tools cannot see what a Filigran engineer checks by hand. Section 13 makes the
**agentic check** the product: on every registered OpenCTI, OpenAEV and OpenCRQ the fleet
runs a named list of checks every day, findings or not, each with the evidence it read and
a pass, warn or fail verdict, and opens a finding for what the module itself does not
declare. The checks cover the health of the module, the health of its own data, and
integrations only generically (platform errors, failing internal jobs counted platform-wide,
the link between Filigran modules); no check reads a third party or a specific connector
(Sam, 22 Nov 2026, 13.4). The deterministic checks of sections 4.3 to 4.5 stay as they are; the
recommendation on a declared finding (section 12.4) stays as one output of a round.

Reused from S14 unchanged: the cycle model (target type `module`, one open cycle per
registration), the triggers and debounce of 12.2, the two read locks of 12.3, the
`health_finding_recommendations` store and `record_agentic_finding` of 12.4, the quota
exemption and the ceiling ledger of 12.5. Superseded: the finding-centric prompt of the
`module_analyst` assignment, the single member, "the orchestrator does not delegate"
(12.8), the 12.3 "what it looks at" table (replaced by the playbooks of 13.4).

### 13.1 One health checker per module

Three members replace the Module Analyst, one per module, so each carries one skill and
one tool set and the graph shows which module it works on:

| Slug | Name | Role | Skill | Reads |
|---|---|---|---|---|
| `health-opencti-checker` | OpenCTI Health Checker | `opencti_checker` | `health-module-checks` (section OpenCTI) | the OpenCTI registration's integration |
| `health-openaev-checker` | OpenAEV Health Checker | `openaev_checker` | `health-module-checks` (section OpenAEV) | the OpenAEV registration's integration |
| `health-opencrq-checker` | OpenCRQ Health Checker | `opencrq_checker` | `health-module-checks` (section OpenCRQ) | the OpenCRQ registration's integration |

Why three and not one member with three skills: a member's tool set is fixed at run start
and the allow-list is per provider (12.3); a single member would hold every provider's
tools and every playbook in one prompt, cost more per round, and draw as one node with no
visible module. Three members cost nothing when a module is not registered (no cycle).

Migration: the seeded `health-module-analyst` agent row is renamed in place to
`health-opencti-checker` (`former_slugs`, as S1 did for the Concierge), so its cycles,
recommendations, spend rows and agentic findings keep their history; the two other members
are seeded new. `identity.HealthRole` keeps `module_analyst` as an alias read on old rows.
`is_module_analyst_run` (12.5) becomes `is_module_checker_run`: true for a run of the seeded
assignment of any of the three members, decided from the database at run start, as before.

### 13.2 The orchestrator delegates the module round

- **Daily module round** (S15a-2). Once per UTC day, the trigger pass of
  `module_analyst_tick` hands every active module registration that no cycle covered that
  day to its module's checker (`CHECKS_KEY_BY_PROVIDER`), one cycle per registration,
  trigger `schedule`, run payload `module_trigger: "round"`. It replaced the S14 `daily`
  branch (`due_trigger` answers `change` or nothing; `round_due` decides the round). It runs
  with zero open findings: the round is the check list, not the findings. The round is
  started by the platform, not by a model run of the orchestrator: no registration is
  skipped by a model, the orchestrator spends nothing on it, and the round shares the pass
  cap (`MAX_STARTS_PER_PASS`, changes first) and the ceiling of 13.7. The orchestrator's
  `delegate` handovers to the checkers (below) carry the fleet's graph and an
  administrator's request in chat.
- **Finding-set change** (12.2) and **on demand** ("Run checks now" on the module card,
  administrators) start the module's checker directly, with the same debounce and ceiling.
- The orchestrator's handovers gain the three checkers; the fleet graph, Members and Data
  flow draw them as delegates of the orchestrator (13.8). Delegation is a handover only: the
  checker's run is a seeded assignment of the checker, so the quota exemption of 12.5 holds
  and the orchestrator's own run stays metered as today.

### 13.3 Check playbooks as built-in skills

The three checkers share one skill since #1943, "Platform Health: module checks"
(`health-module-checks`, content in `agents/seed_data/skill_content/health-module-checks.md`,
bound at seed to each checker): shared rules and report, then one `## OpenCTI`,
`## OpenAEV` and `## OpenCRQ` section. The row of the former `health-opencti-checks` is
adopted in place (`RENAMED_BUILTIN_SKILL_SLUGS`), the OpenAEV and OpenCRQ rows are deleted
as stale. A checker runs only the section of the module its trigger names. The check logic
hash of #1742 (`check_reports._skill_hash`) is the sha256 of `module_playbook(module)`, the
shared part plus that module's section, so editing one module's section leaves the other
modules' findings current. Each section is a playbook: a list of named checks, each with

- **id** (`<module>.agentic.<check>`, stable: it is the finding's check id),
- **read**: the tools and arguments, and the values taken from the result,
- **rule**: pass, warn, fail thresholds, written as numbers (defaults below, editable later
  as section 7 thresholds; S15 ships them as constants of the playbook),
- **recommendation**: what the administrator does on warn or fail, in the module's terms.

A check whose tool is not available on the module's version is reported `not_run` with the
reason, never guessed. The checker runs every check of its playbook in one cycle, in order,
and records the report (13.6) at the end, a partial report when the ceiling stops it.

### 13.4 Check lists (first version)

Scope (Sam, 22 Nov 2026, section 11): the checks read **the module and its own data**, and
integrations only in a generic way. No check reads, names or counts a specific connector,
feed, collector, injector, executor or third-party data source; there is no per-connector
split anywhere. Each list has three parts: module-native health, data health of the
module's own objects, and generic integration health.

**OpenCTI** (`health-module-checks`, section OpenCTI):

| Check | Reads | Fail / warn (defaults) |
|---|---|---|
| `platform_version_licence` | platform version, licence expiry, settings sanity (base URL set) | licence under 30 days warn, 7 days fail; version behind the range XTM One supports fail; a missing setting warn |
| `managers_running` | `get_opencti_managers`: managers classed stream (continuous `running`) or cron (`running` only while a cycle executes; never judged on it), stream managers re-read 3 times 60 s apart (#1640); TASK_MANAGER by its effect in the background tasks | an enabled stream manager not running on all 3 reads fail; an unfinished background task with no execution for over 1 h warn, 6 h fail; absence of new objects is never evidence of a stopped manager |
| `background_tasks` | background tasks: state, progress, errors | in progress over 24 h warn; any in error in 24 h warn, over 10 fail |
| `workers_and_queues` | platform workers and the platform queues as a whole (total messages now and 1 h ago) | total over 100,000 and growing warn, over 1,000,000 fail; no worker consuming fail |
| `streams` | live stream collections and their consumers: lag | lag over 1 h warn, 6 h fail |
| `playbooks_executing` | native playbooks: running flag, last executions | last 10 executions over 50% failed fail; running with no execution for 24 h and messages queued warn |
| `stale_drafts` | drafts not touched for 30 days | over 20 warn |
| `data_volume_growth` | objects created in the last 24 h, platform-wide, vs the 7-day daily mean | under 25% of the mean warn, 0 with a non-zero mean fail |
| `data_freshness` | the newest object creation and update times, platform-wide | no object created or updated for 24 h on a platform with a non-zero 7-day mean fail |
| `data_completeness` | the module's own objects without author or without marking, relationships whose source or target no longer exists, where the API exposes them | over 5% of the sampled objects warn, over 20% fail; not exposed: `not_run` |
| `platform_errors` | platform error log entries and error rate of the last 24 h (activity and audit errors) vs the 7-day mean | rate over 2 x the mean warn, over 5 x fail |
| `works_in_error` | works in error in the last 24 h, counted platform-wide, never per source | over 10% of the works warn, over 30% fail |

**OpenAEV** (`health-module-checks`, section OpenAEV):

| Check | Reads | Fail / warn (defaults) |
|---|---|---|
| `platform_version_licence` | platform version, licence, settings sanity | as OpenCTI |
| `simulations_state` | simulations failed, or running past their end | any failed in 24 h warn, running 24 h past end fail |
| `scenarios_scheduled` | scenarios with a schedule and their last start | a scheduled start missed by over 1 h fail |
| `expectations_due` | expectations past their due time without result | over 10% warn, over 30% fail |
| `findings_flowing` | findings created in the last 7 days vs the previous 7, platform-wide | 0 after a non-zero week warn |
| `data_volume_growth` | simulations, scenarios, assets and findings created in the last 7 days vs the 4-week weekly mean | under 25% of the mean warn |
| `data_completeness` | scenarios with no inject, simulations with no target, assets in no asset group, injects with no expectation, where the API exposes them | over 10% warn, over 30% fail; not exposed: `not_run` |
| `data_consistency` | assets duplicated by hostname or IP, as the module exposes them | any duplicate warn |
| `platform_errors` | platform error log entries and failed internal jobs in the last 24 h, platform-wide | rate over 2 x the 7-day mean warn, over 5 x fail |
| `module_link` | the configured link to the Filigran module it reads (OpenCTI): configured and last successful call | configured and failing fail |

**OpenCRQ** (`health-module-checks`, section OpenCRQ):

| Check | Reads | Fail / warn (defaults) |
|---|---|---|
| `platform_version` | platform version vs the version XTM One supports | behind the supported range fail |
| `jobs_state` | jobs failed or stuck, platform-wide | any failed in 24 h warn, running over 6 h fail |
| `crq_run_fresh` | CRQ run history: last successful run and its age | over 7 days warn, 30 days fail, none fail |
| `scenario_inputs` | risk scenarios missing a TEF input or a financial profile | any warn, over 25% fail |
| `data_completeness` | risk scenarios never scored, or scored before their inputs last changed | any warn, over 25% fail |
| `data_volume_growth` | risk scenarios and CRQ runs created in the last 30 days vs the previous 30 | 0 after a non-zero month warn |
| `platform_errors` | failed internal jobs and platform error entries in the last 24 h, platform-wide | rate over 2 x the 7-day mean warn, over 5 x fail |
| `module_link` | the configured links to the Filigran modules it reads (OpenCTI, OpenAEV): configured and last successful call | configured and failing fail |

`module_link` is our reading of "integrations, generic": a link between two Filigran modules
is not a third party. It reads the configured flag and the last call result only, never the
linked module's data. Dropped at the first word otherwise.

Out of scope by decision (Sam, 22 Nov 2026), and not to be added back by a playbook edit:
connector, feed, collector, injector and executor liveness, queues, works, syncs or versions
read per component, ingestion per connector, and the freshness of third-party data sources
(compliance tools, vulnerability feeds and the like). The deterministic checks of sections
4.3 to 4.5 are not changed by this section.

The lists grow by playbook edits within this scope; a check added needs its tool on the
allow-list first.

### 13.5 Tools

The two locks of 12.3 stay: the registration's integration under the read-only binding, and
`MODULE_CHECKER_TOOLS` (the S14 `MODULE_ANALYST_TOOLS`, renamed in S15b-1, one tuple per provider). Each tool
added is a read tool the classifier reads as a read (test (k) of 12.9). No tool on the
list reads a specific connector, feed, collector, injector, executor or third-party data
source (13.4 scope).

- **Removed from the S14 allow-list:** `list_opencti_connector_states`,
  `list_opencti_feed_states`, `query_openaev_connectors`, `list_opencrq_connectors`,
  `list_opencrq_connector_syncs`. Kept: `get_opencti_platform_version`,
  `list_opencti_playbook_states`, `get_opencrq_platform_version`, `list_opencrq_jobs`,
  `get_opencrq_job`. The tools themselves stay in their providers for the other agents.
- **Existing read tools added to the allow-list:** OpenCTI `get_opencti_entity_stats`,
  `query_opencti_activity`, `list_opencti_drafts`; OpenAEV `search_openaev_exercises`,
  `get_openaev_exercise`, `search_openaev_scenarios`, `search_openaev_findings`,
  `search_openaev_assets`, `search_openaev_asset_groups`; OpenCRQ
  `list_opencrq_crq_run_history`, `get_opencrq_crq_run`, `get_opencrq_crq_settings`,
  `list_opencrq_risk_scenarios`, `get_opencrq_tef_input`, `get_opencrq_financial_profile`.
- **New read-only health tools** (`providers/<module>/health_reads.py`, the S14a-2b-3
  pattern), where the existing tools cannot read the data or classify as writes. Every
  count is platform-wide; none takes or returns a connector id:
  - OpenCTI: `list_opencti_background_tasks` (state, progress, errors),
    `get_opencti_managers` (platform managers and their running flag),
    `get_opencti_platform_queues` (workers and total queued messages, platform-wide),
    `list_opencti_stream_consumers` (live collections, consumers, lag),
    `get_opencti_licence` (expiry, settings sanity fields), `get_opencti_data_health`
    (objects created and updated per day over 7 days, total and newest creation time,
    the newest 200 works with errors or timed out, counted platform-wide; sampled
    completeness is read with the existing `get_opencti_entity_stats`),
    `get_opencti_error_rate` (Activity log entries with `event_status` error per day over
    7 days; the role needs SETTINGS_SECURITYACTIVITY, else the check is `not_run`).
  - OpenAEV: `get_openaev_platform_health` (version, licence, settings sanity, the
    OpenCTI link: configured and last call result), `list_openaev_expectations_due`,
    `get_openaev_data_health` (scenarios, simulations and atomic testings: totals and
    updates per week over 4 weeks, as OpenAEV filters no creation time; simulations
    running past their end or canceled; completeness and duplicate assets are not
    exposed by its search API, so those checks are `not_run`), `get_openaev_error_rate`
    (atomic testings in ERROR or PARTIAL per day over 7 days; OpenAEV exposes no
    platform error log, so that part is `not_run`).
  - OpenCRQ: `get_opencrq_data_health` (scenarios by readiness, inputs missing or
    outdated since the last score, never scored; CRQ simulations per 30 days, as the
    scenario overview carries no creation time, and the last successful one),
    `get_opencrq_platform_health` (failed jobs per day over 7 days, jobs running over 6 h,
    connector states and the OpenCTI and OpenAEV links: configured and state; OpenCRQ
    exposes no platform error log, so that part is `not_run`).
  Each S15b tool names in its docstring the module query it runs and the module version it
  needs; a module that lacks the query answers `not_run` for the checks that need it.

### 13.6 The check report

- **Store.** A new `health_module_check_reports` row per cycle (migration): registration
  id, cycle id, started and finished, counts (passed, warn, failed, not run), and one entry
  per check (id, verdict, evidence: tool, arguments, object ids, values, read time, the
  recommendation on warn and fail). Written by a new health tool `record_module_check_report`
  (checker only, inside its own cycle on its own registration, one report per cycle, a
  second call replaces it), the guard of `record_finding_recommendation`.
- **Agentic findings.** Each warn or fail opens or refreshes a finding
  `<module>.agentic.<check>` through `record_agentic_finding` (severity warning or
  critical), carrying the evidence and the recommendation; a check that passes resolves
  its open agentic finding in the same cycle (no 7-day wait, which stays only for a check
  that was `not_run`). Agentic findings count in the module score like any other.
- **Read.** The cycle digest of a checker carries a `check_report` section (counts and the
  checks not passed with their finding id; a round that stored none is skipped work
  `check_report`/`not_recorded`). `GET /api/v1/health/modules/{module}/checks`
  (administrators) answers the latest report per registration of the module, stored rows
  only, so it is readable with AI off.
- **Recommendations on declared findings** (12.4) stay: after the checks, the checker
  reads the module's open deterministic findings and attaches a recommendation to each one
  the checks explain.
- **Check logic version (#1732).** Every agentic finding (`evidence.check_logic`) and every
  report entry records the check logic that produced it: `build` (`settings.app_version`)
  and `skill_hash` (first 16 hex of the sha256 of the checker's playbook
  `seed_data/skill_content/health-<module>-checks.md` as the build ships it). An open
  agentic finding whose recorded logic differs from the running one, or that records none
  (written before #1732), is **stale** (`check_reports.is_stale`): `GET /health/findings`
  serves it `stale: true` with `check_build`, and the trigger pass starts one round for its
  registration with `module_trigger` `deploy` (`module_analyst.deploy_due`): after the
  change cycles, under the same debounce, pass cap and Module Analyst ceiling, once per
  running build (`last_value.deploy_build`), so a finding the new playbook no longer reports
  stays marked stale and never re-triggers. The round's report resolves (pass) or refreshes
  (warn, fail) the finding under the running logic; a deploy that changes no checker still
  re-checks once, because the build is part of the logic.
- **Analyse now vs Run checks now.** Analyse now (`POST /health/analysis/{module}/run`)
  never re-runs a check: one model call rewrites the module's summary from the stored open
  findings. Run checks now starts a checker round, which writes new evidence. The analysis
  prompt carries, per agentic finding, `evidence_at`, `check_build` and `stale` (another
  check logic, or evidence over 24 h old) and `running_build`; the instruction forbids a
  rollback, downgrade, restart or other disruptive action from a stale finding and names Run
  checks now instead, and `analysis.guard_summary` drops any summary line that still
  proposes one (no rollback target is ever named from stale evidence).

### 13.7 Triggers, quota, ceiling

- On by default in XTM One mode; off in Copilot mode, under `ai_disabled`, when Platform
  Health is off, and per registration (`excluded_registration_ids`), as 12.1.
  `HealthSettings.module_analyst` keeps its name in storage (no settings migration) and is
  labelled "Module health checks" in Spend limits.
- Outside agentic quota under the S14 ceiling (12.5), one ledger for the three checkers.
- **Default ceiling, measured (S15d-3, 22 Nov 2026).** Lab `lab-module-health` on main
  `7cc0b59`, live model `claude-opus-5-5` through the Copilot's LLM proxy, zero open findings,
  no admin action: the trigger pass opened the three rounds at once (`trigger` schedule,
  `module_trigger` round) and each closed in about 61 seconds with its report stored.

  | Checker | Checks in the report | Tokens | Cost (USD) |
  |---|---|---|---|
  | OpenCTI | 12 (all `not_run`) | 53,786 | 0.256 |
  | OpenAEV | 10 (all `not_run`) | 53,563 | 0.258 |
  | OpenCRQ | 8 (all `not_run`) | 43,147 | 0.211 |

  These are the floor of a round: the lab's registrations point at `.invalid` hosts, so
  every read fails fast and returns no module data. A day of the three rounds costs
  about 150,000 tokens and 0.73 USD at that floor, a 30-day month about 4,500,000 tokens
  and 22 USD. Decision: the defaults that ship in `HealthSettings.module_analyst` stay,
  **80,000 tokens per cycle, 500,000 tokens and 4 USD a day, 10,000,000 tokens and 60 USD
  a month** (a round at the floor uses 54 to 67% of the cycle cap; the day and month caps
  leave room for change cycles and the reads of reachable modules). The provisional
  150,000 per cycle of the spec was never written to the settings and is withdrawn. The
  figure of a round on reachable modules, and so whether 80,000 per cycle holds, is read
  at the deploy retest on ctem.one; a round stopped at its cap there raises the default
  in a follow-up of this section.
- **Measurement lab (S15d-3).** `[service.lab-module-health]` is `[service.lab-live]` with
  `LAB_PLATFORM_HEALTH = "on"` (the seed turns Platform Health on through the real
  `PUT /health/settings`, `seed_platform_health`) and `LAB_WORKER = "1"`: the seed's
  OpenCTI, OpenAEV and OpenCRQ registrations get their daily round with no admin action on
  the live model, and Spend limits reads the tokens per cycle. Its registrations point at
  `.invalid` hosts, so every module read fails and the checks answer `not_run`: the figure
  it gives is the floor of a round (playbook, reads attempted, report stored); the figure
  of a round on reachable modules is read at the deploy retest on ctem.one.

### 13.8 Where it shows

- **Health page, module card:** "Agentic checks: 9 passed, 2 warn, 1 fail, last run
  <time>" with a link to the report; "Run checks now" for an administrator (replaces
  "Investigate now"). The report view lists every check with its verdict, evidence and
  recommendation; a failed check links to its agentic finding.
- **Cycles:** each round is a `HealthCycle` of the checker titled with the module and the
  registration (closes advisory A1 of #1025 round 1), its digest the report counts.
- **Weekly digest:** the "Modules" part gains, per module, the round counts of the week
  and the checks that failed most, computed from the stored reports into the digest's
  trigger payload (`modules`) by the schedule, never counted by the model (S15d-1).
- **Fleet:** About, graph, Members and Data flow show the three checkers as delegates of
  the orchestrator, their skills, and as inputs the module integrations they read (the
  registrations' integrations, drawn read-only).
- Access as 12.7 (administrators); the access matrix and link guard of 8.1 and 8.2 gain the
  report route and files.

### 13.9 Acceptance pinned by tests

- (l) XTM One mode, Platform Health on, an OpenCTI, an OpenAEV and an OpenCRQ registered,
  zero open findings: the daily module round starts one cycle per registration with no
  admin action, each run calls its module's tools and records a report with every check of
  its playbook (backend test, stubbed model and integrations,
  `tests/test_health_module_round_postgres.py`; lab retest at deploy).
- (m) A stubbed module-native fault per module (OpenCTI: an enabled stream manager not running on 3 reads;
  OpenAEV: a scheduled scenario whose start was missed; OpenCRQ: a failed job) yields a fail verdict and an open `<module>.agentic.<check>` finding with its
  recommendation; the next round with the fault removed resolves it.
- (n) The seeded fleet: the orchestrator holds a `delegate` handover to each checker, each
  checker holds its skill, and the fleet graph payload draws the edges.
- (h), (i), (j), (k) of 12.9 extended to the three checkers: no agentic quota counter
  moves, the ceiling stops the round, nothing starts in Copilot mode, off or `ai_disabled`,
  every allow-listed tool classifies as a read and a write by name is refused.
- (o) Scope: no tool of `MODULE_CHECKER_TOOLS` and no check id of the three playbooks
  names a connector, feed, collector, injector, executor or third-party source (a
  deny-list of name fragments over the tuple and the seeded skill content), and no new
  health tool accepts a connector id argument.

### 13.10 Sub-slices (`Refs #101 (S15x of the plan)`)

| Slice | Content | UI |
|---|---|---|
| S15 spec | This section | no |
| S15a-1 | Members: rename in place and two new checkers, roles (`module_analyst` kept as the alias of rows written before S15), `is_module_checker_run`, cycles started on the module's own checker, orchestrator handovers, playbook skills (seed data), the Cycles label of each checker, tests (n) and the 12.9 extensions | no |
| S15a-2 | Orchestrator `module_round` schedule replacing the daily branch of the triggers pass (13.2) | no |
| S15b-1 | Allow-list reshaped to the 13.5 lists (connector and feed tools removed, existing module reads added), renamed `MODULE_CHECKER_TOOLS`, scope test (o) | no |
| S15b-2a | OpenCTI module health reads (`list_opencti_background_tasks`, `get_opencti_managers`, `get_opencti_platform_queues`, `list_opencti_stream_consumers`, `get_opencti_licence`), allow-listed, classifier and snapshot tests, `backend-snapshots` regenerated | no |
| S15b-2b | OpenCTI `get_opencti_data_health` (objects created and updated per day over 7 days, total and newest creation, newest works with errors or timed out, platform-wide) and `get_opencti_error_rate` (Activity log entries with `event_status` error per day over 7 days), allow-listed, same tests | no |
| S15b-2c | OpenAEV `get_openaev_platform_health` (`GET /api/settings`: version, licence, XTM Composer, XTM Hub, SMTP and IMAP; `GET /api/tenants/{tenant}/tenant-settings`: OpenCTI and OpenCRQ links enabled and URL set; OpenAEV exposes no last call result of a link, so that part is `not_run`) and `list_openaev_expectations_due` (`GET /api/injects/expectations`, READ on SIMULATION: expectations with no result yet by type, overdue, oldest) in a new `providers/openaev/health_reads.py`, allow-listed, same tests | no |
| S15b-2c2 | OpenAEV `get_openaev_data_health` (`POST /api/scenarios/search`, `/api/exercises/search`, `/api/atomic-testings/search` with `size` 1, `totalElements` only: totals, updates in the last 7 days vs the 3 weeks before, since `*_created_at` is not `@Queryable`; simulations `RUNNING` 24 h past `exercise_end_date`; simulations `CANCELED` in 24 h, OpenAEV having no failed status; completeness and duplicate assets not exposed by the search API) and `get_openaev_error_rate` (atomic testings with `inject_status` in `ExecutionStatus.ERROR_STATUSES` per day over 7 days by `inject_updated_at`, with the daily mean; no platform error log in the OpenAEV API) in the same file, allow-listed, same tests | no |
| S15b-2d | OpenCRQ `get_opencrq_data_health` (`riskScenarios.getRiskScenariosOverview`: scenarios by readiness status, inputs missing or outdated since the last score, never scored, oldest last score; `jobs.listAllRuns` with `limit` 1, `total` only: `crq-simulation` runs enqueued in the last 30 days vs the 30 before and the newest succeeded one, the overview carrying no scenario creation time) and `get_opencrq_platform_health` (`jobs.listAllRuns`: runs in `dlq` per day over 7 days by `finishedAt` with the daily mean, runs `running` started over 6 h ago; `connectors.listConnectors`: connectors by state, the failing ones, the OpenCTI and OpenAEV links configured and their state; no platform error log in the OpenCRQ API) in a new `providers/opencrq/health_reads.py`, allow-listed, `tool_procedures.json` and `tool_input_keys.json` regenerated, same tests | no |
| S15c | Report store (migration), `record_module_check_report`, agentic finding open and resolve per check, digest sections, API (`GET /health/modules/{module}/checks`), tests (l), (m); shipped in two PRs: S15c-1 the store (`health_module_check_reports`, migration `h129healthcheckreports01`, one row per cycle), the tool in the `health_modules` group and `services/health/check_reports.py` (a warn opens `<module>.agentic.<check>` as a warning and a fail as a critical at once, a pass resolves it in the same cycle, not run leaves it, a live dismissal is refreshed and never reopened; `tests/test_health_check_reports.py`), the seeded checks prompt and the three playbooks telling the checker to store its verdicts; S15c-2 the cycle digest section `check_report` (counts, the checks not passed with their finding, a round that stored no report listed as skipped work `check_report`/`not_recorded`), `GET /health/modules/{module}/checks` (administrators, the latest report per registration, stored rows only so readable with AI off, 404 for a module of another mode) and test (m) (`tests/test_health_check_report_api_postgres.py`: a stubbed fault per module fails its check and opens a critical agentic finding, the next round resolves it); test (l) and the weekly digest's module part move to S15d with the round measurement | no |
| S15d | Lab measurement and default ceiling written in 13.7, test (l), the weekly digest's module part; shipped in two PRs: S15d-1 the weekly digest's Modules part (`check_reports.weekly_module_summary`: per module, the rounds, registrations and verdict totals of the reports finished in the window and the 5 checks failed most, fail before warn, ties by id; computed by `cycles._digest_payload` into the digest's trigger payload as `modules`, `modules_unavailable` when the read fails, so the model restates figures it did not count; `tests/test_health_weekly_module_summary.py`); S15d-2 test (l) (`tests/test_health_module_round_postgres.py`: three registrations through their integrations, zero open findings, one trigger pass starts one checker cycle each with no admin action, each run's reads are its module's allow-list and its report holds every check of its playbook, a second pass the same UTC day starts none); S15d-3 the measurement lab `[service.lab-module-health]` (`LAB_PLATFORM_HEALTH`, `seed_platform_health`), then the lab measurement of the three rounds (43,147 to 53,786 tokens a round at the unreachable-module floor) and the default ceiling written in 13.7 (the shipped 80,000 per cycle kept, the provisional 150,000 withdrawn) | no |
| S15e | Health page: module card counts, report view, "Run checks now", Cycles title, weekly digest part, docs and i18n; shipped in parts: S15e-1 the "Agentic checks" line under the AI block of an OpenCTI, OpenAEV and OpenCRQ card (`ModuleCheckReport.tsx`, administrators: the latest report of every registration summed into passed, warnings, failed and not run counts as ICU plurals, the newest round's end, and every check with its verdict, recommendation or reason in the shared popover, failed first; `GET /health/modules/{module}/checks`, stored rows, no model call, readable with AI off; "No check round yet" before the first round; en, fr, es, ja; `ModuleCheckReport.test.tsx`); S15e-2 "Run checks now" in the card's AI actions menu, replacing "Investigate now" (`POST /health/modules/{module}/investigate`, unchanged route, the checker's round on every active registration, toast "Checks started" with the rounds started as an ICU plural, refusals worded for a check; audit summary "Asked Platform Health to run the <module> checks"; the AI block caption "Last check round" and its empty line "No check round yet", so the action and its outcome share one name, and the "Agentic checks" line under it shows only once a round reported, never a second "No check round yet"); S15e-3 the Cycles title (`GET /health/cycles` and `GET /health/cycles/{id}` answer `target_name`: a module cycle's registration `platform_title`, an agent's, flow's or assignment's name, `null` once the target is gone; `HealthCyclesList` shows it after the specialist, so a round reads "OpenCTI Health Checker" then the registration, closing advisory A1 of #1025 round 1; `HealthCyclesList.test.tsx`, `test_a_module_cycle_is_named_by_its_registration_title`); S15e-4 the weekly digest's Modules part in the Cycles tab (`digest.modules`: the weekly digest cycle's digest copies the `modules` its run's trigger carries, `unavailable` gains `modules` when the trigger carries `modules_unavailable`; `CycleDigestView` shows a "Module checks" section: per module the rounds and registrations, the verdict totals and the checks failed most, as ICU plurals in en, fr, es, ja, "No check round since the last digest" for an empty part, "Unavailable" never 0; `CycleDigestView.test.tsx`, `test_health_weekly_module_summary.py`) | yes |
| S15f | Fleet surfaces (About, graph, Members, Data flow inputs), access matrix and link guard, UX Director journey review; shipped in parts: S15f-1 (#1306) the Data flow inputs and the About page: each checker draws a `read` edge to the integration of every active registration of its module (`module_analyst.checker_reads`, passed to `fleet_flow.assemble_fleet_flow` as `implicit_reads` for the Platform Health fleet only, configuration only, never a write and never a binding written; `test_fleet_flow.py`), and the fleet's About names the "Module checks" daily round and the module check reports (`builtin_fleet_docs.PLATFORM_HEALTH_*`, `test_health_fleet_state.py`); S15f-2 the graph and Members skills pinned by test (n) of 13.9 (`test_health_module_round_postgres.py`: the orchestrator's `delegate` handover to each checker drawn as a handover edge of the fleet graph, each checker holding its `health-<module>-checks` playbook), the access matrix row of `GET /modules/{module}/checks` (section 8.1, `test_health_role_mode_matrix_postgres.py`) and the link guard note of `ModuleCheckReport.tsx` (section 8.2); then the UX Director journey review in the lab | yes |
