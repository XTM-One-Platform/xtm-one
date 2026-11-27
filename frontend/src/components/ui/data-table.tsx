// SPDX-License-Identifier: Apache-2.0

/**
 * DataTable: the ONE records table of the app (dev-docs/reference/data-tables.md).
 *
 * A toolbar (search, one filter pill per filterable column with its count,
 * "Clear all filters", the shown columns, the page size, CSV export, the
 * caller's controls and, once rows are selected, the bulk actions), a sticky
 * header whose sortable columns carry `aria-sort`, the rows (an optional
 * selection box, a real link on the first column, the row actions last), the
 * pagination in the footer, and four distinct states: loading, error (with
 * Retry), empty (nothing exists) and no results (nothing matches; clears).
 *
 * State (search, filters, sort, page, page size) lives in the URL when the
 * table names a `urlKey` (its prefix, so two tables of one page keep apart),
 * else in the component. The page size and the hidden columns are also kept
 * per user (`storageKey`). Pure logic: `lib/data-table-state.ts` (URL, filters)
 * and `lib/table-rows.ts` (sort, search, pages); the CSV is `lib/csv.ts`.
 *
 * Client mode (default) sorts, searches, filters and pages the rows it holds:
 * for small bounded sets. Server mode (`server`) draws the page it is given and
 * hands the state to the caller (`onStateChange`), which sends it to the API.
 * `EntityTable` (asset index pages) is a preset of this component.
 */
import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Check, Columns3, Download, ListFilter } from "lucide-react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { FilterClearButton, FilterPillPanel, FilterPillTrigger, FiltersPill, PANEL_CHIP_HEIGHT, useFilterPopover } from "@/components/ui/filter-pill";
import { Label } from "@/components/ui/label";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { TagFilter } from "@/components/ui/tag-filter";
import { ViewToggle } from "@/components/ui/view-toggle";
import { VisibilityFilter } from "@/components/ui/visibility-filter";
import { useFitToViewport, usePageStickyHead } from "@/hooks/useFitToViewport";
import type { ViewMode } from "@/hooks/useViewMode";
import type { Visibility as VisibilityScope } from "@/lib/visibility";
import { Pagination } from "@/components/ui/pagination";
import { RowTargetContext } from "@/components/ui/row-target-context";
import { TotalChip } from "@/components/ui/total-chip";
import { ToolbarCompactContext } from "@/components/ui/toolbar-compact-context";
import { SearchInput } from "@/components/ui/search-input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { TableSortHeader } from "@/components/ui/table-sort-header";
import { TruncatedText } from "@/components/ui/truncated-text";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { csvBlob, csvFilename, toCsv, type CsvValue } from "@/lib/csv";
import {
  activeFilterCount,
  initialTableState,
  matchesFilters,
  readTableState,
  writeTableState,
  type DataTableDefaults,
  type DataTableSort,
  type DataTableState,
  TABLE_STORAGE_PREFIX,
} from "@/lib/data-table-state";
import { activeFilterChips, facetValueCounts } from "@/lib/data-table-facets";
import { formatList } from "@/lib/format-list";
import { formatCount } from "@/lib/number-format";
import { sortFilterOptions } from "@/lib/select-options";
import { groupRows, matchesSearch, pageOf, sortRows, type RowGrouping, type SortDirection, type SortValue } from "@/lib/table-rows";
import { cn, saveBlobAs, toggleArrayItem } from "@/lib/utils";

export type { DataTableState, SortDirection, SortValue };

export interface DataTableColumn<T> {
  /** Stable identifier, also the sort key and the filter key. */
  key: string;
  /** English header text, translated by the table. */
  header: string;
  /** What the column means, shown on hover of its header (translated by the caller). */
  hint?: string;
  /** The column holds a required field (a row editor in edit mode): its header carries the required marker. */
  required?: boolean;
  cell: (row: T) => ReactNode;
  /** Makes the column sortable. */
  sortValue?: (row: T) => SortValue;
  /** The value written to the CSV; a column without it is not exported. */
  csv?: (row: T) => CsvValue;
  /** The CSV header, when it should say more than the column (a unit). English, translated. */
  csvHeader?: string;
  /** Written to the CSV, never drawn: an id, an email the row already shows by name. */
  exportOnly?: boolean;
  align?: "left" | "right";
  className?: string;
  headerClassName?: string;
  /**
   * Drawn only once the table's box reaches this container width (xl 36rem, 3xl 48rem,
   * 4xl 56rem, 6xl 72rem, 7xl 80rem, 8xl 88rem): a secondary column steps aside in a narrow card instead of
   * scrolling the table sideways. Still sorted, searched and exported.
   */
  hideBelow?: keyof typeof HIDE_BELOW;
  /** The direction a first click sorts: numbers biggest first by default. */
  firstSort?: SortDirection;
  /** The cell holds its own buttons or links: a press in it never opens the row. */
  interactive?: boolean;
  /**
   * The user may hide the column (the "Columns" pill); the first column never, nor a column
   * with no header text (row actions, selection, an icon): it would be an empty row there (#11289).
   */
  hideable?: boolean;
  /**
   * With `hideable`: not drawn until the user brings it back from the "Columns" pill, one
   * click away, so a secondary column never squeezes the main ones (UX r5 B1 on #10581).
   */
  defaultHidden?: boolean;
  /**
   * Width of the column in px, set on its header. Honoured by `layout="fixed"`, where the
   * columns without a width share what is left; a width that changes with the screen is
   * chosen by the caller (useMediaQuery), as the column set is.
   */
  width?: number;
}

export interface DataTableRemainder<T> {
  key: string;
  row: T;
  /** The first cell (a label and its reason), drawn instead of the first column's cell. */
  label: ReactNode;
  testId?: string;
}

export interface DataTableFilterOption {
  value: string;
  /** Already translated. */
  label: string;
}

export interface DataTableFilter<T> {
  /** The filter's key in the state and the URL (`f_<key>`). */
  key: string;
  /** English label of the pill, translated by the table. */
  label: string;
  /** The values a row carries for this filter (client mode). */
  values?: (row: T) => readonly (string | null | undefined)[];
  /**
   * The choices; client mode derives them from the rows when omitted. Shown
   * alphabetically on their label in the interface language, whatever the order given.
   */
  options?: DataTableFilterOption[];
  /** Values listed first, in the order given ("All", "None", "Me"), before the alphabetical rest. */
  pinned?: readonly string[];
  /**
   * Keep the order given: an ordinal scale only (severity, priority,
   * criticality, confidence, a time range), with a comment at the call site.
   */
  ordered?: boolean;
}

export interface DataTableGroupBy<T> extends RowGrouping<T> {
  /** The group's header text, already translated. */
  label: (value: string) => ReactNode;
}

export interface DataTableSelection {
  /** Keys (`rowKey`) of the selected rows. */
  selected: readonly string[];
  onChange: (keys: string[]) => void;
  /** The bulk actions, shown in the toolbar while rows are selected. */
  actions?: ReactNode;
  /**
   * The count the toolbar reads instead of `selected.length`: a selection that holds every
   * row the filters match, past the page (server mode), says how many it deletes.
   */
  count?: number;
  /**
   * Why a row cannot be selected (already translated), or undefined when it can: its box
   * renders disabled with this reason as its tooltip, and "select every row on this page" skips it.
   */
  disabledReason?: (key: string) => string | undefined;
}

/** Whole class names, so Tailwind sees them: hidden, then a table cell from the container width. */
const HIDE_BELOW = {
  xl: "hidden @xl:table-cell",
  "3xl": "hidden @3xl:table-cell",
  "4xl": "hidden @4xl:table-cell",
  "6xl": "hidden @6xl:table-cell",
  "7xl": "hidden @7xl:table-cell",
  "8xl": "hidden @min-[88rem]:table-cell",
} as const;

/** The same breakpoints in px (Tailwind container sizes), for a column the picker may bring back. */
const HIDE_BELOW_PX: Record<keyof typeof HIDE_BELOW, number> = {
  xl: 576,
  "3xl": 768,
  "4xl": 896,
  "6xl": 1152,
  "7xl": 1280,
  "8xl": 1408,
};

/**
 * Whether the Columns picker lists the column: `hideable` and a header to show. A headerless
 * column (row actions, selection, a star) is always drawn and never listed, else the picker
 * shows an empty row and a stored choice could hide the row's own controls (#11289).
 */
function isPickable(column: { hideable?: boolean; header: string }): boolean {
  return Boolean(column.hideable) && column.header.trim() !== "";
}

const PAGE_SIZES = [10, 25, 50, 100] as const;
/** The page size a table opens at when its caller names none and the user kept none: 25 across the platform (Sam, 23 Nov 2026). */
const DEFAULT_PAGE_SIZE = 25;

/**
 * The two row heights of every records table (standard v2, dev-docs/reference/data-tables.md):
 * a one-line row and a two-line row (a primary line and one secondary line). Never a third
 * line: a badge goes inline on line 2 or to its own column. One height per table.
 */
export const ROW_HEIGHT = { 1: "h-10", 2: "h-14" } as const;
export type RowLines = keyof typeof ROW_HEIGHT;

/** ROW_HEIGHT in pixels, for the loading rows to fill the window. */
const ROW_HEIGHT_PX: Record<RowLines, number> = { 1: 40, 2: 56 };

/**
 * How many skeleton rows the first read draws: a window's worth at the table's row
 * height, never more than a page (#11345).
 */
