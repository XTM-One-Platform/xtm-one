# Dashboard rendering reference: OpenCTI and OpenAEV widgets

XTM One draws the dashboard widgets of its modules with **one generic visualization engine**
(`frontend/src/components/custom-dashboard/charts/`, `viz/`): product knowledge lives only in the
server adapters (`backend/app/services/dashboard_live/<platform>.py`,
`dashboard_platform/<platform>.py`), which turn a module's answer into generic data and a
presentation preset of generic options. The engine must reproduce each module's own widget
rendering - full compatibility with the existing products comes first; nothing is invented.

This page is that reference: how OpenCTI and OpenAEV draw every widget type of their custom
dashboards, read from their code (paths relative to the `opencti/` and `openaev/` repositories;
`file:line` references as of October 2026). The engine follows it, with these decisions:

- **Frame.** One XTM One widget frame for every module (title, border, padding); only the content
  reproduces the module's rendering.
- **Stack.** Recharts, SVG, `@xyflow/react` and the Filigran design system - not ApexCharts, MUI or
  react-wordcloud. Library defaults the products rely on are reproduced where this page names them.
- **Map.** XTM One's bundled atlas (no tiles: the CSP and the PDF export), styled as OpenCTI's map
  (threat colours by level, opacities, city pins, zoom and centre).
- **Defects.** Where a module's code visibly fails at what it means to do, the engine draws the
  intent, and the defect is listed below to be fixed in the module too.
- **Existing dashboards.** A platform widget drawn with its module's own view takes the reproduced
  rendering; a widget drawn with another view keeps XTM One's charts and options.

## The engine's contract

- **Generic data.** The closed shapes of `dashboard_live` carry, optionally, what a module's widget
  draws besides its values, in the platform's vocabulary: an item's, a series' or an event's `color`
  (strict hex or a named colour), `entityType` and `link` (built by the adapter from the
  integration's base URL); a series point's `key` (what a click filters on); a number's `previous`,
  `period` and `icon` (a Lucide name); a rows column's cell `kind` (`number`, `date`, `datetime`,
  `entityType`, `chip`, `chips`), a row's `_entityType` and `_icon`; an event's `secondary` text; a
  region's `level` (0-9). A field that does not validate is dropped. Data written in a dashboard
  links only inside the platform.
- **Presentation.** The data response carries `presentation: {view, options}`, computed by the
  adapter's `presentation(native)` and validated against the view's options model, so a preset can
  only say what a person could set. The frontend lays it under the visualization's own options when
  the widget is drawn with that view (`definition.presentedOptions`). It is never cached, and a
  derived source does not inherit its parent's.
- **Primitives.** `charts/formats.ts` (SI, compact and locale numbers, the named date formats, axis
  dates per interval, durations, count-up) and `charts/palettes.ts` (the Material ramps, result,
  threat and heat palettes, heat ranges, the coverage, average and gauge rate scales) hold the values
  of this page, by name.
- **No module in the engine.** `frontend/tests_unit/dashboard-engine-no-product.test.ts` fails when
  the code of `charts/` or `viz/` names a module.

## Defects drawn as intended (to fix in the modules)

| Module | As coded | As drawn by XTM One |
|---|---|---|
| OpenAEV | Attack path nodes never receive `total`, so every node is muted (`AttackPatternNode.tsx:20-25` vs `AttackPatternBox.tsx:33`) | Nodes coloured by their success rate, with the count |
| OpenAEV | The attack path legend's `marginLeft: auto` has no effect on an auto-width block: it sits left (`AttackPath.tsx:287-291`) | Legend on the right |
| OpenAEV | The attack path canvas keeps React Flow's own background (`#141414` in the dark theme), a square inside the card (`AttackPath.tsx:71-83` overrides the node background only) | The canvas takes the widget's surface |
| OpenAEV | A first-load error shows the raw "Not implemented yet" for every widget but the vertical bars (`WidgetViz.tsx:77-209`) | The error message |
| OpenCTI | The distribution list shows the raw key `entity_<type>` instead of its translation (`StixCoreObjectsDistributionList.tsx:109-115`) | The translated entity type |
| OpenCTI | A relationship list's icon column passes `color="primary"` into `style.color` (`WidgetListRelationships.tsx:60-65`) | The primary colour |
| OpenCTI | The radar's click handlers call an undefined `handleClick` (`Charts.jsx:621-624`) | No click on the radar (as observed) |
| OpenCTI | A heatmap whose values span fewer than 9 gets a step of `trunc((max−min)/9) = 0`: its ranges cover only the lowest value and every other cell takes the series' default colour (`WidgetMultiHeatMap.tsx:57-73`) | Steps at least 1 wide (`charts/palettes.heatRanges`) |
| OpenCTI | A donut's or a polar area's own colours (labels, markings, statuses) drop the items without one (`filter(Boolean)`), so every later item takes its neighbour's colour (`WidgetDonut.tsx:28-50`) | Each item its own colour, an item without one the palette's at its place |
| OpenAEV | The posture radar tests SUCCESS / DETECTED / PREVENTED before FAIL / "NOT ", so a "NOT DETECTED" or "NOT PREVENTED" series is drawn green (`PostureRadarWidget.tsx:76-88`) | The misses tested first: red |

## The chart kinds

The engine's chart renderers (`charts/XYChart.tsx` for `lineArea` and `bars`, `charts/PieChart.tsx`
for `donut`, `charts/PolarCharts.tsx` for `radar`, `polarArea`, `wordCloud` and `heatmap`,
`charts/TreemapViz.tsx` for `treemap`) draw XTM One's own look by default, and a module's look from
the generic options of `charts/look.ts` (`_Look` in `dashboard_visualization_options.py`). Each
adapter's `presentation(native)` (`dashboard_live/opencti.py`, `openaev.py`) sends them per native
type, with the data's colours, keys and links. The radar, polar area, word cloud and heatmap are
drawn as the modules draw them by default (XTM One had no look of its own for them).
`backend/tests/fixtures/dashboards/module_charts.json` records each type's answer, data and preset;
`test_dashboard_live_charts.py` and `charts/ChartKinds.test.tsx` both read it.

| Kind | Widgets | Preset (generic options) | Data the adapter adds |
|---|---|---|---|
| `lineArea` | OpenCTI line, area | accent then `materialMixed`, `siSpaced`, `dateFormat` by interval (`monthYear` for month / quarter, `year`, else `date`), `xAxisType: time` for day / week, x 12 px, y 14 px, labels in `text`, no axis line, horizontal grid at 0.1, legend from `parameters.legend`; the area adds `fill: gradient` 0.7 → 0.1 and `stackMode` from `parameters.stacked` | - |
| `lineArea` | OpenAEV line | one series `paletteLead: accent`, several `palette: material`; an area under each line (`fill: gradient` 0.35 → 0.02), markers 4 px, time axis, `longDate`, legend below, sample `temporal` | point `key` |
| `bars` | OpenCTI vertical bars | as the line, a category axis, `barRadius` 4, `barSize` 70 (the library's column width) | - |
| `bars` | OpenCTI horizontal bars | `barSize` 30, `barRadius` 4, two value labels (`valueTickCount: 1`), `colorByPoint` from `parameters.distributed`, `openLinks` | item `color` (entity type's family, or the label's / marking's / status' own), `entityType`, `link` (not for "Restricted") |
| `bars` | OpenAEV vertical bars | `results` palette, `fill: gradient` 0.95 → 0.65, `barRadius` 4, `barSize` 55, `barGap` 4, locale numbers, time axis and `longDate` when temporal, sample `temporal` or `platforms` | series `color` by meaning (`chartColorUtils.ts`), a breakdown's items its series' colour, `key` |
| `bars` | OpenAEV horizontal bars | `fill: gradient` 1 → 0.75 (horizontal), `barRadius` 5, `barSize` 38, `barGap` 3, vertical grid at 0.08, labels cut at 28 characters (`…`), sample `ttp`; one series: `colorByPoint` (accent then `material`), values at the bars' ends (`dataValuesStrong`, locale), no legend | `key`; two series: colour by meaning |
| `donut` | both donuts | its palette (`materialMixed` / `material`), `separatorWidth` 3, `donutHoleSize` 70, shares on the ring (`labelDisplay: percentage`, none under 10°), label-only tooltip, legend below, nothing collapsed; OpenAEV's sample `status` | item `color`: OpenCTI's own colours or true / false in Material green / red; OpenAEV's statuses (`getStatusColor`) or true / false; `key` |
| `radar` | OpenCTI radar | accent, `areaOpacity` 0.2, `lineWidth` 1, markers 5 px, labels cut at 25 on a word (`...`), polygons at 0.1, no legend for one series | - |
| `radar` | OpenAEV posture radar | `lineWidth` 2, `areaOpacity` 0.18, markers 3 px, `radius` 108, labels 10 px in `muted`, polygons at 0.08, legend below, sample `radar` (lighter grey) | series `color` (success, a miss red, else accent / secondary / warning), point `key` |
| `polarArea` | OpenCTI polar area | `materialMixed`, `areaOpacity` 0.5, a 2 px surface line between slices, rings at round steps (`polarRings`) with their values up the top spoke, spokes, label-only tooltip, legend below | item `color` as the donut's |
| `heatmap` | OpenCTI heatmap | `colorRanges: heat` (ten ranges, zero apart, `palettes.heatRanges`), the ranges as the legend (`from - to`), the first series at the bottom row, cells parted by 1 px, `date` x labels 12 px, y 14 px | - |
| `treemap` | OpenCTI tree | distributed: `colorByPoint`, `materialMixed` without its near-white colours; else the accent in shades of the value (`valueShades`: half way to black in the dark theme, to white in the light one); `separatorWidth` 3, the name centred in bold (12 px at most), the value in the tooltip | - |
| `wordCloud` | OpenCTI word cloud | `materialMixed`, 20 → 50 px on d3's log scale (truncated to a pixel, one word alone at 35), rectangular spiral (4 px down, 4 × width / height across), tooltip `word (value)` | - |

A data colour named in its dark tint is drawn in the light theme as that theme draws it
(`palettes.themedColor`: a Material 400 tint takes its 600 twin, white turns black). A module's
primary colour is the design system's own brand primary (`--color-module-brand`, which XTM One mode and an
operator's branding leave alone): a module's widget names it as its content's accent (`accentColor:
brand`, set as `--color-accent` on the content alone, the frame keeping the platform's own); a theme
colour that is a design-system
token (success, info, warning, alert, neutral) is that token, which matches both modules in the dark
theme.

Library defaults assumed, not read in a repository (the charts library is not installed here):
ApexCharts' radar markers (size 5), its polar area's slice stroke (2 px), its treemap's shades
(`enableShades`, intensity 0.5) and centred labels, its heatmap's legend of ranges and bottom-up
rows, its donut's percentage labels (one decimal, none under 10°), d3-cloud's spiral and size
truncation.

Deviations, for later: the word cloud's colours and first positions are not react-wordcloud's
seeded random ones (colours follow the data's order, the spiral starts at the centre); the area's
`gradientToColors` (each series fading to the primary) is not drawn; entity-type labels are the
platform's names ("Intrusion-Set"), not OpenCTI's translations; OpenAEV's bars of two
`structural-histogram` series draw their first series only (the `distribution` shape, set by
`dashboard_platform/openaev.py`); OpenAEV's raw axis numbers (`toString`) are written in the locale
(`1,234`); the chart hover "lighten" filter is not drawn.

## Values, lists, cards, timeline, text and map: how the engine draws them

Each kind is one renderer whose look its options choose; the adapters' presets
(`presentation(native)`) choose OpenCTI's or OpenAEV's.

| Kind | Option (values) | Renderer | Reproduces |
|---|---|---|---|
| number | `valueStyle` (`standard`, `difference`, `tile`) of `xtm.singlevalue` | `charts/ValueStyles.tsx`, `viz/value.ts` | `difference`: OpenCTI's number (32 px SI value, the change written whole, its arrow and "(24 hours)"); `tile`: OpenAEV's (42 px icon tile at 10% with a 13% glow, 14 px value counting up over 1200 ms in compact form, trend chip at 14% / 28% with "(n previously)") |
| table / list | `listStyle` (`compact`, `table`, `rows`, `cards`), `rowHeight` (32-64), `cardsPerRow` (1-6) of `xtm.list` | `charts/RowsViews.tsx`, `charts/cells.tsx`, `viz/rows.ts` | `table`: OpenCTI's DataTable widget variant (42 px header, 48 px rows, equal columns, chips, `+N` label overflow, 42 px arrow column, a 56 px icon column when rows carry an icon); `rows`: OpenAEV's list (50 px rows, leading icon, `100/n`% columns of 10% at least, hover accent, trailing arrow); `cards`: OpenCTI's bookmarks |
| distributionList | `listStyle` (`bars`, `rows`) of `xtm.distribution` | `charts/DistributionList.tsx` | `rows`: OpenCTI's distribution list (50 px rows, icon in the item's colour, 18 px SI value in the accent, a link only on an entity) |
| timeline | `timelineStyle` (`lanes`, `alternate`) of `xtm.timeline` | `charts/AlternateTimeline.tsx`, `viz/alternate.ts` | `alternate`: OpenCTI's timeline (date opposite, outlined dot in the entity's colour holding its icon, card with the representative and its Markdown second line cut to 150) |
| markdown | a text widget's `format: "markdown"` | `charts/MarkdownText.tsx` | OpenCTI's text (GitHub-flavoured Markdown, first heading flush); an imported OpenCTI text widget gets it (`dashboard_interop/opencti.py`) |
| map | `regionColors` (`classes`, `level`), `basemap` (`surface`, `contrast`), `markerIcon` / `markerColor` / `markerSize`, `zoom`, `centerLat` / `centerLng`, `centerOnFirstMarker` of `xtm.map` | `charts/MapViz.tsx`, `viz/geo.ts` | OpenCTI's map: regions by level on the threat scale (fill .1 dark / .5 light, 1 px outline drawn above the neighbours' borders), its night / day basemap colours, city markers, a Web Mercator view at the widget's zoom minus one on its centre, else its first city, else Paris |

What the adapters send for them: OpenCTI's entity colours (`itemColor`, the hash
of `stringToColour` included), icons and English type names come from
`dashboard_live/opencti_entities.py`; marking colours from `ItemMarkings`'
severities as named colours; OpenAEV's status chips from `statusSeverity` and
`computeInjectExpectationLabel`; every link is built by the adapter from the
integration's base URL and an id.

**Deviations (said, not hidden):**

- **Frame and list details read from the products.** `expandShown` keeps the
  frame's "Expand" in its header, before the menu (OpenAEV's coverage matrix
  offers its fullscreen there); an OpenCTI title (`capitalize`) wraps onto a
  second line rather than ending in "..."; "No data has been found." is in the
  text's own colour (OpenCTI's plain paragraph). `arrowColor` draws a list's
  arrow in the accent (OpenCTI's tertiary icon button). OpenCTI's relationship
  rows mean their icon in the primary colour (`ItemIcon color="primary"`), but
  `ItemIcon` writes the word as CSS, which the browser drops, so OpenCTI shows
  it white: the engine draws what the code means (to fix in OpenCTI). The
  relationship glyph (`VectorRadius`) and OpenAEV's `HubOutlined` have no
  Lucide twin: `spline` and the design system's mapping (`network`) stand in.
- **Category labels as ApexCharts tilts them.** `crowdedLabels: rotate` writes
  every category label at -45 degrees once the widest no longer fits its
  category's room (`look.crowdedAxis`), as an OpenCTI vertical bar chart does;
  without it some labels are dropped.
- **A radar of two axes.** Its labels sit above and below the shape, so they
  keep their 25 characters, and the radius is ApexCharts' (`Radar.js`: the
  box's shorter side over 2.1, less the stroke and the longest label over
  1.75); with three axes or more the labels beside the shape still bound it.
  With `niceTicks` the rim is the value axis' nice end (`niceScale`), and the
  markers are solid (the shape's 0.2 fill does not reach them).

- **One frame, the module's title.** The engine draws one frame for every
  module, its title row above the card as OpenCTI's (OpenAEV writes its title
  inside the card: OpenAEV is to follow the common frame, not the reverse);
  what it writes in its title row is the module's: `titleStyle`
  `capitalize` (OpenCTI: each word's first letter, secondary text) or `caps`
  (OpenAEV: 11 px letter-spaced capitals, 85% opacity), and `headerTitle` in
  place of the visualization's title (OpenCTI's number heads its card with
  "Entities number" and writes its own title in the body as `topLabel`, beside
  the change). Nothing to show reads as the module says it (`emptyMessage`:
  OpenCTI's "No data has been found.", OpenAEV's "No data to display" under an
  inbox, across the widget). OpenAEV's list paginates in its title row; the
  engine says "n of total". The "Open in" link is the widget menu's, not a
  footer under the drawing.
- **Axes and legends as ApexCharts lays them out.** `niceTicks` reads the value
  axis in round steps from the data's bounds (`charts/nice-scale.ts`, a port of
  ApexCharts' `niceScale`: a series of zeros reads 0 to 2, a horizontal bar of
  value 4 asked for one interval reads 0 to 5); bars stand on zero; a
  horizontal bar's labels hold on one line and the grid parts its categories.
  `legendMarker` (`circle`, `square`) and `legendLabelColor` draw the legend's
  marker and text; `pieAlign: top` sets a donut at the top of its widget, as
  large as the width and the room above its legend allow. A sample of more
  series than the widget has (OpenAEV's TTPs) is drawn as several: with its
  legend, no value written on its bars; a series' categories keep the order
  the module sends them (a radar's axes), dates are sorted in time.
- **OpenAEV's number in words.** Its title's info sign says the change as
  OpenAEV does ("+3 findings over the last 3 months (1 previously)"); the
  engine puts the sign beside the trend chip.
- **Icons.** Material and MDI icons become Lucide through the design system's
  `icon-mapping.json`; where it has none, the nearest Lucide icon
  (`ShieldSearch` → `scan-search`, `LockPattern` → `lock-keyhole`,
  `LaptopAccount` → `laptop-minimal`, `Fire` → `flame`, `BoltOutlined` → `zap`,
  `CrisisAlertOutlined` → `siren`, `SelectGroup` → `group`, `city_orange.png` →
  `building-2` in `#d95f00`, 25 px).
- **Links.** An OpenCTI row, bookmark, timeline entry or distribution item opens
  `/dashboard/id/{id}` (OpenCTI's redirect to any entity's page); a relationship
  timeline entry opens its remote end, not `…/knowledge/relations/{id}`.
  OpenAEV's vulnerable endpoint opens its asset without the CVE filter, and an
  inject outside a simulation its atomic testing (not the scenario's filtered
  list).
- **Not read.** OpenCTI custom attribute columns (empty cells); OpenAEV's tag,
  attack pattern and expectation source columns are shown as their raw values
  (OpenAEV resolves them with further calls), and an asset row leads with the
  device icon, not its category's.
- **Chips.** OpenCTI's markings show one chip and a dot badge; the engine fits
  what it can and ends with `+N`, as for labels. Chip widths are estimated
  (7 px a character), not measured.
- **Colours by theme.** A colour an adapter sends is one value: OpenAEV's
  scenario tile is `secondary.main` of the dark theme (`#00f0bc`) in both;
  OpenCTI's "white becomes black in the light theme" rule for an item's own
  colour is not applied.
- **Map.** The bundled 1:50m atlas: no roads, place names or tiles; the map is
  not zoomable with the wheel.
- **As coded, not fixed.** OpenAEV's list columns are all `100/n`% (its
  `ColumnStyles` loop iterates indices, so a fixed width is never counted); a
  heading in OpenCTI's timeline card is lower case but its first letter.
