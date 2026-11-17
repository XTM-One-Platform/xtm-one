# CTEM in Community Edition and with AI disabled: sweep and plan

Issue #9207, epic #8975 (program `dev:ctem-fleets`). Slice 1 was #9195 (Force sync no longer needs AI, issue #9194).
Scope: XTM One mode only (`platform_targets` xtm_one and dev), never Copilot mode.
Code read at main `fd52fa75`. Every verdict below comes from the code. The "Lab" column says what still has to be seen rendered.

## 1. Goal

XTM One with no Filigran license and AI disabled must be a complete open-source CTEM command center:

- every metric computable without a model is shown and useful;
- every AI or Enterprise Edition (EE) surface shows one honest call to action (CTA), never an empty, broken or misleading state;
- no Community Edition (CE) path starts model traffic or consumes agentic quota.

## 2. States and predicates

| State | License (`is_agentic_ai_licensed`) | AI switch (`is_ai_enabled`) | Model configured | Frontend reads |
|---|---|---|---|---|
| S-CE | no | any (on by default, see below) | any | `useEdition() === "community"`, `useAgenticAIEnabled() false` |
| S-CE-nollm | yes | off, or on with no model | none / off | `useAgenticAIEnabled() true`, `useAiEnabled()` false or `ModelConfigWarnings` shown |
| S-EE | yes | on | yes | the reference: must not regress |
| S-group-off | any | off for the reader's group (`useAiDisabledGroup`) | any | `useAiDisabledGroup() !== null` |

Each state is read as administrator and as non-administrator.

AI default in CE, verified in code (`backend/app/services/ai_state.py`, `ai_enabled_by_mode`): AI is on unless `PLATFORM_MODE` is `ai_disabled` or an administrator switched it off. The edition (license) does not change the default, so "off by default in CE" was wrong: a CE install is on by default, and S-CE meets both AI on and AI off.

Precedence of the lock reasons (one answer per surface, design system "AI off wins over the license"): **group AI off > platform AI off > no license > no model > none**. One resolver hook, `useAiLockReason()` in `frontend/src/hooks/`, returns exactly one of `group_off`, `ai_off`, `unlicensed`, `no_model`, `null`, built on `useAiDisabledGroup` / `useAiDisabledReason`, `useEnterpriseLockedReason` and the `ModelConfigWarnings` predicate (licensed and `agent_default_model` missing). Read with S-CE: an administrator in CE with AI off sees "Turn AI on" first, then "Start free trial" once AI is on. The backend statuses (`ee_locked`, `ai_disabled`, `no_model`, section 5) feed the cards; the resolver alone decides which reason the UI shows.

Backend gates (`backend/app/api/deps.py`):

- `require_ai_enabled`: refuses when AI is switched off. No license check.
- `require_agentic_ai_licensed`: license only. Used for surfaces that must survive AI being off.
- `require_agentic_ai`: AI on AND licensed.
- `ee_features_enabled` (`platform.py`): an alias of the license predicate. The briefing and the stage assessments answer `ee_locked` through it.

Routing (`XtmOneDashboard.tsx`): in CE with platforms registered, `CtemTrialSplash` shows once, then the command center in its CE shape. With no platform registered, `WelcomeLanding` shows.

## 3. Endpoints