export function tableSkeletonRowCount(pageSize: number, rowLines: RowLines, viewportHeight?: number): number {
  const height = viewportHeight ?? (typeof window === "undefined" ? 0 : window.innerHeight);
  const fit = Math.ceil(height / ROW_HEIGHT_PX[rowLines]);
  return Math.max(1, Math.min(pageSize, fit || pageSize));
}

/** The scope switcher of a list that has scopes (Mine / Company / Shared / All, each with its count). */
export interface DataTableScope {
  value: VisibilityScope;
  onChange: (value: VisibilityScope) => void;
  counts?: Partial<Record<VisibilityScope, number>> | null;
  /** Segments the list cannot hold (e.g. `["company"]`). */
  hidden?: readonly VisibilityScope[];
  /** A segment's own tooltip (already translated), e.g. Company's built-ins hidden (#11831). */
  tips?: Partial<Record<VisibilityScope, ReactNode>>;
}

/**
 * A search the page holds itself (its URL key predates the table, its counts follow it):
 * drawn first in the toolbar like the table's own search, never a second search box.
 */
export interface DataTableSearch {
  value: string;
  onChange: (value: string) => void;
  /** Already translated: the accessible name of the box. */
  placeholder: string;
}

/** The Tags pill: one filter pill of the same kind as the others. */
export interface DataTableTags {
  all: string[];
  selected: string[];
  onChange: (tags: string[]) => void;
}

/** The grid / list toggle of a list that has a grid view (last control of the toolbar). */
export interface DataTableView {
  value: ViewMode;
  onChange: (mode: ViewMode) => void;
  /** The views offered (default grid and list). */
  modes?: readonly ViewMode[];
}

const STORAGE_PREFIX = TABLE_STORAGE_PREFIX;

interface StoredPreferences {
  pageSize?: number;
  /** Columns the user hid. */
  hidden?: string[];
  /** Columns the user brought back, over their `hideBelow` width. */
  shown?: string[];
}

function readPreferences(storageKey: string | undefined): StoredPreferences {
  if (!storageKey) return {};
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + storageKey);
    return raw ? (JSON.parse(raw) as StoredPreferences) : {};
  } catch {
    return {};
  }
}

function writePreferences(storageKey: string | undefined, preferences: StoredPreferences) {
  if (!storageKey) return;
  try {
    localStorage.setItem(STORAGE_PREFIX + storageKey, JSON.stringify(preferences));
  } catch {
    /* storage disabled or full: the preference lasts the session */
  }
}

export interface DataTableProps<T> {
  rows: T[];
  columns: DataTableColumn<T>[];
  rowKey: (row: T) => string;
  defaultSort?: DataTableSort;
  /** The fields a client-side search reads; omitted (and no `server`) = no search box. */
  searchFields?: (row: T) => (string | null | undefined)[];
  /**
   * Already translated. The accessible name of the search box; the box itself
   * shows the short "Search..." so it never clips in a narrow card.
   */
  searchPlaceholder?: string;
  filters?: DataTableFilter<T>[];
  /**
   * Why the table has no filter (a short list with one dimension): the gate of
   * `data-table-gates.test.ts` asks every caller for `filters` or this reason.
   */
  noFilters?: string;
  /** The CSV file's stem; omitted = no Export. */
  exportName?: string;
  /** Rows after the last page (remainders): never sorted, searched, paged or exported. */
  trailingRows?: ReactNode;
  /**
   * Remainder rows drawn after the last page through the same columns as the rows, so a
   * column hidden by the picker or the container width is hidden in them too (UX round 1
   * on #10020, B1): `label` replaces the first cell, the other cells come from `cell`.
   * Never sorted, searched, paged, linked or exported.
   */
  remainderRows?: DataTableRemainder<T>[];
  defaultPageSize?: number;
  /** false draws every row on one page with no pager and no page size (a short list). */
  paginate?: boolean;
  isLoading?: boolean;
  isError?: boolean;
  /** Already translated; defaults to a generic sentence. */
  errorLabel?: string;
  onRetry?: () => void;
  /** Already translated: nothing exists yet. */
  emptyLabel: string;
  /** Already translated: nothing matches the search and filters. */
  noResultsLabel?: string;
  /** Controls placed before the page size and the Export button. */
  toolbar?: ReactNode;
  /**
   * A control of the host drawn first on the toolbar row, before the search (the fleet
   * page's list switcher): never a filter of the rows, so it is not counted or cleared.
   */
  toolbarLead?: ReactNode;
  /** The page's own search (named filter kind `search`), instead of `searchFields`. */
  search?: DataTableSearch;
  /** Scope switcher, drawn right after the search (named filter kind `scope`). */
  scope?: DataTableScope;
  /** Drawn instead of `emptyLabel` when nothing exists and nothing narrows (a scope hint). */
  emptyContent?: ReactNode;
  /** Tags pill, drawn after the column filter pills (named filter kind `tags`). */
  tags?: DataTableTags;
  /**
   * The page's own typed filter pills (a property filter, a category pill), drawn after the
   * column filter pills and before Clear all; never a plain select (gate of data-table-gates).
   */
  filterPills?: ReactNode;
  /** Grid / list toggle, the last control of the toolbar's right end. */
  view?: DataTableView;
  /**
   * Grid render mode: the page's rows (searched, sorted, pinned and paged by the table) drawn
   * as cards under the same toolbar, total and pager, instead of the table. The card grid hugs
   * its cards; the Columns chooser is not offered (no columns on screen).
   */
  renderGrid?: (rows: T[]) => ReactNode;
  /**
   * The grid class `renderGrid` lays its cards on, when it is not CARD_GRID: the first read
   * draws its loading cards on the same grid, so the cards land in place (#11345).
   */
  gridClassName?: string;
  /**
   * The first read of a `renderGrid` whose cards are not asset cards (an Operations row
   * list): the page draws its own row skeleton for `pageSize` rows, so the rows land in
   * place (#11345). Omitted = asset-card skeletons on `gridClassName`.
   */
  renderGridSkeleton?: (pageSize: number) => ReactNode;
  /**
   * Every record the list holds before the page's own scope, tags and filters (outside the
   * table): the total then reads "n of N" when they narrow it. Omitted = the rows given.
   */
  totalAll?: number;
  /** One-line (default) or two-line rows: the two row heights of the standard. */
  rowLines?: RowLines;
  /** false hides the toolbar (a preset whose page holds its own search). */
  showToolbar?: boolean;
  /**
   * A crowded toolbar (several filter pills plus a `toolbar` control) that must stay
   * one row at 1024: the search floors at 128 px with the short "Search..."
   * placeholder (`SearchInput compact`) and gives its width back first; the full
   * `searchPlaceholder` stays its accessible name (UX r1 B1 on #10336).
   */
  compactSearch?: boolean;
  /**
   * One filter row that never wraps (#10215 criterion 6, the Operations lists): the
   * column `filters` and the `filterPills` move into one "Filters" pill counted by
   * the filters that narrow the list, the scope segments keep one row (the ones that
   * do not fit in its "More tabs" menu) and the search takes its compact floor.
   */
  filtersInPill?: boolean;
  /**
   * With `filtersInPill` (#2556): the Filters panel lays its groups out in columns that fit
   * a 1080p viewport, each value shows how many rows it would keep (counted on the rows
   * the table holds, given the other filters and the search), and the active filters show
   * as removable chips under the toolbar. Client mode only: a `server` table keeps the stack.
   */
  facetGrid?: boolean;
  /**
   * The row's own action (open its page or its detail): the whole row is the target, by
   * pointer and by keyboard (focusable, Enter), never a link drawn in a cell (#12070).
   */
  onRowClick?: (row: T) => void;
  /** With `onRowClick`: whether this row opens anything; a row that does not is neither a target nor a tab stop. */
  rowOpens?: (row: T) => boolean;
  /** Detail route of a row: the first non-interactive cell becomes a real link, the row opens it. Undefined: that row opens nothing. */
  rowHref?: (row: T) => string | undefined;
  rowClassName?: (row: T) => string | undefined;
  /** Rows kept above the others whatever the sort (favourites). */
  pinFirst?: (row: T) => boolean;
  /**
   * Groups the rows (client mode): the rows of one group follow each other, in the
   * group order (`rank`, else the value), the sort kept inside each group, and a
   * header row names each group on the page with its row count (#1176).
   */
  groupBy?: DataTableGroupBy<T>;
  /** Last column: the row's own buttons (never opens the row). */
  rowActions?: (row: T) => ReactNode;
  /**
   * Width in px of the row-actions column under `layout="fixed"` (default 96, two compact
   * icon buttons): an unsized column there would share the free width with the content
   * column (UX round 1 on #9975, B3).
   */
  rowActionsWidth?: number;
  /**
   * An opened row: what it returns is drawn on a line of its own under the row, across
   * every column on screen (null or undefined = the row is closed). The caller holds which
   * rows are open, usually toggled by `onRowClick`.
   */
  expandedRow?: (row: T) => ReactNode;
  /**
   * A cell already holds the row's own open button (with aria-expanded): the row itself is
   * then not a second tab stop. Omitted, a row with `onRowClick` and `expandedRow` is the
   * keyboard disclosure (focusable, Enter or Space, aria-expanded).
   */
  toggleInCell?: boolean;
  selection?: DataTableSelection;
  /** The prefix of the table's keys in the URL; omitted = state kept in the component. */
  urlKey?: string;
  /** Per-user key of the page size and the hidden columns (localStorage). */
  storageKey?: string;
  /** Server mode: `rows` is the current page, `total` every match; the caller sorts, filters and pages. */
  server?: { total: number };
  /** Called with every new state (server mode sends it to the API). */
  onStateChange?: (state: DataTableState) => void;
  /**
   * Controlled state: the caller owns search, filters, sort, page and page size
   * (a page whose URL keys predate the table, such as `/observability/traces`), and each change
   * reaches it through `onStateChange` only. Omitted = the table keeps its state.
   */
  state?: DataTableState;
  /**
   * Filters the page holds outside the table (a filter card above it): counted in the
   * active-filter count, so the toolbar offers its "Clear all filters" for them too.
   */
  externalFilterCount?: number;
  /**
   * A filter the page draws on the toolbar row and counts on its own pill (a Status pill
   * away from its default): not in the Filters badge, but "Clear all filters" shows for it.
   */
  toolbarFilterActive?: boolean;
  /**
   * Replaces the table's own clear when the page holds filters outside it: "Clear all
   * filters" then clears every filter of the page in one write (the caller keeps the sort).
   */
  onClearAll?: () => void;
  /** `comfortable` is the asset index density (EntityTable). */
  density?: "compact" | "comfortable";
  /**
   * `fixed` sizes the columns from their header `width` (CSS table-layout: fixed): the
   * truncated cells keep their width whatever a long message in another column holds.
   * The table clips every cell and header to its column and ends a plain string or number
   * cell with an ellipsis and a tooltip: no caller opts in to stay inside its column.
   * `auto` (default) lets the browser size the columns from their content.
   */
  layout?: "auto" | "fixed";
  /** Draws the table inside its own bordered card (default) or bare. */
  framed?: boolean;
  /**
   * Fills the card it sits in (a dashboard widget in a `WidgetRow`, its card as tall
   * as the row's tallest): the rows still hug their count and the pager is pinned
   * to the card's bottom instead of floating under the last row (#11470).
   */
  fill?: boolean;
  /**
   * The card list (`renderGrid`) fits the window like the table: its own scroll area takes
   * the room left (`useFitToViewport`), the page does not scroll. For a page's main grid of
   * fixed-height rows (Operations, `OpsRowGrid`); an asset card grid keeps the page scroll.
   */
  fitGrid?: boolean;
  /** data-testid of every body row, for the page's tests. */
  rowTestId?: string;
  /** `data-*` attributes of one body row (its id, its status), for the page's tests and styles; a row with `rowHref` takes none. */
  rowDataAttributes?: (row: T) => Record<`data-${string}`, string | undefined>;
}

