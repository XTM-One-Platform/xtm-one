# Data tables: inventory and target (issue #9378)

Every list of records in the frontend becomes ONE shared data table. This page is the inventory the migration works from (slice 0 of #9378): every table and records list on `main`, where it lives, what it does today, what it becomes and in which slice. A slice that migrates a table updates its row here in the same PR.

## The standard

A records table, whatever the screen:

- **Toolbar**: a search box, filters by column type (enum, date range, owner, boolean, number range) with an active-filter count and "Clear all", a column visibility menu, the Export when the screen exports, the grid / list toggle where the screen has one.
- **Sort** on every meaningful column, the active sort shown in the header.
- **Sticky header**: the column header row stays visible while the rows scroll, inside the page or the table's own scroll area.
- **One scrollbar** (Sam, 11 Nov 2026, issue #11233): a list page never shows two vertical scrollbars. The table's scroll area takes the room the window has left under its top, minus what the page draws below it (pager, rest of the page, main padding): `useFitToViewport` (`hooks/useFitToViewport.ts`), measured from the content, never a fixed `calc(100dvh - Nrem)` guess. Under 160 px of room (`FIT_FLOOR_PX`, the header and three rows) the page is long-form: no bound, the page scrolls alone and the table draws no scroller (`data-scroll-fit="page"`, else `"viewport"`); its header then stays under the top bar while the page scrolls (`usePageStickyHead`), never past the last row. An asset card grid has no inner scroller; a page's main grid of fixed-height rows (Operations, `OpsRowGrid`) passes `fitGrid` and its card list fits the window the same way (`data-scroll-fit` on `data-table-grid`; Work Items, UX round 1 on #11917, B3). `tests_e2e/data-table-single-scrollbar.spec.ts` asserts no page scroll at 1440x900 and 1280x720 while the rows scroll, and the header under the top bar on a long-form page.
- **Pagination** in the footer under the rows (or virtual scroll for a log-like list), page size kept per user. The total is said once, at the top (the toolbar chip, or the `ListControlBar` total with `paging="footer" showTotal`), never under the rows: a list of one page has no footer and no "N results" line (Sam, 11 Nov 2026, #11191; gate 6 of `data-table-gates.test.ts`).
- **States**: loading (skeleton rows at the row height), empty (nothing exists yet, with the create action), no results (filters or search hide every row, with "Clear filters"), error (with retry).
- **Rows**: one fixed height on one grid, columns aligned, the essentials in the row and the rest in a hover card, truncation with a tooltip, dates relative plus absolute in the user's locale, row actions in the last column in the same order everywhere, bulk selection with the shared `SelectionBox`.
- **The table clips and truncates, never the caller** (Sam, 15 Nov 2026: a long owner name was painted over the Due date of its row in Work items): under `layout="fixed"` every body cell clips on the x axis (`overflow-x-clip`, so a 2 px focus ring inside the cell padding still draws whole and nothing is cut vertically), every header label is cut with an ellipsis and the design-system tooltip (`TableSortHeader truncate`), and a plain string or number cell is drawn as the shared `TruncatedText` (ellipsis plus tooltip, no native title). A renderer that draws its own markup (a link with an icon, a chip) keeps it within `max-w-full`, its label as `TruncatedText` or `truncate`, so it ends with an ellipsis inside the column (`WorkItemAgent`). `tests_e2e/work-items-list-fit.spec.ts` asserts no cell content past its cell with a 70-character owner name.
- **State in the URL** (search, filters, sort, page, page size), so a link and the back button restore the view; the page size and the visible columns also per user.
- **Server or client**: server-side search, filter, sort and pagination whenever the list is unbounded (the API grows the parameters additively in the slice that migrates it); client-side only for a small bounded set, said per table below.

Reference: OpenCTI `opencti-platform/opencti-front/src/components/dataGrid/` (`DataTable.tsx` toolbar with `SearchInput` + `DataTableFilters` + header buttons, `DataTablePagination.tsx`, `components/DataTableHeaders.tsx` sort and column sizing, `components/DataTableEmptyState.tsx` and `DataTableSearchEmptyState.tsx` for the two empty states, `dataTableHooks.ts` for the state kept in local storage and the URL). XTM One keeps its own tokens and components (`components/ui/*`); OpenCTI is the model of behaviour, not code to copy.

## Standard v2 (Sam, 10 Nov 2026, issue #10916)

One toolbar anatomy on every records table, same order, same control height, one row:

| Side | Control, in this order | Prop of `DataTable` (named filter kind) |
|---|---|---|
| left | Search | `searchFields` (table-held) or `search` (page-held, its own URL key): `search` |
| left | Scope switcher, each option with its count (Mine / Company / Shared / All), where the list has scopes | `scope`: `scope` |
| left | Filter pills: typed column filters, then the page's typed pills (`PropertyFilter`, a category pill); never a plain select | `filters`, `filterPills`: `filters` |
| left | Tags, a pill of the same kind | `tags`: `tags` |
| left | Active-filter count with "Clear all filters" | drawn by the table (`externalFilterCount`, `onClearAll` for the page-held kinds); the scope switcher is never counted as a filter: "Clear all" shows only when a search, tag or pill narrows the list, and clearing keeps the scope shown (`clearAll(alsoClear, { keepScope: true })`), so it never lowers the total |
| right | Total, always: "N", or "n of N" when a search or a filter narrows it | drawn by the table; `totalAll` = every record before the page-held kinds; in server mode `server.total` is the narrowed count, so a page with a search, a filter or a page-held kind always passes `totalAll` from an unfiltered read (a `limit: 1` query without them, or a summary count), else the chip never reads "n of N" (#12039; `form-action-buttons.test.ts` pins each list); never "0" before a page answered: a placeholder while `isLoading`, no chip on `isError` (the error row says why) |
| right | Columns chooser | every column but the first `hideable` |
| right | Page size (10, 25, 50, 100), opening at 25 when the caller names no `defaultPageSize` and the user kept none, kept per user with the visible columns | `paginate` (default), `storageKey`; a server-paged page keeps both `page` and `pageSize` of `onStateChange` and asks the API with `limit = pageSize`, `offset = page * pageSize`; a server-paged page asks the API with the kept size: its URL state default is `storedPageSize(storageKey, PAGE_SIZE)` (`lib/data-table-state.ts`), the same key the table is given, so the fetch, the picker, the footer and the rows agree after a reload |
| right | Columns on every `<DataTable>` of a file | gate 7 counts the column sets (`DataTableColumn<Row>[]` with a `header:`) per file: every set has a `hideable` column |
| right | Export, where the screen exports | `exportName` |
| right | Grid / list toggle, where a grid view exists | `view`, `renderGrid` |

The page's primary action stays in the page header. The footer keeps the pager alone, past one page (`Pagination`): no "Showing X-Y of Z" range and no "N results" line under the rows (Sam, 11 Nov 2026, #11191). Every list says its total once, at the top, with the shared `TotalChip` (`components/ui/total-chip.tsx`, 32 px, "N" or "n of N" when a search narrows it): the data table's toolbar, a `ListControlBar` with `showTotal`, and the toolbar row of every hand-made list under a `Pagination` (audit logs, activity tabs, chats, API keys, versioning, files, objects, evaluation runs, the dashboard table widget). A list whose header already counts it (KB documents, dataset entries, group members, the public catalog headers) keeps that count. Gate 6 of `data-table-gates.test.ts` fails on a pager without a top total and on a local total chip (UX r1 B1 to B3 on #11192).

**Two row heights only**, on one grid, from `ROW_HEIGHT` in `components/ui/data-table.tsx`: `rowLines={1}` (one line, `h-10`) or `rowLines={2}` (a primary line and one secondary line, `h-14`). Never a third line: a badge goes inline on line 2 (`EntityNameCell badgesPlacement="below"`) or to its own column; line 2 never cuts a badge: the badges that do not fit (64 px kept for the description) fold into a focusable "+N" badge whose tooltip lists their labels as plain text (`text-default-primary` on `bg-tooltip`, 4.5:1 in both themes, one wrapping line per label, never a nested badge or `Tip` trigger: the design-system Tooltip holds no interactive content; read from the badges rendered in a `display: none` source and read again by a `MutationObserver` whenever a hidden badge changes its own text after mount, so the tooltip shows the same labels as the badges on a cold load too; the unit test and `integrations-list-name.spec.ts` compare the two), and the e2e `agents-list-fit.spec.ts` checks every line 2 has `scrollWidth <= clientWidth`; line 2 itself never clips (no `overflow-hidden`: the fold and the description's `truncate` keep it within its width), so the marker's 2 px focus ring draws whole, which `integrations-list-name.spec.ts` checks against every clipping ancestor; a description is one line cut with its tooltip. All rows of one table share one height. Agents and Integrations use the two-line row. The row's cells carry no vertical padding (`rowCellPadding`, `py-0`) and line 2 of `EntityNameCell` has one fixed height (`h-6`, a badge's height) with or without a badge, so a row renders at the token whatever it holds; `tests_e2e/agents-list-fit.spec.ts` measures the rendered heights (one value, 56 px).

**The row is the target, never a cell** (Sam, 15 Nov 2026, issue #12070): every records table looks the same; tables vary only by their columns and by one or two lines per row. A row that opens something opens as a whole: `onRowClick` (focusable, Enter opens it; `rowOpens(row)` says which rows open, the others are neither a pointer target nor a tab stop) or `rowHref`. No cell draws a `<Link>`, an `<a>`, an underline or a link colour, and no cell clamps past line 2 (gate 10). A second destination of a row (its agent, its knowledge base) goes in the row menu (`rowActions`), not as a link in a cell. A routed row is ONE tab stop: `DataTable` provides `RowTargetContext` (`components/ui/row-target-context.ts`) to the cells of a row with `rowHref` or an opening `onRowClick`, and a cell that is focusable elsewhere drops its own tab stop there: every shared tooltip trigger takes its `tabIndex` from `useTriggerTabIndex()` (`DateTimeCell`, `IconMark`, `BadgeFocusTrigger`, `InfoTip`, `InfoLabel`, `HeldBadge`, the `TagList` +N marker, and `CellTipText` for a free text cut to one line). A cell of a routed table writes no `tabIndex={0}` of its own, and no `<button>` whose `onClick` calls the row's own `onRowClick` handler (gate 10; a cell button that IS the row's disclosure declares `toggleInCell`). A cell hint uses the shared `Tip`, never a native `title`. A detail page a table opens reads `location.state.backTo` / `backLabel` before its default way back (Administration > Objects sends "Back to Objects").

**Height to content**: the table card hugs its rows up to the page size or the room the window has left (One scrollbar above), no fixed container height. Only the page's main records list (its last block, as on /users) fits the window: a table in a dashboard widget, with a card beside it or more of the page under it, gets no bound, hugs its rows up to the page size and lets the page scroll (`isLastBlock` in `hooks/useFitToViewport.ts`, #11470). Every card of a dashboard row is as tall as the row's tallest (`WidgetRow`, `components/ui/widget-row.tsx`, the card itself a direct child); a table in such a card takes `fill`, so its pager sits at the card's bottom (#11470); the header is sticky in the table's scroll area. The grid view (`renderGrid`) keeps the same toolbar, total and pager over cards that hug their content.

### Exemptions

A table that can never exceed one page may draw no toolbar, no Columns and no page size. Each exemption is listed here and in `EXEMPT` of `data-table-gates.test.ts`, with its reason, and accepted by the UX Director. None by default.

| File | Reason |
|---|---|
| `pages/integration-detail/openrouter/OpenRouterModelsTab.tsx` (the endpoints table) | The providers serving ONE model, a handful of rows; the page's own model table carries the toolbar |

## FDS transition

The Filigran design system has no DataTable yet (XTM-Foundation/filigran-design-system ROADMAP: DataTable priority 1, `todo`). Its named reference is the `@filigran/ui` DataTable (FiligranHQ/filigran-ui `packages/filigran-ui/src/components/clients/data-table.tsx`), built on TanStack Table. `components/ui/data-table.tsx` is the single adapter: every call site speaks its props, so the switch to the FDS DataTable is a renderer swap inside that one file, call sites untouched. No second table component, no fork (gate 2).

| XTM One (`DataTableColumn`, `DataTableProps`, `lib/data-table-state.ts`) | TanStack `ColumnDef` / `TableState` |
|---|---|
| `column.key` | `ColumnDef.id` |
| `column.header` (English, translated by the table) | `ColumnDef.header` |
| `column.cell(row)` | `ColumnDef.cell: ({ row }) => cell(row.original)` |
| `column.sortValue(row)` | `ColumnDef.accessorFn` + `enableSorting: true`; `firstSort` maps to `sortDescFirst` |
| `column.hideable`, `defaultHidden` | `ColumnDef.enableHiding`; initial `TableState.columnVisibility[key] = false` |
| `column.width` | `ColumnDef.size` |
| `column.csv`, `exportOnly` | `ColumnDef.meta.csv`, `meta.exportOnly` (export stays the adapter's) |
| `column.align`, `hideBelow`, `interactive` | `ColumnDef.meta` (renderer concerns) |
| `DataTableState.sort` (`{ key, direction }`) | `TableState.sorting = [{ id: key, desc: direction === "desc" }]` |
| `DataTableState.page`, `pageSize` | `TableState.pagination = { pageIndex: page, pageSize }` |
| `DataTableState.filters` (`Record<key, string[]>`) | `TableState.columnFilters = [{ id: key, value: values }]` (OR inside, AND across: `filterFn`) |
| `DataTableState.search` | `TableState.globalFilter`; `searchFields` maps to `globalFilterFn` |
| hidden / shown columns kept per user (`storageKey`) | `TableState.columnVisibility` |
| `selection.selected` (row keys) | `TableState.rowSelection = { [rowKey]: true }`; `rowKey` maps to `getRowId` |
| `server={{ total }}` | `manualPagination`, `manualSorting`, `manualFiltering`, `rowCount: total` |
| URL keys (`urlKey`: `q`, `sort`, `page`, `size`, `f_<key>`) | outside TanStack: the adapter keeps `readTableState` / `writeTableState` |
| toolbar kinds (`search`, `scope`, `filters`, `filterPills`, `tags`, `view`), total, Columns, page size, Export | the FDS toolbar slot, total and pager; until it ships, drawn by the adapter |
| `rowLines` (`ROW_HEIGHT`) | the FDS row density token (asked in LIBRARY-FEEDBACK F44) |

Each of these requirements is recorded in `fds-migration/LIBRARY-FEEDBACK.md` (F44) so the design system DataTable ships with them.

## Components today

| Component | Has | Lacks | Callers | Target |
|---|---|---|---|---|
| `components/ui/data-table.tsx` `DataTable` | since slice 1: search, a filter pill per filterable column (`filters`, OR inside a filter, AND across) with its count and "Clear all filters", sort with `aria-sort`, page size, pagination in the footer (one pager per list, gate 6), CSV export, sticky header, loading, error with Retry (`onRetry`), empty and no-results told apart, state in the URL (`urlKey`: `<prefix>q`, `sort`, `page`, `size`, `f_<filter>`), page size and hidden columns per user (`storageKey`, `hideable` columns), selection with the shared `SelectionBox` and bulk actions (`selection`), row actions (`rowActions`, a fixed `rowActionsWidth` column, 96 px by default, under `layout="fixed"`), row link (`rowHref`), a row that opens in place (`onRowClick` + `expandedRow`: focusable, Enter or Space, `aria-expanded`; `toggleInCell` when a cell already holds the open button), pinned rows, server mode (`server` + `onStateChange`) | | 2 (`analytics/BreakdownCard`, `analytics/ErrorsCard`) + `EntityTable` | The one table |
| `components/ui/entity-table.tsx` `EntityTable` | a preset of `DataTable` since slice 1 (comfortable density, in a card, no toolbar, no pager): sort, row link, pinned rows, interactive cells | search, filters and paging inside the table (the pages still hold their own above it) | 11 asset index list views | Removed when slices 4 and 7 move those pages onto `DataTable` with its toolbar |
| `components/ui/list-control-bar.tsx` `ListControlBar` | toolbar row and pager of a list (search slot, filters slot, page controls) | sort, column visibility, states, a table under it | 3 (`CustomDashboards`, `fleets/FleetMembersView`, `flows/FleetsTab`; no Operations tab since slice 8e) | Becomes the toolbar and pager of `DataTable` (slice 1), not a second toolbar next to it; removed once its last caller migrates (slice 8), gate 4 |
| `components/ui/operations-list.tsx` `OpsRowGrid` + `OPS_ROW_HEIGHT` | fixed row height, every slot vertically centred on the card (`OPS_ROW_ALIGN`, 12 Nov 2026), hover card, row actions (contract of 4 Nov 2026, `operations-row-actions.test.ts`) | header, sort, shared toolbar | 6 Operations lists | Row renderer kept INSIDE the data table (slice 8): the row contract stays, the toolbar and paging become the table's |
| `components/ui/table-sort-header.tsx`, `pagination.tsx`, `search-input.tsx`, `date-range-filter.tsx` | building blocks | | many | Used only through the data table after the sweep |
| `components/ui/filter-pill.tsx` `FilterClearButton` | the one clear control of a filter toolbar: 28 px high, 12 px compact text, focus ring (slice 4g) | | `DataTable` ("Clear all filters"), `PropertyFilter` and `TagFilter` ("Clear", the Agents list and the asset index pages) | `filter-pill-class.test.ts` fails on a hand-made clear button or an 11 px clear |

## Inventory

Columns: **S** search, **F** filters, **O** sort, **P** pagination, **E** empty state, **L** loading, **R** error, **B** bulk selection, **A** row actions, **U** URL state. `y` present, `-` absent, `~` partial (search held by the page outside the table, a fixed page, a filter row of the page). Read from source on main (c6a3942a) by a scan of each file, not on a live instance: each slice re-reads its tables and corrects the row before it migrates them. Volume is an order of magnitude of rows in a working tenant (`10`, `100`, `1k`, `10k+`). Mode is the target: `server` or `client`.

### Asset index list views (`EntityTable`): slice 1 then 4 and 7

| Route | Component | Volume | S | F | O | P | E | L | R | B | A | U | Mode | Slice |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `/agents` (list view) | `pages/Agents.tsx` | 100 | ~ | ~ | y | - | y | y | - | - | y | - | server | 4 |
| `/flows/library` (list view) | `pages/flows/LibraryTab.tsx` | 100 | y | y | y | y | y | y | y | - | y | y | client | T2 E (v2 toolbar, list and grid) |
| `/custom-tools` | `pages/CustomTools.tsx` | 100 | y | y | y | y | y | y | y | - | y | y | client | T2 E (v2 toolbar on the built-in and custom tables, list and grid) |
| `/connections/variables` | `pages/Variables.tsx` | 100 | y | y | y | y | y | y | y | - | y | y | client | T2 E (v2 toolbar, list and grid) |
| `/prompts` | `pages/Prompts.tsx` | 100 | y | y | y | y | y | y | - | - | y | y | client | T2 E (v2 toolbar, list and grid) |
| `/skills` | `pages/Skills.tsx` | 100 | y | y | y | y | y | y | y | - | y | y | client | T2 E (v2 toolbar, list and grid) |
| `/mcp-servers` | `pages/MCPServers.tsx` | 10 | y | y | y | y | y | y | - | - | y | y | client | T2 E (v2 toolbar, list and grid) |
| `/integrations` | `pages/Integrations.tsx` | 100 | ~ | - | y | - | y | y | y | - | y | - | client | 7 |
| `/knowledge` | `pages/KnowledgeBases.tsx` | 100 | y | y | y | y | y | y | y | - | y | y | client | T2 E (v2 toolbar, list and grid) |
| `/investigation-packs` | `pages/InvestigationPacks.tsx` | 10 | y | y | y | y | y | y | - | - | y | y | client | T2 E (v2 toolbar, list and grid) |
| `/dashboards` | `pages/CustomDashboards.tsx` | 100 | y | y | y | y | y | y | y | y | y | y | client | T2 E (v2 toolbar, list and grid; bulk selection bar kept above the list) |

### Observability and traces: slice 3

| Route | Component | Volume | S | F | O | P | E | L | R | B | A | U | Mode | Slice |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `/observability/traces` | `pages/Traces.tsx` | 10k+ | y | y | y | y (cursor) | y | y | y | - | y | y | server, DataTable controlled by the page's URL keys (done 3b); dates, sources, groups, agents, models and failure causes in the table's one Filters pill (`filtersInPill`, #12070), so the list is the page's last block and fits the window at 1080 | 3 |
| `/observability/sessions` | `pages/TraceSessions.tsx` | 10k+ | y | y | y | y (cursor) | y | y | y | - | y | y | server, DataTable controlled by the page's URL keys (done 3b); dates, sources, groups, agents and models in the table's one Filters pill (`filtersInPill`, #12070) | 3 |
| `/intents` | `pages/Intents.tsx` (intents; verticals panel) | 100 | y | y (platform, usage; verticals: `noFilters` reason) | y | y | y | y | y | - | y (edit, add binding; verticals: rename, delete) | y (`in_`, `iv_`) | client: `GET /intents` returns the bounded list whole; a row opens its bindings in a dialog (done 3c) | 3c (done) |
| `/observability` breakdowns | `pages/analytics/BreakdownCard.tsx` | 100 | y | y (agents: status; users: groups; groups and models: `noFilters` reason) | y | y | y | y | y | - | - | y (`ba_`, `bu_`, `bg_`, `bm_`) | client | 3a (done); T2 v2 (#10977): Columns on every column but the name, page size and columns per user (`analytics-breakdown-<kind>`), agents and users on two-line rows (name, then owner or email) |
| `/observability` top errors | `pages/analytics/ErrorsCard.tsx` | 100 | y | y (agent, model) | y | y | y | y | y | - | - | y (`te_`) | client | 3a (done); T2 v2 (#10977): Columns, per-user page size and columns (`analytics-top-errors`) |
| `/agents/:id/observability` | `pages/agent-detail/ObservabilityTab.tsx` (3 tables) | 100 | y | y (recent traces: status, model; comments: rating, server) | y (traces, models; comments ordered newest first by the API) | y | y | y | y | - | - | y (`rt_`, `mb_`, `fc_`) | client (traces, models), server (comments) | 3a (done); T2 v2 (#10977): Columns on the three tables, per-user page size and columns (`agent-observability-*`) |

### Agents, assignments and memory: slice 4

| Route | Component | Volume | S | F | O | P | E | L | R | B | A | U | Mode | Slice |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `/agents/:id/memory` | `pages/agent-detail/MemoryTab.tsx` | 1k | y | y | y | y | y | y | y | - | y | y | server | 4f done; T2 v2 (#10977): Columns (When, Type, Source), per-user page size and columns (`agent-memory`) |
| `/agents/:id/feedback` | `pages/agent-detail/FeedbackTab.tsx` (2 tables) | 1k | y | y (ratings: rated work, rating; by kind: `noFilters` reason) | y (ratings: date, rated work, rating by the API's `sort` and `direction`) | y (offset) | y | y | y | - | - | y (`fb_`, `fk_`) | server (ratings: `GET /annotations` gains `sort`, `direction` and a repeated `target_type`), client (by kind, at most five rows) | 4a (done); T2 v2 (#10977): per-user page size and columns (`agent-feedback-*`); by kind: Positive, Negative and Positive share hideable (UX round 3, B2) |
| `/agents/:id/feedback` criteria | `pages/agent-detail/FeedbackCriteriaCard.tsx` | 10 | y | y (type, status) | y | y | y | - | - | - | - | y (`fcr_`) | client: the criteria come whole with the summary | 4a (done); T2 v2 (#10977): per-user page size and columns (`agent-feedback-*`) |
| `/profile/agent-accounts/:id` overview | `pages/profile/agent-accounts/AgentAccountOverview.tsx` (3 tables: rules, conversations, people) | 100 | y | y (`noFilters` reason: a name and three counts) | y | y | y | y | - | - (a conversation or person row opens the Decisions tab on it) | y (`aor_`, `aoc_`, `aop_`) | client: the activity endpoint returns the bounded top lists whole | 4b (done); T2 v2 (#10977): Columns on every column but the first, per-user page size and columns |
| `/profile/agent-accounts/:id/decisions` | `pages/profile/agent-accounts/AgentAccountDecisionsTab.tsx` | 1k | y | y (Outcome, Kind of conversation; conversation or person from the overview link) | y (date, conversation, sender) | y | y | y | y | - | - (a row opens in place on why: `expandedRow`) | y (`q`, `group`, `type`, `sort`, `dir`, `page`, `limit`) | server: `GET /agent-accounts/{id}/decisions` gains repeated `group` and `conversation_type`, `sort`, `direction` | 4c (done); 4e: CSV export, one-line rows (badges cut with a title), error row with Retry (`throwOnError: false`); T2 v2 (#10977): Columns on every column but the first, per-user page size and columns |
| `/profile/agent-accounts/:id/sent` | `pages/profile/agent-accounts/AgentAccountMessagesSentTab.tsx` | 1k | y | y (Kind, Status: every reply status, several at once) | y (date, conversation) | y | y | y | y | - | - (a row opens in place on the whole message, a draft sent or dropped there: `expandedRow`) | y (`q`, `kind`, `status`, `sort`, `dir`, `page`, `limit`; the former `show` still read) | server: `GET /agent-accounts/{id}/replies` gains repeated `status`, `sort`, `direction` | 4d (done); T2 v2 (#10977): Columns on every column but the first, per-user page size and columns |
| `/flows/fleets/:id` members | `components/fleets/FleetMembersView.tsx` | 10 | - | y | - | - | y | y | y | y | y | - | client | 4 |
| `/flows/fleets` | `pages/flows/FleetsTab.tsx` (card list) | 10 | ~ | - | - | - | y | y | - | - | y | - | client | 4 |

### Evaluations and datasets: slice 5

| Route | Component | Volume | S | F | O | P | E | L | R | B | A | U | Mode | Slice |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `/evaluations` | `pages/Evaluations.tsx` (records list) | 100 | to read | | | | | | | | | | server | 5 |
| `/evaluations/datasets` | `pages/Datasets.tsx` | 100 | y | - | - | - | y | y | y | - | y | - | server | 5 |
| `/evaluations/:id` (2 tables) | `pages/EvaluationDetail.tsx` | 1k | y | y | - | y | y | y | y | - | y | - | server | 5 |
| `/evaluations/runs/:id` | `pages/EvaluationRunDetail.tsx` | 1k | - | - | - | - | y | y | y | - | - | - | server | 5 |
| `/evaluations/:id/compare` | `pages/EvaluationRunCompare.tsx` | 1k | - | - | - | - | - | y | y | - | - | y | client | 5 |
| placebo comparisons | `components/evaluation/PlaceboComparisonsCard.tsx` | 10 | y | y | y | y | y | y | - | - | - | y | client | 5 (done on standard v2: DataTable, search agent/model/target, verdict and target filters, sort on every figure, CSV, URL `pc_`, Columns and page size kept per user, two-line rows: the row warnings and the skill share in their own columns) |
| placebo snapshot | `components/evaluation/PlaceboSnapshotCard.tsx` | 10 | - | - | - | - | - | - | - | - | - | - | content | 5 (content: the part lengths of one element, at most four rows; allow-listed in `CONTENT`) |

### Admin and adoption: slice 6

| Route | Component | Volume | S | F | O | P | E | L | R | B | A | U | Mode | Slice |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `/ai-adoption/gaps` (inactive users, agentic AI only, #11380) | `pages/ai-adoption/GapsTab.tsx` | 100 | y (name, email) | y (`noFilters` reason: a name and an email) | y (name, email; name A to Z first) | y | y | y | y | - | - | y (`giua_`) | client: the inactive users come whole with `GET /ai-adoption/gaps` | 6i done; T2 v2 (#10977): Columns on every column but the first and the actions, per-user page size and columns (slice C) |
| `/settings` feedback criteria | `pages/settings/FeedbackCriteriaSection.tsx` | 10 | y | y (type, status) | y | y | y | y | y | - | y | y (`sfc_`) | client: the definitions come whole, admin-defined | 9 (done) |
| `/xtm-one` knowledge health (stale embeddings debug list) | `pages/xtm-one/KBHealthWarnings.tsx` | 100 | y | y (`noFilters` reason: a name and two counts) | y (name, stale tuples, stale chunks; most stale chunks first) | y | y | y | y | - | - | y (`kbs_`) | client: the knowledge bases come whole with `GET /knowledge-bases` | 6h done; T2 v2 (#10977): Columns on the two counts |
| concierge signals | `components/concierge/HealthSignalsList.tsx` | 100 | y | y (signal, target, cycle) | y (signal, target, cycle, fired) | y | y | y | y | - | - | y (`cs_`) | client: the API returns the crossed signals whole, one per kind and target | 6a (done); T2 v2 (#10977): Columns on every column but the first, per-user page size and columns |

### Integrations: slice 7

| Route | Component | Volume | S | F | O | P | E | L | R | B | A | U | Mode | Slice |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `/integrations/:id/keys` (OpenRouter) | `pages/integration-detail/openrouter/OpenRouterKeysTab.tsx` | 100 | - | y | - | - | y | y | y | - | y | - | client | 7; T2 v2 (#10977): Columns on Used by, This month, Total, Limit, Created and Expires, per-user page size and columns (`openrouter-api-keys`, slice D) |
| `/integrations/:id/models` (OpenRouter) | `pages/integration-detail/openrouter/OpenRouterModelsTab.tsx` | 1k | y (name, id) | y (Model author, Capabilities, Offered) | y (every column) | y (25 per page) | y | y | y | - | y (offer switch, default star, opened row with the providers) | y (`orm_`) | client: the catalog comes whole | 7e done: DataTable on standard v2, two-line rows (name, then model id), Columns on Capabilities, Context, prices and Created, page size and columns per user (`openrouter-models`) |
| `/integrations/:id/usage` (OpenRouter) activity per model | `pages/integration-detail/openrouter/OpenRouterUsageTab.tsx` | 100 | y (model, providers) | y (Providers) | y (every column, most spent first) | y | y | y | y | - | - | y (`orm_`) | client: the 30-day activity comes whole | 7e done: DataTable on standard v2, Columns on Providers (hidden below 4xl), Requests, Input and Output tokens, page size and columns per user (`openrouter-activity-by-model`) |
| relay group activity | `pages/relay-group-detail/ActivityTab.tsx` | 1k | y | - | - | y (offset) | y | - | y | - | - | - | server | 7; T2 v2 (#10977): the relay select is a Relay filter pill of the table (several relays, `act_f_relay`; `GET /relays/groups/{id}/requests` takes a repeated `relay_id`), per-user page size and columns (`relay-group-activity`, slice D) |

### Operations and Scheduling lists: slice 8 (fixed-height row contract of 4 Nov 2026 kept)

| Route | Component | Volume | S | F | O | P | E | L | R | B | A | U | Mode | Slice |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `/operations/executions` | `pages/operations/ExecutionRow.tsx` (rows) | 10k+ | y | y | - | y | y | y | y | - | y | y | server | 8 |
| `/operations/background-tasks` | `pages/BackgroundTasks.tsx` | 10k+ | y (description, agent names) | y (Status pill ON the toolbar: Pending, Running, Completed, Incomplete, Failed, Cancelled, multi-select, one click to Running, each with its count from `GET /chat/sub-agent-tasks/status-counts`; execution and fleet in the Filters pill) | y (created, updated = the execution's last progress, status, delegated by, delegated to, both ways: `GET /chat/sub-agent-tasks?sort=`) | y (offset) | y | y | y (rate, kill, delete) | y (page box, select all matching, bulk delete) | y | y (`q`, `page`, `sort`, `dir`, `size`, `status`, `liveness`, `fleet` keep their names; an old `?sort=last_progress` reads as Updated) | server (DataTable, slice 8e: `/chat/sub-agent-tasks/count` total) | 8e done: DataTable with a grid / list toggle, the grid keeps each row on the Operations row contract (`OpsRowGrid`, fixed height), the list draws Task, Status, Delegated by, Delegated to, Created and Updated with Columns per user; page size, hidden columns and view per user (`operations-background-tasks`); the Status pill is the shared `pages/operations/OperationsStatusFilter.tsx` that Executions (8f) draws too; the in-flight counts of the header are the pill's numbers |
| `/tasks/work-items` | `pages/operations/WorkItemsTab.tsx` | 10k+ | y (title, key) | y (Status pill ON the toolbar: Open, Claimed, In progress, Blocked, Done, Cancelled, multi-select, each with its count from `GET /work-items/status-counts`, open work by default; view, scope, fleet and owner in the Filters pill) | y (created, updated, due, priority, status, owner: `GET /work-items?order=&direction=`) | y (offset) | y | y | y (claim, requeue, close, ask the agent, delete; scheduled cancel and edit) | y (page box, select all matching, bulk delete) | y | y (`wi_`; `status`, `view`, `agent`, `fleet` keep their names) | server (DataTable, slice 8d: `/work-items/count` total, stale banner kept) | 8d done: DataTable with a grid / list toggle, the grid keeps each row on the Operations row contract (`OpsRowGrid`, fixed height), the list draws the columns with Columns per user (`operations-work-items`); `?status=a,b` (none = open work, `all` = every status); a legacy `?q=` and `?status=<one>` land on the same view; Done / Cancelled alone keep the completion order unless a column sort is chosen |
| `/operations/shared-state` | `pages/operations/SharedStateTab.tsx` | 1k | y (key, label) | y (Status on the toolbar: Active, Stale, Resolved, All, each with its count; Owner and Fleet in the Filters pill) | y (key, owner, updated, size: `GET /shared-state?sort=&direction=`) | y | y | y | y (resolve, delete; opened row with the value and the notes) | y (page box, select all matching, bulk delete) | y | y (`sst_`) | server (DataTable, slice 8c: `/shared-state/count` total, stale banner kept) | 8c done: DataTable with a grid / list toggle, the grid keeps each row on the Operations row contract (`OpsRowGrid`, fixed height), the list draws the columns with Columns per user; the Status filter is a radio group ON the toolbar row (`sst_status`, Active by default, Stale implies Active), never in the Filters pill; the former `status` / `stale` keys still land on the same view |
| `/operations/worksets` | `pages/operations/WorksetsTab.tsx` | 100 | y | y | y | y | y | y | y | - | y | y | server (DataTable, slice 8b: search, Status, Scope, sort name/items/size/updated, `/worksets/count` total, `wks_` URL keys) | 8b |
| `/tasks/workspaces` | `pages/operations/WorkspacesTab.tsx` | 100 | y (name, slug, description, owner) | y (Owner, Status) | y (toolbar sort: Name, Owner, Slug, Updated) | y | y | y | y | - | y | y (`ws_`) | client: the list comes whole | 8a done: DataTable with a grid / list toggle: the grid keeps each row on the Operations row contract (`OpsRowGrid`, fixed height), the list draws the columns with Columns per user; archived workspaces load through the Status filter (`Archived`), the one archived control; New workspace in the Operations page header; your default pinned first under every sort; CSV export; page size, hidden columns and view per user (`operations-workspaces`). The data table gains a toolbar sort for card lists (`GridSort`, `<Icon>` arrows as `TableSortHeader`) |

### Settings lists: slice 9 (with dev:form-standard-sweep)

| Route | Component | Volume | Mode | Slice |
|---|---|---|---|---|
| `/settings/organization` tracked organizations | `pages/settings/TrackedOrganizations.tsx` | 10 | client | done (`DataTable` since #9443: search, relationship, criticality and OpenCTI pills, every column sorted, count); T2 v2 (#10977): Columns on every column but the first and the actions, per-user page size and columns (slice C) |
| `/settings/organization` departments | `pages/settings/DepartmentsSection.tsx` | 10 | client | done (`DataTable` row editor: read mode has search (name and directory values), every column sorted, a People column from the preview, CSV, pager, URL state `odp_`; edit mode keeps every row on one page, unsorted, state in the component; no filter: one dimension. The group rules stay an ordered list: their order is the data, never sorted); T2 v2 (#10977): Columns on every column but the first and the actions, per-user page size and columns (slice C) |
| `/settings/organization` crown jewels, tech stack, AI targets | `pages/settings/OrganizationSection.tsx` | 10 | client | done (three `DataTable` row editors: read mode has search, filters (criticality and OpenCRQ link; vendor; kind and exposure), every column sorted, CSV, pager, URL state `ocj_` / `ots_` / `oai_`; edit mode keeps every row on one page, unsorted, state in the component); T2 v2 (#10977): Columns on every column but the first and the actions, per-user page size and columns (slice C) |
| `/settings` API keys | `pages/settings/ApiKeysAdminSection.tsx` | 100 | server | 9 |
| `/settings` tool approvals | `pages/settings/ToolApprovalsSection.tsx` | 100 | client | 9 |
| `/settings` secret vaults | `pages/settings/SecretVaultsSection.tsx` | 10 | client | 9 |
| `/settings` model directory | `pages/settings/ModelDirectory.tsx` | 1k | client | 9 |
| `/settings` security audit | `pages/settings/SecurityAuditSection.tsx` | 1k | server | 9 |
| `/settings` channels | `pages/settings/ChannelManagement.tsx` | 10 | client | 9 |
| `/settings` fleets | `pages/settings/FleetsSettingsSection.tsx` | 10 | client | 9 |

Capabilities of the settings lists are read in slice 9; most are card or row lists with no toolbar.

### Records lists with a page-local pager or search (added after UX round 1)

These lists hold their own `<Pagination>`, `<ListControlBar>` or `SearchInput` outside any table component. **S** and **P** and **U** are read from source on the branch head (5c1ecf5c); the other capabilities are read by the owning slice before it migrates the row. Admin and audit lists and `TaskHistory` are unbounded or large: server mode.

| Route | Component | Volume | S | P | U | Mode | Slice |
|---|---|---|---|---|---|---|---|
| `/admin/users` | `pages/Users.tsx` | 1k | y | y | y (`u_`) | server | 6z done: server mode, `GET /users` takes `search`, `role`, `status`, `group` (`none`: in no group), `created`, `sort`, `direction`, `limit`, `offset` (none: the whole list, as the pickers read it) and `GET /users/count` the total; Role, Status, Created and Groups pills, every column sorted on the server, per-user page size and columns (`users`) |
| `/admin/groups` | `pages/Groups.tsx` | 100 | y | y | y (`g_`) | server | 6z done: server mode, `GET /groups` takes `search`, `kind`, `members` (`some` / `empty`), `sort`, `direction`, `limit`, `offset` and `GET /groups/count` the total; Kind and Members pills, a Kind column, per-user page size and columns (`groups`) |
| `/admin/groups/:id` members | `pages/GroupDetail.tsx` | 1k | y | y | y (`gm_`) | server | 6z done: the hand list and its pager replaced by the shared table; `GET /groups/{id}/members` takes several roles, `manager` (`yes` / `no`), `sort` and `direction`; Role and Manager pills, Name, Email, Role (manager badge) and Added columns, the row opens the user, the remove action kept, Add Member on the toolbar, per-user page size and columns (`group-members`) |
| `/admin/users/:id` audit | `pages/UserDetail.tsx` (`components/audit-logs/AuditLogList.tsx`) | 10k+ | y | y | y (`audit_`) | server | 6y done: the shared audit log table with the user fixed (no User column or pill), per-user page size and columns (`user-audit-logs`) |
| `/admin/audit-logs` (`/logs`) | `pages/AuditLogs.tsx` (`components/audit-logs/AuditLogList.tsx`) | 10k+ | y | y | y | server | 6y done: DataTable standard v2: search, Action, Entity and User pills ON the toolbar (several values each; `GET /audit-logs` takes comma-separated `action`, `entity_type`, `user_id`) and a Time range pill (preset or From / To), server sort on time, action, entity and user (`sort`, `direction`), TotalChip, one pager, Columns (IP address hidden by default), row opens the detail dialog with Copy; old URL keys `q`, `user`, `action`, `entity`, `dp`, `df`, `dt`, `page` kept, plus `sort`, `dir`, `size`; per-user page size and columns (`admin-audit-logs`) |
| `/admin/objects` | `pages/AdminObjects.tsx` | 1k | y | y | y | server | 6x done: `DataTable` server mode: search, Type / Owner / Status / Scope pills (multi-select), server sort on name, type, owner, status, created, updated (`GET /admin/objects` `sort`, `direction`, `status`, `is_company_managed`; `object_type` and `user_id` take several values), TotalChip, URL keys `q`, `f_type`, `f_owner`, `f_status`, `f_scope`, `sort`, `page`, `size` (old `?type=` / `?user=` links read once), per-user page size and columns (`admin-objects`), one pager |
| concierge improvements | `components/concierge/ImprovementsInbox.tsx` | 100 | y | y | y (`imp_`) | server | 6l done (search in summaries, Type and Status pills, sort by date, type or status, CSV, row opens in place; `GET /concierge/improvements` takes repeated `proposal_type` and `status`, `q`, `sort`, `direction`) |
| `/files` | `pages/Files.tsx` | 1k | y | y | y | server | 4 |
| `/agents/:id/chats` | `pages/agent-detail/ChatsTab.tsx` | 1k | y | y (above and below) | - | server | 4 |
| `/prompts/:id/versions` | `pages/prompt-detail/VersioningTab.tsx` | 100 | - | y | - | server | 4 |
| `/skills/:id/versions` | `pages/skill-detail/VersioningTab.tsx` | 100 | - | y | - | server | 4 |
| `/knowledge/:id/documents` | `pages/knowledge-base-detail/DocumentsTab.tsx` | 1k | - | y (above and below) | - | server | 4 |
| `/knowledge/:id/activity` | `pages/knowledge-base-detail/ActivityTab.tsx` | 1k | y | y (DataTable, Event type pill: sync, documents, embeddings, several at once) | y (When sortable, Event, Details, Source, Documents; Source hidden below 3xl) | server (`act_` URL keys; `GET /knowledge-bases/{id}/activity` and `/activity/count` take repeated `event_type`, the list takes `direction`; per-user size and columns `kb-activity`) | done (4a) |
| runs Activity tabs (shared: agent, assignment, integration incl. A2A, MCP server, custom and built-in tool, skill, prompt) | `components/ui/run-activity-list.tsx` (`entity-activity-tab.tsx`, `agent-detail/ActivityTab.tsx`, `assignment-detail/ActivityTab.tsx`) | 1k | y | y (DataTable, Status pill visible on the toolbar with counts, ROI review pill on agent and assignment) | y (Run, Status, Type, Agent, Started, Duration; Agent hidden on an agent's tab) | server (`q`, `page`, `size`, `sort`, `dir`, `status`, `review`; old `?sort=oldest`/`status` kept) | done (4a) |
| `/flows/map` list | `pages/flows/MapTab.tsx` | 100 | - | y | y | client | 4 |
| `/public/agents` | `pages/public/PublicAgents.tsx` | 100 | - | y | - | server | 4 |
| `/public/custom-tools` | `pages/public/PublicCustomTools.tsx` | 100 | - | y | - | server | 4 |
| `/public/mcp-servers` | `pages/public/PublicMCPServers.tsx` | 100 | - | y | - | server | 4 |
| `/public/prompts` | `pages/public/PublicPrompts.tsx` | 100 | - | y | - | server | 4 |
| `/public/skills` | `pages/public/PublicSkills.tsx` | 100 | - | y | - | server | 4 |
| `/evaluations/datasets/:id` entries | `pages/DatasetDetail.tsx` | 1k | - | y (client pages) | - | server | 5 |
| `/connections/relays` | `pages/Relays.tsx` | 10 | y | - | - | client | 7 |
| `/templates` | `pages/Templates.tsx` | 100 | y | - | - | client | 7 |
| `/connections` | `pages/BrowserProfiles.tsx` | 10 | y | - | y | client | 7 |
| `/operations/task-history` | `pages/TaskHistory.tsx`: migrated in slice 8f on `DataTable` server mode, controlled state in the URL (`q`, `page`, `sort`, `dir`, `size`; the old `filter=<status>` still reads as one status), storage key `operations-executions` (25 rows); columns Run, Status, Type, Agent, Started, Duration, the last four sorted by the server (`sort` newest, oldest, status, status_desc, agent, agent_desc, longest, shortest on both feeds); the multi-select Status pill with counts is the `toolbar` slot, run type, view, ROI review and Fleet + Agent (with the bulk cancel) the Filters pill; the grid view keeps the Operations run cards; selection skips flow runs (`disabledReason`) | 10k+ | y | y | y | server | 8 (done) |

Routes are the screen the component renders on; the owning slice confirms the exact path.

### Records lists still to read (each with its owning slice)

| Records list | Owning slice |
|---|---|
| `/knowledge/documents` (`pages/KnowledgeDocuments.tsx`) | 4: done, server search (file or KB name), status filter, sort (document, KB, status, created) on `GET /knowledge-bases/global/documents`, `doc_` URL keys, `kb_ids` scope chip |
| `/dashboards/packs` (`pages/WidgetPacks.tsx`) | 4: done, client mode (the API serves at most 100 packs), search (name, description), Mine / Company / Shared / All scope with counts, Origin and Platform pills (Built-in is an origin), sort (pack, widgets, origin, import changes, updated), Columns, CSV, `wp_` URL keys, two-line rows, card grid kept behind the grid / list toggle |
| `/ctem/:stage` (`pages/xtm-one/CtemStageDetail.tsx`) | 6: read, no records list: the stage page draws the assessment, a metrics grid (`mergedFacts`), the stage chart and the trend chart, all content, no `<table>` |
| `pages/xtm-one/ctem/FleetActivityCard.tsx` (fleet tiles), `FleetTimelineCard.tsx` (the 30 latest fleet runs, `TIMELINE_LIMIT`) | 8: read in slice 6, moved: both are widgets of the command center (`CtemCommandCenter.tsx`), not of `/ctem/:stage`; each links to its full records list (`/fleets`, `/operations/executions`), so the command center slice decides their form with the rest of its widgets |
| `/presentations` (`pages/Presentations.tsx`) | 7: done (#11075), server search, visibility scope, Kind filter, sort (name, size, updated, created) on `GET /presentations`, card grid behind the grid / list toggle |
| `/videos` Videos tab (`pages/VideoStudio.tsx`) | 7: done, server mode: search (title, template, owner), Mine / Shared / Company / All scope with counts, Render filter pill (rendered, not rendered yet) on `GET /video-studio/projects?render=`, sort (name, scenes, updated, created) on `GET /video-studio/projects?sort=&direction=`, page size 10 to 100, Columns, CSV export (`videos`), `q` / `render` / `sort` / `dir` / `page` / `limit` URL keys, inline error with Retry, two-line rows, card grid kept behind the grid / list toggle; parity: Play, Open or View, Share, Copy link, downloads, Duplicate, Delete on the row menu as on the card |
| `/videos/media` Media tab (`pages/VideoStudio.tsx`) | 7c: done, server mode on `GET /video-studio/assets`: search (name, prompt), Kind filter (recordings, AI shots; `kind` URL key kept), the use and time range filters in the Filters pill (`filterPills`, counted by `externalFilterCount`), sort (name, created) on `?sort=created_at|title&direction=`, page size 10 to 100 (default 50, was a fixed 48), Columns, CSV export (`media`), card grid (default) behind the grid / list toggle, selection with select all matching and bulk delete through `selection` (`count` reads every match), Select all unused kept as a toolbar action, keep switch on card and row |
| `/desktop` runners (`pages/Desktop.tsx`, `RunnerFleet` in `pages/desktop/RunnersSection.tsx`) | 7d: done, client mode (`GET /runners` returns every machine the caller sees, a bounded set): search (name, hostname, tags, labels), Status, Platform and Permission mode filters, sort (name, status, platform, hostname, permission mode, commands, last seen), page size, Columns, CSV (`machines`), `dr_` URL keys, the machine you sit at pinned first; the expandable machine cards (permissions, approvals, jobs) stay the default view behind the grid / list toggle, the row opens `/desktop/runners/:id` |
| the command center (item 56e0e378) | 8 |
| the left menu hub pages (item cca585ad) | 9 |

The owning slice reads the capabilities and adds the full row before it migrates the list.

## Excluded on purpose: tables drawn as content

These render a table that IS the content (a document, a slide, a chart), not a list of records. They stay raw `<table>` and form the allow-list of the build gate (slice 2).

| File | Why |
|---|---|
| `components/chat/ChatMarkdown.tsx` | Markdown tables in a chat answer |
| `components/presentation/blocks/table.tsx`, `components/presentation/SchemaFieldControls.tsx` | Slide table block and its editor grid, laid out as a slide |
| `components/report/blocks.tsx` | Report table and matrix blocks, printed as a document |
| `components/custom-dashboard/charts/DataTable.tsx` | A dashboard widget of type table, sized by the widget |
| `pages/xtm-one/ctem/CoverageMatrixCard.tsx` | A coverage matrix (stages x controls), a chart drawn as cells |
| `pages/agent-detail/MemoryTab.tsx` line 122 | Markdown table inside a memory's rendered content (the tab's records list is migrated) |
| `pages/EvaluationRunDetail.tsx` line 1520 | Inline per-case score grid inside one case, to confirm in slice 5 |

## Gates (slice 2)

Tests under `frontend/src` that fail the build:

1. No `<table` in a `.tsx` file outside `components/ui/data-table.tsx` and the allow-list above.
2. No import of `components/ui/entity-table` (once slice 7 lands) and no second component exporting a table of records.
3. Every `<DataTable` caller passes search, at least one sortable column, at least one typed filter (or an explicit `noFilters` reason, checked non-empty by the test) and pagination (or declares `virtual`), checked on the source.
4. No `<Pagination` or `<ListControlBar` in a `.tsx` file outside `components/ui/data-table.tsx` and the allow-list, so a hand-made records list (a `<ul>` or div rows with a pager) cannot pass gate 1.
6. One pager per list, under the rows (a list of one page shows only "N results" through the shared `Pagination`, never a disabled "Page 1 of 1", `DataTable` included): a file draws at most one `<Pagination` per list it holds (a file with two lists names them in the gate), and `DataTable` draws its pager in the footer only (#10215 criterion 6: no pager row above the list). A `<ListControlBar>` counts as a pager unless it passes `paging="footer"` (the bar keeps the selection, its bulk actions and `extra`; the `<Pagination>` under the rows carries the count and pager, a single page included) or `pager={false}`.
7. Standard v2 (#10916): every records table on the shared toolbar (no `<SearchInput>` of its own beside the table, no `showToolbar={false}` or `paginate={false}` outside the exemptions), Columns on every `DataTable` caller (a `hideable` column), the total once at the toolbar's right end, `ROW_HEIGHT` holding the two heights only and no caller row or cell class setting a height, a vertical padding or a wrap past line 2, no plain select (`Select`, `SearchableSelect`, `<select>`) in `toolbar` or `filterPills`. Pending lists: `PENDING_OWN_TOOLBAR`, `PENDING_COLUMNS`, `PENDING_TOOLBAR_SELECTS`, each only shrinking; exemptions: `EXEMPT`.
8. A column steps aside only through `hideBelow` (a container width: `xl`, `3xl`, `4xl`, `6xl`, `7xl`, `8xl`), never through a window breakpoint class (`hidden xl:table-cell`, `hidden min-[1440px]:table-cell`) on its `className` or `headerClassName`: the Columns picker reads `hideBelow` and brings the column back, while a page class hides it with the picker still showing it checked (UX Director on #11291, `Agents.tsx`).
9. A selectable card grid (`renderGrid` with `selection`) gets its page box from `DataTable`, drawn as the first control of the toolbar row at `TOOLBAR_CONTROL_HEIGHT` (its slot held during the first read); no page draws its own "select every row on this page" box or a `*-page-select` row above the cards (Sam on `/operations/shared-state`, 14 Nov 2026).
10. No `<Link>`, `<a>`, underline or `line-clamp-3` and above in the column cells of a `DataTable` caller (the `DataTableColumn<...>[]` arrays): the row opens through `onRowClick` / `rowOpens` or `rowHref` (#12070). Pending list `PENDING_CELL_LINKS`: empty since slice 3, and it stays empty. A cut text in a cell is a `TruncatedText` (the design-system tooltip opens on hover and on row focus), never a `truncate` element named by a native `title=`; the files that still do are in `PENDING_TITLED_CUTS`, which only shrinks.

The gates land with an allow-list of the files not yet migrated, and each migration slice removes its files from that list, so the build is green at every slice and the list is empty at the close.

Since slice 2 they live in `frontend/src/components/ui/data-table-gates.test.ts` (`yarn test`): `CONTENT` (the table above, for good), `PENDING_TABLES` (gate 1), `PENDING_ENTITY_TABLE` and `TABLE_COMPONENTS` (gate 2), `PENDING_CALLERS` (gate 3: the rules each caller still fails), `PENDING_PAGERS` (gate 4), each pending entry commented with its slice. Every list only shrinks: a file that no longer trips its gate fails the "keeps ... to the files that still ..." test until it leaves the list. A short list with a single dimension passes gate 3 with `noFilters="<why>"` (a non-empty string literal), never with an empty filter list.

## Slices

| # | Slice | Content | UI |
|---|---|---|---|
| 0 | Inventory | This page | no |
| 1 | The table | `DataTable` to the standard (toolbar, filters, no-results, URL state, server mode, sticky header, row actions, bulk selection, column visibility); `EntityTable` a preset of it; analytics callers kept | yes |
| 2 | Gates | The four tests above with the shrinking allow-list | no |
| 3 | Observability and traces | 3a: analytics filters, agent observability (done); 3b: traces and sessions (server mode, controlled state); 3c: intents (done) | yes |
| 4 | Agents, assignments and memory | Asset index lists, memory, feedback, agent accounts, fleets, files, chats, versions, knowledge base tabs, shared activity and run lists, public catalog | yes |
| 5 | Evaluations and datasets | Evaluations, datasets and their entries | yes |
| 6 | Admin and adoption | Users, groups, members, user audit, audit logs, admin objects (server mode), adoption, knowledge health, concierge, CTEM lists | yes |
| 7 | Integrations | Integrations, custom tools, variables, MCP servers, OpenRouter, relays, templates, browser profiles, presentations, videos, desktop runners | yes |
| 8 | Operations and Scheduling | `OpsRowGrid` rows inside the table, task history, command center; `ListControlBar` removed | yes |
| 9 | Settings lists | With dev:form-standard-sweep | yes |

Every UI slice: UX Director verdict against the design standards with a Standards table, captures 1440 and 1024 in dark and light, before and after; a parity list per table in the PR (columns, links, actions, permissions, exports kept); existing tests pass.
