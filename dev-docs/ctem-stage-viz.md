# CTEM visual first: signature viz per stage and the command center audit

Issue: #11547. Design note, step 1: the UX Director approves this note before any code.
Sources: Sam, 13 Nov 2026, "viz really impactful like this one (the Cortex XSIAM 4.0
funnel), not necessarily the same one in each stage subpage and in the dashboard"
and "the dashboard is still too much textual. We need much more impactful viz."
Builds on `ctem-command-center-v2.md` (section 7, visual language), which stays
the reference for density, controls and tokens.

Round 2 (UX round 1 on 5084754f: CHANGES REQUESTED, B1 to B7). Each fix is
marked with its finding id in the section it changes; the mocks are redrawn in
the app shell (section 7).

Round 3 (UX round 2 on 3de0ef58: CHANGES REQUESTED, B1 to B5, A1 to A8) and
Sam's scope add of 13 Nov 2026 10:25 Paris on the score pair and the
lifecycle strip ("Qualitative / quantitative insight should have better
visuals. Maybe icons... so repetitive, not well designed, not well
arranged"). Round 3 fixes are marked `R3-B<n>`, `R3-A<n>` and `SP<n>` (scope
add point n).

Round 4 (UX round 3 on ef89a0f4: CHANGES REQUESTED, B1 to B6) and Sam's
rules of 13 Nov 2026 ~11:17 and ~11:20 Paris: the dashboard is
multi-framework and context-adaptable, never ATT&CK-only, and the command
center is the user's own. The new section 8 "Frameworks and customization"
carries R4-B1 to R4-B3; R4-B4 to R4-B6 and the advisories are marked in the
sections they change.

Round 5 (UX round 4 on 85628fb1: CHANGES REQUESTED, B1 and B2, A1 to A7)
and Sam's rule of 13 Nov 2026 ~11:50 Paris on the posture dialog. The new
section 9 "The Exposure Posture dialog" carries R5-B1; rule 12 carries
R5-B2; section 10 is the self-check of this round.

Round 6 (UX round 5 on a6621f00: CHANGES REQUESTED, B1 to B4, A1 to A5).
Section 9 carries R6-B1 to R6-B3 (dialog `size="md"`, action chips, the
footnote that prints every exception); the round 6 mocks fix R6-B4 (hero
equals totals in every capture); section 10 is the self-check of this round.

## 1. Rules

1. **Hero first.** Each stage page opens on one viz that answers the stage's
   question. The AI assessment, the facts grid and the methodology go below it,
   or into hover and drill-down.
2. **Different on every surface.** No two stage pages use the same chart type.
   The command center summarises the stages without copying any stage hero.
3. **Text budget.** A widget shows a headline number, a short label (3 words or
   fewer) and a delta. Anything else (narrative, definitions, lists past 3 rows)
   goes into hover, a drill-down or a page.
4. **Five-second test.** From a screenshot at 1080 and 1728, in light and dark,
   a reader can name the posture and the top risk within 5 seconds.
5. **Honest data.** A mark is drawn only from a value the posture API returns.
   When a source is missing the viz shows a dashed "not configured" state with
   the one way to connect it (connect a module, or fill the organization field).
   It never shows an invented number. A greyed sample keeps the `SampleBadge`.
6. **No new chart library.** The charts are SVG, built on the primitives in
   `ctem-viz.tsx` (gauge, trend, bars, donut, meter). Two surfaces draw a flow
   whose volume is conserved: the Prioritization exclusion flow (section 3.3,
   what stays plus what leaves equals what entered, at every step) and the
   Cases status path of item C. The exclusion flow is a fixed left-to-right
   path with one branch per step, so its geometry is written in the CTEM
   files (`prioritization-funnel.ts`, Apache-2.0); it does not import the
   studio sankey (`custom-dashboard/viz/sankey.ts`, Enterprise Edition
   licence) and adds no chart dependency. Item C may use the studio sankey for
   the case status path. No other surface of this note draws a flow.
7. **One encoding per mark (B1).** A mark carries one quantity. Where a score
   and a metric sit together, the arc and the big number are the score, the
   band name is written under it, and the metric is one short label line.
8. **The score pair is encoded once (SP1, SP2).** Every CTEM score has two
   readings: quantitative (the connected modules' metrics alone) and
   qualitative (the same metrics read against the organization context). They
   are always drawn as the two concentric rings of one gauge: outer ring
   quantitative, inner ring qualitative, as `PostureGauge` already draws them.
   Each reading has one icon, used everywhere and never replaced by a word:
   `database` for quantitative (measured), `building-2` for qualitative (in
   your context). The words "Quantitative" and "Qualitative" appear once per
   surface, in the legend or the tooltip, never repeated per card.
9. **No "No data" text in place of a number (SP4).** A value that cannot be
   computed is the not-configured state of rule 5: a dashed mark (dashed ring
   track, dashed bar) and one link to the setting or module that fills it.
10. **Any framework, never ATT&CK alone (R4-B1).** A technique-based chart
    draws the kill chain of the framework the tenant's data carries (section
    8): MITRE Enterprise ATT&CK, Mobile, ICS, ATLAS, DISARM or a custom one.
    Its columns, their order and their labels come from the data. No tactic
    list, technique id or framework name is a constant in the CTEM viz code.
11. **Every widget reads its context (R4-B1, R4-B2).** A widget draws what
    the dashboard context names (section 8.2) unless the person overrides it
    on that widget. A context with no data draws "not configured", never a
    count from another context.
12. **Large figures are compact, through the platform's one formatter
    (R4-B5, R5-B2).** A figure of 1,000 or more is printed by
    `CompactFigure` with `useNumberFormat` in a component (the exact value
    in its `title` and in the hover card), or by `formatCompactNumber`
    (`frontend/src/lib/utils.ts`) outside React: "1.3K", "12K", "1.2M" in
    en, the UI locale elsewhere. No CTEM viz calls `Intl.NumberFormat`
    itself or adds a formatter; the hidden table carries the exact value.

## 2. Shared visual language

These rules are shared with the funnel (A) and the Cases flow (C).