/**
 * The search the last table write produced and the rendered search it started
 * from. The router hands every table the search of its last render, so two
 * writes before the next render (two tables of one page, or a typed search and
 * a sort) would each start from that render and the second would drop the
 * first: a write whose render is still the one the last write started from
 * starts from that write instead.
 */
let pendingUrlWrite: { base: string; search: string } | null = null;

/** State of a table in the URL, under the prefix `urlKey`. */
function useUrlTableState(urlKey: string, defaults: DataTableDefaults): [DataTableState, (next: DataTableState) => void] {
  const [params, setParams] = useSearchParams();
  const key = JSON.stringify(defaults);
  const state = useMemo(() => readTableState(params, urlKey, JSON.parse(key) as DataTableDefaults), [params, urlKey, key]);
  // Once a new search is rendered, the pending write is in it (or was replaced by a navigation).
  useEffect(() => {
    pendingUrlWrite = null;
  }, [params]);
  const setState = useCallback(
    (next: DataTableState) => {
      const rendered = params.toString();
      const start = pendingUrlWrite?.base === rendered ? new URLSearchParams(pendingUrlWrite.search) : params;
      const written = writeTableState(start, urlKey, next, JSON.parse(key) as DataTableDefaults);
      pendingUrlWrite = { base: rendered, search: written.toString() };
      setParams(written, { replace: true });
    },
    [params, setParams, urlKey, key],
  );
  return [state, setState];
}

export function DataTable<T>(props: DataTableProps<T>) {
  const preferences = readPreferences(props.storageKey);
  const defaults: DataTableDefaults = {
    sort: props.defaultSort ?? null,
    pageSize: props.paginate === false ? Number.MAX_SAFE_INTEGER : (preferences.pageSize ?? props.defaultPageSize ?? DEFAULT_PAGE_SIZE),
  };
  if (props.state) return <DataTableView {...props} state={props.state} setState={ignoreState} />;
  // An empty prefix is a valid one (the page's only table): test for presence, not truthiness.
  return props.urlKey !== undefined ? (
    <UrlStateDataTable {...props} urlKey={props.urlKey} defaults={defaults} />
  ) : (
    <LocalStateDataTable {...props} defaults={defaults} />
  );
}

/** A controlled table hands its changes to `onStateChange` alone. */
function ignoreState() {}

function UrlStateDataTable<T>(props: DataTableProps<T> & { urlKey: string; defaults: DataTableDefaults }) {
  const [state, setState] = useUrlTableState(props.urlKey, props.defaults);
  return <DataTableView {...props} state={state} setState={setState} />;
}

function LocalStateDataTable<T>(props: DataTableProps<T> & { defaults: DataTableDefaults }) {
  const [state, setState] = useState<DataTableState>(() => initialTableState(props.defaults));
  return <DataTableView {...props} state={state} setState={setState} />;
}