- **Fixed in the adapter.** OpenCTI's number read `count` as its change and up
  to now: it is `total - count`, `count` read up to a day ago (`dayAgo()`).

## OpenCTI

Everything below comes from the code; a value that comes from a library default, not from OpenCTI's code, says so.

**Root (R)** = `opencti/opencti-platform/opencti-front/src`. In the text, `R/...` means that prefix.

### 0. File index

- **Dispatch:**
  - R/components/dashboard/DashboardContent.tsx
  - R/components/dashboard/DashboardViz.tsx
  - R/components/dashboard/DashboardEntitiesViz.tsx
  - R/components/dashboard/DashboardRelationshipsViz.tsx
  - R/components/dashboard/DashboardAuditsViz.tsx
  - R/components/dashboard/DashboardRawViz.tsx (text widget)
- **Chrome:**
  - R/components/dashboard/WidgetContainer.tsx
  - R/components/common/card/Card.tsx
  - R/components/common/card/CardTitle.tsx
  - R/components/common/label/Label.tsx
  - R/components/dashboard/WidgetRenderContent.tsx
  - R/components/dashboard/AuditsWidgetRenderContent.tsx
  - R/components/dashboard/WidgetNoData.tsx
  - R/components/dashboard/WidgetAccessDenied.tsx
  - R/components/dashboard/WidgetNoHostEntity.tsx
  - R/components/dashboard/WidgetNoSavedFilters.tsx
  - R/components/dashboard/WidgetNotImplemented.tsx
  - R/components/dashboard/DashboardWidgetPopover.jsx
  - R/components/Loader.tsx
  - R/private/components/common/charts/ChartExportPopover.tsx
  - R/private/components/common/charts/Chart.tsx (ApexCharts wrapper)
- **Leaves:** R/components/dashboard/Widget{Number,DistributionList,ListCoreObjects,ListRelationships,ListAudits,VerticalBars,MultiLines,MultiAreas,MultiHeatMap,Timeline,Donut,HorizontalBars,Radar,PolarArea,Tree,Bookmarks,WordCloud,Text}.tsx
- **Option builders:** R/utils/Charts.jsx
- **Helpers:**
  - R/utils/hooks/useDistributionGraphData.ts
  - R/utils/defaultRepresentatives.ts
  - R/utils/hooks/useTimeSeriesAxisFormatter.ts
  - R/utils/Number.ts
  - R/utils/Colors.ts
  - R/components/ItemIcon.tsx
  - R/components/NumberDifference.tsx
  - R/components/i18n.jsx
  - R/utils/Time.ts
  - R/utils/Entity.ts (resolveLink)
  - R/utils/hooks/useAppData.ts (computeLink)
- **Containers (data):**
  - R/private/components/common/stix_core_objects/StixCoreObjects*.tsx
  - R/private/components/common/stix_relationships/StixRelationships*.tsx
  - R/private/components/common/stix_relationships/StixRelationshipsMultiHorizontalBars/{StixRelationshipsMultiHorizontalBars.tsx, useStixRelationshipsMultiHorizontalBars.js}
  - R/private/components/common/stix_domain_objects/StixDomainObjectBookmarksList.tsx
  - R/private/components/common/audits/Audits*.tsx
- **Map:**
  - R/private/components/common/location/LocationMiniMapTargets.tsx
  - R/private/components/common/location/GeoMap.tsx
- **Tables:**
  - R/components/dataGrid/DataTableWithoutFragment.tsx
  - R/components/dataGrid/components/{DataTableComponent,DataTableBody,DataTableLine,DataTableHeader}.tsx
  - R/components/dataGrid/dataTableUtils.tsx (column registry)
  - R/private/components/widgets/WidgetListsDefaultColumns.ts
- **Theme:**
  - R/components/ThemeDark.ts
  - R/components/ThemeLight.ts
  - R/components/fds-tokens.generated.ts
  - R/static/css/custom-theme-tokens.css
  - R/utils/hooks/useFdsThemeScope.ts

---

### 1. Grid and widget chrome (common to all)

**Grid (DashboardContent.tsx:51-61)**
- react-grid-layout with `cols: 12`, `rowHeight: 62`, `margin: [20,20]`, `containerPadding: [0,0]`.
- A widget of h rows is h·62 + (h−1)·20 px tall.
- Dragging is cancelled on the `.noDrag` class. Every interactive element (popover, export button, list rows, timeline dots) carries `noDrag`.
- In edit mode, the widget being resized renders an empty `<div/>` (l.78).
- The placeholder is `2px solid primary.main`, borderRadius 4 (l.43-47). The resize handle in dark mode is `2px solid rgba(255,255,255,0.4)` (ThemeDark.ts:686-689).

**Perspective dispatch (DashboardViz.tsx:29-58)**
- `entities` goes to DashboardEntitiesViz, `relationships` to DashboardRelationshipsViz, `audits` to DashboardAuditsViz.
- `perspective === null` goes to DashboardRawViz. That covers `text` and also `custom-attributes`.
- Unknown types render WidgetNotImplemented: the container with the text "Not implemented yet".
- Everything is wrapped in an ErrorBoundary.

**Type coverage**

| Type | Entities | Relationships | Audits |
|---|---|---|---|
| bookmark | ✓ | – | – |
| number, list, distribution-list, vertical-bar, line, area, donut, horizontal-bar, radar, polar-area, heatmap, tree, wordcloud | ✓ | ✓ | ✓ |
| timeline | ✓ | ✓ | – |
| map | – | ✓ only | – |

