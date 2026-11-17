// SPDX-License-Identifier: Apache-2.0

/**
 * Sortable list view for asset index pages: a preset of `DataTable`.
 *
 * The card grid on Agents / Tools / Variables / Prompts / Skills / MCP Servers
 * / Integrations / Knowledge optimizes for looks at the cost of density: no
 * column headers means no sorting and no side-by-side comparison. This is the
 * dense counterpart: one row per entity, click-through to the same detail
 * route, and any column that declares a `sortValue` becomes sortable.
 *
 * It draws through the one records table (`components/ui/data-table.tsx`) in
 * its comfortable density, inside a card. A page on standard v2 hands its
 * search, scope, tags, own filter pills and view toggle to the table (`search`
 * and the others): the shared toolbar then draws them in the one order, with
 * the total, Columns and the page size (dev-docs/reference/data-tables.md).
 * A page not migrated yet passes none and keeps its toolbar above the card.
 *
 * Pair with `useViewMode()` + `<ViewToggle>` so the grid ⇄ list choice sticks.
 */
import { Children, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import {
  DataTable,
  type DataTableColumn,
  type DataTableScope,
  type DataTableSearch,
  type DataTableTags,
  type DataTableView,
  type RowLines,
  type SortDirection,
  type SortValue,
} from "@/components/ui/data-table";
import { cn } from "@/lib/utils";

export type { SortDirection, SortValue };

export interface EntityTableColumn<T> {
  /** Stable identifier — also the sort key. */
  key: string;
  /** English header text (translated by the table). */
  header: string;
  cell: (item: T) => ReactNode;
  /**
   * Provide to make the column sortable. Booleans sort `true` first ascending,
   * so a "Status" column puts enabled entities on top.
   */
  sortValue?: (item: T) => SortValue;
  /** Extra classes for the `<td>` (e.g. `whitespace-nowrap`). */
  className?: string;
  /** Extra classes for the `<th>`. */
  headerClassName?: string;
  /** Right-align the header and cells — use for counts and timestamps. */
  align?: "left" | "right";
  /**
   * Set on columns whose cells contain their own buttons/links (row actions,
   * reveal toggles). The table wraps the cell so clicks don't also trigger the
   * row's navigation.
   */
  interactive?: boolean;
  /** Hidden below this table width, brought back by the Columns chooser (DataTable `hideBelow`). */
  hideBelow?: DataTableColumn<T>["hideBelow"];
  /** false keeps the column out of the Columns chooser (default: every column but the first and the headerless ones). */
  hideable?: boolean;
}

interface EntityTableProps<T> {
  items: T[];
  columns: EntityTableColumn<T>[];
  rowKey: (item: T) => string;
  /**
   * Detail route for a row. The first column's content is rendered as a real
   * anchor (so middle-click and keyboard navigation work) and the whole row
   * becomes clickable.
   */
  rowHref?: (item: T) => string;
  /** Alternative to `rowHref` for rows that open a dialog instead of a route. */
  onRowClick?: (item: T) => void;
  /** Column sorted on first render — must name a column with a `sortValue`. */
  defaultSortKey?: string;
  defaultSortDirection?: SortDirection;
  /** Per-row classes, e.g. dimming disabled entities. */
  rowClassName?: (item: T) => string | undefined;
  /**
   * Rows to keep above every other row, whatever the active sort — favorites
   * on an index page. Applied *after* sorting and stable within each block, so
   * the pinned rows are themselves still ordered by the sorted column.
   */
  pinFirst?: (item: T) => boolean;
  /**
   * Standard v2: the page's search. Given, the table draws the shared toolbar (search,
   * scope, filter pills, tags, Clear all | total, Columns, page size, view) and pages.
   */
  search?: DataTableSearch;
  scope?: DataTableScope;
  tags?: DataTableTags;
  /** Typed filter pills of the page (a property filter, a category pill). */
  filterPills?: ReactNode;
  view?: DataTableView;
  /**
   * Grid view of the page (the view toggle on grid): the paged rows as cards under the same
   * toolbar, total and pager; drawn bare, the cards being their own surfaces.
   */
  renderGrid?: (items: T[]) => ReactNode;
  /** The grid class of `renderGrid` when it is not CARD_GRID, for its loading cards. */
  gridClassName?: string;
  /**
   * The first read of `renderGrid` when its cards are not the default asset card: the
   * page draws its own card skeleton (its card anatomy and height), so the cards land on
   * their placeholders (#11345).
   */
  renderGridSkeleton?: (pageSize: number) => ReactNode;
  /** Every record before the page's scope, tags and filters: the total reads "n of N". */
  totalAll?: number;
  /** Filters the page holds (scope off its landing, tags, properties): counted for Clear all. */
  externalFilterCount?: number;
  onClearAll?: () => void;
  /** Per-user key of the page size and the shown columns. */
  storageKey?: string;
  /** One-line or two-line rows (default 2: an icon tile, a name and a secondary line). */
  rowLines?: RowLines;
  emptyLabel?: string;
  emptyContent?: ReactNode;
  noResultsLabel?: string;
  /**
   * The first read is in flight: the table draws its real toolbar and head with
   * window-filling skeleton rows at its own row height (#11345), never a generic
   * page skeleton, so nothing moves when the rows land.
   */
  isLoading?: boolean;
}

/** The first page size of an asset list on the standard toolbar (one of the table's PAGE_SIZES). */
const ENTITY_PAGE_SIZE = 25;

export function EntityTable<T>({
  items,
  columns,
  rowKey,
  rowHref,
  onRowClick,
  defaultSortKey,
  defaultSortDirection = "asc",
  rowClassName,
  pinFirst,
  search,
  scope,
  tags,
  filterPills,
  view,
  renderGrid,
  gridClassName,
  renderGridSkeleton,
  totalAll,
  externalFilterCount,
  onClearAll,
  storageKey,
  rowLines = 2,
  emptyLabel = "",
  emptyContent,
  noResultsLabel,
  isLoading = false,
}: EntityTableProps<T>) {
  const standard = search !== undefined;
  // A first click sorts ascending on every column, as the asset pages always did. On the
  // standard toolbar every column but the first is in the Columns chooser.
  const tableColumns = useMemo<DataTableColumn<T>[]>(
    () => columns.map((column) => ({ ...column, firstSort: "asc", hideable: standard ? column.hideable !== false : false })),
    [columns, standard],
  );
  const table = (
        <DataTable
          rows={items}
          columns={tableColumns}
          rowKey={rowKey}
          rowHref={rowHref}
          onRowClick={onRowClick}
          defaultSort={defaultSortKey ? { key: defaultSortKey, direction: defaultSortDirection } : undefined}
          rowClassName={rowClassName}
          pinFirst={pinFirst}
          paginate={standard}
          showToolbar={standard}
          search={search}
          scope={scope}
          tags={tags}
          filterPills={filterPills}
          view={view}
          renderGrid={renderGrid}
          gridClassName={gridClassName}
          renderGridSkeleton={renderGridSkeleton}
          totalAll={totalAll}
          externalFilterCount={externalFilterCount}
          onClearAll={onClearAll}
          storageKey={storageKey}
          defaultPageSize={ENTITY_PAGE_SIZE}
          rowLines={rowLines}
          density="comfortable"
          framed={false}
          emptyLabel={emptyLabel}
          emptyContent={emptyContent}
          noResultsLabel={noResultsLabel}
          isLoading={isLoading}
        />
  );
  // The grid's cards are their own surfaces: no card around them.
  if (renderGrid) return table;
  return (
    <Card>
      <CardContent className="p-0">{table}</CardContent>
    </Card>
  );
}

/** The width line 2 keeps for the description once badges share it (about eight characters). */
const LINE_TWO_DESCRIPTION_MIN = 64;

/** The width of the design-system focus ring (`ring-2`) drawn around the "+N" marker. */
const FOCUS_RING_WIDTH = 2;

/**
 * Line 2 of a two-line row: the badges, then `children` (the description).
 * One line that never wraps, one fixed height (`h-6`, a badge's height).
 *
 * A badge is never cut (UX round 2 on #10920, B1: "Shared - 1 way" was cut
 * mid-word and the description squeezed to 0 px on a row with three badges).
 * While the badges overflow the line, less the room kept for the description,
 * the last ones fold into a "+N" badge: focusable, its tooltip draws the
 * hidden badges whole. Same fold as `TagList`, measured again when the line's
 * width changes.
 *
 * The line itself never clips (UX round 3 on #10920, B1: `overflow-hidden` on
 * this 24 px line cut the top and bottom of the marker's 2 px focus ring). The
 * badges fit by the fold, which keeps the ring's width free at the end of the
 * badges, and the description cuts itself with `truncate`, so nothing overflows
 * the line and the ring draws whole in the row's vertical room.
 */
function LineTwoBadges({
  hasBadges,
  badges,
  children,
}: {
  hasBadges: boolean;
  badges?: ReactNode;
  children: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const items = Children.toArray(badges);
  const count = items.length;
  const lineRef = useRef<HTMLDivElement>(null);
  const badgesRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(count);
  const [layoutPass, setLayoutPass] = useState(0);
  const reserve = children ? LINE_TWO_DESCRIPTION_MIN : 0;

  // A new badge set starts from the widest layout again.
  useLayoutEffect(() => {
    setVisible(count);
  }, [count]);

  // Fold one more badge into "+N" while the badges (marker included) do not fit.
  useLayoutEffect(() => {
    const line = lineRef.current;
    const row = badgesRef.current;
    if (!line || !row || visible <= 0) return;
    if (row.offsetWidth > Math.max(0, line.clientWidth - reserve - FOCUS_RING_WIDTH)) setVisible((v) => Math.max(0, v - 1));
  }, [visible, layoutPass, count, reserve]);

  // The line's width comes from the column: when it changes, measure again.
  useLayoutEffect(() => {
    const line = lineRef.current;
    if (!line || typeof ResizeObserver === "undefined") return;
    let width = line.clientWidth;
    const observer = new ResizeObserver(() => {
      if (line.clientWidth === width) return;
      width = line.clientWidth;
      setVisible(count);
      setLayoutPass((pass) => pass + 1);
    });
    observer.observe(line);
    return () => observer.disconnect();
  }, [count]);

  const shown = items.slice(0, Math.min(visible, count));
  const hidden = items.slice(shown.length);

  // The tooltip lists the hidden badges as plain text, read from their render
  // in a `display: none` source (UX round 4 on #10920): text-default-primary on
  // bg-tooltip keeps 4.5:1 in both themes, where the badges' own row colours
  // fell to 2.98:1 (B1), and a label wraps whole instead of a nested `Tip`
  // trigger cutting it (B2: the design-system Tooltip holds no interactive content).
  // A hidden badge can change its own text after mount without this line
  // re-rendering (UX round 5 on #10920, B1: a relay badge renders its fallback,
  // then its name once its own query answers on a cold load), so a
  // MutationObserver on the source reads the labels again on every change.
  const hiddenSourceRef = useRef<HTMLDivElement>(null);
  const [hiddenLabels, setHiddenLabels] = useState<string[]>([]);
  useLayoutEffect(() => {
    const source = hiddenSourceRef.current;
    const read = () => {
      const next = source
        ? Array.from(source.children, (child) => (child.textContent ?? "").trim()).filter(Boolean)
        : [];
      setHiddenLabels((prev) => (prev.join("\u0000") === next.join("\u0000") ? prev : next));
    };
    read();
    if (!source || typeof MutationObserver === "undefined") return;
    const observer = new MutationObserver(read);
    observer.observe(source, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  });

  return (
    <div ref={lineRef} data-testid="entity-name-line2" className="flex h-6 min-w-0 flex-nowrap items-center gap-1">
      {hasBadges && (
        <div ref={badgesRef} data-testid="entity-name-badges" className="flex shrink-0 flex-nowrap items-center gap-1">
          {shown}
          {hidden.length > 0 && (
            <Tip
              // Always an element, so the trigger keeps one tree (and its focus)
              // while the labels are read on the first layout pass.
              content={
                <ul data-testid="entity-name-badges-more-list" className="flex flex-col gap-0.5">
                  {hiddenLabels.map((label, index) => (
                    <li key={`${index}-${label}`} className="whitespace-normal break-words">{label}</li>
                  ))}
                </ul>
              }
            >
              <BadgeFocusTrigger
                data-testid="entity-name-badges-more"
                aria-label={t_i18n("{count} more", { count: hidden.length })}
              >
                <Badge variant="outline" className="content-compact">+{hidden.length}</Badge>
              </BadgeFocusTrigger>
            </Tip>
          )}
        </div>
      )}
      {hidden.length > 0 && (
        <div ref={hiddenSourceRef} data-testid="entity-name-badges-hidden-source" aria-hidden="true" className="hidden">
          {hidden}
        </div>
      )}
      {children}
    </div>
  );
}

/**
 * Standard first-column cell: icon tile + name + truncated description, with
 * optional badges. Capped in width so long names truncate instead of
 * squeezing every other column off the table.
 *
 * Badges trail the name by default. A page whose rows carry several badges
 * sets `badgesPlacement="below"`: they move to the second line, before the
 * description, and the name keeps a minimum width, so a row with three badges
 * still shows its name at 1024 px. Trailing, they are `shrink-0` and
 * take the width from the name.
 *
 * Standard v2 (Sam, 10 Nov 2026): never a third line. Below, every row is the
 * same two lines (the name, then the badges and the description on one line
 * that never wraps, the description cut with its title), so one table has one
 * row height, the two-line height of `ROW_HEIGHT`. A row with no badge and no
 * description keeps an empty second line, so its name sits where the others do.
 */
export function EntityNameCell({
  icon,
  name,
  description,
  badges,
  badgesPlacement = "trailing",
  muted = false,
}: {
  /** Rendered inside the icon tile — a lucide icon element or an `<img>`. */
  icon?: ReactNode;
  name: string;
  description?: string | null;
  /** Badges (company-managed, shared, …). An empty array (or one of only false / null) renders no badge row. */
  badges?: ReactNode;
  /** `trailing` (default): one line after the name. `below`: a wrapping line under it. */
  badgesPlacement?: "trailing" | "below";
  /** Dim the icon tile for disabled entities. */
  muted?: boolean;
}) {
  const below = badgesPlacement === "below";
  // An empty list is no badge: a fragment is always truthy, so a row with no
  // badge drew an empty row with its 4 px top margin. Callers that build their badges conditionally pass an array;
  // `Children.toArray` drops its false, null and undefined entries.
  const hasBadges = Children.toArray(badges).length > 0;
  return (
    <div
      data-testid="entity-name-cell"
      className={cn(
        "flex items-center gap-2.5 min-w-0",
        // Below xl the secondary columns are hidden and the card is narrower:
        // a 360 px cap plus the cell padding pushed the table 8 px past its
        // card at 1024 px with a long name.
        below ? "min-w-[12rem] max-w-[280px] xl:max-w-[360px]" : "max-w-[360px]",
      )}
    >
      {icon && (
        <div
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-sm",
            muted ? "bg-elevation-hover" : "bg-accent/10",
          )}
        >
          {icon}
        </div>
      )}
      <div className={cn("min-w-0", below && "flex-1")}>
        <div data-testid="entity-name-text" className="min-w-0">
          <p data-testid="entity-name" className="font-medium truncate" title={name}>{name}</p>
          {below ? (
            // Line 2: the badges, then the description cut with its title. One line, never wraps,
            // one fixed height (h-6, a badge's height) badge or not, so every row of the table
            // renders at ROW_HEIGHT[2] (UX round 1 on #10920, B1: 20 px without a badge, 24 with).
            // A badge is never cut (UX round 2 on #10920, B1): the badges that do not fit fold
            // into a "+N" badge whose tooltip shows them (LineTwoBadges).
            <LineTwoBadges hasBadges={hasBadges} badges={badges}>
              {description ? (
                <p data-testid="entity-description" className="min-w-0 truncate content-compact text-default-secondary" title={description}>
                  {description}
                </p>
              ) : !hasBadges && (
                <p data-testid="entity-description-slot" aria-hidden="true" className="content-compact">{"\u00a0"}</p>
              )}
            </LineTwoBadges>
          ) : description && (
            // The line truncates: the whole description travels as a title, as the name does.
            <p data-testid="entity-description" className="content-compact text-default-secondary truncate" title={description}>
              {description}
            </p>
          )}
        </div>
      </div>
      {hasBadges && !below && (
        <div data-testid="entity-name-badges" className="flex items-center gap-1.5 shrink-0">{badges}</div>
      )}
    </div>
  );
}