| Endpoint | Calls a model | Gate today | Right gate | Verdict |
|---|---|---|---|---|
| `GET /ctem-metrics/posture`, `/posture/history`, `/stages/*` reads | no | none (`_require_xtm_one`) | none | OK |
| `POST /ctem-metrics/sync` (Force sync) | no | none since #9195 (was `require_ai_enabled`) | none | fixed in slice 1 |
| `GET /ctem-metrics/briefing` | no (reads the last artifact) | license through `ee_features_enabled`, returns `ee_locked` | license, plus an AI-off status | **bug B1**: licensed with AI off answers `pending`, see section 4 |
| `POST /ctem-metrics/briefing/refresh` | yes (starts a run) | `require_ai_enabled` + license | same | OK as a gate |
| `GET /ctem-metrics/stages/{stage}/assessment` | no | license, returns `ee_locked` | license, plus an AI-off status | **bug B1** (same) |
| `POST /ctem-metrics/stages/{stage}/assessment/refresh` | yes | `require_ai_enabled` + license | same | OK |
| `GET /ctem-metrics/stage-agents` (admin), `/my-stage-agents` | no | none | none | OK |
| `PUT /ctem-metrics/stage-agents`, `/my-stage-agents` | no (configuration) | `_require_ctem_agentic_ai` | license | OK, the UI shows the EE badge and the license dialog |
| `GET /activity/stats` (KPI strip, digests) | no | none | none | OK |
| `GET /assignments/runs/all` (Live activity) | no | none | none | OK |
| `GET /fleets`, `/fleets/{id}/health` (Fleet activity) | no | `require_agentic_ai_licensed` | license (fleets are an EE feature) | gate OK, **CE state missing** (B3) |
| `GET /roi` (Fleet activity, admin) | no | admin | admin | OK, only read when a fleet exists |
| `GET /platform/registrations` | no | none | none | OK |

## 4. Command center widgets

Abbreviations: A = administrator, U = non-administrator. "Lab" = still to see rendered in a CE lab.

| Widget | S-CE | S-CE-nollm | S-EE | Verdict |
|---|---|---|---|---|
| `hero` posture text, orbit, ribbon, product pills | live posture, deterministic | same | same | OK |
| `hero` sync line | "Live - fully agentic" while never synced | same | same | **misleading M1**: CE is not agentic. Proposed: "Not synced yet" + Force sync, which works in CE since #9195 |
| `hero` "N agents watching" | counts seeded agents that cannot run in CE | agents cannot run with AI off | OK | **misleading M2**: in CE and with AI off, drop the clause or say "N agents ready" with the CTA below |
| `hero` "Start chatting" button | leads to `/chat`, where `ChatGate` refuses | refused with AI off | OK | **dead end D-1**: the primary button of the hero is the one action CE cannot take. Proposed: the single CTA of section 6 in CE, "Start chatting" when AI runs |
| `kpi_posture` | live score | same | same | OK |
| `kpi_exposure_1..3` (vertical KPIs) | live when the source product is connected, sample otherwise | same | same | OK |
| `kpi_agents` | count of active agents (seeded ones included), click to `/agents` | same | OK | **missing CE value V1**: a number with no meaning in CE. Proposed: replace in CE by a deterministic KPI (open KEV, validation pass rate) or show it as "available with Enterprise Edition" |
| `kpi_executions` | 0 "last 30 days", click to `/operations/executions` | 0 | OK | **missing CE value V1** (same) |
| `briefing` (AI briefing) | `ee_locked`: sample, lock, subtitle "An AI capability - available with at least one Enterprise product (OpenCTI or OpenAEV EE)..." (`BriefingCard.tsx:91`), a third CE wording that leaves out OpenCRQ and the license path | **bug B1**: `pending`, the dashboard auto-calls `/briefing/refresh`, refused 403 (AI off) or a run on no model; the card shows a sample with a "Generate my briefing" button that fails | OK | locked state OK in CE but **missing CTA C1** (no "Start free trial" / "Activate License" / "Ask your administrator to activate a license.") |
| `lifecycle_header` | deterministic | same | same | OK |
| `stage_*` cards (5) | live scores, sample when not covered; `aiEnabled` false (briefing `ee_locked`), no AI strip | **bug B1**: `aiEnabled` true (status `pending`), each card auto-calls the assessment refresh: up to 5 refused calls per load, or 5 runs with no model | OK | CE OK; B1 in S-CE-nollm |
| `stage_governance` (OpenCRQ) | static sample "joining the XTM suite" | same | same | OK until OpenCRQ registers (separate item) |
| `recommended_actions` | deterministic rules: connect a product, triage KEV, close validation gaps, coverage under 60 %, wire the stack (A), first simulation, "Ask your CTEM agent" | same | AI next steps from the briefing | **dead ends D-2**: "Create agent", "Design your first ATT&CK simulation" (chat) and "Ask your CTEM agent" (chat) lead to locked pages in CE. The deterministic rules are the right CE content; the AI rows need the CTA instead |
| `live_activity` | no run: grey sample, "real agent runs stream here as assignments fire" | same | runs | **misleading M3**: promises runs that cannot happen in CE. Proposed: CE explainer + the single CTA, or a deterministic feed (last syncs, registrations, validation results) |
| `digests` | "No results yet" | same | digests | **empty E1**: an empty card with no next step. Same treatment as M3 |
| `fleet_activity` | fleet list refused (license), card says "No fleet yet ... Open fleets", leads to a locked page | licensed: real fleets; runs need AI | OK | **dead end B3**: CE must explain what fleets do with the single CTA, never "Open fleets" |

