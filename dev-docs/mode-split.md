# Mode split: generic AI in both modes, domain content per mode

Issue: #9533. Status: S0 to S6 delivered (S6: #9775). Read on main `72d6449b`, 7 Nov 2026.

## The rule

| Class | Rule |
|---|---|
| **Generic AI** | ROI and outcomes, AI adoption analytics, evaluations, persona improvement proposals, usage and quota views, fleet docs and design, agent and fleet tooling. Available in **both** modes through **one** code path. Never gated on `is_xtm_one`, `is_copilot_like`, `useIsXtmOne()` or the `xtmOne` nav / command key. |
| **Domain content** | Built-in catalogs (ROI units, roles, rates), seeded agents, fleets, skills, templates, starter prompts, examples, help copy, empty states. Differs per mode: **Copilot mode is corporate** (knowledge work, office functions), **XTM One mode is CTEM / cyber**. |
| **Licence** | Edition semantics stay per mode: Licensed / Community in Copilot mode, Enterprise / Community Edition in XTM One mode. A gate that changes what is sold is not moved without a product decision. |
| **Internal tooling** | Filigran's own tooling (`AGENTIC_CODING` labs) stays where it is and says so. |
| **Branding** | Product name, icons, manifest, email chrome. Per mode by definition. |

`ai_disabled` mode is neither: it is the mode in which no model call happens. A generic feature opened in both modes keeps `ai_disabled` closed where it makes model or vendor calls.

Constraints for every slice: no regression of an existing agent, intent, fleet, automation or setting in its mode; nothing seeded in Copilot mode starts a run or uses quota; opening a feature in a new mode never runs a model at page load; Sam's form and drawer rules (fields only, no new drawer).

## Slices

