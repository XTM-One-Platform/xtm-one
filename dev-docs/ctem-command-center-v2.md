# CTEM command center v2: design and parity inventory

Issue #10277, slice C2 of the CTEM fleets program (epic #8975). Read on main
`3b76da6a`. This document is step (a): the design the UX Director reviews
before any code. The left navigation consolidation (#9301) shares section 3,
the information architecture.

## 1. Why a v2

The XTM One mode home (`frontend/src/pages/xtm-one/CtemCommandCenter.tsx`,
1,593 lines, 22 grid widgets in `ctem/dashboard-layout.ts`) is about four
screens tall at 1440x900 and answers "what is my posture" with raw volumes
(CVEs, observables, findings) before it answers "what do I do now". Defects
read on main:

| # | Defect | Where |
|---|--------|-------|
| D1 | The page is long: hero, 6 KPI tiles, briefing, lifecycle header, 6 stage cards, actions, live activity, digests, fleet activity, fleet timeline, coverage, stats (default layout 111 grid rows) | `CTEM_WIDGETS` |
| D2 | Governance & Risk draws hard-coded sample figures (score 72, $4.2M ALE, 48 policies, "-12% QoQ") beside live data, whatever OpenCRQ's state | `CtemCommandCenter.tsx` `stage_governance` |
| D3 | Nothing ranks what to do first across sources: Recommended actions caps at 6 rows and drops the deterministic rows as soon as a briefing exists | `recs` build, `briefingSteps.length === 0` guards |
| D4 | The fleet timeline is a vertical list; Sam asked for horizontal timelines | `FleetTimelineCard.tsx` |
| D5 | Work items and approvals waiting for a person are not on the page (only the notice region counts them) | `DashboardNotices`, `useWorkItemsWaitingForMe` |
| D6 | The value of the work already done (runs, hours, net value) is only in the Fleet activity card and only for fleets | `FleetActivityCard.tsx` |
| D7 | Stage labels come from the server label, so es / ja show English stage names (advisory A4 of #9237) | `CoverageMatrixCard`, stage cards |
| D8 | Arbitrary `ring-[var(--border-elevation-subtle)]` classes and a 1.62:1 optional-cell ring in light (A1, A2 of #9237); header rows 21 px vs 28 px (A3) | `CoverageMatrixCard.tsx:38,102` |

Fixed already on main and kept: fleet tiles use "Not set up / Partially on /
On / Paused" (#9570), the CTEM fleet view is scoped to CTEM fleets with a
"View all fleets" link, the fleets invitation is dismissible per person
(#9566).

## 2. Principles (fixed by the brief)

1. **Fleets off is the primary case.** Enterprise Edition with every CTEM
   fleet "Not set up" must be a complete page. Every section has a fleets-off
   source (column F of section 6); a section with none is not drawn. No tile
   reads 0 h / $0 or "Healthy" because a fleet is off.
2. **Five seconds to understand.** The first screen at 1440x900 holds the
   value band and the top of Act now. The landing page is at most about two
   screens; everything else is one click away.
3. **Zero feature lost.** Every widget, figure, tooltip, link, action, the
   force sync, the CSV export, the customisable grid and its saved layouts
   keep a place (section 6). A test asserts every current widget id and deep
   link still resolves.
4. **No model call and no run at page load or when a widget opens.** Text
   written by an agent comes from runs that already happened (the briefing, the
   stage assessments). The auto-generation of a missing briefing on first open
   of a vertical stays as today (quota exempt, `assignment_engine._run_is_quota_exempt`).
   Nothing on the page enables a fleet, starts a run or uses quota beyond what
   main does now (existing test kept).
5. **Every number traceable**: tooltip with source and period, a click to
   the module object or the stage page.
6. **No sample figure on a connected instance.** A sample is allowed only in
   the CE / AI-off preview and the not-connected product state, labelled.
7. No new drawer (Sam, 7 Nov 2026): a detail opens a page with its own URL or
   a centred dialog.
8. XTM One mode only; Copilot mode unchanged. Light and dark, en / fr / es /
   ja, 1440 and 1024, accessible names, text >= 12 px, controls 36 px.

## 3. Information architecture

```
Home (XTM One mode, route /dashboard)               <- this page
├── 1 Value band                (first screen)
├── 2 Act now                   (first screen, top 5 rows)
├── 3 Done for you              (horizontal timeline, 7 d / 30 d)
├── 4 Lifecycle band            (6 stage chips) ──> /ctem/:stage  (stage page, every figure)
├── 5 Stats and coverage        (4 compact widgets) ──> /ctem/:stage, /value, module
└── 6 Fleets row                (one line per CTEM fleet) ──> /flows/fleets/:id, setup wizard
Customize (grid editor, per vertical, unchanged) ── restores any v1 widget
/briefing      full exposure briefing (unchanged)
/value         value page (ROI, #9836), reached from the value band
/operations    work items, approvals, executions (Act now "All" links)
```

Navigation: the nav half of this information architecture is #9301 (left
navigation consolidation, body revision 2); the names below are shared and
kept in both. The home row of the nav is "Home" (lit on `/dashboard`,
`/briefing` and `/ctem/*`), the page title stays the greeting and the stage
pages go "Back to Command Center"; studio dashboards stay "Dashboards"; the
"Fleets" row sits at `/flows/fleets` under Agents and the fleets row of this
page links to it; "Value" moves under Work and the value band keeps a direct
link to `/value`; the admin analytics family is "Observability" (not
"Insights"), so it does not compete with the stats of this page.

## 4. The six sections

### 4.1 Value band (first screen)

One band, full width, about 180 px:

- **Headline sentence**: the briefing's `headline` when a ready briefing
  exists ("By Exposure Briefing Analyst, 2 d ago"); otherwise a deterministic
  sentence from the posture ("Exposure posture 64/100 across 4 of 5 stages,
  up 6 points in 30 days, 12 known-exploited exposures").
- **Posture gauge** (semi-circle, 0-100, band colour) with the 90-day
  sparkline and the delta vs start of period; click opens the existing
  "How the Exposure Posture is computed" dialog. Last synced and the force
  sync button sit under it (unchanged behaviour, `useRefreshCtemPosture`;
  a 36 px icon button with a translated accessible name).
- The headline and Act now quote the same figures from the same read (round 1
  A2): "12 known-exploited exposures open, 4 on internet-facing assets" in
  both places, never 12 in one and 4 in the other.
- **Risk tile**: OpenCRQ annualized loss exposure and its trend when an
  OpenCRQ instance is registered; otherwise known-exploited exposures on
  connected sources (`headline.kev_count`). Never the sample $4.2M (D2).
- **Value tile**: value of the work done on the platform this period from
  `GET /roi?window=30d` (`useValueReport("platform"|"mine")`, #9836 R5b):
  hours saved and net value as low / likely / high, "estimated" until the
  organization sets its rates. Fleets off: the figures come from built-in
  automations, chat and agents (37242387); with no record at all the tile
  shows the count of agentic executions (30 d) and the link "How value is
  computed" (`/value/methodology`), never 0 h / $0. When the organization
  profile has no currency or loaded hourly rate (the case today on many
  instances), the tile shows the USD default rate labelled "Default rate"
  beside "estimated" (round 1 A7).
- **Done vs waiting**: "N actions done automatically, M waiting for you"
  (runs completed with an outcome in the period vs the Act now items owned by
  the viewer). Click scrolls to Act now / Done for you.
- Product pills (OpenCTI, OpenAEV, OpenCRQ, XTM Hub, extra registrations),
  the edition badge and the vertical switcher sit on the band's top line.

### 4.2 Act now (first screen, top rows)

One ranked queue, 5 rows on the first screen, "Show all (N)" expands in place
to every row of the queue (N, at most 20; round 2 A2); no "All work" link, since no page lists these rows
and the work-items list was empty under a full card (#11213). Sources, merged and
deduplicated by target object:

| Source | Fleets off | Row action |
|--------|-----------|-----------|
| Briefing `next_steps` (priority high / medium / low) | yes (built-in analyst) | Open briefing |
| Stage assessment `recommended_action` of each covered stage | yes (built-in analysts) | Open stage |
| Module facts: KEV exposures, failed OpenAEV expectations by type, stages connected with no data, detection coverage < 60 % (all read today); OpenCRQ scenarios over threshold once the posture carries them (not read on main) | yes | Open the in-app stage page (never an unfiltered module list, #11213), Ask AI |
| Work items waiting for the viewer (`/work-items/waiting-for-me`) | yes | Open, Answer |
| Tool approvals pending | yes | Approve, Open |
| Fleet work items (CTEM fleets) | only when on | Open, Assign |
| Connect a product (blind spot), wire in the stack (admin, < 4 integrations) | yes | Connect, Add integration |

v1 hides the deterministic rows once a briefing exists (D3); v2 keeps both
and dedupes: a briefing step citing the same object as a module fact replaces
it. Ranking: severity of the source (KEV and failed prevention first), owner
is the viewer, age, then the briefing priority. Each row: icon of its source,
title, one line "why it matters" (organization context when filled: crown
jewels, PIRs, loss threshold; the profile fields are empty on many instances,
so the line falls back to the module fact), the source and period on hover,
one primary action (a 36 px outline button; the page has no filled primary
button, round 1 A3). "Ask your CTEM agent" stays the last row when AI can
run; "Show all (N)" is a 36 px text button under the rows and becomes "Show
fewer" once expanded (round 1 A6; the expanded state is drawn in the states
mockup).

#11213 (slice 1): each row shows its one-line why under the title and no
source label ("Module" on every rule row said nothing); the failed
expectations of one stage that quote the same count read as one phrase
("Detection and Prevention: 232 failed expectations"). A module fact or a
setup row carries Ask AI: it opens `/chat` with the row's title, why, stage
and scope in the composer (`actNowAskAiPrompt`, never sent on its own) and
asks for a step-by-step fix path; not on a work item or an approval, nor when
AI is locked. A row says whether it is `tailored` (filtered and ranked by the
organization profile); while no module fact is, the card shows the info notice
"These actions are generic" with "Set up organization context" (or "Review"
once a profile exists) to `/settings/organization?setup=1` for an
administrator.

#11213 (slice 2): the KEV row is tailored when the primary organization
profile lists a `tech_stack`. `build_posture_for_user` (cyber) reads the
profile in its DB phase (`_organization_software` since #1495, the Software entities plus the profile rows no entity carries), and in the network phase
`_octi_kev_tailored` resolves the OpenCTI `Software` observables matching the
stack (CPE equal, else product name with the vendor when both name one; at
most 500) and counts the KEV vulnerabilities related to them
(`regardingOf` relationship `has`). The posture headline carries
`kev_tailored_count` and `kev_tailored_products` (at most 3, in profile order);
both are null without a tech stack or when OpenCTI cannot answer, and the row
then keeps `kev_count`. A tailored row counts `kev_tailored_count`, its why
names the products ("Known-exploited and affects {products} in your tech
stack") and it is `tailored`, so the generic notice drops. Primary assets and
PIRs do not rank rows yet.

#11324: the notice is `ctem/GenericContextNotice.tsx`, shared by every widget
whose content ignores the organization context (title and two bodies per
widget, the admin call to action "Set up" or "Review organization context").
Audit of the default layout, and where it shows:

| widget | state | notice when | otherwise |
|---|---|---|---|
| hero | tenant posture | never | |
| briefing | AI, the analyst prompt calls `get_organization_context` | no profile saved (`useNoOrganizationProfile`), ready briefing only | |
| act_now | module facts | `actNowIsGeneric` | KEV tailored by tech stack |
| lifecycle_band | tenant posture, chips open `/ctem/<stage>` | never | |
| stats_coverage | KEV module-wide | cyber, OpenCTI connected, `kev_tailored_count` null | Findings open `/ctem/<stage>` (no module list) |
| done_for_you, fleets_row | tenant runs and fleets | never | |

### 4.3 Done for you (horizontal timeline)

A horizontal time axis (7 d default, 30 d toggle), three lanes:

- **Modules**: OpenCTI / OpenAEV / OpenCRQ events the page already holds
  (syncs, registrations, health, validation: `deterministicActivity`).
- **Automations**: built-in automation runs and digests
  (`useAllAssignmentRuns`, `stats.recent_digests`): briefing, stage
  assessments, posture build, decay.
- **Fleets**: fleet runs (`/fleets/{id}/activity`) only when a CTEM fleet is on;
  the lane is not drawn otherwise.

The third lane is **Outcomes** (ticket, test, re-test passed, risk updated,
briefing refreshed), drawn as dots like the others, not as a line of text.
A dot per run or event; more than three in one day become one cluster badge
with the count (click expands that day). A dashed line links a module event
to the run it started and the run to its outcome when the run records it
(C1 slice 1 handover rule). A legend row under the title names the dot
colours (module event, automation run, outcome, needs attention) and the
dashed link ("started"). Hover gives a card with the run name, agent, time
and outcome; click opens `/operations/executions/:id`. The 7 d / 30 d toggle is a 32 px
segmented control. The card is sized to its lanes (no empty band).
"All executions" opens `/operations/executions`. Replaces Live Activity,
Latest Results & Digests and Fleet timeline on the first two screens; their
lists stay in the grid catalog (section 6).

### 4.4 Lifecycle band

One row of six chips (Scoping, Discovery, Prioritization, Validation,
Remediation, Governance & Risk), each: localized stage name (by key, D7),
score in its band colour, 30-day sparkline, one key figure (the stage's first
fact), an accent dot with an accessible name when an assessment action is
pending. Click opens
`/ctem/:stage`, which already holds every figure, the AI assessment, the score
trend, the owner, the module links and the stage agents; v2 adds to that page
the facts and viz the v1 card shows that the page does not (parity rows S1-S9
of section 6). The stage page, like every CTEM page, draws the CTEM header
band (`ctem/CtemPageHeader.tsx`, #11660): the shared `LocationBackButton`
"Back to Command Center" first, at the content edge above the band and outside
its border, the same component, size and spacing (`space-y-1`) as the back link
of `EntityPageHeader` (Sam, 22 Nov 2026: no CTEM-only caption link inside the
band); inside the band the breadcrumb line holds only the current page
("Stage 1 of 5") and its extras (vertical chip, analyst), then the title with
its "i"; the stat cells (STAGE SCORE, ORGANIZATION) stretch over the band's
content height, so their hairlines run top to bottom and their figures centre
on the title's axis, at the narrow, medium and wide layouts. Captions use the
12 px caption class (round 1 A4).
A stage with no source connected shows "Connect <module>" instead of a score
(never a greyed sample on a connected instance). Governance & Risk: live
OpenCRQ figures when registered, else "OpenCRQ not connected" with Connect /
Soon, no hard-coded numbers (D2). The CSV export button of the lifecycle
header moves to the band's title line.

### 4.5 Stats and coverage

Four compact widgets in one row (1440) or a 2x2 grid (1024):

- **Coverage heat map**: stages x modules (`coverageMatrix`), cell colour by
  score band, design-system border tokens (A1, A2 of #9237), header row
  28 px (A3). Row labels are the localized stage names by key, never
  abbreviations (round 2 B3). An empty cell has a dashed border on the
  secondary text token (>= 3:1 in both themes) and every cell carries its band
  as an accessible name; a legend row (good, watch, at risk, no data) names
  the bands, so the band is never told by colour alone (round 2 A1).
- **Exposure stats**: posture and stage deltas over the recorded period
  (`exposureStats`), improved vs declined as two bars, and the stage deltas
  line built from the localized stage names and the period (round 2 B3).
- **Findings by severity**: the vertical's three headline KPIs (cyber: KEV,
  criticals, detection coverage; FIMI and fraud their own), each opening
  its in-app stage page (#11324; KEV counts the tech stack's when tailored).
- **ATT&CK / ATLAS tested vs untested**: exercised coverage and validation
  success from the OpenAEV headline fields; "Connect OpenAEV" otherwise.

Each widget opens its detail (stage page or module). The section is drawn in
the round 2 mockups; the whole landing page measures about 1,270 px at 1440
(fleets off; about 1,470 px with the fleets rows) and about 2,040 px at 1024
where the tiles and chips wrap to two columns (section 7): inside the
two-screen budget at 1440, about 2.6 viewports of 768 px at 1024.

### 4.6 Fleets row

One compact row per CTEM fleet (built-in and custom fleets declared CTEM):
name, opt-in state (Not set up / Partially on / On / Paused), last outcome,
value produced (7 d) when on, About link, Set up (admin). Fleets off: ONE
line "Fleets add agentic work to this page" with each fleet's quota estimate
per month (existing `About {min} to {max} runs per month`) and Set up (an
outline button, round 1 A3), closed per person by a 36 px icon button named
"Close the fleets invitation" (`ctem_fleet_invite_dismissed`), back when a
module or fleet appears. At 1024 the per-fleet line wraps under the title; it
is never dropped (round 1 B3). With fleets on, a row's value ("11 h saved,
7 d") wraps onto its own line under the name at 1024; no column is hidden at
any width (round 2 B2). No banner, no empty tile, no overlay.

The fleets are each person's choice (#11464): every row (and every line of the
invitation) can be hidden, "Choose fleets" picks any fleet the person can
read, built-in or their own, and "Restore the built-in fleets" goes back to
the default. Stored in `users.ctem_dashboard_fleet_ids` (null = the CTEM
fleets, a list = those fleets in that order, filtered on write to fleets the
person may read); the runs, findings and hours saved of the page follow it.

### 4.7 Lifecycle on top, two scores, organization context (#11464)

- The lifecycle band sits right under the hero and is the one drawing of the
  stages: the hero's ribbon of circles is gone; moving particles run along the
  band's tiles (decorative, hidden under reduced motion).
- Every score is shown twice, labelled: **Quantitative** (the metric score of
  `ctem_scoring.py`) and **Qualitative** (read against the organization
  context of Settings). The hero gauge draws the qualitative one as its inner
  ring and both as a labelled pair; each lifecycle tile and each stage page
  show the pair.
- Qualitative formula (`services/ctem_context_scoring.py`, pinned by
  `tests/test_ctem_context_scoring.py`): each stage reads a set of context
  fields (scoping: primary assets, tech stack, sector, regions; discovery: PIRs,
  sector, regions; prioritization: tech stack, primary assets, PIRs; validation:
  primary assets, tech stack, AI targets; remediation: primary assets, approval
  threshold, ticketing). `C` is the share filled. The evidence `E` is the
  quantitative score `Q`, except Prioritization with a tech stack:
  `E = (Q + T) / 2`, `T = max(0, 100 - 20 x known-exploited CVEs in the
  stack)`. `qualitative = round(E x (0.4 + 0.6 x C))`; no profile or `C = 0`
  gives none. The posture's qualitative score is the weighted average of the
  stages that have one, with the vertical's weights.
- The organization context of Settings is the only company context: the
  Company Context knowledge base is no longer read by the CTEM analysts nor
  shown on any page. Every CTEM page and the briefing draw
  `CompanyContextNotice`: missing (set it up), partial (the empty fields
  named), complete (one quiet line). The state is computed on every
  `GET /ctem-metrics/posture` read, never cached.

### 4.8 Nothing permanent on the home dashboard (#11586)

- **Rule**: the command center carries no permanent, non-dismissible
  notification or block. A notice there has a close control, and the close is
  stored on the user (`users.organization_context_notice_dismissed`, written
  through `PUT /settings/preferences`, served by `/auth/me`), never in
  localStorage, so it holds on every device. A brief keeps its notice: it
  explains the quality of the analysis.
- **Organization context incomplete** (`CompanyContextNotice`,
  `placement="dashboard"`): administrators only (the people who can fill
  Settings > Organization). The close stores the missing fields seen then;
  the notice comes back only when a field outside them becomes missing.
  Settings > Organization keeps showing completeness.
- **Briefs** (`placement="brief"`: every stage page, the exposure briefing):
  every reader sees an info notice naming the missing fields, never
  dismissible; the Settings link for administrators only. Agent-written briefs
  and reports get the same line from the context injection
  (`organization_context.brief_notice_instruction`, in the basic summary of
  every turn and in the `get_organization_context` reading): one info line,
  "Organization context incomplete: missing <fields>. The analysis would be
  sharper with them.", none when the profile is complete.
- **Audit of the home notices**:

  | Notice | Dismissible | Why |
  |---|---|---|
  | Organization context incomplete | yes, per user, back on a new missing field | #11586 |
  | "Generic" qualifier of Act now, Briefing, Stats and coverage (`GenericContextNotice`) | yes, per user (`generic:<id>`) | #11586 |
  | Fleets invitation | yes, per user (#9566) | |
  | Shared OAuth credentials | yes, per user | |
  | Work items waiting for you | no | the reader's own to-do: it goes when the items are answered |
  | Model, embedding and knowledge base health (`ModelConfigWarnings`, `KBHealthWarnings`) | no | a broken platform state that stops the analysis; it goes when fixed |
  | Enterprise upsell (Community Edition) | no | the edition locks the page's AI; it goes with an Enterprise licence |

## 5. States

| State | Value band | Act now | Done for you | Lifecycle | Stats | Fleets row |
|-------|-----------|---------|--------------|-----------|-------|-----------|
| **EE, fleets off (primary)** | posture, risk, value of built-in automations / chat / agents | briefing, assessments, module facts, work items, approvals | modules + automations lanes | live | live | one invitation line, dismissible |
| EE, one fleet on | + fleet value | + that fleet's work items | + fleets lane | live | live | one row on, invitation for the rest |
| EE, all on | full | full | three lanes | live | live | rows |
| CE / AI off | posture and risk; value tile becomes the AI lock reason with its one way forward (`AiLockedFooter`) | deterministic rows only (module facts, connect, work items) | modules lane only | live, no assessment dot | live | hidden, one CE line in the value band |
| No product connected | sample, labelled, with Connect pills | connect rows | sample lane, labelled | "Connect" chips | coverage shows the connect path | as above |

## 6. Parity inventory

Every v1 widget of `CTEM_WIDGETS` and every figure, link and action in it,
with its v2 place and its fleets-off source (F). "Grid" = still available
from Customize (the widget stays in the registry and in saved layouts).

| # | v1 element (widget id) | v2 place | F: fleets-off source |
|---|------------------------|----------|----------------------|
| W1 | `hero`: greeting, platform name, edition badge + tooltip | Value band top line | n/a (static) |
| W2 | `hero`: posture orbit / score chip, "How computed" dialog | Value band gauge, same dialog | posture build (built-in) |
| W3 | `hero`: subtitle (score, stages covered, agents watching, running, KEV) | Value band headline (deterministic form) | posture, dashboard stats |
| W4 | `hero`: Start chatting / AI lock footer | Act now last row "Ask your CTEM agent"; lock footer in value tile | n/a |
| W5 | `hero`: last synced, next sync, Force sync | Value band under the gauge | posture |
| W6 | `hero`: composite trend + DiffBadge | Value band gauge sparkline + delta | posture history |
| W7 | `hero`: product pills (OpenCTI, OpenAEV, OpenCRQ Soon, XTM Hub, extra registrations), Connect dialog | Value band top line | registrations |
| W8 | `hero`: CTEM flow ribbon | Lifecycle band | posture |
| W9 | `kpi_posture` | Value band gauge | posture |
| W10 | `kpi_exposure_1..3` (vertical KPIs, deep links) | Stats: findings widget; grid | posture headline |
| W11 | `kpi_agents` (agents, assignments, diff) / locked KPI | Value tile fallback; grid | dashboard stats |
| W12 | `kpi_executions` (runs 30 d, diff) / locked KPI | Value tile fallback; grid | dashboard stats |
| W13 | `briefing` (headline, summary, findings, focus, generate / refresh, open) | Value band headline + Act now rows; `/briefing`; grid | briefing (built-in, quota exempt) |
| W14 | `lifecycle_header` (covered count, Export CSV) | Lifecycle band title line | posture |
| W15 | `stage_*` x5: score, band, prev score, sparkline, facts, viz, owner, assessment, regenerate, connect, open in module, open stage | Lifecycle chip (score, sparkline, key figure) + `/ctem/:stage` (every other figure) | posture, stage assessments (built-in) |
| W16 | `stage_governance` (OpenCRQ) | Lifecycle chip; live OpenCRQ figures or Connect; sample figures removed (D2) | OpenCRQ registration |
| W17 | `recommended_actions` (up to 6, AI badge, owner, Open briefing) | Act now | briefing, assessments, module facts |
| W18 | `live_activity` (runs, deterministic events, sample) | Done for you lanes; grid | automations, module events |
| W19 | `digests` (up to 9, view all, read more) | Done for you automation lane (click opens the run); grid | built-in digests |
| W20 | `fleet_activity` (tiles, ROI, invitation, closed line) | Fleets row; value tile | invitation line |
| W21 | `fleet_timeline` | Done for you fleets lane; grid | not drawn when off |
| W22 | `coverage_matrix` | Stats: heat map | posture |
| W23 | `exposure_stats` | Stats: exposure stats | posture history |
| X1 | Customize: move, resize, remove, add, per vertical, saved in `User.ctem_dashboard_layouts` | Kept. The v2 sections become 6 new widget ids; a saved v1 layout keeps its widgets; a person without a saved layout gets the v2 default | n/a |
| X2 | Vertical switcher (cyber, FIMI, fraud) | Value band top line | n/a |
| X3 | `DashboardNotices` region | Kept above the value band | n/a |
| X4 | Auto-generation of a missing briefing on first open | Kept, unchanged | n/a |
| X5 | Deep links: `/ctem/:stage`, `/briefing`, `/agents/:id`, `/agents/new`, `/operations/executions`, `/operations/executions?filter=digests`, `/operations/executions/:id`, `/integrations`, `/integrations/new?provider=`, `/settings/platform#xtm-hub`, `/chat`, `/flows/fleets`, module deep links (`ctemDeepLink`, `ctemBestDeepLink`) | All kept; added `/value`, `/value/methodology`, `/operations/work-items` | n/a |

Stage page parity rows (what the v1 stage card shows, kept on `/ctem/:stage`;
most already exist on the page, V4 adds the missing ones):

| # | v1 stage card element | Place on `/ctem/:stage` | F: fleets-off source |
|---|-----------------------|-------------------------|----------------------|
| S1 | Score, band, previous score and `DiffBadge` "vs prev" | Header: gauge as leading, delta badge beside the title (delta added in V4) | posture |
| S2 | Full fact set, gaps greyed as placeholders | Metrics card (exists) | posture |
| S3 | Stage viz: severity bars (Prioritization), validation donuts and their legend (Validation), headline meter (others) | Metrics card (exists) | posture |
| S4 | AI assessment: headline, assessment, recommended action, Regenerate, generate on landing | AI assessment card with Refresh, Follow up in chat, Assign agent on every assessment; "Generated <date> by <agent>" with the age on hover and Out of date past 7 days (`STAGE_ASSESSMENT_FRESH_DAYS`); generated on visit when missing or out of date, once per stage and vertical (`shouldAutoGenerate`, #12002: the v1 stage card held the only auto-generation and left the default layout in e3685252); AI lock footer when locked | stage assessment (built-in) |
| S5 | Owning analyst, open agent | Header "Owned by {name}" (exists) | n/a |
| S6 | Products feeding the stage, Connect CTA per blind spot | "Products" card with "Connect {name}" (exists) | registrations |
| S7 | Open in module (`ctemDeepLink`, `ctemBestDeepLink`) | Header action (exists) | registrations |
| S8 | In-card score sparkline | History card, 30 / 90 d period control (exists) | posture history |
| S9 | Sample state of an unconnected blind spot | Header `SampleBadge` (exists); never on a connected stage (D2) | n/a |

The stage page also keeps the methodology (header description), the vertical
badge and the Company Context card; none of them moves.

Endpoints read (unchanged, none added for the page load): `/ctem-metrics/posture`,
`/ctem-metrics/posture/history`, `/ctem-metrics/briefing`,
`/ctem-metrics/stages/{stage}/assessment` (stage page and Act now; read, no
refresh), `/dashboard` stats, assignment runs, integrations, XTM Hub status,
`/roi`, `/fleets/{id}/health`, `/fleets/{id}/activity`,
`/work-items/waiting-for-me`, `/tool-approvals`. Writes only on a click:
briefing refresh, posture sync, stage assessment refresh, profile (vertical,
layout, invitation dismissed).

i18n: every v1 key in use stays in the four catalogs until the old layout is
removed (last slice); new keys go through `sandbox_json_keys`, plurals as ICU.

Acceptance test (slice 7): every id of `CtemWidgetId` resolves in the grid
registry or maps to a v2 widget in a `V1_TO_V2` table, and every route of X5
is a route of `App.tsx`.

## 7. Visual language

- Gauge (semi-circle) for the posture; sparklines (30 / 90 d) on every score.
- Horizontal timeline with lanes for activity; no vertical list on the landing
  page.
- Heat map for coverage; two-bar deltas for stats.
- One accent per section, band colours from `CTEM_BAND_CONFIG`; borders and
  rings with design-system token classes only (no arbitrary `[var(...)]`).
- Density: 16 px section gap, card padding 16 px, title rows 28 px.
- Controls: buttons and row actions 36 px, segmented controls (7 d / 30 d,
  Cyber / FIMI / Fraud) 32 px, icon buttons 36 px with a translated
  accessible name. Outline buttons only on the landing page (no filled
  primary competing with the data).
- Text at 12 px or more everywhere; light-theme status text uses the
  success / warning / danger tokens that reach 4.5:1 on the card surface
  (round 1 A1: the light success text moves to the darker success token).
- Text on a filled status token (the timeline cluster count badge, any
  filled status chip) uses an "on-status" text token chosen per theme:
  dark text `#04121f` on the dark success fill `#2ecc71` (8.99:1), white
  `#ffffff` on the light success fill `#16733f` (5.9:1). Never one text
  colour for both themes; the build reads the design-system foreground
  token of the fill (round 3 B1).
- French uses U+202F before ":", ";", "?", "!" and "%" and as the thousands
  separator (U+00A0 where the catalogue already does); percentages go
  through `Intl.NumberFormat` with `style: "percent"`, which emits it
  (round 2 B3).

Mockups, attached to the UX design review item, not committed (round 3): the
whole landing page, fleets off, dark and light at 1440 and 1024 with
its measured height; fr dark at 1024; one fleet on at 1440 and 1024; CE / AI
off at 1440;
the Act now "Show all" expanded state and the timeline hover and cluster
states; the Prioritization stage page, dark 1440, on the shared header.

## 8. Build slices

| Slice | Content | Gate |
|-------|---------|------|
| V1 | Value band (new widget `value_band`, deterministic headline, risk and value tiles) | UX on head |
| V2 | Act now queue (merge, dedupe, work items, approvals) | UX on head |
| V3 | Done for you horizontal timeline | UX on head |
| V4 | Lifecycle band + stage page parity rows; sample Governance figures removed | UX on head |
| V5 | Stats and coverage (A1-A4 of #9237 fixed) | UX on head |
| V6 | Fleets row | UX on head |
| V7 | New default layout, v1 widgets left in the catalog, parity test; acceptance on the lab (fleets off first) | UX on head |

Each slice merges at green after the UX Director's APPROVE on its head. The
nav slices of #9301 merge first; these slices land on the final menu.

V7 as built: `WidgetSpec.hidden` (components/dashboard-grid/layout.ts) keeps a
widget off the default layout and in the catalog; `resolveLayout` never adds
a hidden widget to a saved layout that skips it. Every v1 id of section 6 is
marked hidden, the six v2 sections are the default from row 0, and the value
band's top line carries the greeting (W1) and the product pills (W7). A saved
v1 layout draws as saved, with the v2 sections below it.
`ctem/command-center-v2-default.test.ts` is the parity test.
