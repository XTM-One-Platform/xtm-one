# Platform Health: the Concierge reworked into a built-in health suite

Issue: #101. Status: S0 (this spec). Read on main `7674eb87`, 18 Nov 2026.

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
individual checks.

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
| Fleet seed (5 agents) | `fleet_seed.py`: `concierge`, `concierge-memory-steward`, `concierge-quality-analyst`, `concierge-flow-lab`, `concierge-experimenter` | Keep and rename the fleet "Platform Health", orchestrator "Platform Health"; the migration renames rows by slug and leaves a hand-edited persona as written (compare to the seeded hash). Done in S1 (part 3): the default name is "Platform Health" in every mode (`naming.DEFAULT_NAME`; the copilot "Alfred" default is gone), the former defaults stay in `DEFAULT_NAMES` so the next seed renames an orchestrator, fleet and workspace still carrying "Concierge" or "Alfred" and keeps a hand-set name; seeded assignment descriptions point at Settings > Automations > Platform Health. Assignment names and slugs (seed keys) change with the migration |
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
| Page | `pages/Concierge.tsx` (Improvements, Cycles, Signals) | `pages/Health.tsx` (section 8) |
| Settings | `pages/settings/ConciergeSection.tsx`, `ConciergeTriggerBlocks.tsx`, `ConciergePetLibrary.tsx`, `concierge-block.tsx` | One compact `HealthSection` (section 7) |
| Agent page | `pages/agent-detail/OverviewTab.tsx` opt-in card and Improvements block | One compact row at the bottom of the overview, after the configuration: enrolment switch, content opt-in, last finding count, link to Health |
| Flow page | `pages/flow-builder/ConciergePanel.tsx`, toolbar button | Same compact row in the flow side panel |
| Profile | `ConciergeOptOutRow.tsx`, `ConciergeCompanionCard.tsx` | Opt-out kept and renamed; companion kept as a profile option and renamed |
| i18n | about 577 occurrences in 5 catalogs | Renamed in every language. Done in S1 (part 2): the 47 user-facing keys the frontend asks for (opt-in card, consent notice, profile opt-out, Settings section and trigger blocks, assignment pause reason) read "Platform Health" in en, fr, es and ja; the keys the backend also emits (built-in tool group titles, "Concierge cycle", "Concierge settings", "Concierge pet") follow the backend rename with the migration |
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
- Deployments: FiligranHQ/platform and the helm charts are searched for
  `ENABLED_FEATURES=CONCIERGE` at slice S1; any hit is removed in the same slice (Copilot
  values) with its helm-xtm-suite mirror when it exists. Searched 18 Nov 2026 (S1): no
  hit in FiligranHQ/platform nor in the helm charts, so no deployment change.

### 2.4 The former `CONCIERGE` flag

The flag is gone from `FeatureFlag` / `KNOWN_FEATURE_FLAGS` (backend and
`frontend/src/lib/utils.ts`). `ENABLED_FEATURES=CONCIERGE` left in an environment is an
unknown id: logged and ignored at startup like any unknown flag, never an error.
`services/concierge/availability.concierge_enabled` answers `True`; whether the fleet
works is the admin switch alone (`concierge_settings.enabled`, off by default,
`concierge_active`). Every gate the flag held is now open: the identity, consent reads
(enrolled targets only, section 1.2), the budget, `/api/v1/concierge/*`, the agent, flow
and profile cards, the Settings section, the fleet seeding, the scheduler, the
`gate="concierge"` tool groups and `/api/v1/improvement-proposals`. Kept as they were:
`platform_settings.concierge_settings`, the opt-ins and opt-outs, `agents.concierge_role`,
the budget ledger, cycle digests, signal states and schedule marks (renamed by the S1
migration, 2.3). `backend/tests/test_concierge_built_in.py` pins the built-in behaviour.

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

### 4.5 OpenCRQ (XTM One mode, automatic)

Read live on OpenCRQ-Platform/opencrq main, 18 Nov 2026: OpenCRQ exposes process states
(`back/src/utils/health-check.ts`: FORKING ... RUNNING ... ERROR, sent to the parent
process only) and a job health (`module/src/modules/jobs/health.ts`: running, queued,
never_ran, healthy, failing). It does not register with XTM One today (no registration
client found by code search). Checks: registration heartbeat, version, licence once it
registers; process states and failing jobs once the snapshot carries them. Missing on the
OpenCRQ side: the registration itself and a snapshot of those two states.

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
- **Default ceiling: not set in this spec.** It is set in slice S9 from a measured lab run
  (tokens and dollars of one full analysis pass per module on the lab with OpenCTI and
  OpenAEV registered), the measurement written here then. No guessed figure.

## 7. Settings

One compact "Platform Health" section (Settings > Agents and automations > Platform Health):

- Enrolled agents and fleets (list, bulk enrol, bulk remove).
- Per module (XTM One mode only): check toggles and the (E) thresholds of section 4.
- Analysis: on or off, daily and monthly ceiling.
- Alert emails: off by default; recipients, minimum severity, immediate for a new
  critical finding plus a daily digest, one email per finding (dedupe on finding id).
- Removed from the UI: cron editors, durations, dry run, name. Kept as constants or env.

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

## 11. Product decisions (decided by Sam, 18 Nov 2026)

- Name: **Platform Health** (not "Health", not "Health Assistant"), in routes, nav,
  settings, i18n, docs and the seeded fleet from S1.
- Companion and pets: **kept** as a profile option; not removed in the rename.
- Acquisition: **push** in the heartbeat, with automatic refresh of the UI (section 5).
- Analysis ceiling: **go**, set from the S9 measurement (section 6).