| Slice | Content | UI |
|---|---|---|
| S0 | This inventory | no |
| S1 | Corporate built-in ROI catalog in Copilot mode (sourced numbers from the Analyst, research:corporate-roi-baselines); CTEM catalog stays XTM One mode's built-in; custom units, overrides, rates by role, review, report and export identical | yes (built-in card) |
| S2 | AI Adoption page, its command, its settings section and its hourly sync in both modes | yes |
| S3 | Evaluations in both modes (flag condition, seed modes), persona improvement proposals in both modes | yes |
| S4 | Fleets settings section in both modes (#9582); General Assistant channel routing editable in both modes (#9709) | yes |
| S5 | Guard test: a generic AI route, nav entry or command gated on the mode fails, with an allowlist (domain content, licence, internal tooling, branding), each entry with its reason; done (#9766) | no |
| S6 | Copilot-mode content review: starter prompts, help copy and empty states with cyber wording shown in Copilot mode; done (#9775) | yes |

## Inventory

`Today` is the behaviour on main; `Target` is the behaviour after the slice; `keep` means the gate is correct and justified by its class.

### Generic AI (to open in both modes)

| Gate | What it gates | Today | Target | Slice |
|---|---|---|---|---|
| `frontend/src/components/layout/nav-model.ts:278` `(f) => !f.xtmOne && f.aiAdoption` | AI Adoption nav entry | both (S2) | `f.aiAdoption` | S2, done |
| `frontend/src/pages/AdminObjects.commands.ts:30` `whenNot: ["xtmOne"]` | AI Adoption command palette entry | both (S2) | drop `whenNot` | S2, done |
| `frontend/src/pages/Settings.tsx:2480` `!isXtmOne && <AiAdoptionConfigSection />` | AI adoption settings (observability) | both (S2) | both | S2, done |
| `backend/app/services/autonomous.py:2482` `is_copilot_like and _ai_on` | Hourly AI adoption metrics sync (vendor calls) | Copilot, dev and `xtm_one`, never `ai_disabled` (S2) | Copilot, dev and `xtm_one`, never `ai_disabled` | S2, done |
| `frontend/src/App.tsx:724` route `/ai-adoption` | Page, on `features.ai_adoption` | both (licence) | keep (no mode gate) | - |
| `backend/app/api/v1/platform.py:1801`, `:1866` `ai_adoption=` | Feature flag of the page | both, on licence | keep | - |
| `backend/app/core/feature_flags.py:140` `"EVALUATION": _copilot_like` | Evaluations (route, nav, API); read through the surface gate `evaluation_enabled()` in `backend/app/services/evaluation/availability.py:3-11` (Platform Health's own evaluation already runs whatever the mode) | Copilot / dev | no mode condition (`ai_disabled` closed); update `dev-docs/evaluation-feature-flag.md`, `evaluations.md`, `health-evaluation.md`, `agent-placebo-evaluation.md` | S3, done (#9666): `"EVALUATION": _ai_mode` (`config.AI_MODES`) |
| `backend/app/evaluations/seed.py:78` `_SEED_MODES = COPILOT_LIKE_MODES` | Seeded datasets and evaluations | Copilot / dev | both, content per mode (corporate in Copilot, CTEM in XTM One), seeded without schedule so nothing runs | S3, done (#9666): `_SEED_MODES = AI_MODES`, no schedule; the one benchmark (OCR, fictional CTI documents) is shared, a corporate benchmark for Copilot mode is S6 content |
| `frontend/src/pages/agent-detail/PersonaImprovementsCard.tsx:203` `!!data?.can_propose && !isXtmOne` | Ask for a persona rewrite | Copilot only | `!!data?.can_propose` (the backend already decides: `improvement_proposals.py:342-348`, on `evaluation_enabled()` and write rights) | S3, done (#9666) |
| `frontend/src/pages/Settings.tsx:415` `key === "fleets" ... ? isXtmOne` | Settings > Automations > Fleets | both (S4) | both | S4, done (#9582, d2e78ace): the filter keeps only `ctem-agents` XTM One only |
| `frontend/src/pages/settings/settings-routes.ts:58-62` | Automations fallback section | `fleets` in XTM One, `agent-runs` in Copilot; `fleets` allowed in both (S4) | keep the fallback, allow `fleets` in both | S4, done (#9582) |
| `frontend/src/pages/settings/GeneralAssistantSection.tsx:950-951` | Edit of the General Assistant's channel routing | both (S4) | both: the backend has no mode condition (`channels/router.py` `_ga_allows_channel`, `api/v1/settings.py` `channel_config` write); in XTM One mode the linked CTEM Assistant (`services/provisioning.py` `wire_default_agents`) answers on the enabled channels | S4, done (#9709) |
| `frontend/src/pages/Flows.tsx:42` Fleets tab, `App.tsx:652` `/flows/fleets/:fleetId` | Fleet list and fleet page (About, docs, design) | both | keep | - |
| `backend/app/api/v1/roi.py`, ROI settings (Units of work, Rates by role, #9458) | ROI views, overrides, report, export | both | keep | - |

### Domain content (per mode, correct unless noted)

| Gate | What it gates | Today | Target | Slice |
|---|---|---|---|---|
| `backend/app/services/roi.py` `BASELINES`, `HOURLY_RATES` | Built-in ROI catalog | security only in **both** modes | CTEM in XTM One mode, corporate catalog in Copilot mode | S1, #9550: `CTEM_BASELINES` / `CORPORATE_BASELINES` (24 units, 9 functions) and `CTEM_HOURLY_RATES` / `CORPORATE_HOURLY_RATES` (10 roles, USD and EUR, sourced); `builtin_baselines` / `builtin_rates` / `fallback_rate_role` pick the mode's for listings and the report fallback; lookups keep the union so no recorded run loses its figures |
| `backend/app/services/roi_report.py:55-62` | Report title, export filename | per mode | keep | - |
| `frontend/src/components/fleets/fleet-documentation.ts` `SECTION_HELP`, read by `FleetAboutView.tsx` | Fleet documentation editor help examples | CTEM examples (threat report, SOC, indicators, OpenCTI) in **both** modes | CTEM in XTM One mode, corporate (`CORPORATE_SECTION_HELP`: support tickets, shared mailbox) in Copilot mode | S6, done (#9775); the editor and its help lines removed by #9768 S2 (#9795): the documentation is AI generated only |
| `frontend/src/components/roi/OutcomesCard.tsx` unit of work help | ROI unit field info and helper | CVE assessed, threat report triaged in **both** modes | CTEM in XTM One mode; Support ticket resolved, email triaged and answered (built-in corporate units of S1) in Copilot mode | S6, done (#9775) |
| `frontend/src/pages/Value.tsx` page subtitle | Value page subtitle (#9836 R5b) | per mode | keep: "Value of your exposure management" in XTM One mode, "Value of your agents" in Copilot mode; figures, scope, window and tables are the same code in both modes | R5b (#10020) |
| `frontend/src/pages/NewAgentWizard.tsx` Skills step | Skill examples | detection rules, STIX bundles in both modes | one mode-neutral line (weekly report, data conversion, house style) | S6, done (#9775) |
| `frontend/src/pages/Profile.tsx:433`, `Settings.tsx:415` `ctem-agents` | CTEM stage agents card and section | XTM One only | keep | - |
| `frontend/src/App.tsx:369` `XtmOneRoute` (`/ctem/:stage`, `/briefing`), `backend/app/api/v1/ctem_metrics.py:95` | CTEM lifecycle, exposure briefing | XTM One only | keep | - |
| `frontend/src/App.tsx:349`, `components/companion/CompanionToday.tsx:63` | Home dashboard | CTEM dashboard / general dashboard | keep | - |
| `backend/app/agents/seed.py:1330` `seed_matches_platform`, `:746`, `:1133`, `:1588`, `:1908` `_seed_xtm`; `fleet_seed.py:54`; `cti_skills.py:20`; `cti_templates.py:32`; `templates.py:8`; `services/template_container_seed.py:1580` | Seeded agents, fleets, skills, templates, decks | per `platform_targets` | keep | - |
| `backend/app/api/v1/auth.py:578`, `:679`, `api/v1/users.py:273`, `main.py:1882`, `frontend/src/pages/Chat.tsx:902` | Default chat agent = CTEM Assistant | XTM One only | keep | - |
| `backend/app/services/builtin_tools/prompting.py:101` | Active CTEM vertical in the system prompt | XTM One only | keep | - |
| `backend/app/main.py:2868` | OpenAPI description | per mode | keep | - |
| `backend/app/api/v1/integration_catalog.py:40`, `frontend/src/components/ui/platform-mcp-server-list.tsx:42`, `platform-mcp-user-section.tsx:93`, `settings/MCPEndpointSection.tsx:97` | OpenCTI / OpenAEV bridges listed before they are connected | XTM One only | keep | - |
| `frontend/src/pages/settings/ParametersSection.tsx:139` | XTM Hub registration card | XTM One only | keep | - |
| `frontend/src/pages/settings/ParametersSection.tsx` `ResetOnboardingCard`, `backend/app/api/v1/organization_context.py` `reset_setup_prompt` | Reset onboarding: the guided setup proposed again to administrators | XTM One only (Copilot mode has no guided setup; the API answers 409) | keep | #1215 |
| `frontend/src/pages/settings/HealthModuleThresholdsBlock.tsx:62` | Platform Health module thresholds (OpenCTI, OpenAEV); the health engine and the other blocks run in both modes | XTM One only (Copilot mode has no module) | keep | #101 S10c (#745) |
| `frontend/src/pages/settings/HealthModuleAnalystBlock.tsx:79` | Platform Health Module Analyst spend limits (ceilings and spend of the agentic module checks); the other Spend limits blocks run in both modes | XTM One only (Copilot mode has no module) | keep | #101 S14d-2 (#1025) |
| `frontend/src/pages/settings/HealthRunAsBlock.tsx:110` | Platform Health "Run checks as" user: the identity the agentic module checks read OpenCTI, OpenAEV and OpenCRQ as; the other Health checks blocks run in both modes | XTM One only (Copilot mode has no module) | keep | #1351 |
| `frontend/src/components/layout/Sidebar.tsx`, `use-nav-group.ts`, `lib/commands/CommandsProvider.tsx` `intents: xtmOne`; `App.tsx` `/intents` behind `XtmOneRoute` | Intents (agent routing for requests of OpenCTI and OpenAEV): sidebar row, Orchestration hub card, Browse intents command, page | XTM One only, administrators (Sam, 19 Nov 2026: not used in Copilot mode) | keep | #445 |
| `frontend/src/components/layout/Sidebar.tsx`, `use-nav-group.ts` `ctemCases: xtmOne`; `App.tsx` `/ctem/cases` behind `XtmOneRoute` | The CTEM group (the records of the CTEM loop): a top-level sidebar group between Chat and Orchestration holding Cases (Assets joins it with its page), palette destinations "CTEM > Cases", open with AI off; the group is dropped when it has no visible row | XTM One only, every user (Sam, 22 Nov 2026) | keep | #1135 |
| `frontend/src/components/layout/OrganizationSetupPrompt.tsx:37`, `frontend/src/components/fleets/CtemLoopSetupEntry.tsx`, `frontend/src/components/fleets/FleetSetupDialog.tsx`, `frontend/src/pages/xtm-one/ctem/CaseWorkCard.tsx`, `backend/app/services/organization_setup_prompt.py` | First-connection setup proposal, versioned (`SETUP_VERSION` `ctem-loop-1`: at install and once after an upgrade carrying a new version; each step saved by `PUT /organization-context/setup-prompt/steps`) | XTM One only (the server answers `propose` in XTM One mode only, and to administrators only; the "Set up the CTEM loop" entry shows to administrators in XTM One mode only) | keep | #9173 S3 (#10209), #1087 |

### Licence and edition (per mode, keep)

| Gate | What it gates |
|---|---|
| `backend/app/api/deps.py:762`, `services/channel_notices.py:866`, `services/ai_hold.py:64` | Refusal wording: license vs Enterprise Edition |
| `backend/app/api/v1/platform.py:974`, `:1075`, `:1710`, `:6193`; `services/telemetry.py:438`; `services/agent_failure_pool.py:208` | Edition resolution |
| `backend/app/services/quota.py` (`:1273`, `:1405`, `:1534`, `:1612`, `:1651`, `:2064`, `:2117`, `:2316`, `:4673`, `:4820`, `:4830`); `api/v1/settings.py:7767` | Agentic quota source (licence, EE platform, global) |
| `frontend/src/components/ui/enterprise-upsell.tsx`, `license-gate-dialog.tsx`, `held-badge.tsx`, `lib/held.ts`, `hooks/useEnterpriseUpsell.ts`, `components/ui/a2a-unavailable-notice.tsx`, `pages/settings/QuotasSection.tsx` (edition cards) | Edition copy and badges |
| `backend/app/main.py:3609` | Licence middleware (Copilot / dev only; `ai_disabled` needs none) |
| `backend/app/core/security_controls.py:143-166`, `core/security_options.py:239-248`, `core/config.py:3903-3907`, `core/sso_helpers.py:157` | Code interpreter, custom tools, custom variables and self-registration defaults per mode (security boundary) |
| `backend/app/api/v1/agents.py:1569`, `api/v1/helpers.py:2108`, `:2163`; `frontend/src/pages/AgentDetail.tsx:118-128`, `agent-detail/ConfigTab.tsx:1478`, `agent-detail/config/ToolingSection.tsx`, `assignment-detail/edit-rights.ts:37`, `lib/handover-edge-edit.ts:68`, `hooks/useHandoverEdgeEditing.tsx` | Built-in agents are product-managed (read-only) in XTM One mode |
| **LLM proxy**: `backend/app/main.py:4463`, `services/api_key_access.py:168`, `api/v1/metrics.py:330`, `api/v1/groups.py:529`, `api/v1/users.py:1842`; `frontend/src/pages/UserDetail.tsx:1227-1276`, `GroupDetail.tsx:661-694`, `Profile.tsx:1397`, `settings/QuotasSection.tsx:520`, `:1179`, `:1322` | The OpenAI-compatible LLM proxy is mounted in Copilot / dev mode only, and its usage and quota rows follow it. Opening it in XTM One mode changes what is sold: kept, recommendation sent to the Assistant (keep the proxy a Copilot product; the rows follow the proxy, so no usage view is missing in XTM One mode) |
| `backend/app/main.py:1378` | Built-in embedding server default (XTM One ships one, Copilot is configured by the administrator) |

### Internal tooling (keep)

| Gate | What it gates |
|---|---|
| `backend/app/core/feature_flags.py:140` `"AGENTIC_CODING": _copilot_like` | Filigran's agentic coding labs, never an XTM One feature |

### Branding (keep)

`services/branding.py:183`, `:781`; `services/email.py:364`, `:378`; `services/mobile_push.py:237`; `api/v1/platform.py:1624`, `:1645`; `frontend/src/hooks/usePlatformConfig.tsx:240-256`, `:326-327`; `components/layout/AboutDialog.tsx:18`; (the former `lib/concierge.ts` mode-dependent name is gone: Platform Health has one name in every mode, #101 S1).

## Guard (S5)

Done in #9766. Two tests fail the build when a generic AI capability is gated on the mode again:

- `frontend/src/lib/mode-split-guard.test.ts` (`yarn test`): no nav entry (`nav-model.ts`) and no command (`*.commands.ts`) reads `xtmOne`; no generic AI route of `App.tsx` (`/ai-adoption`, `/evaluations`, `/roi`, `/flows`, `/concierge`) sits behind `XtmOneRoute` (`/intents` is domain content since #445); a source file reads the mode (`useIsXtmOne()`, `isXtmOneMode()`, a `platform_mode` comparison) only when `MODE_READERS` names it with its class (domain content, licence and edition, built-in agents, branding, plumbing) and its reason from this document. The list only shrinks: a file that no longer reads the mode fails the stale test until it leaves the list.
- `backend/tests/test_mode_split_guard.py`: every `FLAG_CONDITIONS` entry answers the same in `xtm_one` and `copilot` mode, except those in `MODE_GATED_FLAGS` (only `AGENTIC_CODING`, internal tooling); the evaluations seed covers every mode of `config.AI_MODES`.