| Concern | Rule |
|---|---|
| Score colours | Band colours from `CTEM_BAND_CONFIG` (Critical, At risk, Fair, Good, Strong), always shown with the band name, never colour alone. The mocks use stand-in hex values; S1 measures the real tokens (`text-feedback-warning-primary` for Fair) in the lab at 4.5:1 for text under 18 px in both themes (R3-A1). No band uses the accent (#11661): Good is the turquoise tonic family (`text-filigran-tonic-accent`, ring `--color-filigran-tonic-primary`), pinned by `ctem-band-config.test.ts`. Text (a score, a band name, a caption) always takes the band's `color` class; `ring` is a 3:1 graphics colour for strokes, fills, meters and sparklines only, also pinned by that test |
| Series colours (A2) | Data series use feedback tokens (success, warning, error) for outcomes and the brand primary or neutral secondary for counts. The AI accent (`--color-filigran-ia-*`) is never a data colour. Design system 1.5.1 has no data-viz series tokens: the need is recorded in `fds-migration/LIBRARY-FEEDBACK.md` with S1 (A1) |
| Keys (A3) | Every colour in a chart has a key: a swatch before each legend label, or the label inside the mark |
| Delta colour | A delta is coloured by the direction of improvement, not by its sign: fewer critical CVEs or a smaller gap to target is success, a lower prevention rate is error. The sign is always printed |
| Outcome colours | Prevented = success token; detected only = warning token; missed = danger token; untested = dashed outline. Each also has a pattern (solid, diagonal, cross, dashed) for colour-blind readers |
| Hover card | Exact value (`exactMetricValue`), the definition (`MetricInfo` copy), the change against the previous period, and the source module |
| Click | A mark opens the filtered list behind it: the module page through `ctemBestDeepLink`, or the in-app stage page |
| Motion | Bars, arcs and areas grow in over 400 ms using transform and opacity only. Height is reserved before data arrives, so nothing shifts. `prefers-reduced-motion` turns the motion off |
| Keyboard | One tab stop per chart; arrow keys move between marks (roving tabindex); Enter follows the click; focus uses the focus token |
| Screen reader | Each mark has an `aria-label` ("Endpoint: covered, 12 contracts"). Each chart also has a visually hidden table of the same values |
| Totals | Segments add up to the headline, with no rounding drift (percentages through `formatPercent` of `lib/number-format.ts`, never a local `Intl.NumberFormat`, the remainder given to the largest segment; R5-B2) |
| Not configured | Dashed frame, a one-line reason, and one outline button (Connect OpenAEV, Open organization settings) |
| Width (R3-B5) | Every hero chart fills its card width at every breakpoint (the SVG `viewBox` width follows the card through a `ResizeObserver`, never a fixed aspect); its height is fixed per chart. S1 shows it rendered at 1080 and 1728 |
| Score pair (SP1, SP2) | The gauge rings of rule 8. Beside a gauge, a `ScoreRingKey` replaces the text `ScorePair`: two lines, each its icon, a 12 px ring-position swatch (thick outer arc, thin inner arc), the score in its band colour, the band name and the period delta. The definition is in the icon tooltip (`MetricInfo` copy of today's `ScorePair`). No uppercase label words. When a posture target is set (item B), a tick on the outer ring marks it |
| Bars carry a denominator (R3-B4) | A bar is drawn only for a ratio whose total the payload returns ("38% of 412"). A count with no total is printed as a figure, never as a bar |

## 3. Stage pages

Today `CtemStageDetail.tsx` `StageViz` draws a chart only for Prioritization
(CVSS bars) and Validation (a donut per expectation type), at the bottom of the
Metrics card. The new order of each page: the existing header, then the hero
viz full width, then the AI assessment and Metrics cards side by side, then
History.

**Header (R3-B3): unchanged.** The header stays the `EntityPageHeader` of main
(`CtemStageDetail.tsx` lines 226-284): back link, the 104 px `PostureGauge`
with its two rings, title, methodology, badges, owner link and deep link. The
only change under it is SP1: the text `ScorePair` row (lines 287-294) becomes
the `ScoreRingKey` of section 2, so the pair is read from the gauge and its
key, not from two uppercase words. The hero starts right under it; the
round 3 mocks are drawn under that real header height at 1080. The back link
reads "Back to command center" (sentence case, R3-A8), fixed in S1.

The existing viz stays on the page until its replacement is approved: the CVSS bars move under A's funnel and the donuts move under the
Validation matrix.

### 3.1 Scoping: attack surface ring

- **Question:** what is in scope, and what part of the attack surface is never tested?
- **Viz (B4):** a ring with one segment per OpenAEV core attack domain
  (Endpoint, Network, Web App, E-mail, Exfiltration, URL Filtering, Cloud).
  Every segment carries its label and Icon outside the ring, so no domain needs
  hover to be named. A covered segment is filled (success token). An untested
  segment is outlined with the danger hatch and its label reads "untested".
  There is no inner guide circle and no other dashed treatment on a configured
  ring. The centre is settled as **scope breadth %** (`scope_breadth_pct`, the
  stage headline) with its delta; covered/total is in the hover and the hidden
  table. Beside the ring, three labelled figures with no bars (R3-B4: the
  payload returns no total for them): endpoints, asset groups and security
  platforms in scope, each a number and a 3-word label.
- **Business context:** primary assets and business services from the
  organization context (item B, `0f898868`) draw an outer ring, each marked
  covered or uncovered, only once those fields are filled. While they are
  empty there is no outer ring: one not-configured row under the ring reads
  "Business context not configured" with "Open organization settings".
- **Data:** `scoping.attack_domains_covered` and `attack_domains_total` exist
  today. The build adds `scoping.attack_domains`, a list of `{name, covered,
  contracts}` already computed in `_aev_domain_coverage`, to the viz payload of
  `_stage_viz`. This is additive: no fact is renamed and no score input changes.
- **Not configured:** OpenAEV not connected: the seven segments are drawn as
  seven thin dashed segments (not one circle, R3-A7) with their labels and
  `Icon` glyphs, the centre is a dashed dash "--", and the figures are
  replaced by one not-configured row with "Connect OpenAEV" (the OpenCTI
  scope completeness stays a fact below). Two not-configured frames on one
  card keep a 12 px gap.
- **Hover:** domain, covered or not, contract count. **Click:** the OpenAEV
  injector contracts filtered on that domain.

### 3.2 Discovery: tested vs untested threat techniques over time

- **Question:** of the techniques the threats tracking us use, how many do we
  test, and is the gap closing?
- **Viz (B5):** a 100% stacked area over 30 or 90 days (the period control of
  History). The bottom layer is the share of threat techniques simulated
  (success token); the top layer is the untested share (danger hatch). The
  boundary between the layers is the threat-informed coverage %, labelled at
  the right edge with the headline value and its delta. There is no line and
  no second axis. The y axis reads 0 and 100%; the time axis shows the first,
  middle and last dates. Absolute counts (tested, untested, tracked) are in
  the hover. Source chips in the card header name the module behind each layer
  (OpenCTI threats, OpenAEV simulations).
- **Framework (R4-B1):** "threat techniques" are the techniques of the
  framework in context (section 8.1 selector in the card header, default the
  framework with the most tracked techniques). The snapshots gain a
  per-framework split, additive (`by_framework: {name: {threat, exercised}}`);
  until two days of it exist, a framework other than the stored total reads
  "Trend after 2 days of history".
- **Data:** the posture snapshots (`ctem_posture_snapshots.stage_metrics.discovery`)
  already store `threat_techniques`, `exercised_techniques`,
  `untested_techniques` and `threat_informed_coverage_pct` per day; this is read
  through `GET /ctem-metrics/posture/history`. No backend change.
- **Under 2 recorded days:** today's split as one stacked bar, with "Trend after 2 days of history".
- **Title (R3-A5):** the card title is "Threat techniques tested" in every state.
- **Not configured:** OpenCTI missing: "Connect OpenCTI". OpenAEV missing: no
  share can be computed, so the chart area is an empty dashed frame, the
  headline is the count of threat techniques tracked, the OpenAEV chip is
  dashed, and one row reads "Tested share needs OpenAEV simulations" with
  "Connect OpenAEV".
- **Hover:** day, the two counts, coverage %, change since the previous point.
  **Click:** the untested layer opens the OpenCTI attack patterns used by the
  tracked threats.

### 3.3 Prioritization: the exclusion flow (item A `e0ebb40b`, redrawn by `dev:ctem-funnel-exclusion-flow`)

Sam, 13 Nov 2026 18:26 Paris: the Prioritization hero is the exposure
management flow of the reference (Cortex XSIAM 4.0, Exposure Management
Command Center), not a stepped funnel. The data, the layers, the honesty
rule and the tests of #11557 and #11658 stay; only the drawing changes.

- **Question:** how many vulnerabilities are known, how many need action,
  and what removed the most.
- **Viz:** one horizontal flow, left to right, full card width, fixed height
  (360 px at 1080 and 1728, reserved before data arrives).
  - **The main band** starts at the left as tall as Known vulnerabilities
    and narrows at each layer: Known, Relevant to you, Exploitable, Exposed,
    Business impact, After policy, then Cases. Above the band, at each step,
    the layer's figure (`CompactFigure`) and its name in sentence case.
    Band height follows the square root of the share of Known, floored at
    14 percent (the `funnelWidthPct` rule of #11557, read vertically): the
    exact number is printed, the height only orders the eye.
  - **A branch per live exclusion** peels off the band downward at its step
    and ends in a label: the count it removed and the reason in plain words:
    "not in your tech stack" (Relevant), "not exploitable" (Exploitable),
    "not exposed" (Exposed), "low business impact" (Business impact),
    "deprioritized by policy" (After policy). The branch's count is the
    layer's `drop`, never recomputed in the UI.
  - **Conservation in drawn height (design round 1, B1):** at each live step
    the band that enters (height `h`) is split by true share: the branch root
    is `h * drop / entered` (at least 2 px when `drop > 0`), the kept part is
    `h - branch`. Only then does the kept band ease, over half a step pitch
    (at most 48 px), to its own square-root height, never taller than what
    entered. The drawn share of a branch is its true share at the root;
    a test holds it within 2 points.
  - **Branch labels (B3):** one line each (count, then reason), in a
    staircase: the first exclusion ends lowest and each later one a row
    (28 px) higher, so no branch crosses an earlier label and each label
    runs to the card's right edge; a label longer than its room truncates
    (`TruncatedText`, full text in the tooltip). Step names above the band
    take a fixed two-line height. Checked in en, fr and ja at 1080.
  - **A not configured exclusion** is a dashed hairline branch that removes
    nothing: the band keeps its height through the step, the branch label
    reads "Not configured" with the layer's call to action to Settings >
    Organization (rule 5).
  - **Cases** close the flow: the band ends in a node with the open cases
    count (Needs attention plus In progress), which fans out into those two
    state tiles only, so the fan adds up to the node (B2), in the words,
    order and tokens of the Cases board (`CasesCountsCard` `SEGMENT_FILL`).
    Resolved and Accepted risk are one secondary line of links under the
    tiles ("12 resolved, 2 accepted risk", ICU plurals). Every count links
    to `/ctem/cases?state=`. The fan is laid out from the measured card
    width (tiles end inside the SVG at 1080 and 1728, B4).
    Cases are another unit (they group issues): the band into the Cases
    node keeps the height of the last live layer, no branch is drawn there
    (as `isCasesLayer` of #11658). Before the Cases API answers, or with no
    case possible, the node is the not available or not configured state
    of #11658, and the tiles are not drawn.
  - **One outcome line** under the flow: "21 issues grouped into 7 open
    cases" (ICU plurals), drawn only when both counts are live.
- **Colours (decided by the UX Director, design round 1):** the band is
  `fill-accent`, solid, no gradient; a removed branch is
  `feedback-neutral-secondary` fading from 0.85 to 0.15 toward its label,
  with a 1 px `stroke-subtle` edge; a not configured branch is a dashed
  `stroke-subtle` line; the case fan and tiles use the `CasesCountsCard`
  `SEGMENT_FILL` tokens. No AI accent, no red on the band. Named Styles
  only, no px literals in the component.
- **States (C1, C2):** hover cards, one roving tab stop across steps,
  branches and tiles, and the hidden data table; growth animation off under
  reduced motion; the 360 px height is held in loading, error, empty and
  sample.
- **Biggest removal:** the branch with the largest drop gets its label in
  `font-semibold`, so "what removed the most" is read first.
- **Kept from #11557 and #11658:** compact figures with the exact value in
  the tooltip and the accessible name; each live step a link to the list it
  counts in OpenCTI (`funnelLayerHref`); one tab stop with arrow keys across
  steps, branches and tiles; the hidden data table (layer, count, removed,
  reason); growth animation off under reduced motion; the Sample badge on the
  sample; the empty state when Known is 0.
- **Tests:** at every live step the drawn branch share is within 2 points
  of the true share (`drop / entered`), with a 2 px minimum branch; the
  case fan adds up to the open cases node; the case tiles end inside the
  measured width at 1080 and 1728; the branch counts plus the last live
  layer equal Known; counts never increase;
  a not configured step draws a branch of zero thickness; a saved command
  center layout loads unchanged.
- **Command center (rule 2):** the `prioritization_funnel` widget keeps its
  strip and links to this page; it never draws the flow. The UX Director
  confirms in the design round.

The CVSS bars stay below it.

### 3.4 Validation: outcome matrix, any framework (R4-B1)

- **Question:** what holds under attack, phase by phase of the framework the
  attacks follow?
- **Framework:** the matrix draws one kill chain at a time, picked in the
  card header by the framework selector of section 8.1 (default: the
  framework with the most exercised techniques). It reuses the chain model
  of the custom dashboard `outcomeMatrix` (`viz/outcomes.ts` `matrixModel`,
  `WidgetOutcomesData.chains`), which already draws any chain the data
  names: no new matrix model.
- **Viz (B6):** a matrix with one column per phase of the selected kill
  chain, in the chain's phase order (14 for Enterprise ATT&CK, 16 for
  DISARM, 14 for ATLAS: the count comes from the data). The header row
  carries the phase name as the data gives it, on at most three lines (full
  name in a tooltip past that), and, under it, a bar of the column's
  prevented share. A column is at least 122 px wide (the longest single word
  of a phase name, "Reconnaissance", fits at 11 px): when the chain needs
  more width than the card (any framework at 1080, DISARM at 1728), the
  matrix scrolls horizontally inside the card with the header row pinned,
  and a fade on the right edge says more columns follow. Below, one cell per exercised technique, sorted by outcome within
  the column (prevented, detected only, missed, untested). Outcome is given by
  colour and pattern together: prevented solid success, detected only diagonal
  warning, missed cross-hatched danger, untested dashed outline. Each column is
  capped at 8 rows (the height that fits the card at 1080 in the shell); past
  that, the last cell reads "+k" and opens the filtered OpenAEV list. The
  legend is one row of four swatches at 1080. The headline shows prevention %
  and detection % with deltas. The expectation donuts stay below the matrix.
- **Data:** OpenAEV `GET /api/attack_patterns/coverage` (`_aev_attack_coverage`)
  already returns per-technique `prevention_success/total` and
  `detection_success/total` with the technique's external id. The build adds
  `validation.outcome_matrix` to the viz payload, in the `WidgetOutcomesData`
  shape: `chains` (one per kill chain name found, each with its phases in
  `phase_order`, labels as the data names them, `default` on the one with
  the most techniques), `reference` (every technique of those chains,
  `{id, label, code, phases[]}`) and `items` (the exercised ones with their
  outcome counts). The phases come from OpenAEV's kill chain phases
  (`phase_kill_chain_name`, `phase_name`, `phase_order`, one cached read per
  sync); to verify in the lab whether the coverage rows carry them already.
  Payload limit: the existing `MAX_OUTCOMES` (600) and `MAX_OUTCOME_CHAINS`
  (10) bounds of `dashboard_live`.
- **Not configured:** OpenAEV missing: the matrix frame is dashed, with "Connect
  OpenAEV". The OpenCTI detection-rule coverage stays a fact below. A
  framework picked (or pinned by the context) with no exercised technique:
  the columns of its chain are drawn as dashed outlines with the line "No
  simulation follows DISARM yet" (the framework's name from the data) and
  "Open OpenAEV scenarios"; no count from another framework.
- **Hover:** technique, outcome, prevention x/y and detection x/y. **Click:** the
  OpenAEV technique page.

### 3.5 Remediation: Cases flow (item C, `ee680193`)

Built by item C, not here. It follows section 2 of this note.

### 3.6 Governance & Risk: posture against target

Governance is not a scored stage (`STAGE_KEYS` holds five stages), and the
lifecycle chip links to OpenCRQ. The build adds a light page
`/ctem/governance`. It has no score, so stage and posture scores do not change.

- **Question:** are we where we said we would be, and what risk did we accept?
- **Viz:** a line of the posture score over 90 days on the band zones, with a
  dashed target line and the gap shaded. Beside it, a bullet bar for SLA
  adherence and a strip of accepted risks positioned by days until expiry.
- **Data:** posture history: live today. Target: organization risk appetite
  (item B). SLA adherence and accepted risks: OpenCRQ.
- **Band zones (A4, R3-A6):** the five band zones are labelled on the right
  axis (Critical to Strong). The tints are decorative (about 1.1-1.2:1
  against the card in light): the axis labels carry the meaning, so WCAG
  1.4.11 holds through the labels, not the tint.
- **Header (R3-B3):** Governance has no stage score. The `EntityPageHeader`
  leads with the gap to target as its big figure ("Gap 13", delta coloured
  by improvement); the posture pair is labelled "Posture 62 Fair" in the
  `ScoreRingKey`, never in a stage score slot.
- **Not configured:** no target: the line is drawn without the gap, and the
  headline is the button "Set a posture target" (organization settings).
  OpenCRQ not connected: only the SLA and accepted-risk part is one dashed row
  "SLA adherence and accepted risks need OpenCRQ" with the "Soon" badge. The
  posture against target stays live (B2).
- **Placement (decided, UX round 1):** a page of its own, `/ctem/governance`.
  No drawer. The strip's Governance column opens it.

## 4. Command center audit

Every widget below reads the dashboard context of section 8.2 (framework,
scope, asset groups, intel type, period, sources) and may override it; what
it counts and how it labels follows that context (R4-B1).

**Fixes of round 4 on the drawn widgets:**
- **Strip metric fits its column (R4-B4).** The metric row is two lines: the
  label alone (at most 10 characters: "Breadth", "Coverage", "Critical",
  "Prevented", "Closure"), then the value, the delta and the bullet bar on
  one line. The cell has `min-width: 0` and an ellipsis on the label, and a
  probe at 1080 and 1728 checks that no text passes its column. Prioritization
  reads "Critical" over "22% -6".
- **Compact figures (R4-B5, R5-B2).** `stats_coverage` prints 1.3K (not
  1284) through `CompactFigure`, per rule 12; the exact value is in the
  hover.
- **Heat map and delta bars drawn with axes and key (R4-B6).** The heat map of
  `stats_coverage` has its row labels (the five stages) on the left, its
  column labels (the last 8 weeks, first and last dated) on top, and a
  five-step key under it ("Critical" to "Strong", one swatch each, the band
  names). The delta bars are drawn under it: one signed bar per stage, the
  value printed at the bar end, a labelled zero line, coloured by direction
  of improvement (section 2).

Every widget id stays valid. A saved layout loads unchanged: an id keeps its
place and size and draws its visual replacement under the same id, so the
backend allowlist (`services/dashboard_layouts.py`) does not change. A test
loads a layout saved before the change and checks every item draws.

| Widget id (default?) | Today | Replacement |
|---|---|---|
| `hero` (default) | Orbit, the two scores as bare numbers under uppercase words (QUANTITATIVE 40, QUALITATIVE 34), greeting, pills | Kept, with SP1: the orbit's gauge keeps its two rings, and the bare pair becomes the `ScoreRingKey` beside it (icon, ring swatch, score in band colour, band name, delta; the definition in the icon tooltip), plus the target tick on the outer ring once a target is set. A **top risk** tile is added beside the gauge: known-exploited exposures with a 30-day sparkline that takes the tile's free height (R3-A3), opening the Prioritization page. This is the five-second pair: posture and top risk |
| `lifecycle_band` (default) | Six chips: name, score, the words "Quantitative" and "Qualitative" under two numbers in each chip, sparkline, first fact as a text line, "No data" text | **Exposure story strip** (B1, R3-B1, SP2 to SP4): the strip reads as a flow. **Header:** title "Exposure lifecycle" and, once, the ring key: outer ring `database` measured, inner ring `building-2` in your context (tooltips carry the definitions). **Track:** the six columns sit on one continuous track line with a chevron between columns, equal width, equal height. **Grid:** one CSS subgrid of five rows shared by all six columns (name, ring, delta, metric, sparkline), so every row shares a baseline across columns (tolerance 1 px). **Stage column:** the stage name; a 56 px concentric ring, outer arc the quantitative score, inner arc the qualitative score, each in its band colour (colour by threshold, never per stage); in the centre the quantitative score as the big number; under the ring the band name and the score delta; the stage metric as a small bullet bar with value and delta (Breadth 71% +4, Coverage 38% +3, Closure 68% +2, Prevented 54% -2; Prioritization: critical share of open findings, drawn as a bar only when the payload returns the open total, else the figure alone, to verify in S2); a 30-day score sparkline at full column width, 24 px high. The qualitative number is in the ring's hover and hidden table, not printed. **Governance column (R3-B1):** the gap to target as the big number with its delta right after it ("13 -4", green as improvement), a bullet bar of posture against the target tick, "Target 75" as a plain label, and the posture sparkline on the same subgrid rows. **Not configured (SP4, R6-B1 of round 6):** a stage with no data draws a dashed ring track for both rings, the big number is "--", and the delta row is one link naming the product to connect ("Connect OpenAEV", "Connect OpenCTI"). A stage with a quantitative score but no organization context (`qualitative_score` null) keeps its outer arc and big number, draws NO inner arc (the inner track only, dashed), and its delta row is the link "Add organization context" to Settings > Organization, the same words and target as the dialog chip of section 9 (one i18n key shared by the strip and the dialog); its ring's accessible name reads "Remediation: measured 68, no organization context, add organization context". No target: dashed bullet bar and "Set a posture target". A click opens the stage page. It copies no stage hero (no attack-domain ring, matrix, funnel or area) |
| `briefing` (default) | Headline, summary, insight, 3 findings, 3 steps, focus | One-line headline, a **risk movers** chart (B3): the top 5 objects that moved over the period (a CVE that gained or lost exposed assets, a technique that became untested, an asset group that left scope), each a signed bar with its value printed at the end and a labelled zero line; the bar area scales with the card width (R3-A8); a row opens the object. Stage deltas stay in the strip only. Three key findings as three severity-coded chips, as drawn (R3-A4). The narrative and the steps go behind the link button "Read the briefing" |
| `briefing` data (B3) | | Risk movers needs a per-object delta. The posture snapshots store stage metrics only, so S2 adds a bounded `movers` list (top 5 by absolute change, object kind, id, label, delta, unit) computed from the previous and current collector runs, additive in the briefing payload. Until two runs exist the chart shows "Movers after 2 days of history", never a guessed value. Built in S2c: each daily snapshot keeps `ctem_posture_snapshots.objects` (`{"<kind>:<id>": {label, value}}`, at most 1,000, from the user's own build; kinds `technique` = % of prevention expectations met, `attack_domain` = injector contracts, `vulnerability` = EPSS % of a top-CVSS vulnerability, which moves only on a value change, never by entering or leaving the top list) and `GET /api/v1/ctem-metrics/posture/movers?vertical&days=1&limit=5` answers `{vertical, as_of, status: ready or insufficient_history, since, items: [{kind, id, label, value, previous, delta, unit, change: changed, new or gone}]}` for the current user only (`backend/app/services/ctem_movers.py`) |
| `act_now` (default) | Up to 5 text rows | A segmented bar of what waits, by source (posture, blind spots, setup, approvals) with a swatch and count before each legend label (A3), then 3 rows by default, each a priority dot in its source colour (the bar's swatch, R3-A4) plus verb and object. "Show all" stays and is drawn |
| `stats_coverage` (default) | Heat map, delta bars, 3 findings figures, ATT&CK tested vs untested | **Final content (R3-B2):** the heat map and the delta bars kept; the three findings figures (open, critical, known-exploited) as three figures, each with its 30-day sparkline at full row width. The ATT&CK tested vs untested part is dropped: the strip's Discovery column carries that coverage, and the Discovery page its trend. No ring, no new chart type. Drawn in the 4 command center mocks |
| `done_for_you` (default) | Lane timeline | Kept (already visual) |
| `fleets_row` (default) | One text line per fleet | Each fleet line gets a runs-per-day sparkline over 30 days and a last-outcome dot. The invitation state is unchanged |
| `exposure_stats` (catalog) | Numbers with trend lines | Diverging bars per stage, the same component as risk movers |
| `coverage_matrix` (catalog) | Table | Draws the heat map of `stats_coverage` |
| `kpi_*` (catalog) | Number tiles | A sparkline is added under the number |
| `stage_*` (catalog) | Stage cards | Unchanged (catalog only) |
| `recommended_actions`, `live_activity`, `digests`, `fleet_activity`, `fleet_timeline`, `value_band`, `lifecycle_header` (catalog) | Lists and bands | Unchanged in this program (catalog only, not on the default layout) |

## 5. Non-regression

- Saved layouts: no id is removed or renamed, and the test covers a layout from
  before the change. The context and the per-widget settings of section 8 are
  optional fields: a layout saved without them loads unchanged, with the
  default context, and a test loads one.
- Frameworks (R4-B3): the outcome matrix, the Discovery chart and every
  command center widget render from fixtures of at least two non-ATT&CK
  frameworks (DISARM and ATLAS) and of a tenant with no data for the picked
  framework ("not configured"). A lint test greps the CTEM viz paths
  (`frontend/src/pages/xtm-one/ctem/`, the CTEM viz payload builders) for
  ATT&CK constants (`mitre-attack`, a `TA00` tactic id, a fixed tactic name
  list) and fails on any. The one exemption is the label table of 8.1
  (`ctem-framework-labels.ts`), which holds display labels only.
- Scores: `compute_posture` inputs are untouched. The new payloads are added to
  `viz` only. A test checks that the score of each stage is identical on the
  fixture before and after.
- Existing viz is kept until its replacement is approved. Sample mode keeps the `SampleBadge`.
- UX evidence: before and after captures of the command center and every stage
  page, in light and dark, at 1080 and 1728, side by side in the PR. The
  five-second test (rule 4) also applies to the strip alone (SP5): the
  weakest stage and its band are readable in 5 seconds.
- Tests that read the words today (`ctem-lifecycle-quantitative`,
  `ctem-score-qualitative`, `ctem-gauge-qualitative` in
  `ctem-stage-detail-unscored.test.tsx` and the lifecycle tests) keep their
  test ids on the new marks, so the values they assert stay covered.

## 6. Build slices (one PR each, the largest a review can judge)

| Slice | Content |
|---|---|
| S1 | Shared primitives (ring, stacked area, hover card, not-configured frame, hidden data table), the framework detection and selector of section 8.1, the hero-first page order, Scoping ring, Discovery area, Validation matrix on the `outcomeMatrix` chain model, and the backend `attack_domains` and `outcome_matrix` payloads with their tests, the non-ATT&CK fixtures and the no-ATT&CK-constant test included |
| S2 | Command center visual first (section 4), the dashboard context bar, per-widget settings and saved dashboards of section 8 (optional layout fields, backend schema and frontend together), `ScoreRingKey` replacing `ScorePair` on the command center and the stage pages (SP1), the strip of section 4 (SP2 to SP4), the Governance page, and the layout and score non-regression tests; the PR carries before and after captures of the hero and the strip (SP5) and the command center on two contexts |

A and C build the funnel and the Cases flow on the S1 primitives.

## 7. Mockups

The mockups are attached to the UX design item (`ux:xtm-one-ctem-stage-viz-design`)
and are not committed. All figures are mock data.

Round 2 (B7): every mock is drawn in the app shell (navigation rail 180 px,
top bar 68 px, from `layout-metrics.ts`), so the content column is the real
one at a 1080 and a 1728 window.

| Set | Themes and widths |
|---|---|
| Command center: real hero (gauge, score pair, greeting, product pills) with the top risk tile, strip, risk movers, act now | dark and light, 1080 and 1728 |
| Stage pages in context (header with score pair, hero, AI assessment and Metrics cards) for Scoping, Discovery, Validation, Governance | dark and light, 1080 and 1728 |
| States: Governance with no target, Scoping and Validation with OpenAEV missing, Discovery under 2 days, Discovery with OpenAEV missing, Governance with no target and OpenCRQ missing | dark and light, 1080 |

Not mocked: the fleets-off state of `fleets_row` (unchanged by this note),
hover cards, keyboard focus and reduced motion, which the S1 lab round shows.

Round 3: the command center mocks redraw the hero score pair (`ScoreRingKey`),
the strip (flow track, concentric rings, legend once, subgrid rows, metric
bullet bars, Governance "13 -4") and `stats_coverage` in its final content,
with the annotation copy removed (R3-A2). The stage mocks are drawn under the
real `EntityPageHeader` (104 px gauge) at 1080. The states mock adds the
strip's not-configured column and keeps the Validation data identical across
themes (R3-A5).

Round 4 (R4-B3): the command center is drawn on two contexts, "Enterprise
(ATT&CK)" and "FIMI (DISARM)", with the context bar, dark and light at 1080
and 1728; the Validation hero is drawn on Enterprise ATT&CK, DISARM and
ATLAS, plus the DISARM "no simulation yet" state, dark and light at 1080;
the widget settings panel and the dashboard switcher are drawn once in dark
at 1080. The strip metric (R4-B4), the compact figures (R4-B5) and the heat
map axes, key and delta bars (R4-B6) are redrawn in every command center
mock.

## 8. Frameworks and customization (R4-B1 to R4-B3)

Sam, 13 Nov 2026 ~11:17 and ~11:20 Paris: the dashboard is multi-framework
and context-adaptable, never ATT&CK-only; the command center is the user's
own, like the OpenCTI and OpenAEV custom dashboards.

### 8.1 How the framework is detected, and the selector

- **Detection.** A framework is a kill chain name the tenant's data carries:
  `kill_chain_name` of the attack patterns in OpenCTI (`mitre-attack`,
  `mitre-mobile-attack`, `mitre-ics-attack`, `mitre-atlas`, `disarm`, any
  custom name) and `phase_kill_chain_name` of the kill chain phases in
  OpenAEV. The collector lists them with their phases (`phase_name`,
  `phase_order`) and, per framework, how many techniques carry data (tracked
  by threats in OpenCTI, exercised in OpenAEV). It is read per sync and
  cached with the posture payload; nothing is hard-coded.
- **Label.** The framework's display label is the one the data gives (the
  OpenCTI kill chain or the OpenAEV phase name); a known name gets a
  readable label from one mapping table that holds labels only, never phases
  or ids ("mitre-attack" -> "Enterprise ATT&CK", "disarm" -> "DISARM"). An
  unknown name is shown as written.
- **Default.** The framework with the most techniques carrying data for the
  widget's question (exercised for Validation, tracked for Discovery). A tie
  goes to the one with more phases. With no data at all the widget is "not
  configured" (Connect OpenCTI or OpenAEV), never an empty ATT&CK grid.
- **Selector.** A compact select in the card header of every technique-based
  widget (Validation matrix, Discovery chart, `coverage_matrix`), listing
  each framework with its technique count ("DISARM · 48"). A framework with
  no data for this widget is listed greyed with "no data", and picking it
  draws the not-configured state of the widget. The pick is the widget's
  override of the context (8.3) and is saved with the layout.

### 8.2 The dashboard context bar

A bar under the page title of the command center (and of each stage page,
which reads the same context), one row of chips, each a select:

| Context | Values | Default |
|---|---|---|
| Framework | The frameworks of 8.1 | The one with the most data |
| Scope | All, primary assets, a business service (item B fields) | All |
| Asset groups | OpenAEV asset groups, multi-select | All |
| Intel type | Threat actors, intrusion sets, campaigns, malware, narratives (FIMI), the types the vertical names | The vertical's types |
| Period | 7, 30, 90 days | 30 days |
| Sources | The connected modules (OpenCTI, OpenAEV, OpenCRQ), multi-select | All connected |

- Every widget inherits the context. A chip that holds a non-default value is
  filled and shows a clear button; "Reset" restores the defaults.
- A context value with no data for a widget turns that widget into its
  not-configured state with the reason ("No simulation follows DISARM yet"),
  never a count from another value.
- The context is part of the saved dashboard (8.4). A scope or asset group
  that no longer exists falls back to All, with a one-line notice.
- The headline scores (posture, stage scores) follow Scope and Period only;
  Framework changes what technique widgets count, not how the posture is
  computed, so `compute_posture` and the stage scores stay identical on
  fixtures (section 5).

### 8.3 Widget settings and overrides

Each widget has a settings menu (the gear of the dashboard frame): the
context fields it reads, each "From dashboard" by default or a value of its
own. A widget with an override shows a small "Overridden" chip in its header
with the overridden values in its tooltip.

| Widget | Context it reads | Its own settings |
|---|---|---|
| `hero` | Scope, period, sources | Show the top risk tile |
| `lifecycle_band` (strip) | Scope, period, sources; Framework for the Discovery and Validation metrics | Stages shown (the six by default) |
| `briefing` (risk movers) | Every field | Movers shown (3 or 5), object kinds |
| `act_now` | Scope, asset groups, sources | Rows shown (3 or 5) |
| `stats_coverage` | Every field | Heat map rows (stages or asset groups) |
| `coverage_matrix` | Framework, asset groups, period, sources | Filter (all, covered, gaps) |
| `exposure_stats`, `kpi_*` | Scope, period, sources | Metric shown |
| `done_for_you`, `fleets_row` | Period | Fleets shown (as today) |

The settings are optional per-item fields of the layout (`context` on the
layout, `settings` on an item), validated by the backend schema
(`CtemDashboardLayout`, `extra="forbid"` today: the fields are added there,
each bounded) and ignored when absent.

**Shipped in S2f** (#11547): `context` {`framework` (a chain id the data
carries, at most 128 characters), `period` (7, 30 or 90)} on the layout and
`settings` {`period`} on an item, both `extra="forbid"`, stored with
`exclude_none` so a layout saved without them is stored and read unchanged; a
default value is never stored. The context bar shows Framework (from
`viz.outcome_matrix.chains`, with counts, "Most data" by default and a
one-line notice under the toolbar when a saved framework has no data any
more, the chip then showing the effective one unfilled), Period and Reset;
the Validation stage page opens its matrix on the context framework of the
vertical shown (`savedLayoutFramework`); the gear of `hero`, `value_band`, `exposure_stats` and
`stats_coverage` sets their own period, marked "Overridden". Scope, asset
groups, intel type and sources wait for the data that fills them (not drawn
empty); the other per-widget settings of the table above come with their
widgets.

### 8.4 The command center is the user's own

- **Edit mode** (exists today on the grid, kept): add from the catalog,
  remove, swap (drop a widget on another), resize, reorder; plus configure
  (8.3). Every widget of section 4 is in the catalog.
- **Several saved dashboards.** A dashboard switcher beside the title lists
  the person's dashboards (name, context summary), with "New dashboard"
  (from the default, or a copy of the current one), rename, duplicate,
  delete, and "Set as default". Examples a person may build: "Enterprise"
  (Enterprise ATT&CK), "FIMI" (DISARM, narratives), "AI" (ATLAS).
- **Storage.** Today `users.ctem_dashboard_layouts` maps a vertical to one
  layout. S2 keeps that key per vertical and lets its value also be
  `{dashboards: [{id, name, context, items, hidden}], default_id}`. A stored
  bare layout reads as one dashboard named after the vertical, default, with
  the default context, so every saved layout loads unchanged; it is rewritten
  in the new form only when the person saves. At most 20 dashboards per
  vertical.

### 8.5 Mockups of two contexts

Attached to the UX design item of round 4: the command center on
"Enterprise (ATT&CK)" and on "FIMI (DISARM)", with the context bar and the
dashboard switcher; the Validation hero on Enterprise ATT&CK, DISARM and
ATLAS; the DISARM not-configured state; the widget settings panel. All
figures are mock data; the DISARM and ATLAS phase names in the mocks come
from the public framework definitions, as the data would carry them.

## 9. The Exposure Posture dialog (R5-B1, Sam 13 Nov 2026 ~11:50 Paris)

Clicking the posture ring of the hero opens "How the Exposure Posture is
computed" (`CtemCommandCenter.tsx`, the `postureExplainOpen` dialog). Today
it lists STAGE / WEIGHT / SCORE and the quantitative posture only. It
answers "why is my posture this number, on measured data and in my
context?" and shows both scores.

**Layout.** The dialog is `DialogContent size="md"` (640 px, DS 1.5.1
`Dialog.md`; R6-B1), never a local width class. A four-column grid,
numbers right-aligned and tabular:

| Column | Content |
|---|---|
| Stage | The stage label (`CTEM_STAGE_CONFIG`) |
| Weight | `posture.weights[stage]` as a whole percent (`formatPercent`) |
| Quantitative | Header: the `database` icon and "Quantitative" (tooltip: "measured data from your connected platforms"). Cell: the outer-ring swatch, `stage.score` in its band colour, the band name |
| Qualitative | Header: the `building-2` icon and "Qualitative" (tooltip: "the measured score judged against your declared organization context"). Cell: the inner-ring swatch, `stage.qualitative_score` in its band colour, the band name |

The icons, swatches and tooltip words are the ones of `ScoreRingKey`
(SP1), so the dialog, the hero and the strip read as one legend. The band
name is printed next to the colour (colour is never the only cue).

**Totals.** Under a rule, the "Exposure Posture" row: weight 100%, then
`posture.posture_score` with its band in the Quantitative column and
`posture.qualitative_score` with its band in the Qualitative column. The
hero ring keeps showing the same two numbers.

**Not configured (R6-B2).** The chip names the action, never a bare
status (Sam, standards section 5), with the same words the strip uses for
the same state. A stage whose `score` is null shows, in both score
columns, the dashed action chip of rule 5 naming the product to connect
for that stage ("Connect OpenAEV", "Connect OpenCTI"), the strip printing
the same words in that stage's column. A stage that has a quantitative
score but no context (`qualitative_score` null) shows the chip only in the
Qualitative column: "Add organization context", linking to Settings >
Organization. Both chips are links. A null is never printed 0 or "--".
The weight stays printed: it is the configured weight, renormalized away
from the totals.

**Footnote (R6-B3: one list item per idea, every exception the formula
applies).** A `<ul>`, one `<li>` per line, each one idea; no paragraph.
The five lines and the code each one states, from
`backend/app/services/ctem_context_scoring.py`:

| Line (en) | Code it states |
|---|---|
| "Quantitative is the measured stage score. Qualitative = Quantitative × (40% + 60% × the share of the stage's context fields filled)." | `qualitative_stage_score`: `round(E * (CONTEXT_FLOOR + (1 - CONTEXT_FLOOR) * coverage))`, `CONTEXT_FLOOR` 0.4, `coverage` from `stage_coverage` |
| "Prioritization: its Qualitative starts from the average of Quantitative and your stack's known-exploited score (100, minus 20 per known-exploited CVE in software you run)." | The exception: for `prioritization`, `E = (Q + T) / 2`, `T = tailored_kev_score(kev_tailored_count)` = `max(0, 100 - TAILORED_KEV_PENALTY * n)`, `TAILORED_KEV_PENALTY` 20. Printed only when `headline.kev_tailored_count` is a number (else `E = Q` and the line is left out, as the code does) |
| "A stage with none of its context fields filled has no Qualitative score." (with the stage named when one is in that state: "(Remediation)") | `coverage == 0` returns `None`; no profile at all returns `None` for every stage |
| "Weights are renormalized over the stages that have a score, separately for each score." With a stage not configured: "Validation has no data: its weight is spread over the other stages, for each score." | Both composites skip a null stage: `compute_posture` for the quantitative one, `with_context_scores` for the qualitative one |
| The band scale (`CTEM_BAND_SCALE_TEXT`, unchanged) | `band()` |

The constants are rendered from the payload, never typed in the i18n
string: the backend adds `context_floor` (0.4) and
`tailored_kev_penalty` (20) to the posture payload next to `weights`, and
the lines interpolate them, so a change of a constant cannot leave the
footnote untrue. A unit test asserts the footnote has the Prioritization
line when `kev_tailored_count` is set and not when it is null, and that
the percentages printed equal the payload constants. Checklist line kept
in section 10: "An explainer prints every exception the formula applies."

**One source, no drift.** The dialog renders the same `posture` payload and
the same `stageByKey` map the hero, the strip and the stage pages read; it
computes nothing. A unit test renders the command center on a fixture with
one stage not configured and one stage with no context and asserts that
every dialog cell equals the strip cell and the stage page header of the
same stage (same test ids, `ctem-posture-dialog-<stage>-quantitative` and
`-qualitative`), and that both totals equal the hero pair. For the stage
with no context it also asserts that the strip ring renders no inner arc
(`ctem-strip-<stage>-inner-arc` absent, the dashed inner track present)
and that the strip link and the dialog chip render the same i18n key and
the same href. A backend test
keeps `posture_score` and `qualitative_score` identical on the fixture
before and after (section 5).

**Accessibility.** The grid is a `<table>` with `<th scope="col">` headers
(the icon `aria-hidden`, the word read); each not-configured chip is a link
with its target in its accessible name ("Validation: not configured,
connect OpenAEV"; "Remediation: no organization context, add
organization context").

**Build.** Slice S2 (the command center), with the strip, since both read
`ScoreRingKey`. New i18n keys go through one `sandbox_json_keys` call in
every locale catalog.

**Mocks (round 6).** The dialog at 640 px over the command center: all
stages configured (dark and light, 1080 and 1728); Validation not
configured (dark 1728, light 1080), the strip showing the same "Connect
OpenAEV" column; Remediation with no context (dark 1080, "Add
organization context" in the Qualitative column). The hero pair equals
the dialog totals in every capture, the command center captures included
(52 / 47; 52 / 46 with Validation not configured; 52 / 44 with
Remediation without context); the round 6 generator asserts it before it
writes a file. Round 7 adds Remediation with no context in all four
themes and widths (dialog open and closed): the strip column draws the
dashed inner track with no inner arc and prints "Add organization
context" as its delta-row link; the round 7 generator asserts the arc is
absent and the label is printed in both the strip and the dialog.

## 10. Self-check, rounds 5 to 7

| Finding | Where it is fixed | Evidence |
|---|---|---|
| R7 (round 6 B1) stage with a score but no context | Section 4 strip Not configured (one shared label "Add organization context", no inner arc, dashed inner track, link), section 9 One source (test asserts no inner arc and the same key and href) | Round 7 mocks cc-nq and posture-nq, dark and light, 1080 and 1728: Remediation ring with dashed inner track, the link in the strip and the chip in the dialog; hero 52 / 44 equals the dialog totals |
| R6-B1 dialog width | Section 9 Layout: `DialogContent size="md"` (640 px) | Round 6 posture mocks: dialog 640 px at 1080 and 1728 |
| R6-B2 chip names the action | Section 9 Not configured, Accessibility | posture-nc mocks print "Connect OpenAEV" in the dialog and the strip; posture-nq prints "Add organization context" |
| R6-B3 footnote prints every exception, one line per idea | Section 9 Footnote table (Prioritization known-exploited line, no-context line, constants from the payload, test) | Every posture mock: five list items |
| R6-B4 hero equals totals | Mocks regenerated and recaptured | Command center light 1080 and dark 1728 read 52 / 47, the dialog totals |
| Checklist: an explainer prints every exception the formula applies | Section 9 Footnote | Code-to-line table |
| R5-B1 posture dialog | Section 9 | Mocks posture-* (6), one source rule and test named |
| R5-B2 compact formatter | Rule 12, section 2 Totals, section 4 | Mocks print "1.3K" (`formatCompactNumber` in en); no local `Intl.NumberFormat` left in the note |
| Requirement 1 (columns, icons, colours) | Section 9 Layout | Mocks |
| Requirement 2 (not configured, renormalization) | Section 9 Not configured, Footnote | posture-nc mocks |
| Requirement 3 (footnote) | Section 9 Footnote | Mocks |
| Requirement 4 (same values) | Section 9 One source | Hero equals totals in the mocks; test named |
| Requirement 5 (scores, layouts) | Section 5 | Unchanged |

Advisories A1 to A7 of round 4, A1 to A5 of round 5 and A1 to A2 of round 6 are noted on
#11547 and are taken in the S1 and S2 lab rounds; none changes this
note's design.