Pages:

| Page | S-CE | S-CE-nollm | Verdict |
|---|---|---|---|
| `CtemStageDetail` | facts, history, sample assessment locked | B1: `pending`, generate button refused | B1, C1 |
| `ExposureBriefing` (`/briefing`) | `ee_locked` sample | B1 | B1, C1 |
| `CtemTrialSplash` | once, with trial and admin/non-admin wording | not shown | OK, the vocabulary to reuse |
| `DashboardNotices` (`EnterpriseUpsellNotice`, `ModelConfigWarnings`) | upsell notice | "Configure a model" notice | OK, the CTA system to reuse |
| `CtemStageAgentsCard` (settings) | EE badge and license dialog | same | OK |

Bug B2 (zero automated quota): in S-CE-nollm with AI on and no model, the auto-generation of the briefing and of the 5 stage assessments can start up to 6 runs on the first load. The first per-vertical run is `quota_exempt`, so it is not metered, but it is model traffic that cannot succeed. To verify in the lab before the fix is written.

## 5. Findings

| Id | Kind | Where | Fix |
|---|---|---|---|
| B1 | bug | `ctem_metrics.py` `ctem_briefing`, `ctem_stage_assessment`; `CtemCommandCenter.tsx` auto-brief effect; `CtemStageCard.tsx` auto-generate effect | the two GETs answer a new status `ai_disabled` when `is_ai_enabled` is false (license checked first, so `ee_locked` keeps priority); the UI treats it as locked with the "AI is switched off" reason (`useAiDisabledReason`) and never auto-generates |
| B2 | bug (lab check first) | same effects | no auto-generation when no model is configured (`ModelConfigWarnings` predicate); status `no_model` if the backend can answer it cheaply |
| B3 | dead end | `FleetActivityCard.tsx` | CE state: what fleets do + the single CTA |
| D-1 | dead end | hero primary button | the single CTA in CE and with AI off |
| D-2 | dead end | `recommended_actions` AI rows | in CE, the deterministic rows only, plus one CTA row |
| M1 | wording | hero sync line | "Not synced yet" |
| M2 | wording | hero "agents watching" | hidden or reworded when agents cannot run |
| M3, E1 | empty or misleading | `live_activity`, `digests` | CE explainer + CTA, or a deterministic feed (D3-4) |
| V1 | missing CE value | `kpi_agents`, `kpi_executions` | deterministic KPIs in CE (D3-1) |
| C1 | missing CTA | briefing card, stage detail, `/briefing` | the single CTA of section 6; the briefing card's locked subtitle (`BriefingCard.tsx:91`) becomes the resolver's reason sentence, with the footer below |

## 6. One call-to-action system (D4, decided with the UX Director, #9210 rounds 1 to 3)

One CTA per AI surface, never two on one card, at the bottom of the card, left aligned, under the greyed sample. The reason comes from `useAiLockReason()` (precedence in section 2). The `group_off` and `ai_off` sentences are the shipped `useAiDisabledReason` keys and "Start free trial" and "Activate License" are the shipped `EnterpriseCtaButtons` and `EnterpriseUpsellDialog` keys, and "Sample" is the shipped key (`en.json:4975`, also in fr, es and ja) that `SampleBadge` reuses; every other word is a new key, listed below with its exact English text. The product says "model", never "LLM": the shipped `ModelConfigWarnings` sentence ("XTM One cannot perform any Agent or LLM operation until a default model is set. Contact your administrator to configure it.") is never reused on a card.

New keys (en, fr, es, ja in the slice 4 PR):