**WidgetContainer (WidgetContainer.tsx:27-96)**
- Outer `div` with height = `height || '100%'`.
- If `variant` is not `'inLine'`/`'inEntity'` (always the case on dashboards), it renders `Card` with:
  - `title`: if `showPreviewTag`, a row with a Tag "Preview data" in `designSystem.tertiary.orange[400]` (#f2933a).
  - `warning`: a ReportProblemOutlined icon (15px, `alert.warning.primary`) with a tooltip.
  - `action`: `<div>{chart && <ChartExportPopover/>}{popover}</div>`.
- Children are wrapped in an ErrorBoundary whose not-found fallback is WidgetNoData.

**Card (Card.tsx:23-127)**
- A vertical `Stack` of height 100%.
- **The title bar sits OUTSIDE and ABOVE the bordered surface** (CardTitle, then CardMui).
- Surface styles:
  - `borderRadius: theme.spacing(0.5)` = 4px.
  - `background: var(--bg-elevation-default-layer-1)`.
  - `border: 1px solid var(--border-elevation-subtle-soft-layer-1-transparency-15)`.
  - `flexGrow: 1`, elevation 0.
- Padding by prop (spacing unit = MUI default 8px):

| Padding prop | Value |
|---|---|
| `default` | 24px |
| `small` | 8px |
| `medium` | 16px vertical / 24px horizontal |
| `horizontal` | 8px vertical / 24px horizontal |
| `none` | 0 |

**CardTitle (CardTitle.tsx:20-50)**
- Row: `justify space-between`, `align center`, height 19px, marginBottom 8px.
- Text: 12px / line-height 19px / weight 400 / "IBM Plex Sans" / `textTransform: capitalize` / colour `text.light` (dark #AFB0B6).
- Actions sit on the right in a row with gap 8px.

**Padding used per container**

| Padding | Widgets |
|---|---|
| `medium` | number (all perspectives) |
| `horizontal` | list (entities, relationships, audits) |
| `small` | vertical-bar, line, area, donut, horizontal-bar (all variants), radar, polar-area, heatmap |
| `none` | map |
| `default` (24px; no prop passed) | distribution-list, timeline, tree, wordcloud, bookmark, text |

**Popover (DashboardWidgetPopover.jsx:46-75)**
- IconButton small, colour primary, `MoreVert` icon small.
- Menu items: Update (DashboardWidgetConfig), Export, Duplicate, Delete.
- Duplicate opens a confirm dialog. Delete opens a DeleteDialog.

**Chart export button (ChartExportPopover.tsx:49-73)**
- IconButton small, colour primary, `FileDownloadOutlined` small.
- Menu: "Download as PNG" / "Download as SVG" / "Download as CSV".
- It appears only when the container passes `chart`:
  - Entities: polar-area, heatmap, tree.
  - All relationships Apex charts.
  - All audits Apex charts.
  - NOT entity vertical-bar, line, area, donut, horizontal-bar or radar (they do not wire `onMounted`).

**Guard and loading (WidgetRenderContent.tsx:32-52)**, checked in this order:
1. Missing host entity: "This widget requires a context entity to be displayed."
2. Missing saved filter: "The saved filter used by this widget is not available."
3. Not granted: "This feature is only available in OpenCTI Enterprise Edition." or "You are not authorized to see this data." Only audits use this; it needs EE and the SETTINGS_SETACCESSES / SECURITYACTIVITY / VIRTUAL_ORGANIZATION_ADMIN rights (AuditsWidgetRenderContent.tsx:42-49).
4. No queryRef yet, or Suspense: `Loader inElement`. This is a table-cell, vertically and horizontally centred, 5rem wide. It shows a `FiligranLoader` height 40 in colour `common.grey` (#95969D dark), or a CircularProgress size 40 thickness 1 when EE+whitemark (Loader.tsx:115-145).

All centred messages are a flex box with height 100%, centred both ways, text in a `<p>` or `<span>`. Default empty text: "No data has been found." (WidgetNoData.tsx:4,14-23).

**Chart wrapper (Chart.tsx:62-124)**
- `react-apexcharts` at width/height 100%.
- Adds `chart.events.mounted`.
- HTML-escapes the legend formatter and the tooltip x/y-title formatters.
- The tooltip y title defaults to `"<seriesName>: "`.
- For non-datetime axes, the tooltip x formatter reuses the category axis formatter: the y-axis formatter when `bar.horizontal`, otherwise the x-axis formatter.

**Shared toolbar (Charts.jsx:30-43)**
- `toolbar.show: false`. CSV export uses delimiter `,` and dates as `toDateString()`.
- No OpenCTI builder sets `chart.animations` or `chart.zoom`, so ApexCharts defaults apply.

---

### 2. Theme values (dark first, light in brackets)

**Source:** ThemeDark.ts:11-18,42-131, with values from fds-tokens.generated.ts (dark l.32-185, light l.187-340). MUI spacing is the default 8px.

| Token | Dark | Light |
|---|---|---|
| background.default (`--bg-elevation-default-layer-0`) | #070d18 | #f2f2f3 |
| background.paper = Card surface (`--bg-elevation-default-layer-1`) | #0d172b | #ffffff |
| `--bg-elevation-default-layer-2` | #13213e | #f4f4f6 |
| background.accent (`--bg-elevation-default-layer-3`) | #1f3965 | #e4e5e7 |
| background.nav (simpleLabelTooltip background) | #070d18 | #f2f2f3 |
| Card border `--border-elevation-subtle-soft-layer-1-transparency-15` | #2b4f8d26 | #95969d26 |
| primary.main (`--color-filigran-brand-primary`) | #42caff | #0015a8 |
| primary.light | #a8e7ff | #7587ff |
| secondary.main | #00f0bc | #009474 |
| text.secondary (Apex `foreColor`) = THEME_*_DEFAULT_TEXT | #f2f2f3 | #18191b |
| text.light (titles, number label) | #AFB0B6 | #494A50 |
| text.tertiary | #848592 | #5F5F60 |
| text.primary | not set in ThemeDark/ThemeLight, so the MUI default applies | |
| success.main | #17ab1f | #117916 |
| error.main | #f14337 | #b8180a |
| common.grey | #95969D | #494A50 |
| designSystem.background.main (timeline card background) | #070d18 | |
| designSystem.tertiary.orange[400] (preview tag) | #f2933a | |
| Marking severity: critical / high / low / info / none | #f14337 / #e6700f / #17ab1f / #42caff / #95969d | |
| divider | not set, so MUI default | |
| MuiTableCell borders | rgba(255,255,255,0.15) (ThemeDark.ts:693-703) | |
| Font family | "IBM Plex Sans", sans-serif | |

Headings h1 to h6 use "Geologica", lowercase with the first letter uppercased. h2 is 16px / 500. body2 is 0.8rem / 1.2rem (ThemeDark.ts:245-336).

A custom platform theme overrides these through `useFdsThemeScope.ts:18-26`, which writes CSS variables, plus `custom-theme-tokens.css`.

**Chart palette `colors(temp)` (Charts.jsx:8-28)**
- `temp = 400` in dark and `600` in light. MUI v6.5.0 values, in order:

| Colour | 400 (dark) | 600 (light) |
|---|---|---|
| red | #ef5350 | #e53935 |
| purple | #ab47bc | #8e24aa |
| pink | #ec407a | #d81b60 |
| deepPurple | #7e57c2 | #5e35b1 |
| indigo | #5c6bc0 | #3949ab |
| blue | #42a5f5 | #1e88e5 |
| cyan | #26c6da | #00acc1 |
| blueGrey | #78909c | #546e7a |
| lightBlue | #29b6f6 | #039be5 |
| green | #66bb6a | #43a047 |
| teal | #26a69a | #00897b |
| lightGreen | #9ccc65 | #7cb342 |
| amber | #ffca28 | #ffb300 |
| deepOrange | #ff7043 | #f4511e |
| lime | #d4e157 | #c0ca33 |
| yellow | #ffee58 | #fdd835 |
| brown | #8d6e63 | #6d4c41 |
| orange | #ffa726 | #fb8c00 |
| grey | #bdbdbd | #757575 |

- Line, area, vertical-bar, horizontal-bar and treemap (non-distributed) use `[primary.main, ...colors(temp)]`.

**Grid lines**
- `borderColor` is rgba(255,255,255,.1) in dark, rgba(0,0,0,.1) in light, with `strokeDashArray: 3`.
- The same rgba value is used for radar polygons and polar rings and spokes.

**Hover state on every chart:** `states.hover.filter = lighten 0.05`.

**Number formatting**
- `simpleNumberFormat(n)` (Number.ts:58-63): SI compaction with 2 digits, trailing zeros stripped, **space before the symbol**. Examples: `1.5 K`, `42`.
- `n()` in i18n (i18n.jsx:174-180): `intl.formatNumber(scaled) + symbol` with **no space**, e.g. `1.5K`; returns `-` for null.

**Dates**
- `fldt` = long date-time (day, long month, year, h:m:s).
- `fsd` and `fd` = `{day numeric, month short, year numeric}`.
- `dateFormat` = moment `YYYY-MM-DD`.
- Empty value is `-` (String.tsx:11).

**Entity colour `itemColor(type)` (Colors.ts:31-44, 251-270)**

| Family | Colour | Types |
|---|---|---|
| analyse | #70B23B | Report, Grouping, Note, Malware-Analysis, … |
| cases | #EA80FC | |
| events | #F96C9B | |
| observations | #FF6F42 | Indicator, Infrastructure, … |
| allThreats | #FF9800 | Intrusion-Set, Threat-Actor-*, Campaign |
| arsenal | #F0B60A | Malware, Tool, Vulnerability, … |
| techniques | #D3E157 | |
| victimology | #BA88FF | Sector, Organization, Individual, User, … |
| locations | #05ACC1 | |
| relationships | #616161 | |
| restricted | #424242 | |

- Cyber observables and unknown strings use the `stringToColour` hash.
- `'true'` gives #2e7d32 and `'false'` gives #bf360c.
- Null gives #5d4037.

---

### 3. Shared data, label and colour rules (distribution widgets)

- **Distribution answer shape:** `label value entity{...}`.
  - Entities and audits use `stixCoreObjectsDistribution` / `auditsDistribution`.
  - Relationships use `stixRelationshipsDistribution`.
- **Typical entity fragment** (e.g. StixCoreObjectsDonut.tsx:47-89):
  - `BasicObject{id entity_type}`, `BasicRelationship{id entity_type}`
  - `StixObject{representative{main}}`, `StixRelationship{representative{main}}`
  - `Label{color}`, `MarkingDefinition{x_opencti_color}`
  - `Creator{name}`, `Group{name}`, `Status{template{name color}}`
- **Query variables:** `field = attribute ?? 'entity_type'`, `operation: 'count'`, `limit = selection.number ?? 10`. Relationship widgets also pass `isTo`, `dynamicFrom` and `dynamicTo`.
- **`isFieldForIdentifier(f)`:** true when f is `id`, or ends with `.id`, `_id` or `_ids` (defaultRepresentatives.ts:7-15).
- **`getMainRepresentative(n, fallback='Unknown')`** (defaultRepresentatives.ts:96-143):
  - Returns `''` if n is null.
  - For markings, returns `definition`.
  - Otherwise takes the first of `representative.main` → name → label → … → fallback.
  - Prefixes `[x_mitre_id] ` when there is no `representative.main`.
- **`buildWidgetLabelsOption`** (useDistributionGraphData.ts:108-119):
  - null node or `label==='unknown'` → `'Unknown'`.
  - Identifier field → main representative.
  - `entity_type` → `t('entity_<label>')` when a translation exists.
  - Otherwise the raw label.
- **`buildDistributionGraphData`** (l.59-74) gives `{x,y,fillColor}`:
  - x: identifier field → representative, or the label; `entity_type` → translated.
  - Null node → `{x:'Unknown', y:'Unknown'}`.
- **`getColorFromDistributionNode`** (l.34-57), each later rule overriding the earlier:
  1. `itemColor(entity.entity_type)` for an identifier field, else `itemColor(label)`.
  2. `entity.color`, then `x_opencti_color`, then `template.color`.
  - In light mode, `#ffffff` becomes `#000000`.
- **`buildWidgetColorsOptions`** (l.139-151): returns `[]` unless the first node's entity has `color`, `x_opencti_color` or `template.color`. Otherwise it maps each node through the rule above (null node → #000000).
- **`redirectionUtils`** (l.76-84): `{id: entity.id, entity_type}` only for nodes with `entity.id`. For a Workspace it uses `entity.type`.

**Time series**
- `stix{CoreObjects|Relationships}MultiTimeSeries` and `auditsMultiTimeSeries`, each `{data{date value}}`.
- One series per dataSelection, mapped to `{x:new Date(date), y:value}`.
- `interval = parameters.interval ?? 'day'` (widgetUtils.ts:263).
- Dates fall back to the last 12 months → now when the dashboard has no dates (dashboardVizUtils.ts:114-129).
- Series name: `selection.label ?? t('Number of entities')`. Heatmap relationships use 'Number of relationships'; audits use 'Number of history entries'.

**Axis date formatter `useTimeSeriesAxisFormatter(interval)` (useTimeSeriesAxisFormatter.ts:8-33)**
- Always uses `timeZone: 'UTC'`.

| Interval | Format | Example |
|---|---|---|
| month / quarter | `{month:'long', year:'numeric'}` | January 2025 |
| year | `{year:'numeric'}` | 2025 |
| else | `{day:'numeric', month:'short', year:'numeric'}` | |

- Null input renders `-`.

---

### 4. Per widget

#### number
- **Data**
  - `stixCoreObjectsNumber` / `stixRelationshipsNumber` / `auditsNumber` returning `{total count}`.
  - Variables: `startDate` = dashboard start, `endDate = dayAgo()` (now − 1 day), plus filters and `dateAttribute` (StixCoreObjectsNumber.tsx:85-98; StixRelationshipsNumber.tsx:97-111).
  - Audits use types `['History','Activity']`, dateAttribute default `timestamp`, and `field`/`unique` (AuditsNumber).
  - Displayed value = `total`. Diff = `total − count`, labelled "24 hours".
- **Titles**
  - The container title is **always the default**: 'Entities number', 'Relationships number' or 'Audits number'.
  - The label inside the body is `parameters.title`, or the default, passed through translateEntityType (widgetUtils.ts:268-272; StixCoreObjectsNumber.tsx:113-131).
- **Layout (WidgetNumber.tsx:31-79)**
  - Vertical Stack, height 100%, `justify space-between`.
  - **Top row:** label as body2 (0.8rem / 1.2rem) in `text.light`, `gutterBottom`. Next to it, with gap 8px, the NumberDifference:
    - 12px, flex, gap 2px.
    - Icon 13px: ArrowUpward green `success.main` when >0, ArrowDownward red `error.main` when <0, ArrowForward `common.grey` when =0.
    - Then the raw diff value (not SI-formatted).
    - Then `(24 hours)` (NumberDifference.tsx:15-52).
  - **Bottom row:** `n(value)` in a div at fontSize 32 / lineHeight 1 / weight 600, using the theme's default text colour.
  - An optional `ItemIcon` (size large, `text.secondary`, opacity .35) appears on the right only if `entityType` is passed. All dashboard viz pass `entityType={undefined}`, so no icon is shown on dashboards.
- **Interactions:** none.

#### list (entities)
- **Data:** `stixCoreObjects(types:['Stix-Core-Object'], first, orderBy, orderMode, filters)` edges→node.
  - Fields read: id, entity_type, created_at, createdBy{name}, objectLabel{id value color}, objectMarking{id definition_type definition x_opencti_order x_opencti_color}, representative{main secondary}, creators{name}, created, modified, status{template{name color}}, workflowEnabled, customFieldValues, plus per-type fields (StixCoreObjectsList.tsx:15-497).
  - `first = number ?? 10`, `orderBy = sort_by || 'created_at'`, `orderMode = sort_mode ?? 'asc'` (l.543-558).
- **Columns:** `selection.columns`, else the default list (WidgetListsDefaultColumns.ts:16-27):

| Attribute | Header |
|---|---|
| entity_type | Type |
| name | Name |
| created | Original creation date |
| created_at | Platform creation date |
| modified | Modification date |
| createdBy | Author |
| creators | Creators |
| x_opencti_workflow_id | Processing status |
| objectLabel | Labels |
| objectMarking | Marking |

- Every column gets `percentWidth = 100/columns.length` and `isSortable:false` (WidgetListCoreObjects.tsx:26-55). Custom fields render as a joined string.
- **Table (DataTable, variant `widget`)**
  - No selection checkbox (`disableLineSelection = !taskScope`, DataTableWithoutFragment.tsx:90). No pagination bar.
  - All rows are rendered (virtualised, ROW_HEIGHT 49) in a scroll area sized to the container (DataTableBody.tsx:101-151).
  - **Header:** 42px row; labels 12px, weight 700, `text.primary`, 8px side padding, no column menu for widget (DataTableHeader.tsx:12-13,30-44,87).
  - **Rows:** each row is an `<a>` with `display:flex` and `borderBottom 1px divider`. Cells are 48px tall (`theme.spacing(6)`), 13px font, 8px side padding, ellipsis. Row hover is rgba(255,255,255,.1) dark (DataTableLine.tsx:15-23,65-73,177-198).
  - A trailing 42px "navigate" column holds a tertiary IconButton `KeyboardArrowRightOutlined` (DataTableComponent.tsx:160; DataTableLine.tsx:245-264).
- **Cell renderers (dataTableUtils.tsx)**

| Column | Render (lines) |
|---|---|
| entity_type | `ItemEntityType` chip with small icon and `itemColor` (l.405-411; ItemEntityType.tsx:41-47) |
| name | `getMainRepresentative` with tooltip and ellipsis (l.763-771) |
| created / created_at | `fd()` short date (l.293-306) |
| modified | `fd()` (l.756-762) |
| createdBy | `createdBy.name` (l.307-312) |
| creators | names joined with ", " (l.322-330) |
| x_opencti_workflow_id | `ItemStatus` (l.1561-1574) |
| objectLabel | `StixCoreObjectLabels` chips, one per label in `label.color`, with a `+N` overflow Tag in `tag.overflowColor` = primary (StixCoreObjectLabels.tsx:53-111) |
| objectMarking | `ItemMarkings limit=1` (l.832-844) |

  - Marking chip colour: `x_opencti_color` if visible on paper; else TLP:RED/PAP:RED/CD/DR → critical; AMBER → high; GREEN/NP → low; SF → info; NONE → none (ItemMarkings.tsx:34-73).
  - Chip visuals come from `@filigran/design-system` `Chip` (not installed in the OpenCTI checkout; XTM One ships the same design system, 1.2.0, so the engine uses that `Chip`).
- **Click:** row navigates to `computeLink(node)` = `${resolveLink(entity_type)}/${id}`. Ctrl, Cmd or middle click opens a new tab (DataTableLine.tsx:151-161,204-206; useAppData.ts:60-61). Label and marking chip clicks call `handleAddFilter` on the table's storage helpers and do not navigate.

#### list (relationships)
- **Data:** `stixRelationships(first = number ?? 50, orderBy = dateAttribute, orderMode = sort_mode ?? 'desc', filters, dynamicFrom, dynamicTo)`.
  - Node fields: id, entity_type, parent_types, relationship_type, confidence, representative{main}, from/to (id, entity_type, parent_types, representative, name …), start_time, stop_time, description, objectLabel, first_seen, last_seen, created, created_at, updated_at, is_inferred, createdBy{name}, objectMarking, …
  - References: StixRelationshipsList.tsx:16-120 and 4510-4525.
- **Default columns:** entity_type 'Type', from_entity_type 'Source type', from_relationship_type 'Source name', to_entity_type 'Target type', to_relationship_type 'Target name', created, created_at, createdBy, objectMarking (WidgetListsDefaultColumns.ts:5-15).
  - From/to type cells are `ItemEntityType` chips (dataTableUtils.tsx:562-569, 1289-1296).
  - Name cells are `getMainRepresentative(from|to)` or 'Restricted' (l.594-604, 1297-1307).
- **Extra icon column** (56px): `ItemIcon type = is_inferred ? 'autofix' : entity_type` with `color="primary"` (WidgetListRelationships.tsx:60-65). That string goes straight into `style.color` (ItemIcon.tsx:142-146), and 'primary' is not a CSS colour.
- **Link:** `computeLink(from ?? to)`, i.e. the remote node's own page, not the relationship page (WidgetListRelationships.tsx:42-46).
- Page size is fixed at "50".

#### list (audits) — WidgetListAudits.tsx:21-129
- **Data:** `audits(types, first, orderBy, orderMode, filters)` with node fields id, entity_type, event_status, event_type, event_scope, timestamp, user{id entity_type name}, context_data{entity_id entity_type entity_name message workspace_type}.
- **Layout:** MUI List with marginTop −10; container `overflow:auto`, paddingBottom 10, marginBottom 10.
- **Rows:** ListItemButton dense with divider, height 50.
  - Icon: `ItemIcon(context_data.entity_type ?? event_type)`, coloured `error.main` if `event_status==='error'`.
  - Five 13px ellipsis columns: `fldt(timestamp)` 15%, user name 18%, event_scope 15%, entity_name or event_type 22%, generated markdown message 30%. In the error case, timestamp and message are red.
- **Link:** `resolveLink(context entity_type, or workspace_type for a Workspace)/entity_id`.

#### distribution-list — WidgetDistributionList.tsx:22-130
- **Entities mapping (StixCoreObjectsDistributionList.tsx:105-124)**
  - Fragment: id, entity_type, representative.main, Label.color, Marking.x_opencti_color, Creator/Group name, Status.template{name color}.
  - `label`: identifier field → `getMainRepresentative(entity)`; `entity_type` → the **raw string** `` `entity_${label}` `` (it is never passed through `t_i18n`, l.109-115, so as written the key itself is displayed).
  - `color = entity.color ?? entity.x_opencti_color`.
  - `id = entity.id` only when the attribute ends with `_id`.
  - `type = entity.entity_type ?? label`.
- **Relationships mapping (StixRelationshipsDistributionList.tsx:121-136):** label is the main representative for identifier fields, else the raw label; no colour.
- **Audits mapping (AuditsDistributionList):** label is translated `entity_<label>` when the translation exists.
- **Layout**
  - Container: 100% × 100%, paddingBottom 10, marginBottom 10, `overflow auto`. List marginTop −10.
  - Each row: ListItemButton dense with divider, height 50, paddingRight 0.
  - Left: ListItemIcon (MUI default min-width) with `ItemIcon` (medium). Type is `entry.type`, or `'default'` when there is no id. Colour is `entry.color` (white → black in light mode), else `itemColor(type)`.
  - Middle: label, nowrap with ellipsis, paddingRight 10.
  - Right: `n(value)`, fontSize 18, weight 600, colour `primary.main` (#42caff dark), marginRight 20.
- **No bars and no percentages.**
- **Link:** only when `entry.id`, label ≠ 'Restricted', not a public widget, and (type ≠ 'User' or the viewer has SETTINGS_SETACCESSES). Target: `computeLink({id, entity_type: type})`. Non-linked rows get a default cursor and no hover background (l.47-68).

#### vertical-bar — WidgetVerticalBars.tsx:28-44; Charts.jsx:294-384
- **Call:** `verticalBarsChartOptions(theme, axisFormatter(interval), simpleNumberFormat, distributed=false, isTimeSeries=false, isStacked=parameters.stacked, legend=parameters.legend (default false), tickAmount='dataPoints')`.
  - The x-axis is **category** for every interval: each bar is labelled with `useTimeSeriesAxisFormatter(interval)` on the Date value.
- **Options**

| Option | Value |
|---|---|
| chart | `type:'bar'`, `background: paper`, `foreColor: text.secondary`, stacked per parameter |
| dataLabels | off |
| colors | `[primary, ...colors(400)]` |
| grid | dashed 3 |
| legend | `show: legend`, `itemMargin {h:5, v:20}` |
| tooltip.theme | mode |
| xaxis | `tickPlacement:'on'`, labels 12px IBM Plex, `axisBorder` hidden |
| yaxis | labels formatted with `simpleNumberFormat`, IBM Plex, `axisBorder` hidden |
| plotOptions.bar | `horizontal:false`, `barHeight '30%'`, `borderRadius 4`, `borderRadiusApplication 'end'`, `borderRadiusWhenStacked 'last'` |

  - Column width is not set (ApexCharts default).
- **Interactions:** tooltips only; no click navigation.

#### line — WidgetMultiLines.tsx:26-36; Charts.jsx:91-174
- `isTimeSeries = !interval || interval ∈ {day, week}`, giving a datetime axis.
- `tickAmount = 'dataPoints'` for month, quarter and year, else undefined.
- `stroke {curve:'smooth', width:2}`, dataLabels off. Markers are not configured (ApexCharts default).
- Colors, grid and legend (`parameters.legend`, default false; itemMargin 5/20) are as for vertical-bar.
- Axis labels: x 12px, y 14px. y uses `simpleNumberFormat`; x uses the UTC axis formatter.
- The `useMemo` deps omit `interval` (l.36).

#### area — WidgetMultiAreas.tsx:28-38; Charts.jsx:185-282
- Same as line, plus `stacked = parameters.stacked`.
- `fill: {type:'gradient', gradient:{shade: mode, shadeIntensity:1, opacityFrom:0.7, opacityTo:0.1, gradientToColors:[primary.main, primary.main]}}`.

#### heatmap — WidgetMultiHeatMap.tsx:9-87; Charts.jsx:1005-1089
- **Data:** time series, one row per selection: `{name: label ?? 'Number of entities', data:[{x:Date, y}]}`. `min`/`max` are computed over all values (StixCoreObjectsMultiHeatMap.tsx:61-94).
- **Options**
  - `heatMapOptions(theme, isTimeSeries=true, axisFormatter() [no interval → day format], undefined, undefined, stacked, ranges)`.
  - `stroke {colors:[paper], width:1}`, dataLabels off, `legend.show:true`, dashed grid.
  - Axis labels: x 12px, y 14px.
  - `plotOptions.heatmap {enableShades:false, distributed:false, colorScale.ranges}`.
- **Colour ranges**
  - `interval = trunc((max−min)/9)`.
  - For i in 0..9: `from = min + i·interval` (replaced by 1 when it is 0), `to = min + (i+1)·interval`, `color = palette[i+1]`.
  - Plus `{from:0, to:0, color: palette[0]}`.
  - Dark palette: #001e3c, #023362, #02407a, #045198, #0561b4, #0b75d9, #2986e7, #3a95f3, #4da3ff, #76bbff, #9eccff.
  - Light palette: #f3f6f9, #76bbff, #4da3ff, #3a95f3, #2986e7, #0b75d9, #0561b4, #02407a, #023362, #001e3c, #021428.

#### timeline — WidgetTimeline.tsx:26-92
- **Entities data:** `stixCoreObjects(first = number ?? 10, orderBy = date_attribute || created_at, orderMode = sort_mode ?? 'desc')`.
  - Node fields: id, entity_type, created_at, updated_at, modified, created, start_time, stop_time, createdBy, objectMarking, representative{main secondary} (StixCoreObjectsTimeline.tsx:15-129).
  - Link: `${resolveLink(entity_type)}/${id}`.
- **Relationships data:** `stixRelationships` (first 10, desc).
  - Each item is the **remote node**: `to` if the fromId filter contains `from.id` and `isTo !== false`, else `from`.
  - Date = `rel[dateAttribute]`.
  - Link: `${resolveLink(remote.entity_type)}/${remote.id}/knowledge/relations/${rel.id}`, or none if `from` or `to` is null (StixRelationshipsTimeline.tsx:992-1022).
- **Layout**
  - Container 100% with `overflow auto`.
  - MUI Lab `<Timeline position="alternate">`. Each `TimelineItem` has:
    - Opposite content: `paddingTop 18px`, colour `text.secondary`, `fldt(date)`, or `-`.
    - Separator: `TimelineDot variant="outlined"` with `borderColor = itemColor(entity_type)` containing `ItemIcon(entity_type)`, followed by a `TimelineConnector`.
    - Content: `Card` (24px padding, background `designSystem.background.main` #070d18 dark) with Typography h2 for the main representative, then a div (marginTop −5, colour `text.tertiary`) with the secondary representative as markdown truncated to 150 chars.
- **Click:** on the dot (wrapped in a router `Link`) only.

#### donut — WidgetDonut.tsx:24-62; Charts.jsx:810-914
- **Series:** `data.map(n => n.value)`. Labels from `buildWidgetLabelsOption`.
- **Colours:**
  - If the first node has `entity.color`, `x_opencti_color` or `template.color`, each node's colour of that kind is used (white → black in light mode), then **falsy entries are filtered out**. Indices can therefore shift (l.28-50).
  - Otherwise `colors(400)`, except when exactly 2 labels are `['true', …]` → [green, red], or `['false', …]` → [red, green].
- **Options:** `donutChartOptions(theme, labels, 'bottom', false, chartColors)` with defaults `displayLegend/Labels/Value/Tooltip = true`, `size = 70`.

| Option | Value |
|---|---|
| fill | opacity 1 |
| stroke | `{curve:'smooth', width:3, colors:[paper]}` (slice separator = surface colour) |
| tooltip.custom | `simpleLabelTooltip`: `<div style="background:nav; color:text.primary; padding:2px 6px; font-size:12px">label</div>`, label only, no value (Charts.jsx:71-80) |
| legend | bottom, IBM Plex |
| dataLabels | on, 10px, weight 600, colour `text.primary` (black where a chart colour is #ffffff); no background, no drop shadow. The ApexCharts default formatter (percentage) applies |
| plotOptions.pie.donut | `{value.show:true, background: paper, size:'70%'}`. `donut.labels` (centre total) is not set |

#### horizontal-bar

**Simple** (WidgetHorizontalBars.tsx:40-77; Charts.jsx:400-592)
- **Series:** `[{name: selection.label || 'Number of entities' / 'Number of relationships', data: [{x: label, y: value, fillColor}]}]` built with `buildWidgetProps`. Each point carries its own `fillColor` (§3).
- **Call:** `horizontalBarsChartOptions(theme, adjustTicks=true, xFormatter=simpleNumberFormat, yFormatter=getFormattedValue, distributed=parameters.distributed, navigate, redirectionUtils, stacked=false, total=false, categories=undefined, legend=false, stackType='normal')`.
- **`getFormattedValue` (category labels):**
  - A number → `simpleNumberFormat`.
  - A string where `parseInt` gives a number (i.e. it starts with digits) → `dateFormat(timestamp(n))`.
  - Otherwise the raw string.
- **Options**

| Option | Value |
|---|---|
| chart | `bar`, background paper, foreColor `text.secondary` |
| dataLabels | `enabled: stackType==='100%'`, so off on dashboards |
| colors | `[primary, ...colors(400)]` |
| grid | dashed 3, `padding.right: 20` |
| legend | hidden; `showForSingleSeries:true` |
| tooltip | `x.show: true` |
| xaxis (value axis) | `simpleNumberFormat`, **`tickAmount: 1`** (adjustTicks), axisBorder hidden |
| yaxis | category labels via `getFormattedValue`, IBM Plex |
| plotOptions.bar | `horizontal:true`, `barHeight '30%'`, `borderRadius 4` end / last, `distributed`; `dataLabels.total` off |

- **Interactions (Charts.jsx:424-497)**
  - `click` on a bar → `navigate(${resolveLink(entity_type)}/${id})` from redirectionUtils, unless `name==='Restricted'`. Ctrl, Cmd or middle click opens a new tab.
  - `xAxisLabelClick` navigates the same way.
  - `mouseMove` sets a pointer cursor and adds `noDrag` on clickable bars and labels.
  - redirectionUtils exist only for nodes with `entity.id`, so in practice only for identifier group-bys.
- `stackType` `'100%'` is only used outside dashboards (RelatedContainersDetails.tsx:59).

**Entities, more than one selection and first attribute ending in `_id`** (StixCoreObjectsMultiHorizontalBars.tsx:391-468)
- The query also fetches sub-distributions, but the render **ignores them**: it draws a single series.
  - x: `getMainRepresentative(entity, 'Restricted')` for `_id`, `t('entity_<label>')` for entity_type, else the label.
  - `fillColor` uses the same colour rule.
- `horizontalBarsChartOptions(theme, true, simpleNumberFormat, undefined, distributed, navigate, undefined)`: no redirectionUtils, so **no click navigation**, and the y labels are raw.

**Relationships, more than one selection and first attribute `internal_id`** (StixRelationshipsMultiHorizontalBars.tsx:466-483; useStixRelationshipsMultiHorizontalBars.js)
- Stacked, total, legend.
- Categories = main representative of each top-level entity, or 'Restricted'.
- Sub-keys are summed across categories, sorted descending, and the top `subSelection.number ?? 15` kept, plus 'Others' = remainder. Each sub-key becomes one series with values per category; all-zero series are dropped.
- Total labels: 13px, weight 900. Legend `showForSingleSeries`.
- Click on a segment navigates to the sub-entity when both fields are `internal_id` (series ids ordered by total).

#### radar — WidgetRadar.tsx:29-44; Charts.jsx:604-702
- **Series:** `[{name: selection.label ?? 'Number of entities', data: values}]`. Labels from `buildWidgetLabelsOption`.
- **Call:** `radarChartOptions(theme, labels, simpleNumberFormat, [], legend=true)`; background paper; size undefined.

| Option | Value |
|---|---|
| fill | `{opacity:0.2, colors:[primary]}` |
| stroke | `{width:1, colors:[primary], dashArray:0}` |
| markers | `{shape:'circle', strokeColors:[primary], colors:[primary]}` |
| legend | show (itemMargin 5/5) |
| xaxis.labels | `show:true`, truncated to 25 chars, IBM Plex, `colors: []` |
| yaxis | `show:false` (formatter `simpleNumberFormat`) |
| polygons | strokeColors / connectorColors rgba(255,255,255,.1), fill `[paper]` |
| tooltip.x | identity |

- No foreColor is set.
- **Code anomaly:** `chart.events.click` and `markerClick` call `handleClick()`, but WidgetRadar does not pass it (`undefined`, Charts.jsx:621-624).

#### polar-area — WidgetPolarArea.tsx:23-36; Charts.jsx:711-794
- **Series:** values (null → 0). Labels from `buildWidgetLabelsOption`. Colours from `buildWidgetColorsOptions`, falling back to `colors(400)` with the same true/false rule as donut.
- **Options**

| Option | Value |
|---|---|
| chart | background paper, foreColor `text.secondary` |
| legend | `{show:true, position:'bottom', floating:false, IBM Plex, markers.strokeWidth:0}` |
| tooltip.custom | `simpleLabelTooltip` (label only) |
| fill | opacity 0.5 |
| rings / spokes | width 1, rgba(255,255,255,.1) |
| yaxis.labels | IBM Plex, no formatter |

#### tree — WidgetTree.tsx:27-47; Charts.jsx:923-994
- **Series:** `[{data:[{x, y}]}]`.
  - x: identifier field → `getMainRepresentative(entity)`; entity_type → translated; else label.
  - Entities fragment: BasicObject, BasicRelationship, representative, Creator.name, Status.template.name. No colours fetched (StixCoreObjectsTreeMap.tsx:46-71).
- **Call:** `treeMapOptions(theme, simpleNumberFormat, 'bottom', parameters.distributed)`.
- **Colours:**
  - distributed → `colors(400)` minus near-white colours (luminance ≥ 0.9, Colors.ts:376-384).
  - otherwise `[primary, ...colors(400)]`.

| Option | Value |
|---|---|
| fill | opacity 1 |
| stroke | width 3, `[paper]` |
| legend | show, bottom |
| dataLabels | IBM Plex 600, `colors:[text.primary, text.secondary]`, no background or shadow |
| plotOptions.treemap | `{distributed}`; `enableShades` not set |
| yaxis formatter | `simpleNumberFormat` (tooltip value) |

- Container padding: default 24px.

#### map (relationships only) — StixRelationshipsMap.tsx:95-145; LocationMiniMapTargets.tsx; GeoMap.tsx
- **Data:** `stixRelationshipsDistribution(field = attribute ?? 'entity_type', limit 10, isTo, …)`. Entity fields: entity_type, Country/City `{name x_opencti_aliases latitude longitude}`.
- **Levels:** `min = values[0]`, `max = values[last]`. Countries get `level = computeLevel(value, max, min+1)` (0..9, Number.ts:107-118).
- **Library:** **maplibre-gl** + **pmtiles** vector tiles at `pmtiles://${origin}${APP_BASE_PATH}/maps/world.pmtiles` (GeoMap.tsx:220).
- **Style (GeoMap.tsx:21-32,74-131)**

| Element | Dark | Light |
|---|---|---|
| Background / earth | #1c2f4a | #dce0fe |
| Water | #0b1422 | #ffffff |
| Roads (zoom ≥5) | #1c8eb6 | #b4b4b4 |
| Road casing (zoom ≥7) | #002850 | #b4b4b4 |
| Country boundaries | #0fbcff | #001bda |
| Labels | #ffffff | #000000 |

  - Label fonts: country "Roboto Bold" 10→14px; cities "Roboto Medium" (zoom ≥5).
- **Country highlight**
  - Colour: `THREAT_COLORS[level]`, or `THREAT_COLORS[5]` when level is 0 or falsy (LocationMiniMapTargets.tsx:6-9,31-34).
  - THREAT_COLORS: #fff59d, #ffe082, #ffb300, #ffb74d, #fb8c00, #d95f00, #e64a19, #f44336, #d32f2f, #b71c1c.
  - Matched by ISO3 found in `x_opencti_aliases`. Fill opacity 0.1 dark / 0.5 light; 1px outline in the same colour (GeoMap.tsx:135-176).
- **Cities:** 25×25px HTML markers using `city_orange.png` (GeoMap.tsx:186-199).
- **View:** `zoom = (selection.zoom ?? 2) − 1`. Center = `selection.centerLat/centerLng`, else first city, else Paris (48.8566969, 2.3514616). Wheel zoom rate 2, attribution off, interactive (GeoMap.tsx:218-232).
- **Box:** the map is inside a nested `Card padding="none"` (no title); the div is 100% × 100% with minHeight 300 and borderRadius 6 (l.254-259). The outer container has padding none.
- No click handlers.

#### bookmark (entities only) — WidgetBookmarks.tsx:24-73
- **Data:** `bookmarks(types, first = number ?? 10, orderBy = sort_by || 'created_at', orderMode = sort_mode ?? 'asc', filters)` with node fields id, entity_type, name, modified, … (StixDomainObjectBookmarksList.tsx:18, 197-209).
- **Layout**
  - Container `overflow auto`, paddingBottom 10, marginBottom 10.
  - MUI `Grid container spacing={3}` (24px); each item `xs={4}`, so 3 per row.
  - Each item is a `Card padding="none" variant="outlined" to={resolveLink(type)/id}`, which makes the whole card a link.
  - Inside: `CardHeader` (paddingBottom 0) with:
    - Avatar: `background primary.main`, containing `ItemIcon(type)` in `background.default` (#070d18).
    - Title: `name`.
    - Subheader: `"Updated on " + fsd(modified)`.

#### wordcloud — WidgetWordCloud.tsx:19-71
- **Data:** distribution, mapped to `{text, value}` using the same label rules ('Unknown' for unknown). Entries with value ≤ 0 or not finite are dropped.
- **Library:** `react-wordcloud`. Options:
  - `colors: colors(400)` (no primary)
  - `fontFamily: 'IBM Plex Sans'`
  - `spiral: 'rectangular'`, `rotations: 1`, `rotationAngles: [0,0]` (all horizontal)
  - `deterministic: true`
  - `fontSizes: [20,50]`, `scale: 'log'`
  - `minSize: [1,1]`
- Tooltip and other options are not set (library defaults).
- It renders only when the container is larger than 10×10; WidgetNoData shows if no positive words remain.
- No ApexCharts, so no export button. Container padding: default 24px.

#### text — WidgetText.tsx:16-43
- Container with default 24px padding and title = `parameters.title ?? ''`.
- `MarkdownDisplay` (react-markdown + remark-gfm, commonmark) renders `parameters.content`. The first h1–h6 gets `marginTop: 0`.
- No data query.

---

### 5. Audits perspective differences
- Every audits widget goes through AuditsWidgetRenderContent (EE + rights guard).
- The same leaf components are used (WidgetDonut, WidgetVerticalBars, WidgetPolarArea, …). AuditsHorizontalBars builds its own `Chart` with the same `horizontalBarsChartOptions(theme, true, simpleNumberFormat, undefined, distributed, navigate, redirectionUtils)`, so y labels are raw and the default series name is 'Number of history entries'.
- Default titles:

| Default title | Widgets |
|---|---|
| 'Activity and history' | vertical-bar, line, area |
| 'Entities history' | heatmap |
| 'Distribution of history' | donut, polar-area, horizontal-bar, wordcloud |
| 'Distribution of entities' | distribution-list, radar, tree |
| 'Audits list' | list |
| 'Audits number' | number |

- Audits have no timeline, map or bookmark.

**Default titles, entities and relationships**

| Widgets | Entities | Relationships |
|---|---|---|
| distribution widgets | 'Distribution of entities' | 'Relationships distribution' (horizontal-bar: 'Distribution of entities') |
| vertical-bar, line, area, heatmap | 'Entities history' | 'Entities history' |
| list | 'Entities list' | 'Relationships list' |
| timeline | 'Entities list' | 'Relationships timeline' |
| bookmark | 'Bookmarks' | – |

---

### 6. Not in the code (do not invent)
- No count-up animation, sparkline or percentage on the number widget. The "trend" is the raw `total − count` against now − 24h.
- No bars or percentages in distribution-list.
- No donut centre total.
- No explicit legend-toggle code (ApexCharts default legend click behaviour only).
- No custom bar column width.
- No animation settings.
- The `@filigran/design-system` Chip CSS is not in the OpenCTI checkout; XTM One's copy of the design system (1.2.0) provides it.
- MUI `text.primary` and `divider` are not overridden by ThemeDark (MUI defaults apply).

## OpenAEV

**Paths.** `F` = `openaev/openaev-front/src`. `W` = `F/admin/components/workspaces/custom_dashboards/widgets`. Line numbers are `file:line`. If something is "not in repo", it lives in a dependency (ApexCharts, xyflow or `@filigran/design-system`).

**The design system.** OpenAEV's chrome components (`Paper`, `Chip`, `Tooltip`, `Button`, `IconButton`, `Select`) come from `@filigran/design-system@1.2.0`. That is Tailwind-based, not MUI. The **same version is already installed in xtm-one**, at `xtm-one/frontend/node_modules/@filigran/design-system`. Reusing it gives pixel parity for those pieces. The values quoted for them below come from that copy.

---

### 0. Common chrome

#### Dashboard grid (`F/admin/.../custom_dashboards/CustomDashboardReactLayout.tsx`)
- **Grid** (react-grid-layout, `:236-253`):
  - 12 columns, `rowHeight` 50, `margin` [20,20], `containerPadding` [0,0].
  - Default item size is w4 h2 (`:218-224`).
  - Drag is cancelled on `.noDrag`, `.MuiAutocomplete-paper`, `.MuiModal-backdrop`, `.MuiPopover-paper` and `.MuiDialog-paper`.
  - Drag and resize are disabled when `readOnly`.
  - The grid stays `visibility:hidden` for 150 ms after mount (`:52-58`).
- **Lazy mount:** each card mounts only once it is visible, via IntersectionObserver with `rootMargin` 200px (`:12-39`).
- **Card:** FDS `<Paper padding={0} style={{height:'100%',margin:0,overflow:'hidden',transition:'box-shadow 0.3s, border-color 0.3s'}}>` (`:194-199`, `:257`). FDS Paper source, default elevation 1:
  - background = `--bg-elevation-default-layer-1` (dark `#0d172b`, light `#ffffff`)
  - text colour = `text-default-primary`
  - `rounded-sm` = 4px
  - 1px border = `--border-elevation-subtle-soft-layer-1-transparency-15` (dark `#2b4f8d26`, light `#95969d26`)
- **While resizing:** the body is replaced by an empty `<div/>` (`W/WidgetWrapper.tsx:197,269`).

#### WidgetWrapper (`W/WidgetWrapper.tsx`)
- **Padding:** outer div `height:100%`, padding = `theme.spacing(1.5)` = **12px** (`:253-256`). Body div is `height: calc(100% - 32px)` (`:271`). Mousedown and touchstart are stopped on the body (`:194-195`).
- **Fetch dispatch by `widget_type`** (`:84-107`). Every call is `POST`, with body = `{paramId: value}` built from the dashboard parameters (`:33-37`):

  | widget_type | Endpoint |
  |---|---|
  | `attack-path` | `/api/dashboards/attack-paths/{widgetId}` |
  | `number` | `/api/dashboards/count/{id}` |
  | `average` | `/api/dashboards/average/{id}`, then `determinePercentage(data, theme)` |
  | `list` | `/api/dashboards/entities/{id}`, body `{parameters, pagination:{page,size}}` |
  | all others | `/api/dashboards/series/{id}` |

  Action definitions: `F/actions/dashboards/dashboard-action.ts:51-76`. Backend: `openaev-api/.../rest/dashboard/DashboardApi.java:38-117`.
- **Request ordering:** a monotonic request id drops stale responses (`:121-171`).
- **List pagination clamp:** if the page is past the end, it resets to page 0 (`:151-161`).
- **Loading:** while `initialLoading` is true, `<Loader variant="inElement"/>` is shown (`:275-277`, `F/components/Loader.tsx`). It is the `FiligranLoader` from `filigran-icon`, height 40, colour `palette.common.grey` (dark `#95969d`, light `#494a50`), centred with `display:table-cell; vertical-align:middle`.
- **Error:** `errorMessage` is only used by the vertical bar chart, as its `noData` text. Every other viz receives `vizData` of type NONE after a failed first load, so `WidgetViz` renders the raw string **"Not implemented yet"** (`W/WidgetViz.tsx:77-209`). The `ErrorBoundary` wraps the body.
- **Lazy charts:** vizzes are `React.lazy`, with a `Suspense` fallback of the same loader (`WidgetViz.tsx:15-27,214`).
- **Series conversion** for chart types (`WidgetViz.tsx:46-63`): `EsSeries[] → [{name: label, data: [{x: n.label, y: n.value, meta: n.key}]}]`.

#### Title row (`W/WidgetTitle.tsx:115-181`)
- **Row:** flex, `align-items:center`, height **22**, `margin-bottom` **10**, `gap` **8**.
- **Title:** MUI Typography h4 with inline overrides. The theme h4 also sets `height:15`, which is not overridden.
  - Size and weight: `fontSize 11`, `fontWeight 600`, Geologica, `lineHeight 1`.
  - Text: `letterSpacing 0.08em`, `textTransform uppercase`, nowrap with ellipsis.
  - Layout: `flex 1`, `opacity 0.85`.
- **Default titles** when `title` is empty (`W/WidgetUtils.tsx:156-173`):

  | widget_type | i18n key / English |
  |---|---|
  | `average` | "Performance by security domain" |
  | `attack-path` | "Attack Path" |
  | `security-coverage` | "Security Coverage" |
  | `exposure-score` | "Exposure score" |
  | `posture-radar` | "Posture radar" |
  | `command-center` | "Exposure command center" |
  | `resilience-gauge` | "Resilience" |
  | anything else | `title ?? ''` |

- **Right side of the row, in order:**
  1. `sample` → FDS `<Chip label={t('Sample')} severity="neutral"/>`. Library values: height 24, px 8, radius 4, background `--color-feedback-neutral-secondary-transparency-30` (dark `#62636a4d`, light `#95969d4d`).
  2. `rightSlot`: list pagination (see List).
  3. Number widgets only: `InfoOutlined` (fontSize 16, colour primary, ml 4px) carrying a tooltip, `side="right"` (`:148-161`). Tooltip content (`:38-107`):
     - The diff: `+N`, or `+0` when 0.
     - Icon and colour: `ArrowUpwardOutlined` / `success.main` if > 0, `ArrowDownwardOutlined` / `error.main` if < 0, otherwise `ArrowForwardOutlined` / `grey.400`.
     - Then a bold resource name: `t('<base_entity>-singular' | '-plural')`. Singular is used when |diff| ≤ 1.
     - Then `t('<timeRangeLabel>_progression')`. Example: "over the last 3 months".
     - Then, unless the range is ALL_TIME, the bold string `t('was previously',{previous_number})` = "({n} previously)".
     - Time-range label keys are in `W/configuration/common/TimeRangeUtils.tsx:23-71`. A widget `time_range` of `DEFAULT` resolves from the dashboard's timeRange parameter.
  4. Security coverage only: FDS `IconButton` (tertiary, size sm) with `OpenInFullOutlined` 16px, aria "Fullscreen", which opens the fullscreen dialog (`:162-171`).
  5. If not read-only: `WidgetPopover`, an icon `ButtonPopover` (small) with entries "Update" and "Delete" (`W/WidgetPopover.tsx:64-79`).

#### Sample state (`W/viz/sample/SamplePreview.tsx`, `SampleContext.tsx`, `sampleData.ts`)
- **Look:** the content is wrapped in a Box with `pointer-events:none` and `user-select:none` (`SamplePreview.tsx:36-50`):
  - `full` variant: `filter: grayscale(1)` and `opacity .45`
  - `subtle` variant: `grayscale(0.6)` and `opacity .8`
- **Marker:** inside a card, the "Sample" chip moves to the title row through `SampleReportContext` (`:26-29`, `:54-63`).
- **Empty tests:**
  - `isSeriesEmpty`: no series, or every `data` empty or all values 0 (`sampleData.ts:18-21`).
  - `isApexSeriesEmpty`: the same test on `y` (`:31-37`).
- **Sample datasets** (`sampleData.ts:49-197`):

  | Dataset | Contents |
  |---|---|
  | `sampleStatusSeries` | SUCCESS 64 / FAILED 27 / PENDING 9 |
  | `samplePlatformsSeries` | "Not Detected" and "Not Prevented" × EDR, SIEM, Firewall, Email Gateway, Web Proxy, Cloud |
  | `sampleTtpSeries` | "Detected TTPs" / "Prevented TTPs" × 5 T-codes |
  | `sampleTemporalSeries(l1, l2?)` | 8 weekly ISO dates ending today; values `[4,7,5,9,12,8,14,11]` and `[2,5,3,7,9,6,10,7]` |
  | `sampleExposureSeries` | SUCCESS / FAILED / PENDING × PREVENTION, DETECTION, VULNERABILITY |
  | `sampleRadarSeries` | SUCCESS / FAILED × 6 platforms |

#### Shared empty state (`F/components/Empty.tsx:21-60`)
- Column, centred, gap 8, `paddingBlock` 40, `paddingInline` 16.
- Medallion: 44×44 circle, background `alpha(text.primary,.04)`, border `1px alpha(text.primary,.08)`, containing `InboxOutlined` 22px in `text.disabled`.
- Message: 13px, `text.secondary`.

#### Click-through target: `openWidgetResults` (`F/admin/.../CustomDashboardWrapper.tsx:107-138`)
It navigates to `/admin/results?…` with these params:
- `widget_id`
- `series_index`, or `''` when absent
- `series_indexes` (repeated)
- `source` (default `workspace`), `context_id`, `dashboard_id`
- `param.<id>=<value>` for each dashboard parameter
- one URL param per filter value: `<key>=<value>` (repeated)
- `back=<pathname+search>`

---

### 1. Theme values (dark / light)

Sources: `F/components/ThemeDark.ts`, `ThemeLight.ts` and `fds-tokens.generated.ts`. Rows marked "MUI default" are not overridden by the theme.

| Token | Dark | Light |
|---|---|---|
| primary.main | `#42caff` | `#001bdb` |
| secondary.main | `#00f0bc` | `#009474` |
| success.main | `#17ab1f` | `#1CA55E` |
| error.main | `#f14337` | `#F14337` |
| warning.main | `#ffa726` | `#ed6c02` |
| warn.main (= `--color-feedback-warning-primary` in dark) | `#e6700f` | `#E6700F` |
| text.primary | `#f2f2f3` | `#18191b` |
| text.secondary | `#afb0b6` | `#494a50` |
| text.disabled | `#75829A` | `#6E7788` |
| background.default | `#070d18` | `#f2f2f3` |
| background.paper | `#0d172b` | `#ffffff` |
| background.nav | `#070d18` | `#ffffff` |
| background.accent | `#1f3965` | `#e4e5e7` |
| common.grey | `#95969d` | `#494a50` |
| palette.grey[500] (MUI default) | `#9e9e9e` | `#9e9e9e` |
| divider (MUI default) | `rgba(255,255,255,0.12)` | `rgba(0,0,0,0.12)` |
| action.hover (MUI default) | `rgba(255,255,255,0.08)` | `rgba(0,0,0,0.04)` |
| CSS `--color-feedback-warning-primary` | `#e6700f` | `#b8550a` |
| CSS `--color-feedback-error-primary` | `#f14337` | `#b8180a` |
| CSS `--color-feedback-success-primary` | `#17ab1f` | `#117916` |
| CSS `--color-feedback-info-primary` | `#42caff` | `#0079a8` |
| CSS `--color-feedback-neutral-primary` | `#95969d` | `#62636a` |
| CSS `--color-feedback-neutral-secondary` | `#62636a` | `#95969d` |

**Security-domain colours** (`ThemeDark.ts:305-313`, `ThemeLight.ts:307-316`):
- success `rgb(2,129,8)`
- intermediate `rgb(255 216 0)`
- warning `rgb(245, 166, 35)`
- failed `rgb(220, 81, 72)`
- pending `rgba(248,243,243,0.74)` in dark, `0.37` in light
- unknown `rgba(73,72,72,0.37)`
- EMPTY_DATA `rgba(128,127,127,0.37)` (`SecurityDomainsWidgetUtils.tsx:38`)

**Typography** (`ThemeDark.ts:318-396`):
- Base font: "IBM Plex Sans". body2 is 0.8rem / 1.2rem; body1 is 14px.
- h4 is 12px/500 with `height:15`. h5 is Geologica 700 16px. h6 is Geologica 600 14px.
- **h1-h6 and subtitle2 are `textTransform: lowercase` with first letter uppercase.** This matters for the kill-chain column titles and the "You must set a simulation" text.
- `spacing` = 8px (`AppThemeProvider.tsx:14,101`). MUI `shape.borderRadius` = 4, so sx `borderRadius:1` = 4px.

**`tint(c, p)`** = `color-mix(in srgb, c p%, transparent)` (`F/utils/tint.ts:11`).

**MUI colour ramp** `colors(t)` (`F/utils/Charts.ts:13-55`):
- Order is red, pink, purple, deepPurple, indigo, blue, lightBlue, cyan, teal, green, lightGreen, lime, yellow, amber, orange, deepOrange, brown, grey, blueGrey at `t`, then the same 19 hues at `t+100`.
- `t` = 400 in dark, 600 in light.
- 400: `#ef5350 #ec407a #ab47bc #7e57c2 #5c6bc0 #42a5f5 #29b6f6 #26c6da #26a69a #66bb6a #9ccc65 #d4e157 #ffee58 #ffca28 #ffa726 #ff7043 #8d6e63 #bdbdbd #78909c`
- 600: `#e53935 #d81b60 #8e24aa #5e35b1 #3949ab #1e88e5 #039be5 #00acc1 #00897b #43a047 #7cb342 #c0ca33 #fdd835 #ffb300 #fb8c00 #f4511e #6d4c41 #757575 #546e7a`
- `resultColors(t)` = deepPurple, indigo, lightBlue: `#7e57c2 #5c6bc0 #29b6f6` (400) or `#5e35b1 #3949ab #039be5` (600) (`Charts.ts:82-86`).

**Semantic series colours** `seriesSemanticColors(theme, names)` (`W/viz/chartColorUtils.ts:8-44`). Each name is upper-cased and the first match wins:

| Name contains | Colour |
|---|---|
| UNDETECTED or "NOT DETECTED" | error.main |
| UNPREVENTED or "NOT PREVENTED" | `#ff9800` |
| "NOT ", FAILED or MISSED | error.main |
| DETECTED | secondary.main |
| PREVENTED or SUCCESS | success.main |
| PENDING | text.disabled |
| none of these | `[primary, secondary, #ff9800, #ff7043][i%4]` |

It returns `null` when no name matched, and the caller then falls back to the default colours.

**Status colours** `getStatusColor(theme, label)` (`F/utils/statusUtils.ts:184-257`), compared lower-case:
- success / executed / prevented / detected / "not vulnerable" → success.main
- failed / error / "not prevented" / "not detected" / vulnerable / undetected / unprevented → error.main
- pending / draft / finished / asset_agentless / agent_inactive → grey[500] `#9e9e9e`
- partial → `var(--color-feedback-warning-primary)`
- "partially *" → warning.main
- executing / info / scheduled → `var(--color-feedback-info-primary)`
- queuing → `var(--color-feedback-alert-primary)`
- anything else → error.main

---

### 2. ApexCharts option builders (`F/utils/Charts.ts`)

All charts render through `react-apexcharts` 1.7.0 (patched) and `apexcharts` 4.7.0, with `width="100%"` and `height="100%"` (`F/components/Chart.tsx`). The toolbar is always hidden. `foreColor` = text.secondary. `theme.mode` follows the palette mode. Grid lines are dashed 3, with border `rgba(255,255,255,.1)` in dark and `rgba(0,0,0,.1)` in light (`.08` for horizontal bars). Legend `itemMargin` is {vertical 8, horizontal 4}.

---

### 3. vertical-barchart (`W/viz/VerticalBarChart.tsx`)

**Data**
- `/series`. Mode is `temporal` for a temporal-histogram config, otherwise `structural` (`:30-35`).
- Sample is shown when there is no error and the series are all-empty (`:37`). Temporal mode uses `sampleTemporalSeries(name0, name1?)`; structural mode uses `samplePlatformsSeries` (`:38-47`).

**Options** (`verticalBarsChartOptions`, `Charts.ts:334-465`, called with `isResult:true`, `legend:true`, `tickAmount:'dataPoints'`, `isTimeSeries: temporal`, `xFormatter: temporal ? fld : null`, `chartColors: semantic`):
- **Chart:** type bar, not stacked, zoom and animations enabled.
- **Colours:** semantic colours if any name matched, otherwise `resultColors(400|600)`.
- **Bars:**
  - `stroke {show, width 4, colors ['transparent']}`, which leaves gaps inside a group.
  - `fill`: gradient, `shade` = mode, vertical, `shadeIntensity .35`, opacity `.95 → .65`, `stops [0,100]`.
  - `plotOptions.bar`: `columnWidth 55%`, `borderRadius 4`, `borderRadiusApplication 'end'`, `borderRadiusWhenStacked 'last'`, not distributed.
- **Hover:** hover and active filters are `lighten`. Bars get `cursor:pointer` (`:22`).
- **dataLabels:** off.
- **Legend:** shown, click toggles the series, hover highlights it.
- **Tooltip:** the ApexCharts default themed tooltip.
- **X axis:** `type` datetime (temporal) or category, `tickPlacement 'on'`. Labels use the formatter (long date such as "January 5, 2026" from `fld` = Intl `{day:'numeric', month:'long', year:'numeric'}`, `F/components/i18n.tsx:204-213`), 12px IBM Plex Sans. No axis border.
- **Y axis:** labels `value.toString()`, IBM Plex Sans, no border.
- **Grid:** padding left/right 24 when temporal.
- **noData:** the error message, otherwise "No data to display".

**Click** (`dataPointSelection`, `:52-66`): `filter_values_map {[temporal ? 'date' : field]: [meta]}`, `series_index`.

---

### 4. horizontal-barchart (`W/viz/HorizontalBarChart.tsx`)

**Data**
- `/series`. Sample is `sampleTtpSeries` when all-empty (`:28-32`).
- `distributed = series.length <= 1` (`:62`).
  - Distributed: no chart colours, no legend, data labels on.
  - Not distributed: semantic colours, legend on, data labels off (`:63-84`).

**Options** (`horizontalBarsChartOptions`, `Charts.ts:517-684`):
- **Chart:** type bar, horizontal, not stacked, animation speed 700.
- **Colours:** chartColors, otherwise `[primary.main, ...colors(400|600)]`. Distributed means one colour per bar, and the first bar is primary.
- **dataLabels** (when enabled): `textAnchor start`, `offsetX 8`, value `toLocaleString()`, 12px Geologica 600, colour text.primary, no background, no shadow.
- **Bars:**
  - `stroke` width 3, transparent.
  - `fill`: gradient horizontal, `shadeIntensity .5`, opacity `1 → .75`, `stops [0,95,100]`.
  - `plotOptions.bar`: `barHeight 38%`, `borderRadius 5` at end, `dataLabels.position 'top'`, total disabled.
- **Grid:** border `.08`, vertical lines on, horizontal lines off, padding right 32 / left 8.
- **X axis** (the value axis): labels 11px, no border, no ticks.
- **Y axis** (categories): labels truncated to 28 chars + "…" (`Charts.ts:507-509`), 12px, `maxWidth 220`.
- **Legend:** IBM Plex Sans.
- Bars get `cursor:pointer`.

**Click:** `{[field]: [meta]}`, `series_index` (`:37-47`).

---

### 5. line (`W/viz/LineChart.tsx`)

**Data:** `/series` (temporal). Sample is `sampleTemporalSeries` (`:21-28`).

**Options** (`lineChartOptions`, `Charts.ts:113-201`, plus overrides at `:62-85`):
- **Chart:** `type="area"`.
- **Colours:** `colors(400|600)` if there are 2 or more series (the first one is red); otherwise `[primary.main]`. Semantic colours are **not** used.
- **Line and fill:** `stroke {curve 'smooth', width 2}`. `fill`: gradient, `shadeIntensity 1`, opacity `.35 → .02`, `stops [0,95]`.
- **Markers:** size 4, `strokeWidth 2`, circle; hover size 6, `sizeOffset 3`.
- **dataLabels:** off. Hover filter: lighten.
- **Legend:** shown. Grid: padding 24/24.
- **X axis:** datetime, `tickPlacement 'on'`. Labels use `fld`, `rotate 0`, `hideOverlappingLabels`, 12px.
- **Y axis:** labels 14px.

**Click:** `markerClick`. Ignored when y = 0. Otherwise `{date: [x]}` (x is the ISO bucket label), `series_index` (`:33-50`).

---

### 6. donut (`W/viz/DonutChart.tsx`)

**Data**
- `seriesData[0].data` from `/series` (`WidgetViz.tsx:133`).
- Sample when empty or all-zero is `sampleStatusSeries` (`:31-38`).
- Labels are the raw bucket labels (`x`), **not translated**, e.g. "SUCCESS" (`:56-59`). Values are `y`.

**Colours** (`:62-68`):
- If the sample is shown, or `field` contains "status" or "vulnerable_endpoint_action": `getStatusColor(label)` per slice.
- Otherwise: `colors(400|600)`. When `labels == ['true','false']` or `['false','true']`, they become green/red pairs (`Charts.ts:884-899`).

**Options** (`donutChartOptions`, `Charts.ts:862-966`, defaults kept):
- **Shape:** donut `size 70%`, background transparent.
  - `plotOptions.pie.donut.labels.value.show: true`, but `labels.show` is not set. The ApexCharts default for `labels.show` is false (not in repo), so there is no centre label.
  - `stroke`: width 3, colour background.paper (slice separators), curve smooth.
  - `fill opacity 1`. Hover filter: lighten.
- **dataLabels:** on, 10px IBM Plex Sans 600, colour text.primary (or `#000000` when the slice colour is `#ffffff`), no background, no shadow. Content is the ApexCharts default (not in repo).
- **Legend:** bottom, IBM Plex Sans, `itemMargin` 8/4. Click toggles the slice, hover highlights it.
- **Tooltip:** custom HTML `<div style="background:{background.nav};color:{text.primary};padding:2px 6px;font-size:12px">{label}</div>`. It shows the **label only, no value** (`Charts.ts:71-80`).
- Slices get `cursor:pointer`.

**Click:** `{[field]: [meta]}` (`:43-53`).

---

### 7. number (`W/viz/NumberWidget.tsx`)

**Data:** `/count` returns `EsCountInterval {interval_count, previous_interval_count, difference_count}`. There is no sample state.

**Layout:** a flex row (`:96-135`) with `align-items:center`, gap 12, `overflow:hidden`, `height:100%`, `paddingBottom` 8. It contains, in order:
1. **Icon tile:** 42×42, radius 4, colour C, background `${C}1a`, box-shadow `inset 0 0 14px ${C}22`. Icon at MUI default size (24px). C and the icon depend on the `base_entity` of the first series filter (`:40-86`):

   | base_entity | Icon | C |
   |---|---|---|
   | scenario | MovieFilterOutlined | secondary |
   | simulation | HubOutlined | primary |
   | inject | BoltOutlined | warning |
   | asset | DnsOutlined | primary |
   | finding | mdi `Binoculars` | error |
   | vulnerable-endpoint | BugReportOutlined | error |
   | asset-group | mdi `SelectGroup` | primary |
   | expectation-inject | CrisisAlertOutlined | warning |
   | anything else | NumbersOutlined | primary |

2. **Value:** `compactNumber(Math.round(animated))`, or `-` when null. It is rendered **inside an FDS `Button` with `priority="tertiary"`**, `minWidth 0`. Library styling: IBM Plex Sans **14px, weight 600**, colour `--color-filigran-brand-primary`, height 36, px 16, hover background brand-primary at 10%.
   - Animation: `useCountUp` from the previous value, 1200 ms, easeOutCubic `1-(1-p)^3` (`F/utils/hooks/useCountUp.ts`).
   - `compactNumber`: below 1000 as-is, otherwise one decimal and a K/M/G… suffix, trailing zeros stripped, negative sign kept (`F/utils/number.ts:53-57`).
   - Tooltip: `interval_count.toLocaleString()`.
3. **TrendChip** (`W/viz/TrendChip.tsx:47-79`):
   - Shape: inline-flex, gap 4, height 22, paddingInline 6, radius 4.
   - Colours: background `alpha(c,.14)`, border `1px alpha(c,.28)`, text colour c.
   - Text: Geologica 12px 600, `lineHeight 1`.
   - Icon 14px: `TrendingUp` / `TrendingDown` / `TrendingFlat`. c is success / error / text.disabled.
   - Label: `+N` or `-N` via `compactNumber` (no sign when 0).
   - Tooltip: `t('was previously',{previous_number})` = "({n} previously)".

**Click** on the value: `filter_values_map {}`, `series_index 0` (`:88-94`).

---

### 8. list (`W/viz/list/ListWidget.tsx`)

**Data**
- `/entities` returns `EsEntities {es_datas, page_number, page_size, total, total_pages}`.
- Page size comes from `useLocalPaginationState(20, 'widget-list-<id>')`. Options are `[20,50,100]` (`WidgetWrapper.tsx:70-73`).
- **Pagination lives in the title row** (`WidgetWrapper.tsx:214-249`). It shows only when `total > page_size`, as MUI TablePagination: toolbar `minHeight 22`, `paddingLeft 0`, labels and select at 12px, action buttons padding 2px. It sets `contentLoading` and shows a loader in the body.

**Rows** (`:38-150`): virtualised (`@tanstack/react-virtual`, row height **50**, overscan 5). Each row is an MUI `ListItem` with `divider`:
- **Leading icon** (`ListItemIcon`, colour primary):
  - If `inject_expectation_type` is set: the expectation-type icon (`F/admin/components/common/ExpectationIconByType.tsx:15-27`): PREVENTION GppGoodOutlined, DETECTION SensorsOutlined, VULNERABILITY BugReportOutlined, HUMAN_RESPONSE SupportAgentOutlined, MANUAL TaskAltOutlined, ARTICLE NewspaperOutlined, CHALLENGE EmojiEventsOutlined, otherwise HelpOutlineOutlined.
  - Otherwise the entity icon (`elements/EntityLeadingIcon.ts:14-38`): asset → category icon or DevicesOtherOutlined, vulnerable-endpoint DevicesOtherOutlined, asset-group SelectGroup, team GroupsOutlined, finding Binoculars, scenario RouteOutlined, simulation PlayCircleOutlineOutlined, inject Target, attack-pattern LockPattern.
  - **Simulation rows have no leading icon** (`:79`).
- **Columns:** from `widgetConfig.columns`, inside a flex row (nowrap). Each cell: `minHeight 20`, `fontSize 14`, nowrap with ellipsis, `paddingRight 8` (`F/components/common/queryable/style/style.ts`).
  - **Width** is `100/n %` with a 10% minimum (`elements/ColumnStyles.ts`). Its loop iterates indices, so fixed widths are never counted.
  - **Per-column renderers** (`elements/ListColumnConfig.tsx:47-147`):
    - `base_tags_side`: ItemTags.
    - `finding_type`: Chip with `color` from CVE `#f44336`, PortsScan `#0fbcff`, IPAddress `#00bcd4`, Hostname `#9575cd`, Text `#78909c`, otherwise `#607d8b`.
    - `base_attack_patterns_side`: AttackPatternFragment.
    - Dates: `nsdt` = Intl `{second, minute, hour, day, month:'short', year}` with a tooltip.
    - `inject_expectation_type`: Chip with a type icon 14px coloured `#0fbcff`; label Prevention / Detection / … (`ExpectationTypeChip.tsx`).
    - `inject_expectation_status`: ItemStatus, an FDS Chip with severity from `statusSeverity(label)`, where the label is `computeInjectExpectationLabel(status,type)`, e.g. "Not Detected".
    - `inject_expectation_source`: Chip with a kind icon (PLAYERS PersonOutlined severity info, TEAMS Groups3Outlined entity analyses, ASSETS DevicesOtherOutlined entity victimology, ASSETS_GROUPS SelectGroup entity arsenal). The label is the target name resolved in a batch via `POST /api/{assets|asset_groups|teams|players}/options` (`useInjectExpectationTargetLabel.ts`).
    - `status`: ExerciseStatus or ScenarioStatus chips.
    - Default: plain text with a tooltip.
- **Hover:** `background action.hover`, `box-shadow inset 2px 0 0 0 primary.main`, radius 4, transition .15s.
- **Trailing:** `KeyboardArrowRight` in colour `action` when the row has a URL.

**Navigation:** rows are router Links (`elements/ListNavigationHandler.tsx:45-97`):

| Entity | URL |
|---|---|
| asset | `/admin/assets/{id}` |
| asset-group | `/admin/asset_groups/{id}` |
| team | `/admin/teams/{id}` |
| scenario | `/admin/scenarios/{id}` |
| simulation | `/admin/simulations/{id}` |
| finding | `/admin/findings/{id}` |
| vulnerable-endpoint | the asset page with a CVE filter query |
| inject | atomic test, simulation inject, or a filtered inject list |
| expectation-inject | `…/injects/{inject}?expectation_id=…&target=…` |
| others | not navigable: disabled button, opacity kept at 1 |

**Empty / misconfigured:** `Empty` with "No data to display" (`:260-270`). With no columns configured, the plain text "No columns configured for this list." (`:227-229`).

---

### 9. average: Security domains (`W/viz/domains/*`)

#### Data
- `/average` returns `EsAvgs.security_domain_average[]`. Each entry is `{label: domain name (server-resolved), data: EsSeries[]}`. Each EsSeries is `{label: expectation type, value: doc count, data: [{key: status, label: status, value}]}` (backend `ElasticService.java es9:850-924`).
- **Transform** `determinePercentage` (`SecurityDomainsWidgetUtils.tsx:224-231`):
  - Drops the "To classify" domain.
  - Per data row: `label = computeInjectExpectationLabel(status, type)`, `percentage = value / series.value × 100`, `color = colorByLabel(status)` (SUCCESS → success, FAILED → failed, otherwise pending) (`:171-199`).
  - Per type: `successRate = SUCCESS / (SUCCESS + FAILED)`, status keys matched case-insensitively, `-1` when nothing is resolved. Its colour comes from `colorByAverageForExpectation`: below 0 EMPTY_DATA, ≤25 failed, ≤50 warning, ≤75 intermediate, ≤100 success (`:106-121`).
- **Card list** (`SecurityDomainsWidget.tsx`): **all** domains from the store (`GET /api/domains`, loaded at login in `F/admin/Index.tsx:143`), minus "To classify".
  - Order: Endpoint, Network, Web App, E-mail Infiltration, Data Exfiltration, URL Filtering, Cloud, Artificial Intelligence, Tabletop, then unknown domains (`F/utils/domains/domainIcons.tsx:14-56`).
  - A domain with no data gets `{data: [], color: EMPTY_DATA}`.
  - Row container: flex, `align-items:stretch`, gap 12, padding 4, `overflow-x:auto`.
  - One card is open at a time. Clicking the open card closes it.

#### Card (`SecurityDomainCardWidget.tsx:95-234`)
- **Domain score:** Σ SUCCESS / Σ (SUCCESS + FAILED) across types. `bandColor` comes from `colorByAverageForExpectation`. Displayed with countUp over 1200 ms.
- **Section:** flex, gap 12, height 100%. `flex` is `0 0 auto` when open, otherwise `1 1 0`.
- **ButtonBase tile:**
  - Layout: `flex 1 1 0`, `minWidth 112`, column, `space-between`, gap 8, padding 12, radius 4, text-align left.
  - Surface: background `alpha(surface, hasData ? .5 : .25)`, where surface is background.accent (`#1f3965`) in dark and background.default (`#f2f2f3`) in light.
  - Border: 1px, `alpha(bandColor,.55)` when open, otherwise divider.
  - Hover: `translateY(-2px)`, border `alpha(band,.55)`, shadow `0 4px 14px alpha(#000,.35), 0 0 0 1px alpha(band,.15)`. Transition .2s.
  - **Header row** (gap 8): an icon box 30×30, radius 4, background `alpha(band,.15)`, holding the domain icon at 18px in bandColor. Domain icons: ImportantDevicesOutlined, PublicOutlined, WebAssetOutlined, MailOutline, StorageOutlined, LockOutlined, CloudOutlined, SmartToyOutlined, GroupsOutlined, otherwise HelpOutline. Next to it, `t(domainName)` in body2, 12px 600, lineHeight 1.25, clamped to 2 lines, colour text.primary (text.disabled when there is no data).
  - **Score:** Geologica 22px 600, lineHeight 1, colour band. Shows `{round}%`, or "—" in text.disabled.
  - **Meter:** height 4, radius 2, track `alpha(text.primary,.08)`, fill width = animated %, transition `width .2s linear`.
  - **Type icons row** (gap 6): one icon per type at 14px, coloured with that type's `color`. With no data, three placeholder icons (prevention, detection, vulnerability) in EMPTY_DATA. Each has a tooltip `t(capitalize(label))`.
- **Open panel** (`:236-366`):
  - Box: width **300**, column, centred, gap 10, padding 10/12, radius 4, border `1px alpha(band,.35)`, background `alpha(surface,.5)`, `box-shadow: inset 2px 0 0 bandColor`.
  - **Per type** (column, gap 4):
    - Header row (gap 6): type icon 15px in the type colour; title `t(capitalize(type))` at 11px 600, `.08em` uppercase, text.secondary, flex 1.
    - Then **one ButtonBase tile per status row**, in ES bucket order: width **42**, height 20, radius 3, Geologica 11px 700, text in the row colour, background `alpha(color,.12)`, hover `.28`. Text = `percentage.toFixed(0)%`. Tooltip `"{Prevented|Not Detected|…} - click to investigate"`.
    - Under the header, a **segmented bar**: flex, gap 2, height 4, radius 2. One segment per row with percentage > 0 (width %, row colour). If every row is 0, a single track in `alpha(text.primary,.08)`.
  - **No data:** body2 centred, maxWidth 240, text.secondary: "No data collected on this domain at this time. Run a scenario to start analyzing your position on this domain."

**Click** on a % tile: `{base_security_domains_side: [domain_id], inject_expectation_type: [type], inject_expectation_status: [status]}`, `series_index 0` (`:79-93`).

---

### 10. security-coverage (`W/viz/SecurityCoverage.tsx`, `SecurityCoverageContent.tsx`, `KillChainPhaseColumn.tsx`, `AttackPatternBox.tsx`, `securityCoverageUtils.ts`, `components/ColoredPercentageRate.tsx`)

#### Data
- `/series` with field `base_attack_patterns_side`. **`series[0]` = SUCCESS and `series[1]` = FAILED.** Each is filtered on expectation-inject, status and type (`W/WidgetUtils.tsx:258-290`). Bucket keys are attack_pattern_ids. There is **no sample state**.
- Store data: `GET /api/attack_patterns` (fields `attack_pattern_id`, `_name`, `_external_id`, `_parent`, `_kill_chain_phases` as ids) and `GET /api/kill_chain_phases` (`phase_id`, `phase_name`, `phase_external_id`, `phase_kill_chain_name`, `phase_order`). Both are loaded at login (`F/admin/Index.tsx:140-141`).
- **`resolvedData`** (`securityCoverageUtils.ts:32-60`): each bucket is mapped to its attack pattern, then **rolled up to its top-most parent's `external_id`**. The bucket keeps its own kill-chain phase external ids.
- **Indexing:** by `phase_external_id` (`:63-75`).

#### Kill chains (`SecurityCoverageContent.tsx:113-142`)
- Phases are sorted by `phase_order`.
- Distinct `phase_kill_chain_name` values are sorted alphabetically.
- Default chain: the configured preferred chain, otherwise the first whose name contains "attack", otherwise the first.
- The selection is persisted in localStorage under `widget-{id}-kill-chain`.
- Display labels: `mitre-attack` → "MITRE ATT&CK", `mitre-atlas` → "MITRE ATLAS", otherwise the raw name (`F/admin/components/common/filters/killChainLabel.ts`).

#### KPIs (`:158-194`)
- **Techniques:** top-level patterns (parent null) that have any phase in the active chain.
- **Covered:** distinct roll-up external ids present in either series.
- **Gaps** = total − covered. **coveragePct** = round(covered / total × 100).
- **Accent** `getCoverageAccent(rate)` (`securityCoverageUtils.ts:105-111`): null or negative `#5c6bc0`… exactly: null/<0 `#5c6b7a`, ≤.25 `#f44336`, ≤.5 `#ff7043`, ≤.75 `#ffb300`, otherwise `#26a96c`.

#### Header (`:204-369`)
- **Box:** flex-wrap, space-between, gap 16, padding 12/16, `margin-bottom 16`, radius 4, `1px divider`, background `linear-gradient(135deg, alpha(primary,.06) 0%, alpha(background.paper,.4) 60%)`. Class `noDrag`.
- **Overline style** (`OVERLINE_SX`, `:65-73`): Geologica 600 10px, `.12em` uppercase, text.secondary, lineHeight 1. Each block is a column with gap 6.
- **Left cluster** (align flex-end, gap 16):
  - "Kill chain" + FDS `Select` (trigger and value).
  - "Techniques" (key `techniques`) + MUI `ToggleButtonGroup`, exclusive, small. Buttons have padding 2/10 and no text-transform. Values: `All`, `Covered <n>`, `Gaps <n>`; the counts are in text.secondary with ml 6. The selection is persisted in localStorage under `widget-{id}-coverage-filter`, default `all`.
  - Theme overrides for the group (`ThemeDark.ts:493-511`): height 36, ripple hidden, button border `1px #2B3447` (light `#D2D2D2`), selected background primary at 25%, hover (not selected) primary at 15%. The `color` entry is the custom primary, null by default, so MUI's default colour applies.
- **Right cluster** (gap 24):
  - **Coverage** (minWidth 132): Geologica 600 18px in the accent colour showing `{pct}%`, plus a caption `{covered}/{total} tested` in text.secondary. Below, a meter 4px high, radius 2, track `alpha(text.primary,.08)`, fill in the accent colour.
  - **"Success rate"** + legend `ColoredPercentageRate`: 4 adjacent body2 labels in white text, padding 4/8, no gap. Labels are `≤ 100%`, `≤ 75%`, `≤ 50%`, `≤ 25%` on `rgba(9,78,11,.4)`, `rgba(184,135,10,.4)`, `rgba(136,65,6,.4)` and `rgba(136,17,6,.4)` respectively (`securityCoverageUtils.ts:81-84`).

#### Matrix
- Container: flex, gap 8, `paddingRight` 8, `overflow:auto`, `flex 1` (`:32-40`).
- If the scope is covered with 0 covered, or gaps with 0 gaps, it shows `Empty "No data to display"` (`:373-375`).
- **Column** (`KillChainPhaseColumn.tsx`):
  - Title: Typography h5 (Geologica 700 16px, the theme lowercases it with the first letter capitalised), `margin-bottom 16`, single-line ellipsis with a tooltip only when truncated (`EllipsisTooltip`). Observed on OpenAEV 3.261009 the phase names show as stored ("Privilege Escalation"): the engine writes them as stored.
  - Body: a grid, gap 8, `paddingBottom` 8, **width 170px**.
  - Techniques: top-level patterns of the phase (by `phase_id`), sorted by name with a plain `<`/`>` comparison (`F/utils/attack_patterns/attack_patterns.ts`).
  - Per technique: success = Σ series0 values whose roll-up id equals its external_id; failure = Σ series1 values, likewise; `present` = it has any bucket in either series.
  - Scope filter: covered = present, gaps = not present.
  - In non-`all` scopes, an empty column is hidden.
- **AttackPatternBox** (`AttackPatternBox.tsx`):
  - `tested` = total > 0 and a rate exists. The accent is computed on the rate when tested, otherwise from null (`#5c6b7a`).
  - **Box:** radius 4, padding `5px 8px 5px 10px`, `overflow:hidden`.
    - Background when tested: `linear-gradient(90deg, alpha(accent, dark ? .22 : .16), alpha(accent,.04))`. Untested: `rgba(255,255,255,.03)` (dark) or `rgba(0,0,0,.03)` (light).
    - Border: 1px, `alpha(accent,.4)` when tested, otherwise `rgba(255,255,255,.06)` / `rgba(0,0,0,.06)`.
    - `::before` left bar: 3px wide, full height, accent colour when tested, transparent otherwise.
    - Hover (only when clickable): `translateY(-1px)`, border `alpha(accent,.7)`, shadow `0 2px 10px alpha(accent,.25)`. Transition .15s.
  - **Row 1** (flex, baseline, space-between, gap 6): external id in Geologica 10px 700, `.04em`, colour accent when tested otherwise text.secondary. If tested, `{successCount}/{total}` at 10.5px 600 in text.primary.
  - **Row 2:** name at 11px, lineHeight 1.25, clamped to 2 lines with `word-break`, minHeight 28. Colour text.primary when tested, otherwise text.secondary.
  - **Meter:** marginTop 4, height 3, radius 2, track `rgba(255,255,255,.08)` / `rgba(0,0,0,.08)`. Fill width = round(rate × 100)% in the accent colour, transition `width .6s cubic-bezier(.22,1,.36,1)`.
  - **Tooltip** (`side="top"`): "{id} - {name}".
  - **Muted state** = untested (styles above).

**Click** on a box: `{[field]: [clickedId, ...all sub-technique ids from the referential]}`, `series_index 0` (`KillChainPhaseColumn.tsx:123-137`).

**Fullscreen** (`SecurityCoverage.tsx:38-63`):
- MUI Dialog, fullScreen, Paper elevation 1, slide Transition.
- DialogTitle (h5 by default): background `background.nav` (dark) or `background.default` (light), `border-bottom 1px divider`, padding 10/0, inline-flex. It holds a close IconButton (`Close`, small, tertiary, md) and the widget title.
- Content: the same component.

---

### 11. attack-path (`W/viz/attack_paths/*`)

#### Data
- `POST /api/dashboards/attack-paths/{id}` returns `EsAttackPath[]`: `{attackPatternId, attackPatternName, attackPatternExternalId, killChainPhases: [{id, name, order}], attackPatternChildrenIds, injectIds, value}`.
- `value` = success × 100 / (success + failed), as an integer, or null (`openaev-model/.../EsAttackPathService.java:155-174`).
- **Simulation id** (`AttackPathContextLayer.tsx:28-30`): the first `base_simulation_side` value in `series[0]`. If that id is a dashboard simulation parameter id, the parameter's current value is used instead.
- Then `GET /api/exercises/{id}` (`F/actions/Exercise.ts:44`), using `exercise_start_date` and `exercise_end_date`.

#### States
- **No simulation:** a column centred at height 80%, containing Typography h5 (lowercased, first letter capitalised) with `t('You must set a simulation')` (`:44-60`).
- **Loading:** `Loader inElement`.

#### Graph layout (`AttackPath.tsx`)
- **Legend:** `ColoredPercentageRate` with `style {marginLeft:'auto', paddingBottom:8}` (`:287-291`). It is a block-level flex with auto width, so the auto margin resolves to 0 and the legend sits **left**. The ReactFlow element (100% height by default) sits under it.
- **Phases:** attack paths are grouped by each of their `killChainPhases` and sorted by `order` (`:42-61`). An attack path with several phases appears once per phase.
- **Positions** (`:96-139`): `x = 50 + 200 × xIndex`; `y = 50 + 150 × yIndex`, or 0 for kill-chain header nodes. Every node: `draggable false`, `targetPosition Left`, `sourcePosition Right`.
  - **Header node:** id = phase id, type `default`, `data.label = phase name`, class `kill-chain-node` (border none, text-align left, top/bottom handles hidden), at y = 0.
  - **Technique nodes:** type `attackPattern`, id `{apId}-{phaseId}`, at x = phaseIndex, y = index within the phase.
  - **Date nodes:** type `default`. `start-date-node` at (0, maxY+1) and `end-date-node` at (maxXIndex, maxY+1), where `maxXIndex = phases > 2 ? phases - 1 : 1`. Label = `fldt(date)` = Intl long date-time (`{day, month:'long', year, hour, minute, second}`).
- **Time edge:** `start-date-node → end-date-node`, default edge type, label `du(end - start)` (format `Ns`, `Mmin Ss`, `Hh Mmin`: `i18n.tsx:340-363`), `labelShowBg false`, `labelStyle {fill: text.primary, fontSize: 14}`. No marker; default stroke.
- **CSS** (`:71-83`): `--xy-node-background-color: none`; left and right handles `visibility:hidden`. Everything else (default node border, padding, 150px width, 12px font) is the xyflow default stylesheet, not in repo.
- **AttackPatternNode** (`AttackPatternNode.tsx`): `AttackPatternBox` with `style {width:150px}`, `successRate = value/100`, **no `total` and no onClick**. AttackPatternBox computes `tested = (total ?? 0) > 0 && …`, so **every node renders in the muted, untested style**: no accent, no count, empty meter, `#5c6b7a`-based. The legend colours never appear on the nodes as coded.
- **Edges only on hover** (`:180-199,248-259`):
  - When a technique node is hovered, edges are drawn from it to every node of each `attackPatternChildrenIds` child (in all of that child's phases).
  - Type `draggableEdge` (`DraggableEdge.tsx`): a bezier path. When source and target are on the same row (±10px) and the source is right of the target, it becomes an arc `M sx,sy-10 A (sx-tx)×.55 35 0 1 0 tx,ty-10`.
  - Stroke `var(--color-feedback-error-primary)`; `markerEnd` ArrowClosed 15×15, same colour. Not animated.
- **ReactFlow props** (`:292-319`):
  - Theme: `colorMode` = palette mode. `proOptions {hideAttribution: true}`.
  - Interaction: nodes not draggable, not connectable, not focusable; edges focusable; `elementsSelectable`. `connectionLineType SmoothStep`.
  - Viewport: `panOnDrag`, `panOnScroll`, **`zoomOnScroll false`**, `preventScrolling false`. **No fitView, no Background, no Controls, no MiniMap.** The default viewport therefore starts at x 0, y 0, zoom 1 (xyflow default).

**Click** on a technique node (`:262-283`): navigates to `/admin/simulations/{simId}?query=btoa(qs.stringify({page:0, size:20, sorts:[{property:'inject_updated_at', direction:'DESC'}], filterGroup:{mode:'or', filters:[{id, key:'inject_attack_patterns', mode:'and', values:[apId], operator:'contains'}]}, key:'simulation-injects-results'}, {allowEmptyArrays:true}))`.

---

### 12. command-center (`W/viz/command_center/*`)

#### Data (`CommandCenterWidget.tsx`)
- `/series`, structural on field `inject_expectation_type`. The default home dashboard declares the series SUCCESS, FAILED, PENDING (`F/admin/components/default_dashboard/defaultHomeWidgets.ts:182-185,273`).
- **Series index resolution** (`:41-52`): first label containing SUCCESS / FAIL / PENDING, falling back to positions 0 / 1 / 2.
- **Sample:** `sampleExposureSeries` when all-empty, shown with **`subtle`** greying.
- **Per type key** (bucket label): success, failed, pending, `resilience = round(s/(s+f)×100)`.
- **Gate order** (`:126-140`): VULNERABILITY, PREVENTION, DETECTION, MANUAL, CHALLENGE, ARTICLE, then the rest alphabetically.
- **Totals:** `exposureScore = round(failed / (success + failed) × 100)`, `validations = s + f`, `breached = failed`.
- **Security platforms:** read from the store (`helper.getSecurityPlatforms()`). The widget does **not** fetch them; only `DefaultHomeDashboard.tsx:51` dispatches `GET /api/security_platforms`.
  - Fields used: `asset_id`, `asset_name`, `security_platform_type`.
  - Logo: `/api/images/security_platforms/id/{id}/{dark|light}`, tenant-prefixed (`:164-205`).
  - With none in the store, a fallback set is used: EDR, SIEM, NDR, SOAR, XDR, ISPM.

#### Layout
- Grid with `grid-template-columns: minmax(240px,3fr) minmax(400px,9fr) auto`, gap 12, overflow hidden (`:276-284`).

#### ExposureConsole: left column (`ExposureConsole.tsx`)
- **Bands** (score; `:92-109`):

  | Score | Label (English) | Colour |
  |---|---|---|
  | < 25 | "Low exposure" | success.main |
  | < 50 | "Moderate exposure" | warning.main |
  | < 75 | "High exposure" | `var(--color-feedback-warning-primary)` |
  | ≥ 75 | "Critical exposure" | error.main |

- **SVG geometry:** viewBox 256×256, cx = cy = 128, rRing 84, rOrbit 102. CSS `max-height:100%`, `max-width:100%`, `overflow:visible`, cursor pointer, class `noDrag`. Clicking it (or pressing Enter/Space; role=button, aria "How is this score computed?") opens the dialog (`:223-235`, `:188-198`).
- **Defs** (`:236-254`):
  - Score gradient: linear `x1 0% y1 100%` to `x2 100% y2 0%`, stops 0 success, 45% warning.main, 100% error.
  - Glass: radial `cx 38% cy 30% r 75%`, stops 0 `tint(c,32)`, 55% `tint(c,10)`, 100% `alpha(dark ? #000 : #fff, .06)`.
  - Gloss: radial 50/50/50, stops 0 `alpha(#fff, dark ? .14 : .5)`, 70% `alpha(#fff, dark ? .06 : .2)`, 100% transparent.
- **Sphere** (`:257-262`):
  - circle r 78, fill glass, stroke `tint(c,18)` 1px
  - circle r 60, stroke `rgba(255,255,255,.05)` (dark) or `rgba(0,0,0,.05)`
  - ellipse rx 74, ry 24.67, stroke `tint(c,12)`
  - gloss ellipse at (114, 94), rx 40, ry 20
- **Orbit** (`:265-346`):
  - Dashed guide: ellipse r 102, stroke `rgba(255,255,255,.04)` / `rgba(0,0,0,.04)`, dasharray "2 6".
  - The group rotates 0→360° around the centre over **90s**, SMIL `animateTransform`, repeating.
  - At most 8 nodes. Node i sits at angle `(360/n)·i − 90°` on r 102. Each node counter-rotates 360→0 over 90s around its own position.
  - Node colour = `SECURITY_PLATFORM_TYPE_COLORS[type]` (EDR `#0fbcff`, XDR `#00bcd4`, SIEM `#ffb300`, SOAR `#9575cd`, NDR `#26a96c`, ISPM `#ff7043`, EMAIL_SECURITY `#4dd0e1`, LLM_FIREWALL `#00f1bd`, AI_GATEWAY `#7e57c2`, VULNERABILITY_SCANNER `#ef5350`; `F/components/securityPlatformType.ts:25-36`), otherwise primary.
  - **Node drawing:**
    - circle r 13, fill `#0c1526` (dark) / `#f4f8fc` (light), stroke `alpha(c,.7)` 1.2
    - overlay circle r 13, fill `alpha(c, dark ? .16 : .1)`
    - pulse ring r 13→16→13, opacity .5→0→.5, duration `3 + (i%3)` s
    - logo: `<image>` 20×20 clipped to a circle of r 10 (`xMidYMid slice`). Without a logo: the type abbreviation as text (≤ 4 chars, else the initials of `_` parts), 8px 700 `.04em`, fill c.
    - name text at y + 21: 7.5px `.03em` in text.secondary, truncated to 21 chars + "…" when longer than 22
    - `<title>`: "name (type)"
- **Score ring** (`:352-384`):
  - Path: a full circle drawn as an arc from −90° to 269.99°. C = 2π·84 ≈ 527.79.
  - Track: stroke `rgba(255,255,255,.07)` / `rgba(0,0,0,.07)`, width 5, round caps.
  - Glow: gradient stroke, width 11, opacity .28. Main: gradient stroke, width 5, round caps.
  - Both: `stroke-dasharray C`, `stroke-dashoffset C·(1 − progress)`, transition 1.4s `cubic-bezier(.22,1,.36,1)`. progress = score/100, set 60 ms after mount.
  - End marker: group rotated by `progress × 360°` around the centre, same transition. Circles at (128, 44): r 8.5 `tint(c,30)` and r 4.5 in c.
- **Centre text:**
  - Number (countUp 1400 ms): y 122, `dominant-baseline central`, Geologica 52px 500, text.primary.
  - "At risk": y 156, 10px 600, `.24em` uppercase, fill c.
- **Chip under the orb** (`:419-470`):
  - Inline-flex, gap 6, paddingInline 8, height 22, radius 999, border `1px tint(c,30)`, background `tint(c,10)`, hover `tint(c,20)`.
  - Dot: 6px circle in c with `box-shadow 0 0 6px c`.
  - Band label: 10px `.08em` uppercase in c.
  - Sub-text, 10px text.secondary: "No validations yet" (0 and 0), "All controls holding" (0 gaps), otherwise "{count} gaps to remediate" (count via `compactNumber`).
  - Tooltip: "Exposure score {score} / 100 - click to understand how it is computed".
  - Clicking it opens the dialog.
- **Dialog** (maxWidth sm, fullWidth, radius 4; `:472-719`):
  - Title: `InfoOutlined` in primary + "Adversarial exposure score".
  - Hero: padding 16, border `tint(c,30)`, background `tint(c,8)`. Score in Geologica 44/500 in c; band label at 600, uppercase, `.08em`; then "{gaps} of {validations} validations breached your controls." or "No validations have run yet."
  - h4 "What it measures" + paragraph.
  - Monospace 13px formula box (padding 12, background action.hover, divider border): `exposure = {gaps breached (error colour)} / {n total} x 100 = {score}`.
  - Paragraph mentioning `({resilience}%)`.
  - "Breakdown by pillar": per pillar with total > 0, a label (Prevention/Detection/…) plus "{failed} / {total} breached ({pct}%)", and an 8px rounded bar (stopped in success colour, breached in error colour) with tooltip "{stopped} stopped - {breached} breached". Legend dots "Stopped" / "Breached".
  - "Severity bands": 4 rows `0 - 24`, `25 - 49`, `50 - 74`, `75 - 100`, each with a description. The current row has background `tint(color,10)`, border `tint(color,40)` and the suffix " - current".
  - Actions: "Close" (secondary) and "Investigate breached validations". The latter triggers drill `breach`.

#### AttackFlow: centre column (`AttackFlow.tsx`)
- **SVG:** viewBox **680×240**, `xMidYMid meet`, `width:100%`, `max-height:100%`, `overflow:visible`.
- **Constants:** CY 104, adversary x 52, assets x 624, gates span 158→528, GATE_HALF 52. Rows: caption y **38**, value y **178**, gauge y **188**, percentage y **208** (`:23-38`).
- **Colours:**
  - Line: `rgba(255,255,255,.10)` (dark) or `rgba(0,0,0,.10)`
  - Track: `alpha(text.primary,.12)`
  - Attack: error.main
  - Assets: text.secondary
- **Beam:** `M78,104 L598,104`, 1px line colour, dasharray "3 5", dashoffset animated 8→0 over 0.8s, repeating.
- **Gates:** one per layer with success + failed + pending > 0; n = count, minimum 1.
  - Position: `x = 158 + 370·(i + .5)/n`. Column width = 370/n. Label max width = max(col − 10, 24). Gauge width = clamp(col − 24, 32, 56).
  - Colour: `#0fbcff` for known types (PREVENTION, DETECTION, VULNERABILITY, HUMAN_RESPONSE, MANUAL, ARTICLE, CHALLENGE); otherwise `['#38bdf8','#818cf8','#c084fc','#22d3ee','#a855f7'][i%5]`.
  - rate = s/(s+f). Gauge colour: rate ≥ .75 success, ≥ .5 warning.main, ≥ .25 `#ff7043`, otherwise error. text.disabled when nothing is resolved.
  - Track rect: x − 1.5, y 52, width 3, height 104, rx 1.5.
  - Fill: halo rect width 7, rx 3.5, `alpha(color,.3)`, plus a core rect width 3 in color. Height `max(rate·104, resolved ? 3 : 0)`, growing upward from the bottom, animated over 1.1s with spline `0.22 1 0.36 1`.
  - Crossing node: circle r 5, fill `#0c1526` / `#f4f8fc`, stroke color 1.5, with an inner r 2 dot pulsing 2→3→2 over `2.4 + (i%3)·.4` s.
  - Caption: y 38, 9.5px `.1em` uppercase, text.secondary. Truncated to `floor(maxW/(9.5·.62))` chars + "…", with a `<title>`. Labels: Prevention, Detection, Vulnerability, Manual, Challenge, Article, else humanised.
  - Value: y 178, Geologica 17/600 in color, `compactNumber(success)`, `<title>` with the exact count.
  - Gauge: rect at y 188, height 5, rx 2.5, track colour. Fill width `max(w·rate, rate > 0 ? 4 : 0)`, animated 0→w over 1.1s.
  - Percentage: y 208, 10px 600 in the gauge colour, `{round(rate·100)}%`. Group `<title>`: "{pct}% {prevented|detected|not vulnerable|validated|stopped}".
  - Hit area: transparent rect x ± col/2, y 26, height 194; role=button; triggers drill of the gate's type.
- **Particles:**
  - Count per value = 0 if ≤ 0, otherwise `clamp(round(v/total·8), 1, 3)`, where total = all attempts including pending (minimum 1).
  - Particle: circle r 3.2 in error colour, plus a glow circle r 6.5 filled with a radial gradient (error at opacity .9, then .35 at 60%, then 0). They follow `animateMotion` from x 78 to x − 6. Opacity keyframes `0; o; o; 0` at times `0; .08; .9; 1`.
  - Stopped particles: duration `2.2 + i·.4 + k·.6` s, begin `i·.7 + k·1.1` s.
  - Impact ring per gate: r 10→24, opacity 0→.9→0, stroke color 1.5, duration `2.6 + i·.3`, begin `1.1 + i·.7`.
  - Breach particles: from 78 to 598, duration `4.4 + i·.8`, begin `1.8 + i·1.9`, opacity .9. Ring at 624 in error colour, duration 4.4s, begin 6.1s.
- **Adversary** (x 52):
  - Caption "Adversary".
  - circle r 22, fill `${error}14` (8% alpha hex), stroke error 1.5. A pulse ring grows r 22→33 while opacity falls .6→0 over 2s.
  - Crosshair: circle r 9; four 1.5px lines from ±5 to ±14 on both axes; centre dot r 2.
  - Value at y 178: `compactNumber(total attempts)`. Its `<title>` is "{total} - Pending: {n}" when pending > 0.
  - Hit rect: x 18, width 68 → drill `all`.
- **Breached** (x 624):
  - Caption "Breached".
  - circle r 24. When breached > 0: fill `${error}10`, stroke error. Otherwise fill `rgba(255,255,255,.04)` / `rgba(0,0,0,.04)` and stroke text.secondary. Width 1.3.
  - Three server rects at x 614, y 96 / 103 / 110, 20×5.5, rx 1.5, stroke 1.2.
  - LED dots at x 618, r .9, in error colour (or secondary.main when nothing is breached), blinking opacity 1→.2→1 over 1.5 / 2.55 / 3.6 s.
  - Value: `compactNumber(breached)`. Hit area → drill `breach`.
- **Drill-downs** (`CommandCenterWidget.tsx:210-233`). All use indexes resolved against the **real** series:
  - `breach`: `series_indexes: [failedIdx]`
  - `all`: `[successIdx, failedIdx, pendingIdx]`
  - gate: `filter_values_map {inject_expectation_type: [KEY]}`, `series_indexes: [successIdx]`

#### Right rail (`CommandCenterWidget.tsx:312-382`)
- **Panel:** column, gap 8, padding 6, radius 6, `1px divider`, background `alpha(background.default,.5)`, class `noDrag`.
- **Four router `Link` tiles**, each 38×38, radius 4, icon 20px, colour C, background `alpha(C,.08)`:

  | Label (i18n) | Caption | Icon | C | Route |
  |---|---|---|---|---|
  | Design a scenario | Emulate real-world adversaries | RouteOutlined | secondary | `/admin/scenarios` |
  | Launch a simulation | Validate your defenses now | PlayCircleOutlineOutlined | primary | `/admin/simulations` |
  | Run an atomic test | Fire a single technique | mdi `Target` | warning | `/admin/atomic_testings` |
  | Review findings | Hunt down validated gaps | mdi `Binoculars` | error | `/admin/findings` |

- **Separators:** an `::after` 1px line in divider colour at bottom −5, left 25%, width 50%, on every tile except the last.
- **Hover:** background `alpha(C,.2)`, shadow `inset 0 0 0 1px alpha(C,.5), 0 0 12px alpha(C,.35)`, `translateX(-2px)`, transition .2s `cubic-bezier(.22,1,.36,1)`.
- **Tooltip** (`side="left"`): "{label} - {caption}".
- **Animation pause:** both SVGs pause their SMIL timeline while the tab is hidden (`F/utils/hooks/useSvgVisibilityPause.ts`).

---

### 13. exposure-score (`W/viz/ExposureScoreWidget.tsx`)

**Data**
- `/series`. SUCCESS series = the first label containing "SUCCESS", otherwise `[0]`. FAILED = the first containing "FAIL", otherwise `[1]`. Pending is ignored.
- Per bucket label: `exposure = round(f/(s+f)×100)`. Overall is computed the same way (`:46-89`).
- Sample is `sampleExposureSeries`, `full` greying.
- Bands are identical to the command center (`:100-123`).

**Layout:** a column, space-between, `overflow:hidden`.
- **Gauge zone:** `flex:1`, centred.
  - **Sweep** (`::before`): a 260×260 circle with `conic-gradient(from 0deg, transparent 0deg, tint(band,13) 40deg, transparent 80deg)`, rotating 360° over 6s linear, infinite (`:155-176`).
  - **SVG:** viewBox 200, cx = cy = 100, r 82. The arc starts at **−225°** and sweeps **270°**: from bottom-left, clockwise over the top, to bottom-right (`:24-40,134-141`).
    - Track: `rgba(255,255,255,.08)` / `rgba(0,0,0,.08)`, width 12, round caps.
    - Value arc: gradient (same stops as the command center, `id exposure-gradient-{widgetId}`), width 12, round caps, `filter: drop-shadow(0 0 6px tint(band,53))`. End angle = −225 + 270·max(progress, .001). progress = overall/100, set 60 ms after mount. CSS `transition: all 1.4s cubic-bezier(.22,1,.36,1)` is on the path; per the code's own comment in `ExposureConsole.tsx:170-172`, browsers cannot transition `d`.
    - Needle: circle r 5 at the arc tip, fill band, `drop-shadow(0 0 8px band)`.
    - Texts (default baseline):
      - score (countUp 1400 ms): y 98, Geologica 44/600, text.primary
      - "/ 100": y 120, 11px `.14em` uppercase, text.secondary
      - band label: y 146, Geologica 12/600 `.1em` uppercase, in band colour
- **Breakdown row:** at most 4 buckets, flex, gap 12, `paddingBottom` 4. Each bucket (flex 1):
  - label `t(bucket label)`: 10px uppercase `.08em`, text.secondary, ellipsis
  - bar: height 4, radius 2, track `.08`, width = exposure %, colour by the same thresholds, transition 1.4s
  - value: Geologica 11/600, same colour

**Interactions:** none (no click handler).

---

### 14. resilience-gauge (`W/viz/ResilienceGaugeWidget.tsx`)

**Data**
- `seriesData[0].data` (`/series`, field `inject_expectation_status`; labels are raw status keys).
- Counts are summed by `x.toUpperCase().includes('SUCCESS' | 'FAIL' | 'PENDING')`. `resilience = round(s/(s+f)×100)`.
- Sample (empty or all-zero): SUCCESS 64 / FAILED 27 / PENDING 9, `full` greying (`:29-45,67-97`).
- **Accent:** ≥ 75 success.main, ≥ 50 warning.main, ≥ 25 `warn.main` (`#e6700f`), otherwise error.main (`:99-106`).

**Layout**
- **Domain glyph:** absolutely positioned at top −32, right 0 (on the title row). 22×22, radius 4, colour accent, background `alpha(accent,.14)`, icon 14px. The icon follows the first series' `inject_expectation_type` filter: PREVENTION GppGoodOutlined, DETECTION VisibilityOutlined, VULNERABILITY BugReportOutlined, MANUAL/ARTICLE/CHALLENGE SupportAgentOutlined, otherwise CrisisAlertOutlined (`:70-81,202-218`).
- **SVG:** viewBox 132, cx = cy = 66, r 58, C = 2π·58. `<title>` "click to investigate".
  - Track: circle in `rgba(255,255,255,.06)` / `rgba(0,0,0,.06)`, stroke 5.
  - Segments, in order: success (success.main), failed (error.main), pending (text.disabled). Only segments with value > 0 are drawn.
  - Each segment: frac = v/(s+f+p); `len = max(frac·C − (3/360)·C, .5)`; dasharray `len C−len`; dashoffset `−startFrac·C`; `rotate(-90 66 66)`; width 5, round caps; `drop-shadow(0 0 2px alpha(color,.5))`; transition 1.1s on dasharray and dashoffset (`:118-149,241-267`).
  - Texts (`dominant-baseline central`):
    - `{animated}%` (countUp 1200 ms): y 60, Geologica 30/500, fill accent
    - "Resilience": y 82, 8px `.16em` uppercase, text.secondary
- **Legend:** flex, centred, gap 14, wrap, `paddingBottom` 4. Entries for SUCCESS, FAILED, and PENDING only if > 0. Each entry is a ButtonBase (gap 4, paddingInline 4, radius 3, hover background `alpha(c,.15)`) holding a 6px dot, the label `t('SUCCESS')` = "Success" (or "Failed", "Pending") at 10.5px in text.secondary, and the count in Geologica 11/600.

**Click**
- Ring: `{inject_expectation_status: ['SUCCESS','FAILED','PENDING']}`
- Legend entry: `{inject_expectation_status: [thatStatus]}`
- Both with `series_index 0`.

---

### 15. posture-radar (`W/viz/PostureRadarWidget.tsx`)

**Data**
- `/series` (structural, e.g. `base_security_platforms_side`, SUCCESS/FAILED).
- Labels = union of bucket labels, in first-seen order. For each label, the first bucket key is kept for filtering.
- Series = `{name: t(label), data: values aligned on labels, 0 when missing}` (`:32-74`).
- Sample is `sampleRadarSeries`, **`subtle`** greying. The chart is remounted (`key`) when the sample state changes.

**Colours** (`:76-88`), per raw label:
- contains SUCCESS, DETECTED or PREVENTED → success.main
- contains FAIL or "NOT " → error.main
- otherwise `[primary, secondary, warning][i%3]`

**ApexCharts options** (`:90-151`):
- **Chart:** radar, `parentHeightOffset 0`, `offsetY 0`, animations speed 900.
- **Series styling:** `stroke.width 2`, `fill.opacity .18`. Markers size 3, `strokeWidth 1`, hover size 6.
- **dataLabels:** off.
- **Legend:** bottom, `offsetY 4`, IBM Plex Sans, marker size 5, `itemMargin` h10 / v0.
- **Tooltip:** themed.
- **X labels:** 10px IBM Plex Sans in text.secondary.
- **Y axis:** hidden.
- **`plotOptions.radar`:** `size 108` (px radius); polygon strokes and connectors `rgba(255,255,255,.08)` / `rgba(0,0,0,.08)`; polygon fill transparent.

**Click** (`dataPointSelection`): `{[field]: [labelKeys[dataPointIndex]]}`, `series_index`.

---

### 16. Extra data each widget needs

| Widget | Main answer | Extra data (fields) |
|---|---|---|
| vertical / horizontal / line / donut / posture-radar / exposure-score / resilience-gauge | `/series` (`label`, `data[{key,label,value}]`) | none. Side-field labels are resolved by the server; missing ones become "deleted" (`ElasticService es9:1003-1023`). |
| number | `/count` | none |
| list | `/entities` | `GET /api/attack_patterns` (store, for the attack-pattern column); `POST /api/{assets,asset_groups,teams,players}/options` with ids → `[{id,label}]` for the source column |
| average | `/average` | `GET /api/domains` (`domain_id`, `domain_name`) |
| security-coverage | `/series` (SUCCESS, FAILED) | `GET /api/attack_patterns`, `GET /api/kill_chain_phases` (fields in §10) |
| attack-path | `/attack-paths` | `GET /api/exercises/{id}` (`exercise_start_date`, `exercise_end_date`); dashboard parameters |
| command-center | `/series` (SUCCESS / FAILED / PENDING over `inject_expectation_type`) | `GET /api/security_platforms` (`asset_id`, `asset_name`, `security_platform_type`) plus the logo image URL. Not fetched by the widget itself. |

---

### 17. Things to reproduce or decide deliberately (as coded)
1. **Attack-path nodes are always muted:** no `total` is passed to AttackPatternBox (`AttackPatternNode.tsx:20-25` vs `AttackPatternBox.tsx:33`).
2. **The attack-path legend sits left, not right:** `marginLeft:auto` has no effect on a block with auto width.
3. **A first-load error shows "Not implemented yet"** for every widget except the vertical bar chart.
4. **The donut uses raw labels** (e.g. "SUCCESS", not translated) and its tooltip shows the label only.
5. **The number value is a 14px tertiary button**, not a large KPI font.
6. **The kill-chain column titles and "You must set a simulation"** are lowercased by the theme, with the first letter capitalised.
7. **The `rgb(255 216 0)` intermediate colour** is passed through MUI `alpha()` in the domain cards (`SecurityDomainCardWidget.tsx:123,149`). Whether MUI parses the space-separated form correctly was not checked: there is no `node_modules` in openaev-front.
8. **Not in repo:** the internals of ApexCharts, xyflow default node CSS and the FDS components. The FDS values quoted above were read from the installed xtm-one copy (same version 1.2.0).

### 18. How the engine draws them (P4: the validation views)

The six security posture widgets are the engine's validation views, drawn from the `outcomes` shape
(`dashboard_live.outcomes_data`) by `charts/OutcomeViews.tsx`, their numbers in `viz/outcomes.ts`; the
adapter (`dashboard_live/openaev.py`) reads what the widgets read (sections 9-16) and presents each
(`presentation`):

| Widget | View (`xtm.*`) | Data the adapter sends | Preset |
|---|---|---|---|
| security-coverage | `outcomematrix` | `chains` (kill chains by name, phases in order, the "attack" one `default`), `reference` (every top-level technique: `code`, `phases`, `subIds`), the tested techniques by `id` | - |
| attack-path | `pathgraph` | the techniques its injects carry (`phases`, `links`, `link` to the simulation's injects on it), one chain of the phases present, `timeline`; `needsScope` without a simulation | - |
| command-center | `exposureconsole` | an outcome per expectation type in gate order (`key`, `color`, `successLabel`, `icon`), `orbit` (`/security_platforms`, the sample set when none), `actions` | `sampleDisplay: subtle` |
| exposure-score | `scoregauge` | an outcome per bucket, as it comes | `sampleDisplay: full` |
| resilience-gauge | `ringgauge` | one outcome, its statuses, the type's glyph as `icon` | `sampleDisplay: full` |
| average | `domaincards` | an outcome per domain (`icon`, parts per expectation type with `total` and status `rows`), `reference` (`/domains` in OpenAEV's order, placeholder parts) | - |

The views XTM One drew before (`xtm.validationgates`, `xtm.exposuregauge`, `xtm.resiliencerings`,
`xtm.outcomecards`, `xtm.outcomegrid`) are read as their replacement (`dashboard_definition.LEGACY_TYPES`,
`definition.LEGACY_TYPES`), a platform widget as its module's own view (an attack path drawn as a grid
becomes a path graph).

Where the engine departs from the module, deliberately:

- **One frame.** The coverage's fullscreen is the frame's own "Expand"; the "Sample" chip and the
  resilience glyph sit in the content's top-right corner, not on the module's title row.
- **Clicks.** The module opens its results page for the widget (`openWidgetResults`), which needs the
  widget's id in the module; an XTM One widget is an ad hoc query and has none. A click says what was
  clicked to the dashboard's interactions instead: a technique `{name, value: id, row: {id, code, ids}}`
  (its sub-techniques in `ids`), a domain's status tile `{name: domain, x: type, value: status}`, a gate
  `{name, value: type}`, the adversary `all`, what got through `breach`, the ring `SUCCESS,FAILED,PENDING`
  and a legend entry its status. Without an interaction, an attack path node opens its simulation's
  injects on the technique (the module's own link, built by the adapter).
- **Selections** (the kill chain, the All / Covered / Gaps scope) are not kept per widget across visits
  (the module keeps them in local storage by widget id).
- **The attack path's rates** come from the ad hoc series by technique (the module's attack-path
  endpoint needs a widget id), with the module's formula (`success × 100 / total`, floored). Without the
  simulation's dates the path is drawn without its date nodes (the module waits for them); a simulation
  named through an OpenAEV dashboard parameter is not resolved.
- **The command center's platforms** are always read (`/security_platforms`); the module orbits them only
  once its home dashboard has loaded them, else its sample set. Their logos are not proxied: the type's
  abbreviation is drawn, the module's own fallback.
- **Words of the data** (expectation types, statuses, the known security domains, what a success is) are
  sent in English and translated by the engine's vocabulary (`charts/outcome-hooks.ts`).