function DataTableView<T>({
  rows,
  columns,
  rowKey,
  searchFields,
  searchPlaceholder,
  filters = [],
  exportName,
  trailingRows,
  remainderRows,
  paginate = true,
  isLoading = false,
  isError = false,
  errorLabel,
  onRetry,
  emptyLabel,
  noResultsLabel,
  toolbar,
  toolbarLead,
  search,
  emptyContent,
  scope,
  tags,
  filterPills,
  view,
  renderGrid,
  gridClassName,
  renderGridSkeleton,
  totalAll,
  rowLines = 1,
  showToolbar = true,
  compactSearch = false,
  filtersInPill = false,
  facetGrid = false,
  onRowClick,
  rowOpens,
  rowHref,
  rowClassName,
  pinFirst,
  groupBy,
  rowActions,
  rowActionsWidth = 96,
  expandedRow,
  toggleInCell = false,
  selection,
  storageKey,
  server,
  onStateChange,
  externalFilterCount = 0,
  toolbarFilterActive = false,
  onClearAll,
  density = "compact",
  layout = "auto",
  framed = true,
  fill = false,
  fitGrid = false,
  rowTestId,
  rowDataAttributes,
  state,
  setState,
}: DataTableProps<T> & { state: DataTableState; setState: (next: DataTableState) => void }) {
  const { locale, t_i18n } = useFormatter();
  // The ids that bind each heading of the Filters pill to its group of toggles.
  const filterHeadingId = useId();
  const [choices, setChoices] = useState<{ hidden: string[]; shown: string[] }>(() => {
    const stored = readPreferences(storageKey);
    return { hidden: stored.hidden ?? [], shown: stored.shown ?? [] };
  });
  // The table's width, so a hideable column under its `hideBelow` width is hidden by the
  // picker state, not by CSS alone: the picker then says what is drawn and brings it back
  // (UX round 1 on #10020, B1). null until measured: the container query decides.
  const [box, setBox] = useState<HTMLDivElement | null>(null);
  const [boxWidth, setBoxWidth] = useState<number | null>(null);
  // One scrollbar per page (#11233): the scroll area takes the room the window has left
  // under it, so the page never scrolls as well; a long-form page gets no bound. Only the
  // page's last block fits: a table in a dashboard widget hugs its rows (#11470).
  const [scrollArea, setScrollArea] = useState<HTMLDivElement | null>(null);
  const scrollRoom = useFitToViewport(scrollArea, box);
  // Long-form page (no bound): the header follows the page under the top bar instead.
  const [tableHead, setTableHead] = useState<HTMLTableSectionElement | null>(null);
  usePageStickyHead(scrollArea, tableHead, scrollRoom === null);
  useEffect(() => {
    if (!box || typeof ResizeObserver === "undefined") return;
    const measure = () => setBoxWidth(box.clientWidth > 0 ? box.clientWidth : null);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [box]);
  // The scroll area's own clientWidth: its border and its vertical scrollbar left out, so a
  // full-width row never adds a horizontal overflow (6 px under the Sources table, UX round 1
  // on #1356, B1). The content box is observed, which shrinks when the scrollbar shows.
  const [scrollClientWidth, setScrollClientWidth] = useState<number | null>(null);
  useEffect(() => {
    if (!scrollArea || typeof ResizeObserver === "undefined") return;
    const measure = () => setScrollClientWidth(scrollArea.clientWidth > 0 ? scrollArea.clientWidth : null);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(scrollArea);
    return () => observer.disconnect();
  }, [scrollArea]);
  // The host's lead control (the fleet page's list switcher) takes its width out of
  // the one-row toolbar: measured so the row can give its right group a line of its own.
  const [lead, setLead] = useState<HTMLDivElement | null>(null);
  const [leadWidth, setLeadWidth] = useState(0);
  useEffect(() => {
    if (!lead || typeof ResizeObserver === "undefined") return;
    const measure = () => setLeadWidth(lead.offsetWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(lead);
    return () => observer.disconnect();
  }, [lead]);
  // The lead and box widths at which the one-row toolbar's full labels ran past its box.
  const [toolbarRow, setToolbarRow] = useState<HTMLDivElement | null>(null);
  const [fullLabelsOverflow, setFullLabelsOverflow] = useState<{ lead: number; box: number } | null>(null);
  // The lead and box widths at which the one-row toolbar still ran past its box once compact.
  const [compactOverflow, setCompactOverflow] = useState<{ lead: number; box: number } | null>(null);

  // A typed search reaches the state after a pause, so the URL (and a server query) follow the words, not the keys.
  const [searchText, setSearchText] = useState(state.search);
  const lastWritten = useRef(state.search);

  const update = (changes: Partial<DataTableState>, keepPage = false) => {
    // A search still in its pause goes with any other change, so this write never carries a stale one.
    const typed = changes.search === undefined && searchText !== lastWritten.current;
    if (typed) lastWritten.current = searchText;
    const merged = typed ? { ...changes, search: searchText } : changes;
    const next = { ...state, ...merged, page: keepPage && !typed ? (changes.page ?? state.page) : 0 };
    setState(next);
    onStateChange?.(next);
  };

  useEffect(() => {
    if (state.search !== lastWritten.current) {
      lastWritten.current = state.search;
      setSearchText(state.search);
    }
  }, [state.search]);
  useEffect(() => {
    if (searchText === lastWritten.current) return;
    const timer = setTimeout(() => {
      // Another change already carried this text.
      if (searchText === lastWritten.current) return;
      lastWritten.current = searchText;
      update({ search: searchText });
    }, 250);
    return () => clearTimeout(timer);
    // update reads the latest state on each render; the timer only follows the text.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchText]);

  const firstColumnKey = columns.find((column) => !column.exportOnly)?.key;
  /** true drawn, false hidden, null left to the container query (not measured yet). */
  const columnShown = (column: DataTableColumn<T>): boolean | null => {
    if (!isPickable(column) || column.key === firstColumnKey) return true;
    if (choices.hidden.includes(column.key)) return false;
    if (choices.shown.includes(column.key)) return true;
    if (column.defaultHidden) return false;
    if (!column.hideBelow) return true;
    return boxWidth === null ? null : boxWidth >= HIDE_BELOW_PX[column.hideBelow];
  };
  const drawnColumns = columns.filter((column) => !column.exportOnly && columnShown(column) !== false);
  const hideableColumns = columns.filter((column) => isPickable(column) && !column.exportOnly && column.key !== firstColumnKey);
  /** The container-query class: kept on a fixed column, on a hideable one only until the table is measured. */
  const widthClass = (column: DataTableColumn<T>) =>
    column.hideBelow && (!isPickable(column) || columnShown(column) === null) ? HIDE_BELOW[column.hideBelow] : undefined;

  const filterOptions = useMemo(
    () =>
      filters.map((filter) => {
        // Every filter lists its options alphabetically (Sam, 16 Nov 2026, #12289), an ordinal scale excepted.
        const order = { locale, ordered: filter.ordered, pinned: filter.pinned };
        if (filter.options) return sortFilterOptions(filter.options, order);
        const values = new Set<string>();
        for (const row of rows) for (const value of filter.values?.(row) ?? []) if (value) values.add(value);
        return sortFilterOptions(
          [...values].map((value) => ({ value, label: value })),
          order,
        );
      }),
    [filters, rows, locale],
  );

  const sortColumn = columns.find((column) => column.key === state.sort?.key && column.sortValue);
  const shown = useMemo(() => {
    if (server) return rows;
    const byFilter = filters.length
      ? rows.filter((row) =>
          matchesFilters(state.filters, (key) =>
            (filters.find((filter) => filter.key === key)?.values?.(row) ?? []).filter((value): value is string => !!value),
          ),
        )
      : rows;
    const matched =
      searchFields && state.search.trim() ? byFilter.filter((row) => matchesSearch(searchFields(row), state.search)) : byFilter;
    const sorted = sortColumn?.sortValue && state.sort ? sortRows(matched, sortColumn.sortValue, state.sort.direction) : matched;
    if (groupBy) return groupRows(sorted, groupBy);
    if (!pinFirst) return sorted;
    const pinned = sorted.filter(pinFirst);
    return pinned.length === 0 || pinned.length === sorted.length ? sorted : [...pinned, ...sorted.filter((row) => !pinFirst(row))];
  }, [server, rows, filters, state.filters, state.search, state.sort, searchFields, sortColumn, pinFirst, groupBy]);
  // The row count of each group over every page, for the group's header row.
  const groupCounts = useMemo(() => {
    if (!groupBy || server) return null;
    const counts = new Map<string, number>();
    for (const row of shown) {
      const value = groupBy.value(row);
      counts.set(value, (counts.get(value) ?? 0) + 1);
    }
    return counts;
  }, [groupBy, server, shown]);

  const total = server ? server.total : shown.length;
  const current = server
    ? { rows, page: state.page, lastPage: Math.max(0, Math.ceil(total / state.pageSize) - 1) }
    : pageOf(shown, state.page, state.pageSize);
  const filterCount = activeFilterCount(state.filters) + externalFilterCount;
  // The faceted panel (#2556): its counts over the rows the search keeps, and its chips.
  const faceted = facetGrid && filtersInPill && !server;
  const facetCounts = useMemo(() => {
    if (!faceted) return null;
    const searched =
      searchFields && state.search.trim() ? rows.filter((row) => matchesSearch(searchFields(row), state.search)) : rows;
    return facetValueCounts(searched, filters, state.filters);
  }, [faceted, rows, filters, state.filters, state.search, searchFields]);
  const filterChips = faceted
    ? activeFilterChips(
        filters.map((filter) => ({ key: filter.key, label: t_i18n(filter.label) })),
        filterOptions,
        state.filters,
      )
    : [];
  const narrowing = !!state.search.trim() || !!search?.value.trim() || filterCount > 0;
  // "Clear all filters" shows when a filter or the page's own search narrows the list
  // (standard v2: a search, tag or pill; never the scope alone, UX round 1 on #10920, B2).
  // The table's own search and a toolbar filter count too (UX round 1 on #1004, B1).
  const clearable = filterCount > 0 || !!search?.value.trim() || !!state.search.trim() || toolbarFilterActive;

  // A card list (`renderGrid`) has no header to click: the toolbar offers these instead.
  const sortableColumns = columns.filter((column) => column.sortValue && !column.exportOnly);

  const onSort = (key: string) => {
    const column = columns.find((item) => item.key === key);
    update({
      sort:
        state.sort?.key === key
          ? { key, direction: state.sort.direction === "asc" ? "desc" : "asc" }
          : { key, direction: column?.firstSort ?? (column?.align === "right" ? "desc" : "asc") },
    });
  };

  const clearAll = () => {
    setSearchText("");
    lastWritten.current = "";
    if (search && !onClearAll) search.onChange("");
    if (onClearAll) onClearAll();
    else update({ search: "", filters: {} });
  };

  const exportCsv = () => {
    if (!exportName) return;
    const exported = columns.filter((column) => column.csv);
    const csv = toCsv(
      shown,
      exported.map((column) => ({ header: t_i18n(column.csvHeader ?? column.header), value: column.csv! })),
    );
    saveBlobAs(csvBlob(csv), csvFilename(exportName));
  };

  const toggleHidden = (key: string) => {
    const column = columns.find((item) => item.key === key);
    const drawn = column ? columnShown(column) !== false : true;
    const without = (list: string[]) => list.filter((item) => item !== key);
    const next = drawn
      ? { hidden: [...without(choices.hidden), key], shown: without(choices.shown) }
      : { hidden: without(choices.hidden), shown: [...without(choices.shown), key] };
    setChoices(next);
    writePreferences(storageKey, { ...readPreferences(storageKey), ...next });
  };

  const setPageSize = (size: number) => {
    writePreferences(storageKey, { ...readPreferences(storageKey), pageSize: size });
    update({ pageSize: size });
  };

  const formatGroupCount = (count: number) => t_i18n("{count, plural, one {# row} other {# rows}}", { count });
  const pageKeys = current.rows.map(rowKey).filter((key) => !selection?.disabledReason?.(key));
  const selected = selection?.selected ?? [];
  const pageSelected = pageKeys.filter((key) => selected.includes(key)).length;
  const togglePage = () => {
    if (!selection) return;
    selection.onChange(
      pageSelected === pageKeys.length
        ? selected.filter((key) => !pageKeys.includes(key))
        : [...selected, ...pageKeys.filter((key) => !selected.includes(key))],
    );
  };

  // The shared footer: the pager alone, past one page. The total is said once, by the
  // toolbar chip at the top (a paginated table always draws its toolbar unless
  // showToolbar={false}), so the footer draws no "N results" and no range text
  // (Sam, 11 Nov 2026); a list of one page ends at its last row.
  const pager = <Pagination page={current.page} pageSize={state.pageSize} total={total} onChange={(page) => update({ page }, true)} />;
  const totalLine = !paginate ? null : fill ? <div className="mt-auto">{pager}</div> : pager;

  const cellPadding = density === "comfortable" ? "px-4 py-2" : "px-3 py-2";
  // A records row takes its height from ROW_HEIGHT alone: no vertical padding, its cells
  // centred, so every row of a table renders at the token whatever its content (a badge
  // on line 2 or none). With py-2 the content set the height: 58 and 62 px rows in one
  // table against a 56 px token (UX round 1 on #10920, B1).
  const rowCellPadding = density === "comfortable" ? "px-4 py-0" : "px-3 py-0";
  // Under layout="fixed" the table, never the caller, keeps every cell inside its column
  // (Sam, 15 Nov 2026: a long owner name was painted over the Due date of its row). The cell
  // clips on the x axis at its padding box, so a 2 px focus ring inside the 12 px padding
  // still draws whole and nothing is cut vertically; a plain string or number cell ends
  // with an ellipsis and the shared tooltip names it whole. A renderer that draws its own
  // markup keeps it within `max-w-full` (WorkItemAgent) to get its ellipsis too.
  const fixedLayout = layout === "fixed";
  const cellClip = fixedLayout ? "overflow-x-clip" : undefined;
  const cellContent = (content: ReactNode) =>
    fixedLayout && (typeof content === "string" || typeof content === "number") && content !== "" ? (
      <TruncatedText text={String(content)} />
    ) : (
      content
    );
  const linkColumnKey = columns.find((column) => !column.exportOnly && !column.interactive)?.key;
  const columnCount = drawnColumns.length + (selection ? 1 : 0) + (rowActions ? 1 : 0);
  // A full-width row (opened row, no-results row) spans the columns actually drawn: a
  // column hidden by its container query (hideBelow) still counts in columnCount, and a
  // colSpan past the drawn columns adds a phantom column under layout="fixed" (UX round 2
  // on #9975, B2). Counted from the header row, recounted when the table resizes.
  const [headRow, setHeadRow] = useState<HTMLTableRowElement | null>(null);
  const [shownColumns, setShownColumns] = useState<number | null>(null);
  useEffect(() => {
    if (!headRow) return;
    const count = () => {
      const shown = Array.from(headRow.children).filter((cell) => window.getComputedStyle(cell).display !== "none").length;
      setShownColumns(shown > 0 ? shown : null);
    };
    count();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(count);
    observer.observe(headRow);
    return () => observer.disconnect();
  }, [headRow, columnCount]);
  const spanned = shownColumns ?? columnCount;
  // A full-width row keeps its content on the visible part of a table wider than its card:
  // centred on the whole table, "Clear all filters" sat 41 px past the scroll edge of the
  // OpenRouter keys at 1080 (UX round 3 on #10601, A1). Sticky to the scroll area's left
  // edge, as wide as that area's client box (inside its border and its vertical scrollbar).
  const visibleWidth =
    scrollClientWidth !== null ? scrollClientWidth : boxWidth === null ? undefined : Math.max(0, boxWidth - (framed ? 2 : 0));
  const fullWidth = (content: ReactNode, className: string, testId?: string) => (
    <div data-testid={testId} className={cn("sticky left-0", className)} style={visibleWidth === undefined ? undefined : { width: visibleWidth }}>
      {content}
    </div>
  );

  const renderRow = (row: T) => {
    const key = rowKey(row);
    const href = rowHref?.(row);
    const cells = (
      <>
        {selection && (
          <td className="w-8 pl-3 pr-0 py-0">
            <SelectionBox
              checked={selected.includes(key)}
              onToggle={() => selection.onChange(toggleArrayItem([...selected], key))}
              label={t_i18n("Select this row")}
              disabled={Boolean(selection.disabledReason?.(key))}
              reason={selection.disabledReason?.(key)}
            />
          </td>
        )}
        {drawnColumns.map((column) => {
          const content = column.cell(row);
          return (
            <td
              key={column.key}
              className={cn(
                rowCellPadding,
                column.align === "right" && "text-right tabular-nums",
                cellClip,
                widthClass(column),
                column.className,
              )}
            >
              {column.key === linkColumnKey && href ? (
                <Link to={href} className="block" onClick={(event) => event.stopPropagation()}>
                  {cellContent(content)}
                </Link>
              ) : column.interactive ? (
                <div
                  onClick={(event) => event.stopPropagation()}
                  className={cn("flex items-center gap-1", column.align === "right" && "justify-end")}
                >
                  {content}
                </div>
              ) : (
                cellContent(content)
              )}
            </td>
          );
        })}
        {rowActions && (
          <td className={cn(rowCellPadding, "text-right")}>
            <div onClick={(event) => event.stopPropagation()} className="flex items-center justify-end gap-1">
              {rowActions(row)}
            </div>
          </td>
        )}
      </>
    );
    const opened = expandedRow?.(row);
    const isOpen = opened !== null && opened !== undefined && opened !== false;
    // A row that opens in place is a keyboard disclosure: focusable, Enter or Space toggles
    // it, aria-expanded says its state (UX round 1 on #9975, B4).
    const opens = Boolean(onRowClick) && (rowOpens?.(row) ?? true);
    const disclosure = Boolean(opens && expandedRow && !href && !toggleInCell);
    // A row that opens a page (onRowClick without expandedRow) is a keyboard target too:
    // focusable, Enter opens it, so no cell needs a link of its own (#12070).
    const focusable = Boolean(opens && !href && !toggleInCell);
    // A routed row whose every column is interactive draws no link cell: the row itself is
    // then the one keyboard target (tab stop, Enter opens it), never a row the keyboard
    // cannot open (UX round 1 on #2027, B1).
    const hrefFocusable = Boolean(href && !linkColumnKey);
    const className = cn(
      "border-t border-elevation-subtle-soft transition-colors",
      ROW_HEIGHT[rowLines],
      (focusable || hrefFocusable) && "outline-none focus-visible:bg-elevation-hover focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus",
      (href || opens) && "cursor-pointer hover:bg-elevation-hover",
      !href && !opens && "hover:bg-elevation-hover",
      rowClassName?.(row),
    );
    const line = href ? (
      <HrefRow key={key} href={href} className={className} testId={rowTestId} focusable={hrefFocusable}>
        {cells}
      </HrefRow>
    ) : (
      <tr
        key={key}
        {...rowDataAttributes?.(row)}
        data-testid={rowTestId}
        className={className}
        onClick={opens ? () => onRowClick?.(row) : undefined}
        tabIndex={focusable ? 0 : undefined}
        aria-expanded={disclosure ? isOpen : undefined}
        onKeyDown={
          focusable
            ? (event) => {
                if (event.target !== event.currentTarget) return;
                if (event.key === "Enter" || (disclosure && event.key === " ")) {
                  event.preventDefault();
                  onRowClick?.(row);
                }
              }
            : undefined
        }
      >
        {cells}
      </tr>
    );
    // Always the same Fragment around the row, open or closed: returning the bare <tr> when
    // closed remounted it at each toggle and the keyboard focus fell to the body (UX round 2
    // on #9975, B1).
    // A routed row (rowHref, or onRowClick opening a page) is ONE tab stop: its cells add none
    // (D2 of the #12074 retest, #12070).
    return (
      <Fragment key={key}>
        <RowTargetContext.Provider value={Boolean(href) || focusable}>{line}</RowTargetContext.Provider>
        {isOpen && (
          <tr data-testid="data-table-expanded-row" className="bg-elevation-hover">
            <td colSpan={spanned} className="p-0">
              {fullWidth(opened, "px-4 py-4", "data-table-expanded-content")}
            </td>
          </tr>
        )}
      </Fragment>
    );
  };

  // Nothing matches the search and filters: one sentence and its clear, in the table or the grid.
  const noResults = (
    <>
      <span>{noResultsLabel ?? t_i18n("No row matches the search and filters.")}</span>{" "}
      <button type="button" onClick={clearAll} className="font-medium text-accent hover:underline">
        {t_i18n("Clear all filters")}
      </button>
    </>
  );

  // The first read draws the table itself: its head with the real column names and a
  // window's worth of rows at the table's row height, each cell greyed in its column
  // (#11345), so the rows land without moving the head or the toolbar. A card grid
  // (`renderGrid`) draws a window's worth of asset-card skeletons on its own grid.
  const skeletonRows = isLoading && !renderGrid
    ? Array.from({ length: tableSkeletonRowCount(state.pageSize, rowLines) }).map((_, index) => (
        <tr key={`skeleton:${index}`} data-testid="data-table-skeleton-row" className={cn("border-t border-elevation-subtle-soft", ROW_HEIGHT[rowLines])}>
          {selection && (
            <td className="w-8 pl-3 pr-0 py-0">
              <Skeleton className="h-4 w-4 rounded-[3px]" />
            </td>
          )}
          {drawnColumns.map((column, columnIndex) => (
            <td key={column.key} className={cn(rowCellPadding, cellClip, widthClass(column), column.className)}>
              <div className={cn("flex", column.align === "right" && "justify-end")}>
                <Skeleton className={cn("h-3", columnIndex === 0 ? (index % 2 === 0 ? "w-3/4" : "w-3/5") : "w-1/2")} />
              </div>
            </td>
          ))}
          {rowActions && (
            <td className={cn(rowCellPadding, "text-right")}>
              <div className="flex justify-end">
                <Skeleton className="size-7" />
              </div>
            </td>
          )}
        </tr>
      ))
    : null;

  let body: ReactNode;
  if (isLoading && renderGrid && renderGridSkeleton) {
    body = renderGridSkeleton(state.pageSize);
  } else if (isLoading && renderGrid) {
    body = <CardGridSkeleton count={cardSkeletonCount(state.pageSize)} label={t_i18n("Loading")} className={gridClassName} />;
  } else if (!isLoading && isError) {
    body = (
      <div role="alert" className="flex flex-col items-center gap-3 py-10 text-center content-compact text-default-secondary">
        <p>{errorLabel ?? t_i18n("Could not load these rows.")}</p>
        {onRetry && (
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>
            {t_i18n("Retry")}
          </Button>
        )}
      </div>
    );
  } else if (!isLoading && rows.length === 0 && !trailingRows && !remainderRows?.length && !narrowing && (emptyContent || emptyLabel)) {
    body = emptyContent ?? <p className="py-10 text-center content-compact text-default-secondary">{emptyLabel}</p>;
  } else if (renderGrid) {
    body = (
      <>
        {current.rows.length === 0 && narrowing ? (
          <p data-testid="data-table-no-results" className="px-3 py-6 text-center text-default-secondary">
            {noResults}
          </p>
        ) : (
          // `fitGrid`: the card list's own scroll area, bounded like the table's (One scrollbar,
          // #11233), so a page's main grid of fixed-height rows fits the window and scrolls
          // inside (UX round 1 on #11917, B3); a grid in a widget hugs its cards (null: no bound).
          <div
            ref={fitGrid ? setScrollArea : undefined}
            data-testid="data-table-grid"
            data-scroll-fit={!fitGrid ? undefined : scrollRoom === undefined ? "capped" : scrollRoom === null ? "page" : "viewport"}
            style={fitGrid && typeof scrollRoom === "number" ? { maxHeight: scrollRoom } : undefined}
            className={cn(fitGrid && typeof scrollRoom === "number" && "overflow-y-auto")}
          >
            {renderGrid(current.rows)}
          </div>
        )}
        {totalLine}
      </>
    );
  } else {
    body = (
      <>
        {/* The table's own scroll area, bounded to the room the window has left for it
            (useFitToViewport, #11233): `overflow-x-auto` alone makes the wrapper the sticky
            header's scroll box with no height, so the header scrolled away with the page
            (UX round 1 on #9434, B1), and a fixed `100dvh-12rem` guess left the page taller
            than the window, two scrollbars side by side. */}
        <div
          ref={setScrollArea}
          data-testid="data-table-scroll"
          data-scroll-fit={scrollRoom === undefined ? "capped" : scrollRoom === null ? "page" : "viewport"}
          style={typeof scrollRoom === "number" ? { maxHeight: scrollRoom } : undefined}
          className={cn(
            "@container overflow-auto",
            // Not measured yet, or outside the app's page (a dialog): the former cap.
            scrollRoom === undefined && "max-h-[calc(100dvh-12rem)]",
            framed && "rounded-sm border border-elevation-subtle-soft",
          )}
        >
          <table
            data-layout={layout}
            className={cn("w-full", layout === "fixed" && "table-fixed", density === "comfortable" ? "content-base" : "content-compact")}
          >
            <thead ref={setTableHead} data-testid="data-table-head" className="sticky top-0 z-10 bg-elevation-hover text-default-secondary">
              <tr ref={setHeadRow}>
                {selection && (
                  <th scope="col" className="w-8 pl-3 pr-0 py-2">
                    {pageKeys.length > 0 && (
                      <SelectionBox
                        checked={pageSelected === 0 ? false : pageSelected === pageKeys.length ? true : "mixed"}
                        onToggle={togglePage}
                        label={t_i18n("Select every row on this page")}
                      />
                    )}
                  </th>
                )}
                {drawnColumns.map((column) => (
                  <TableSortHeader
                    key={column.key}
                    label={column.header}
                    hint={column.hint}
                    required={column.required}
                    sortKey={column.sortValue ? column.key : undefined}
                    activeKey={sortColumn?.key ?? (server ? (state.sort?.key ?? null) : null)}
                    direction={state.sort?.direction}
                    onSort={column.sortValue ? onSort : undefined}
                    align={column.align}
                    truncate={fixedLayout}
                    style={column.width !== undefined ? { width: column.width } : undefined}
                    className={cn(
                      density === "compact" && "px-3",
                      widthClass(column),
                      column.headerClassName,
                    )}
                  />
                ))}
                {rowActions && (
                  <th
                    scope="col"
                    className={cellPadding}
                    data-testid="data-table-actions-header"
                    style={layout === "fixed" ? { width: rowActionsWidth } : undefined}
                  >
                    <span className="sr-only">{t_i18n("Actions")}</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {!skeletonRows && current.rows.length === 0 && narrowing && (
                <tr>
                  <td colSpan={spanned} className="p-0">
                    {fullWidth(
                      noResults,
                      "px-3 py-6 text-center text-default-secondary",
                      "data-table-no-results",
                    )}
                  </td>
                </tr>
              )}
              {skeletonRows ??
                (groupBy && groupCounts
                  ? current.rows.flatMap((row, index) => {
                      const value = groupBy.value(row);
                      const starts = index === 0 || groupBy.value(current.rows[index - 1]) !== value;
                      if (!starts) return [renderRow(row)];
                      return [
                        <tr key={`group:${value}`} data-testid="data-table-group-row" className="border-t border-elevation-subtle-soft bg-elevation-hover">
                          <td colSpan={spanned} className="p-0">
                            {fullWidth(
                              <span className="flex items-center gap-2">
                                <span className="min-w-0 truncate font-medium text-default-primary">{groupBy.label(value)}</span>
                                <span className="shrink-0 tabular-nums text-default-secondary">{formatGroupCount(groupCounts.get(value) ?? 0)}</span>
                              </span>,
                              "px-3 py-1.5 content-compact",
                            )}
                          </td>
                        </tr>,
                        renderRow(row),
                      ];
                    })
                  : current.rows.map(renderRow))}
              {!skeletonRows &&
                current.page === current.lastPage &&
                !narrowing &&
                remainderRows?.map((item) => (
                  <tr
                    key={`remainder:${item.key}`}
                    data-testid={item.testId}
                    className="border-t border-elevation-subtle-soft text-default-secondary"
                  >
                    {selection && <td className="w-8 pl-3 pr-0 py-2" />}
                    {drawnColumns.map((column) => (
                      <td
                        key={column.key}
                        className={cn(cellPadding, column.align === "right" && "text-right tabular-nums", cellClip, widthClass(column), column.className)}
                      >
                        {cellContent(column.key === firstColumnKey ? item.label : column.cell(item.row))}
                      </td>
                    ))}
                    {rowActions && <td className={cellPadding} />}
                  </tr>
                ))}
              {!skeletonRows && current.page === current.lastPage && !narrowing && trailingRows}
            </tbody>
          </table>
        </div>
        {totalLine}
      </>
    );
  }

  const hasSearch = !search && (!!searchFields || !!server);
  // The total, always at the toolbar's right end (standard v2): "N", or "n of N" when the
  // search, the column filters or the page's own scope, tags and filters narrow the list.
  const everything = Math.max(totalAll ?? (server ? server.total : rows.length), total);
  // No page answered yet: the count is unknown, never "0". The first load draws a
  // placeholder in the chip and a read error drops it (the error row says why);
  // a refetch keeps the last count (UX r1 A1 on #11012).
  // The shared TotalChip, the one skin of every list's total (UX r1 B3 on #11192).
  // A list the page's scope, tags or a search can narrow may read "n of N": the loading placeholder
  // keeps that width (UX r3 B5 on #11349); loaded, the chip fits its text (UX r5 B8 on #11349).
  const mayNarrow = narrowing || (!!scope && scope.value !== "all") || (tags?.selected.length ?? 0) > 0;
  const totalChip = isError ? null : (
    <TotalChip testId="data-table-total" total={everything} shown={total} loading={isLoading} reserve={mayNarrow ? "narrowed" : "count"} />
  );
  // A table narrower than 896 px (a card at 1024) draws Columns and Export CSV as icon
  // buttons with their name in a tooltip, and the page size as its number: the full
  // labels pushed them to a second row at 1024 in en and fr (UX r2 B1 on #10336).
  // A one-row toolbar whose full labels were measured past its box, at this lead width or a
  // wider one and this box width or a narrower one, draws the compact controls too: the Tasks
  // lead (Status, Quick filters) left 852 px at 1440 and the full-label right group ran the
  // row 84 px past its box (merge queue E2E on #1004). The room the full labels need depends
  // on the row's controls, so it is measured, never a constant.
  const leadNeedsCompact =
    filtersInPill &&
    boxWidth !== null &&
    fullLabelsOverflow !== null &&
    leadWidth >= fullLabelsOverflow.lead &&
    boxWidth <= fullLabelsOverflow.box;
  const compactToolbar = boxWidth !== null && (boxWidth < COMPACT_TOOLBAR_BELOW_PX || leadNeedsCompact);
  // A one-row toolbar whose lead leaves less room than its compact controls need
  // wraps, so the right group (total, page size, view) takes its own line instead of
  // pushing the row past its box: the fleet Worksets list at 1080 overflowed by up to
  // 137 px behind the 248 px list switcher (UX r3 B3 on #11339).
  const leadCrowds = filtersInPill && boxWidth !== null && leadWidth > 0 && boxWidth - leadWidth < ONE_ROW_TOOLBAR_MIN_PX;
  // A selection adds its count and bulk actions to the row: the one-row toolbar then
  // wraps, so the right group (total, page size, export) never runs past the box
  // (the Cases list at 1080 ended at 1167 px with two rows selected, UX r2 B6 on #11666).
  const selectionShown = Boolean(selection) && selected.length > 0;
  // A compact one-row toolbar that still ran past its box at this lead width or a wider one
  // and this box width or a narrower one wraps too: the page's own controls (the Case opening
  // switch) count in the row, and the Cases list in ja at 1080 ran 21 px past it (UX r3 B1 on #1963).
  // Only a row carrying a page's own controls wraps: Worksets at 1024 has none and keeps one row.
  const compactCrowds =
    filtersInPill &&
    Boolean(toolbar) &&
    compactToolbar &&
    boxWidth !== null &&
    compactOverflow !== null &&
    leadWidth >= compactOverflow.lead &&
    boxWidth <= compactOverflow.box;
  const toolbarWraps = !filtersInPill || leadCrowds || selectionShown || compactCrowds;
  // Before paint: a one-row toolbar that runs past its box records the lead and box widths, so
  // the next render draws the compact controls, and a compact row that still runs past it wraps
  // its right group to its own line (no flash).
  useLayoutEffect(() => {
    if (!toolbarRow || !filtersInPill || toolbarWraps || boxWidth === null) return;
    if (toolbarRow.scrollWidth <= toolbarRow.clientWidth + 1) return;
    if (compactToolbar) setCompactOverflow({ lead: leadWidth, box: boxWidth });
    else setFullLabelsOverflow({ lead: leadWidth, box: boxWidth });
  }, [toolbarRow, filtersInPill, toolbarWraps, compactToolbar, boxWidth, leadWidth]);
  // A card list (`renderGrid`) has no header row: its page box is the first control of the
  // toolbar, never a row of its own above the cards (Sam on /operations/shared-state, 14 Nov 2026).
  // Its slot holds its width during the first read, so the toolbar does not move when it lands.
  const gridPageBox = Boolean(selection && renderGrid) && (isLoading || pageKeys.length > 0);
  const toolbarShown =
    showToolbar &&
    (hasSearch || search || filters.length > 0 || exportName || toolbar || toolbarLead || paginate || hideableColumns.length > 0 || scope || tags || filterPills || view || gridPageBox);

  return (
    <div ref={setBox} className={cn("space-y-3", fill && "flex flex-1 flex-col")}>
      {toolbarShown && (
        <div
          ref={setToolbarRow}
          data-testid="data-table-toolbar"
          data-lead-crowds={leadCrowds ? "true" : undefined}
          data-wraps={toolbarWraps ? "true" : undefined}
          className={cn("flex items-center gap-2", toolbarWraps ? "flex-wrap" : "min-w-0 flex-nowrap")}
        >
          {gridPageBox && (
            <div data-testid="data-table-grid-page-select" className={cn(TOOLBAR_CONTROL_HEIGHT, "flex w-6 shrink-0 items-center justify-center")}>
              {isLoading ? (
                <Skeleton className="h-4 w-4 rounded-[3px]" />
              ) : (
                // The span carries the tooltip's hover: SelectionBox takes no ref of its own.
                <Tip content={t_i18n("Select every row on this page")}>
                  <span className="inline-flex">
                    <SelectionBox
                      checked={pageSelected === 0 ? false : pageSelected === pageKeys.length ? true : "mixed"}
                      onToggle={togglePage}
                      label={t_i18n("Select every row on this page")}
                    />
                  </span>
                </Tip>
              )}
            </div>
          )}
          {toolbarLead && (
            <div ref={setLead} className="flex shrink-0 items-center">
              {toolbarLead}
            </div>
          )}
          {hasSearch && (
            <SearchInput
              // The short "Search..." placeholder never clips in a card at 1024 (a long fr
              // placeholder needs 286 px); the full wording stays the accessible name.
              // Toolbar mode grows the field into the free width and floors it at its placeholder.
              aria-label={searchPlaceholder}
              value={searchText}
              onChange={setSearchText}
              toolbar
              compact={compactSearch || (filtersInPill && compactToolbar)}
              inputClassName={cn(TOOLBAR_CONTROL_HEIGHT, "content-compact")}
            />
          )}
          {search && (
            <SearchInput
              aria-label={search.placeholder}
              value={search.value}
              onChange={search.onChange}
              toolbar
              compact={compactSearch || compactToolbar}
              inputClassName={cn(TOOLBAR_CONTROL_HEIGHT, "content-compact")}
            />
          )}
          {scope && (
            <VisibilityFilter
              value={scope.value}
              onChange={scope.onChange}
              counts={scope.counts}
              hidden={scope.hidden}
              tips={scope.tips}
              iconOnly={compactToolbar}
              overflow={filtersInPill ? "menu" : undefined}
              // The counts of the first read are unknown, never "0": placeholders hold their width.
              pending={isLoading}
            />
          )}
          {filtersInPill ? (
            (filters.length > 0 || filterPills) && (
              <FiltersPill
                label={t_i18n("Filters")}
                count={filterCount}
                testId="data-table-filters"
                panelClassName={faceted ? FACET_GRID_PANEL : undefined}
              >
                <FacetColumns faceted={faceted}>
                {filters.map((filter, index) =>
                  filterOptions[index].length === 0 ? null : (
                    <div
                      key={filter.key}
                      role="group"
                      aria-labelledby={`${filterHeadingId}-${filter.key}`}
                      className={cn("space-y-1", faceted && "mb-3 min-w-0 break-inside-avoid")}
                      data-facet={faceted ? filter.key : undefined}
                    >
                      <Label id={`${filterHeadingId}-${filter.key}`}>{t_i18n(filter.label)}</Label>
                      <FilterOptionList
                        options={filterOptions[index]}
                        selected={state.filters[filter.key] ?? []}
                        counts={facetCounts?.[filter.key]}
                        onChange={(values) => update({ filters: { ...state.filters, [filter.key]: values } })}
                      />
                    </div>
                  ),
                )}
                </FacetColumns>
                {filterPills}
              </FiltersPill>
            )
          ) : (
            <>
              {filters.map((filter, index) => (
                <FilterPill
                  key={filter.key}
                  label={t_i18n(filter.label)}
                  options={filterOptions[index]}
                  selected={state.filters[filter.key] ?? []}
                  onChange={(values) => update({ filters: { ...state.filters, [filter.key]: values } })}
                />
              ))}
              {filterPills}
            </>
          )}
          {/* The Tags pill keeps its place during the first read: without it the loading toolbar
              fits one row and the loaded one wraps to two, moving the list 40 px (UX r2 B5 on #11349). */}
          {tags && <TagFilter collapsed pending={isLoading && tags.all.length === 0} allTags={tags.all} selected={tags.selected} onChange={tags.onChange} />}
          {clearable &&
            // A one-row toolbar in a narrow box draws the clear control as an icon with
            // its name in a tooltip: the full label (173 px in ja) squeezed the scope
            // switcher onto the Filters pill at 1080 (UX r2 B2 on #11339).
            (filtersInPill && compactToolbar ? (
              <Tip content={t_i18n("Clear all filters")}>
                <FilterClearButton
                  className={cn(TOOLBAR_CONTROL_HEIGHT, "w-8 justify-center px-0")}
                  aria-label={t_i18n("Clear all filters")}
                  data-compact="true"
                  onClick={clearAll}
                >
                  <Icon name="x" size={16} aria-hidden />
                </FilterClearButton>
              </Tip>
            ) : (
              <FilterClearButton className={TOOLBAR_CONTROL_HEIGHT} onClick={clearAll}>
                {t_i18n("Clear all filters")}
              </FilterClearButton>
            ))}
          {selection && selectionShown && (
            <div data-testid="data-table-selection" className="flex flex-wrap items-center gap-2">
              <span className="content-compact whitespace-nowrap text-default-secondary">
                {t_i18n("{count} selected", { count: selection.count ?? selected.length })}
              </span>
              {selection.actions}
            </div>
          )}
          <div className={cn("ml-auto flex items-center gap-2", filtersInPill ? "shrink-0 flex-nowrap" : "flex-wrap")}>
            {totalChip}
            {/* A page's own toolbar controls follow the compact rule too (UX r2 B1 on #1963). */}
            <ToolbarCompactContext.Provider value={compactToolbar}>{toolbar}</ToolbarCompactContext.Provider>
            {renderGrid && sortableColumns.length > 0 && (
              <GridSort
                columns={sortableColumns.map((column) => ({ key: column.key, label: t_i18n(column.header) }))}
                sort={state.sort}
                onSort={(sort) => update({ sort })}
                onFirstSort={onSort}
              />
            )}
            {hideableColumns.length > 0 && !renderGrid && (
              <ColumnsPill
                iconOnly={compactToolbar}
                columns={hideableColumns.map((column) => ({ key: column.key, label: t_i18n(column.header) }))}
                hidden={hideableColumns.filter((column) => columnShown(column) === false).map((column) => column.key)}
                onToggle={toggleHidden}
              />
            )}
            {paginate && (
              <Select value={String(state.pageSize)} onValueChange={(value) => setPageSize(Number(value))}>
                <SelectTrigger
                  className={cn(TOOLBAR_CONTROL_HEIGHT, "w-auto gap-2 whitespace-nowrap content-compact", !compactToolbar && "min-w-[130px]")}
                  aria-label={t_i18n("Rows per page")}
                  data-compact={compactToolbar ? "true" : undefined}
                >
                  {compactToolbar ? <SelectValue>{String(state.pageSize)}</SelectValue> : <SelectValue />}
                </SelectTrigger>
                <SelectContent>
                  {PAGE_SIZES.map((size) => (
                    <SelectItem key={size} value={String(size)}>
                      {t_i18n("{count} per page", { count: size })}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            {exportName && (
              <Tip content={t_i18n("Download these rows, as filtered and sorted, as a CSV file")}>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className={cn(TOOLBAR_CONTROL_HEIGHT, compactToolbar ? "w-8 px-0" : "gap-1.5")}
                  aria-label={compactToolbar ? t_i18n("Export CSV") : undefined}
                  onClick={exportCsv}
                  disabled={isLoading || shown.length === 0}
                >
                  <Download className="h-3.5 w-3.5" aria-hidden />
                  {!compactToolbar && t_i18n("Export CSV")}
                </Button>
              </Tip>
            )}
            {view && <ViewToggle value={view.value} onChange={view.onChange} modes={view.modes} />}
          </div>
        </div>
      )}
      {filterChips.length > 0 && (
        <ul data-testid="data-table-filter-chips" aria-label={t_i18n("Active filters")} className="flex flex-wrap items-center gap-1.5">
          {filterChips.map((chip) => {
            // The chip text is one catalog string (UX r1 B1 on #2559): the locale's
            // separator (fr "Statut : Nouveau" with a no-break space) and its list
            // join; the label stays bold when the string opens with it.
            const text = t_i18n("{label}: {value}", { label: chip.label, value: formatList(chip.values, locale) });
            const rest = text.startsWith(chip.label) ? text.slice(chip.label.length) : null;
            return (
            <li key={chip.key}>
              <span className={cn(PANEL_CHIP_HEIGHT, "inline-flex max-w-[28rem] items-center gap-1 rounded-sm border border-accent/30 bg-accent/15 pl-2.5 pr-1 content-compact text-accent")}>
                <span className="min-w-0 truncate" title={text}>
                  {rest === null ? text : (
                    <>
                      <span className="font-medium">{chip.label}</span>
                      {rest}
                    </>
                  )}
                </span>
                <button
                  type="button"
                  aria-label={t_i18n("Remove filter {name}", { name: chip.label })}
                  onClick={() => update({ filters: { ...state.filters, [chip.key]: [] } })}
                  className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-sm hover:bg-accent/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                >
                  <Icon name="x" size={16} aria-hidden />
                </button>
              </span>
            </li>
            );
          })}
        </ul>
      )}
      {body}
    </div>
  );
}

/**
 * The faceted Filters panel (#2556): its groups packed into balanced columns of at least
 * 13 rem (a long group never stretches a row, as a grid would): four in its 60 rem, three
 * when the panel's fitted width (filterPanelMaxWidth) is narrower. The CTEM cases list's
 * 11 groups fit a 1080p viewport without the panel's own scroller (measured in the lab:
 * 958 px of content in a grid, 586 px in four columns). The columns live in an inner block
 * with no height cap: on a short viewport the panel scrolls down, never sideways (a capped
 * multi-column box spills its extra columns to the right).
 */
const FACET_GRID_PANEL = "w-[60rem]";

function FacetColumns({ faceted, children }: { faceted: boolean; children: ReactNode }) {
  return faceted ? (
    <div data-testid="data-table-facet-columns" className="columns-[13rem] gap-x-4">
      {children}
    </div>
  ) : (
    <>{children}</>
  );
}

/**
 * The one height of every toolbar control (search, filter pills, Clear all filters,
 * Columns, page size, Export CSV): 32 px. The pills alone were 28 px next to 32 px
 * fields (UX run 8d20d29c, design standards s.3). Exported so a control a caller
 * puts in the `toolbar` or `selection.actions` slot takes the same height.
 */
export const TOOLBAR_CONTROL_HEIGHT = "h-8";

/** Under this table width (Tailwind 4xl, a card at 1024) the toolbar draws its compact controls. */
const COMPACT_TOOLBAR_BELOW_PX = 896;

/**
 * The width the compact one-row toolbar (`filtersInPill`: search, folded scope, Filters
 * pill, icon clear, total, page size, view) needs: it fits 852 px in ja with a filter
 * (UX r3 on #11339). Less room after the host's lead wraps the row.
 */
const ONE_ROW_TOOLBAR_MIN_PX = 852;

/** A row that opens its detail route; the router hook lives here so a table without links needs no router. */
function HrefRow({
  href,
  className,
  testId,
  focusable,
  children,
}: {
  href: string;
  className: string;
  testId?: string;
  /** No link cell in the row: the row is the tab stop and Enter opens it. */
  focusable: boolean;
  children: ReactNode;
}) {
  const navigate = useNavigate();
  return (
    <tr
      data-testid={testId}
      className={className}
      onClick={() => navigate(href)}
      tabIndex={focusable ? 0 : undefined}
      onKeyDown={
        focusable
          ? (event) => {
              if (event.target !== event.currentTarget || event.key !== "Enter") return;
              event.preventDefault();
              navigate(href);
            }
          : undefined
      }
    >
      {children}
    </tr>
  );
}

/**
 * The sort of a card list (`renderGrid`): the column picker and the direction, in the
 * toolbar, since the cards draw no header to click. Same state as a header click
 * (`state.sort`, URL `sort`), so switching between table and cards keeps the order.
 */
function GridSort({
  columns,
  sort,
  onSort,
  onFirstSort,
}: {
  columns: { key: string; label: string }[];
  sort: DataTableSort | null;
  onSort: (sort: DataTableSort) => void;
  onFirstSort: (key: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const active = columns.some((column) => column.key === sort?.key) ? sort : null;
  const descending = active?.direction === "desc";
  return (
    <div className="flex items-center gap-1" data-testid="data-table-grid-sort">
      <Select value={active?.key ?? ""} onValueChange={(key) => onFirstSort(key)}>
        <SelectTrigger className={cn(TOOLBAR_CONTROL_HEIGHT, "w-auto min-w-[120px] gap-2 whitespace-nowrap content-compact")} aria-label={t_i18n("Sort by")}>
          <SelectValue placeholder={t_i18n("Sort by")} />
        </SelectTrigger>
        <SelectContent>
          {columns.map((column) => (
            <SelectItem key={column.key} value={column.key}>
              {column.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {active && (
        <Tip content={descending ? t_i18n("Descending") : t_i18n("Ascending")}>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className={cn(TOOLBAR_CONTROL_HEIGHT, "w-8 px-0")}
            aria-label={descending ? t_i18n("Descending") : t_i18n("Ascending")}
            onClick={() => onSort({ key: active.key, direction: descending ? "asc" : "desc" })}
            data-testid="data-table-grid-sort-direction"
          >
            {/* The arrows of TableSortHeader: one icon component and one size for a sort direction (UX r1 B4 on #11288). */}
            <Icon name={descending ? "arrow-down" : "arrow-up"} size={16} className="shrink-0" />
          </Button>
        </Tip>
      )}
    </div>
  );
}

function FilterPill({
  label,
  options,
  selected,
  onChange,
}: {
  label: string;
  options: DataTableFilterOption[];
  selected: string[];
  onChange: (values: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useFilterPopover(ref, open, setOpen);
  if (options.length === 0) return null;
  return (
    <div ref={ref} className="relative">
      <FilterPillTrigger
        icon={ListFilter}
        label={label}
        open={open}
        count={selected.length}
        className={TOOLBAR_CONTROL_HEIGHT}
        onClick={() => setOpen(!open)}
      />
      {open && (
        <FilterPillPanel role="group" aria-label={label} className="max-h-72 overflow-y-auto p-1">
          <FilterOptionList options={options} selected={selected} onChange={onChange} />
        </FilterPillPanel>
      )}
    </div>
  );
}

/** The toggles of one column filter: in its own pill, or a group of the "Filters" pill (`filtersInPill`). */
function FilterOptionList({
  options,
  selected,
  counts,
  onChange,
}: {
  options: DataTableFilterOption[];
  selected: string[];
  /** How many rows each value would keep (the faceted panel, #2556); none: no count drawn. */
  counts?: Record<string, number>;
  onChange: (values: string[]) => void;
}) {
  const { locale } = useFormatter();
  return (
    <>
      {options.map((option) => {
        const isSelected = selected.includes(option.value);
        return (
          <button
            type="button"
            key={option.value}
            aria-pressed={isSelected}
            onClick={() => onChange(toggleArrayItem(selected, option.value))}
            className={cn(
              "flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left content-compact transition-colors",
              isSelected ? "bg-accent/15 text-accent" : "hover:bg-elevation-hover",
            )}
          >
            <span className="min-w-0 flex-1 truncate" title={option.label}>
              {option.label}
            </span>
            {counts && (
              <span data-testid="filter-option-count" className={cn("shrink-0 tabular-nums", isSelected ? "text-accent" : "text-default-secondary")}>
                {formatCount(locale, counts[option.value] ?? 0)}
              </span>
            )}
            {isSelected && <Check className="h-3.5 w-3.5 shrink-0" aria-hidden />}
          </button>
        );
      })}
    </>
  );
}

function ColumnsPill({
  columns,
  hidden,
  onToggle,
  iconOnly = false,
}: {
  columns: { key: string; label: string }[];
  hidden: string[];
  onToggle: (key: string) => void;
  /** The icon alone, its name in a tooltip and kept as the accessible name (a narrow table). */
  iconOnly?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useFilterPopover(ref, open, setOpen);
  const label = t_i18n("Columns");
  const trigger = (
    <FilterPillTrigger
      icon={Columns3}
      label={iconOnly ? <span className="sr-only">{label}</span> : label}
      open={open}
      className={TOOLBAR_CONTROL_HEIGHT}
      count={hidden.filter((key) => columns.some((column) => column.key === key)).length}
      onClick={() => setOpen(!open)}
    />
  );
  return (
    <div ref={ref} className="relative">
      {iconOnly ? <Tip content={label}>{trigger}</Tip> : trigger}
      {open && (
        <FilterPillPanel role="group" aria-label={t_i18n("Shown columns")} className="w-56 p-1">
          {columns.map((column) => {
            const shown = !hidden.includes(column.key);
            return (
              <button
                type="button"
                key={column.key}
                aria-pressed={shown}
                onClick={() => onToggle(column.key)}
                className="flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left content-compact hover:bg-elevation-hover"
              >
                <span className="min-w-0 flex-1 truncate">{column.label}</span>
                {shown && <Check className="h-3.5 w-3.5 shrink-0" aria-hidden />}
              </button>
            );
          })}
        </FilterPillPanel>
      )}
    </div>
  );
}