| Key (English text) | Where |
|---|---|
| "Turn AI on" | `ai_off` administrator button |
| "Configure a model" | `no_model` administrator button |
| "AI features run with Enterprise Edition." | `unlicensed` administrator short reason |
| "Ask your administrator to activate a license." | `unlicensed` non-administrator sentence (the shipped `CtemTrialSplash` key "Ask your administrator to activate a license, or" ends on the splash's "update your profile" link and is not reused) |
| "No default model is configured. Contact your administrator to configure it." | `no_model` non-administrator sentence |
| "Fleets run with Enterprise Edition" | Fleet activity empty state title in CE |

| Lock reason | Administrator | Non-administrator |
|---|---|---|
| `group_off` (AI off for the reader's group) | the `useAiDisabledReason` group sentence "AI is disabled for your group. Ask an administrator if you need it.", no button | same |
| `ai_off` (platform AI off) | the `useAiDisabledReason` platform sentence + outline button "Turn AI on" (`/settings/parameters`, the AI section: slice 4 gives `AiEnablementCard` (`ParametersSection.tsx:153`) an anchor id, as `#xtm-hub` on `XtmHubRegistrationCard.tsx:197`, scrolls to it on load and links to that hash). No settings route constant exists on main today (`/settings/parameters` is a literal in `Sidebar.tsx:390`, `Settings.commands.ts:15` and `CtemCommandCenter.tsx:922`): slice 4 uses the constant #9173 S1 (#9211) introduces, or creates it in `frontend/src` if slice 4 lands first, so the #9173 move cannot break the link | the `useAiDisabledReason` platform sentence, no button |
| `unlicensed` (no license) | short reason "AI features run with Enterprise Edition." (new key) + primary small button "Start free trial" + text link "Activate License" (opens the existing `EnterpriseUpsellDialog`, activation form inside). The button is `EnterpriseCtaButtons` with a new prop that leaves out "Contact sales" (today it always renders both, `enterprise-upsell.tsx:42-61`; default unchanged, unit test on the prop), used by `AiLockedFooter`. Never a second trial button in the footer | "Ask your administrator to activate a license." (new key), no button. No "Contact sales" on cards: it stays in the notice and the dialog |
| `no_model` | outline button "Configure a model" (`/settings/models` today; #9211 moves it to `/settings/ai/models` under `LEGACY_SETTINGS_ROUTES` and exports `settingsSectionPath()` but no path constant, so the link follows the same constant rule as "Turn AI on" and never lands on a redirect) | "No default model is configured. Contact your administrator to configure it." (new key; not the `ModelConfigWarnings` sentence, which says "LLM") |

Never a disabled button in place of a sentence for a non-administrator.

Component: `AiLockedFooter` in `frontend/src/components/ui/` (exported next to `EnterpriseCtaButtons` in `enterprise-upsell.tsx`, or in a sibling file), with `useAiLockReason` in `frontend/src/hooks/`. No CTEM-specific footer: the design system says "Everything the UI says about Community Edition lives in `components/ui/enterprise-upsell.tsx`". Used by the briefing card, the stage detail, `/briefing`, the fleet activity card, live activity, digests and the hero. The splash and the notices region stay as they are. Guard test, scoped to `frontend/src/pages`: no file imports `XTM_ONE_TRIAL_URL` or renders "Start free trial" (case-insensitive) outside a frozen allowlist of the four pages that already import it on main: `LicenseRequired.tsx`, `settings/ParametersSection.tsx`, `settings/QuotasSection.tsx`, `xtm-one/CtemTrialSplash.tsx`. The test asserts the allowlist never grows (a new file fails, a removed one is dropped from the list in the same PR); migrating those four to `EnterpriseCtaButtons` is out of slice 4.

Sample: one shared `SampleBadge` renders the design-system `Chip` with `severity="neutral"` and the existing "Sample" key (sentence case, at least 10 px per `no-text-under-10px`), the marker `SampleFrame` (`custom-dashboard/charts/outcome-common.tsx:26`) and `PlatformChart.tsx:106` already render; `SampleFrame` and `PlatformChart` move to `SampleBadge`, so there is one implementation, never a second markup. It replaces the seven CTEM treatments of today: the KPI badge (`CtemCommandCenter.tsx:174`, lowercase, 8 px), the governance tile (`CtemCommandCenter.tsx:1066`, 9 px), the captions "Sample preview - ..." (`BriefingCard.tsx:95`) and "Sample - real agent runs ..." (`CtemCommandCenter.tsx:1246`), the stage card label (`ctem/CtemStageCard.tsx:324-327`, 9 px, uppercase, lowercase key "sample", on every stage card), and the two `Badge variant="outline"` "Sample" of `CtemStageDetail.tsx:215` and `ExposureBriefing.tsx:125`. Every AI widget's sample preview carries it. The footer sentence carries the reason; the badge carries only "Sample".

Hero:

- In every lock state the primary slot holds the footer's administrator button (same `size="sm"`, primary or outline), or the non-administrator sentence as caption text in place of the button. "Start chatting" shows only when the resolver answers `null` (D-1).
- "Not synced yet" replaces "Live - fully agentic" in every state, S-EE included: the line only shows when never synced, so it misleads in EE too (M1).
- "N agents watching" is hidden for any non-null lock reason, `no_model` included (M2).

Fleet activity in CE (B3): `EmptyState variant="inset"` (already used in the card), title "Fleets run with Enterprise Edition", description the existing key "Fleets group the agents that work together; their runs, open work and hours saved show here.", the footer in `children`, not `actionLabel`. The header "All" button is hidden in CE (it leads to the locked page). In CE the card skips the fleet queries (`enabled: licensed`, as `ModelConfigWarnings` does), so `GET /fleets` and `/fleets/{id}/health` are never refused on load.

Digests (E1): the same `EmptyState` inset pattern with the footer. Live activity (M3): the greyed demo rows stay, with `SampleBadge`; the caption becomes the footer until D3-4 ships.

## 7. Deterministic CE features (D3, approved by the UX Director, #9210 round 1)

Every one of them reads the posture already collected; none calls a model. Order by value per effort: D3-1, D3-3, D3-5 (each small, one PR each), then D3-4, then D3-2. D3-6 is later, out of this program.

| Id | Feature | Value in CE | Size | Design conditions |
|---|---|---|---|---|
| D3-1 | CE KPIs instead of `kpi_agents` / `kpi_executions`: open known-exploited exposures and validation pass rate (OpenAEV expectations) | the strip shows exposure, not agent counts that are 0 | small, frontend only | reuses the vertical KPI card and its present/sample logic; the validation pass rate shows as sample when OpenAEV is not connected |
| D3-3 | Rule-based "what to do next": the deterministic rows of `recommended_actions` extended (KEV past SLA, stages with no data source, failed expectations by type) | the CE equivalent of the briefing's next steps | small | replaces the AI rows, never added next to them; the six-row cap stays. The coverage-gap row's "Create agent" goes to a deterministic target or folds into D3-3; it never becomes a second CTA row (A2) |
| D3-5 | Posture export: CSV of stages and KPIs from the posture payload; PDF later | reporting without AI | small (CSV) | "Export CSV" is an outline button on the right side of the lifecycle header, never filled (page-title-actions order rule) |
| D3-4 | Deterministic activity feed for `live_activity` in CE: last syncs, platform registrations and health changes, validation results | replaces an empty card with real events | small (front only: the posture and the registrations already carry every event) | rows reuse the compact run-row layout of Live activity |
| D3-2 | Posture and coverage trend per stage over 30/90 days, from `/posture/history` (already stored daily) | the trend exists in data, the CE screen shows only a sparkline | medium | reuses the existing sparkline/chart component, no new chart library |
| D3-6 | Exposure counts by asset and criticality | needs an asset model (see `ctem-gap-assessment.md`): later, not in this program | large | none yet |

## 8. Plan of the next slices

1. This document (slice 2), reviewed by the UX Director with sections 6 and 7.
2. Slice 3, backend: B1 (`ai_disabled` status) and B2 after the lab check (`no_model` status), with the D5 tripwire extended (no CE read starts a run). No UI.
3. Slice 4, UI (UX gate): `useAiLockReason`, `AiLockedFooter` and `SampleBadge` (section 6) with the guard test, B3, D-1, D-2, M1, M2, M3, E1, C1 (including the briefing card's locked subtitle), with frontend tests of each lock reason, administrator and non-administrator. Scope lines:
   - the stage card `aiEnabled` (`CtemCommandCenter.tsx:741`) treats the `ai_disabled` and `no_model` statuses as off, not only `ee_locked` and `unavailable` (A1);
   - the Force sync icon button's raw `aria-label="Force sync"` (`CtemCommandCenter.tsx:860`) goes through `t_i18n` (A3);
   - the Fleet activity header "All" link uses `/flows/fleets`, never `/flows?tab=fleets`, which the router ignores (A4, tracked with #9170 B5); in CE the button is hidden (section 6);
   - in S-CE the Fleet activity card skips the fleet queries (`enabled: licensed`);
   - the cards that host `AiLockedFooter` in an `EmptyState` never set `requiresAi`: `EmptyState` hides its `children` when `requiresAi` is set and AI is off (`empty-state.tsx:110`), so the footer would vanish in `ai_off`;
   - `EnterpriseCtaButtons` gains the prop that leaves out "Contact sales" (section 6), with its unit test;
   - the "Turn AI on" link uses the settings route constant and the `AiEnablementCard` anchor hash (section 6); "Configure a model" uses the same constant rule for the models route #9211 moves;
   - `SampleBadge` renders the design-system `Chip` (`severity="neutral"`, the shipped "Sample" key), replaces the seven CTEM sites of section 6, and `SampleFrame` and `PlatformChart` use it; a guard test fails on any `t_i18n("sample")` (lowercase) or hand-styled "Sample" badge under `frontend/src/pages`.
   Slice 4 ships in three PRs: 4a (#9284) the resolver `useAiLockReason`, `AiLockedFooter`, the `EnterpriseCtaButtons` `withContactSales` prop, the `ai-enablement` anchor with `frontend/src/lib/settings-routes.ts` (`SETTINGS_AI_ENABLEMENT_PATH`, `SETTINGS_MODELS_PATH`), the briefing card footer (C1, its locked subtitle removed) and the stage card `aiEnabled` (A1); 4b (#9388) `SampleBadge` (`frontend/src/components/ui/sample-badge.tsx`), its guard (`sample-badge.test.tsx`: no `t_i18n("Sample")` or `t_i18n("sample")` anywhere under `frontend/src` outside the badge; the lowercase key is removed from the catalogs) and the seven sample sites, the two captions losing their "Sample" prefix since the badge carries it; 4c (#9407) B3, D-1, D-2, M1, M2, M3, E1, the stage detail and `/briefing` footers, A3 and the CTA guard test (`components/ui/enterprise-cta-guard.test.ts`). A4 was already on main (the header "All" uses `/flows/fleets`). In 4c the stage detail history card shows in every lock state (it is deterministic), and the coverage-gap row of `recommended_actions` opens the Discovery stage instead of "Create agent" when a lock reason is set.
4. Slice 5 and later: the D3 features in the order of section 7, one PR each. Slice 5 (#9425) is D3-1: when `useAiLockReason` returns a reason, the `kpi_agents` and `kpi_executions` slots render the two deterministic KPIs of `frontend/src/pages/xtm-one/ctem/locked-kpis.ts` (`LOCKED_KPIS`), with the present / sample logic and the deep link of the vertical KPIs. Known-exploited and Validation success already sit in the cyber strip, so the slots take the next headline metric of each vertical instead of repeating them: cyber Critical CVEs and ATT&CK exercised, FIMI Channels monitored and Open incidents, fraud Leaked credentials and Containment. With AI able to run, the strip is unchanged. Slice 6 (#9459) is D3-3: when no briefing authored the next steps, `deterministicNextActions` (`frontend/src/pages/xtm-one/ctem/next-actions.ts`) adds one row per OpenAEV expectation type with failed or partial results (Vulnerability and Human response included, most failures first, a reason per type) in place of the single Prevention plus Detection row, and one row per stage whose source is connected but gets no data, opening that stage. The six-row cap stays and the rows never show beside the briefing's steps. `rankNextActions` gives their card priority: under the KEV row (95) and above the coverage gap (80), the failed expectations from 90 and the stages with no data from 85, so a stage with no data is not cut by the cap behind the coverage gap. The stage name is the translated label of `CTEM_STAGE_CONFIG`, also in the "Connect {name}" reason. KEV past SLA is not computable today: the posture carries no exposure age. Slice 7 (#9477) is D3-5: an "Export CSV" outline button on the right of the Exposure lifecycle header (label hidden below the `@md` container width, the icon keeps the accessible name) downloads `ctem-posture-<vertical>-<YYYY-MM-DD>.csv`, built by `postureCsv` (`frontend/src/pages/xtm-one/ctem/posture-export.ts`) from the posture already loaded, no model and no endpoint. Long form, one value per row (`Section, Stage, Metric, Value, Unit`, headers in the UI language): the posture (score, band, stages covered, build time), per stage its score, band, covered, confidence, products present and every fact, then each headline KPI that has a value (a KPI the posture could not compute is left out, never written 0; `_pct` keys carry `%`). RFC 4180 with the formula-injection guard and the UTF-8 BOM of `frontend/src/lib/csv.ts`. The button is disabled while the cards show the sample preview. PDF stays later. Slice 8 (#9567) is D3-4: when no agent run exists, the Live activity card lists `deterministicActivity` (`frontend/src/pages/xtm-one/ctem/activity-feed.ts`), newest first, at most six rows, in the compact run-row layout (`ActivityFeed.tsx`): the posture sync (`generated_at`, stages covered), each connected product's last sync, each platform registration, each registration that stopped sending heartbeats (a red dot), and the OpenAEV validation results (expectations met of those with a result, partial counted as not met, read only while OpenAEV is connected). Sources are the posture and `/platform/registrations` already loaded: no model, no endpoint. A source with no time gives no row, the feed never invents one. The rows link nowhere (no locked page). The greyed demo rows and their Sample badge stay only when there is no event at all; the lock footer stays under the feed. Slice 9 (#9618) is D3-2, the last D3 slice: the History card of the stage page gets a 30 days / 90 days switch (`SegmentedFilter`, radio, small, 30 by default) and one line stating the stage score change over the period, last recorded day minus the first ("Stage score +N points since <date>", "unchanged", or a reason when the stage has no score on two recorded days, never 0). `historyWindow` and `seriesChange` (`frontend/src/pages/xtm-one/ctem/trend-window.ts`) read the days `GET /ctem-metrics/posture/history` already serves (90 days stored, one snapshot a day); the chart stays `TrendChart`, no new chart library, no model, no endpoint. The card shows in every state where the stage has real data, Community Edition and AI off included.
5. Slice 10 (#9660), found in the Community Edition + AI-off lab pass after the D3 list: `KBHealthWarnings` showed "Indexing stalled: no embedding provider configured" with "Configure AI Models" under the Enterprise upsell notice when documents were queued. On Community Edition a provider changes nothing, so the notice is gated on `useAgenticAIEnabled` like the reasoning-provider warning of the same component; the upsell notice stays the one call to action.

Rules kept: zero regression in S-EE (the briefing, the stage assessments and the dashboard automations as in 1.261102.0), zero automated quota in CE, every C1 widget (item 4a1d5ef7) ships its CE state under section 6. UI strings say "license" (A5).

## 9. Lab checklist (D5 acceptance)

- S-CE lab, administrator and non-administrator, AI on then AI off: every widget renders with no error and no empty dead end; one CTA per AI surface, the one the precedence of section 2 names (AI off: "Turn AI on" first; AI on: "Start free trial"); no refused call in the browser console on load (the Fleet activity card does not query `/fleets` or `/fleets/{id}/health`); after 30 minutes idle, `agent_runs` has no new row.
- S-CE-nollm lab (licensed, AI off; then AI on with no model): no refused call in the browser console on load (B1), no run started (B2).
- S-group-off: a reader whose group has AI off sees the group sentence on every AI surface, no button, administrator and non-administrator.
- S-EE: the briefing and the stage assessments generate as before; the hero sync line reads "Not synced yet" before the first sync.
- Widths 1440 and 1024, light and dark.
