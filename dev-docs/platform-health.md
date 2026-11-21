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
  dismissed), `evidence` (values, threshold, read time, source), `recommendation`,
  `doc_link`, `first_seen`, `last_seen`, `resolved_at`. Dedupe on `id`; auto-resolve when
  the check passes twice in a row; dismiss with a reason and an expiry, reusing the
  `fleet_health_dismissals` pattern.
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
- Hard ceiling, tokens and dollars per day and per month, enforced by reservation before
  the call (the `budget.py` ledger, renamed). Over the ceiling the call is refused and the
  page says so. Test (e) forces an over-spend.
- Traces carry their own source (`platform_health`), excluded from customer quota and
  from agentic execution counts, visible in observability; the spend is shown on the
  Health page.
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

## 7. Settings

One compact "Platform Health" section (Settings > Agents and automations > Platform Health):

- Enrolled agents and fleets (list, bulk enrol, bulk remove).
- Per module (XTM One mode only): check toggles and the (E) thresholds of section 4.
- Analysis: on or off, daily and monthly ceiling.
- Alert emails: off by default; recipients, minimum severity, immediate for a new
  critical finding plus a daily digest, one email per finding (dedupe on finding id).
  Stored as `HealthSettings.alert_emails` (S12, `services/health/alerts.py`): after
  each pass commits, `send_alerts` mails every critical finding the pass opened or
  reopened once (Redis `SET NX` on `health:alert:<finding id>`, 7 days), then the
  digest once per UTC day (mark `alert:digest` in `health_schedule_marks`). Nothing
  is sent with SMTP unset or no recipient; a failed send never fails the pass.
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
  OpenCRQ in XTM One mode) with score, counts by severity and trend.
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
| `/overview`, `/findings` | 200, every module of the mode | 200, `agents` and `fleets` of the agents they manage |
| `/history` | 200 | 200, empty |
| `/improvements`, `/cycles`, `/signals` without a target | 200 | 404 (one managed target only, `?agent_id=` or `?target_type=&target_id=`) |
| `/settings`, `/budget`, `/schedules`, `/analysis`, `/me/companion`, `/me/activity` | 200 | 403 |

`POST /analysis/{module}/run` is refused `ai_disabled` (403, `detail.code`) while AI is off.

### 8.2 Link guard (S13, pinned by `frontend/src/lib/health-link-guard.test.ts`)

Visible <=> openable on the frontend: `/health` and `/settings` are `AdminRoute`s, so every
Health surface that links there is drawn for administrators only, either by its own
`isAdmin` gate (companion, profile companion card, Dashboard critical findings notice) or
because only an administrator page renders it (Overview, Signals, analysis panel, the
Settings blocks). The surfaces open to everyone (agent improvement row, flow panel,
improvements inbox, cycles list, profile opt-out) link only to the target's own page
(`/agents/<id>`, `/flows/library/<id>`, `/assignments/<id>`), which has no route guard and
checks access itself. The guard lists every Health file with its audience: a new file fails
until it is classified, a link to an `AdminRoute` from a surface open to everyone fails with
its file and line, and a `/health?tab=` link fails (the page reads its tab from the path).

## 9. Platform email (SMTP) integration

- Built-in company-managed integration "Platform email (SMTP)", present only when
  `email.is_email_configured()`; connectivity probe registered with
  `integrations/connectivity.register_connectivity`.
- Tool `send_email`: to, cc, bcc, reply_to, subject, markdown or HTML body in the Filigran
  layout (`services/email.py`), attachments by file id (20 MB total, the existing cap).
- Guards: recipient domain allowlist (empty = refuse all), per-agent daily send limit,
  tool approval on by default, audit log entry per send, From fixed to the platform sender.
- Health alert emails (section 7) use `deliver_email` directly, not the agent tool.

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
2. An explicit allow-list of read tools per provider (`MODULE_ANALYST_TOOLS`, one tuple
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

The default is set from a measured cost per module per cycle, as section 6 did, in the
S14b lab measurement (`lab-live`, live model, one registered OpenCTI and one OpenAEV, a
cycle on a module with open findings and one on a module with none), written here with
the billed tokens and dollars before S14 ships on by default.

Provisional figure until then, from the S9b blend (6.7 USD per million tokens) and the
shape of the loop (system and tool definitions cached, about 12 read calls of at most
4,000 characters of result each, 2.2 characters per token): about 45,000 input and 5,000
output tokens per cycle, 50,000 tokens, 0.34 USD. Per-cycle cap 80,000 tokens. With 3
modules and at most 3 cycles a module a day (daily plus two change-triggered at the 2
hour debounce in a bad day): 9 x 50,000 = 450,000 tokens, 3.0 USD a day. Provisional
defaults: **500,000 tokens and 4 USD a day, 10,000,000 tokens and 60 USD a month**
(a month does not run at the bad-day rate every day). The measurement replaces these
numbers; if it lands more than 30 % away, the defaults move with it in the S14b PR.

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
- `services/health/module_analyst.py`: triggers (`due_registrations`, the finding set
  hash per registration, debounce), the tool allow-list, the quota exemption predicate
  (`is_module_analyst_run`), the ceiling ledger.
- Built-in tools of the health group for the member: `list_module_findings`,
  `record_finding_recommendation`, `record_agentic_finding`; the module reads are the
  registration integration's own read tools behind the allow-list.
- The orchestrator does not delegate to the analyst; it starts only from its own
  triggers.

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
| S14a | Member, role, assignment, target type `module`, triggers and debounce, tool allow-list and read tools, recommendation and agentic finding store (migration), tests (g) backend, (j), (k); shipped in two PRs: S14a-1 the role, the target type, `module_analyst.py` (allow-list, refusal, `registration_in_scope`) and `health_finding_recommendations` (`tests/test_health_module_analyst.py`), S14a-2 the member's seed, triggers, tools and the OpenCTI read tools | no |
| S14b | Quota exemption and own ceiling ledger, settings model and defaults, the lab measurement and the default ceiling written in 12.6, tests (h), (i) | no |
| S14c | Health page: recommendations on findings, agentic tag and filter, module card last investigation and "Investigate now" | yes |
| S14d | Cycles entry, weekly digest part, Spend limits row, docs (`docs/docs/administration`, `docs/docs/usage`) and i18n | yes |
| S14e | Access matrix and link guard extended (S13 tests), UX Director journey review in the lab | yes |
