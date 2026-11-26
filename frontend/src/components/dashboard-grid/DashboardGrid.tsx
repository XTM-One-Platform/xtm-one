/**
 * A dashboard as a grid of widgets each person builds live: the CTEM command
 * center (XTM One home) and the Copilot dashboard, each with its own widgets
 * (`WidgetRegistry`).
 *
 * In edit mode the real widgets stay on screen: the person drags them to move
 * them, drags their corner to resize them, removes them and adds them back
 * from the catalog of removed widgets. Every change is handed to `onChange`,
 * which the dashboard saves to their profile at once. Out of edit mode the
 * widgets are what they always were: clickable, and fixed.
 *
 * Below `GRID_MIN_WIDTH` there is no room for the grid: the widgets are
 * stacked in reading order at their natural height, and cannot be rearranged.
 *
 * A custom dashboard (`components/custom-dashboard`) draws its widgets on the
 * same grid: its widgets are its own (`onRemove` deletes one, there is no
 * catalog of removed widgets nor a default to reset to), its studio picks a
 * widget by a click on it (`selection`) and has its own toolbar
 * (`hideEditBar`), and it fits narrower containers (`minWidth`).
 */
import { useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import ReactGridLayout, { useContainerWidth, type Layout } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "./dashboard-grid.css";
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { DashboardLayout } from "@/types";
import {
  DEFAULT_GRID_ROWS,
  GRID_COLUMNS,
  MAX_WIDGET_HEIGHT,
  addWidget,
  isDefaultLayout,
  readingOrder,
  removeWidget,
  withGridItems,
  type GridRows,
  type WidgetRegistry,
} from "./layout";
import { keyboardMove, keyboardResize } from "./grid-keyboard";
import { WidgetSizeContext } from "./widget-size-context";

/** Narrowest container the grid is drawn in, in pixels: below it, a two-column
 *  widget (a KPI card) would be narrower than its content, so the widgets are
 *  stacked instead. */
const GRID_MIN_WIDTH = 960;

/** Elements inside a widget's edit overlay that must not start a drag. */
const NO_DRAG = "dashboard-widget-no-drag";

/** The widget's unclipped wrapper. A KPI tile given `buttonRoleProps` fills
 *  the widget scroller, whose overflow clips the tile's own focus ring, so the
 *  wrapper draws the same ring (BUTTON_ROLE_FOCUS_RING: 2 px offset plus 2 px
 *  `ring-focus`) while the widget's own root is a button-role element with
 *  keyboard focus (the `data-card-link` pattern of
 *  index.css). `>*>` keeps it to that root: a button deeper in a widget (a
 *  list row) keeps its own ring. Gate: lib/button-role-focus-ring.test.ts. */
const WIDGET_WRAPPER =
  "relative h-full rounded-sm has-[>*>[role=button]:focus-visible]:ring-2 has-[>*>[role=button]:focus-visible]:ring-focus has-[>*>[role=button]:focus-visible]:ring-offset-2 has-[>*>[role=button]:focus-visible]:ring-offset-focus";

const sameItems = (a: DashboardLayout, b: DashboardLayout) =>
  JSON.stringify(readingOrder(a.items)) === JSON.stringify(readingOrder(b.items));

/** The catalog menu's row glyph: OverflowMenu renders `icon` as a component
 *  given its className, so the library icon is wrapped once here. */
const AddGlyph = ({ className }: { className?: string }) => <Icon name="plus" size={16} className={className} />;

const ARROWS: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};

/** The button that turns edit mode on, at the top right of the dashboard;
 *  `tooltip` says what building this dashboard means (translated). */
export function CustomizeButton({ onClick, tooltip, className }: { onClick: () => void; tooltip: string; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Tip content={tooltip}>
      <button
        type="button"
        onClick={onClick}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm px-2 py-0.5 text-[11px] font-medium text-default-secondary hover:text-default-primary hover:bg-elevation-hover transition-colors",
          className,
        )}
      >
        <Icon name="layout-dashboard" size={16} />
        <span className="hidden sm:inline">{t_i18n("Customize")}</span>
      </button>
    </Tip>
  );
}

export function DashboardGrid<Id extends string>({
  registry,
  layout,
  widgets,
  labels,
  title,
  editing,
  onChange,
  onDone,
  minWidth = GRID_MIN_WIDTH,
  onRemove,
  selection,
  hideEditBar = false,
  configure,
  rows = DEFAULT_GRID_ROWS,
}: {
  /** The dashboard's widgets, with their default places and minimum sizes. */
  registry: WidgetRegistry<Id>;
  layout: DashboardLayout<Id>;
  /** What each widget draws; it fills the cell the grid gives it. */
  widgets: Record<Id, ReactNode>;
  /** Each widget's name, translated (edit overlay and catalog). */
  labels: Record<Id, string>;
  /** The edit bar's title, translated ("Customize your command center"). */
  title: string;
  editing: boolean;
  /** A new layout to save; `null` resets the default one. */
  onChange: (layout: DashboardLayout<Id> | null) => void;
  onDone: () => void;
  /** Narrowest container the grid is drawn in, in pixels. */
  minWidth?: number;
  /** Removing a widget deletes it: no catalog of removed widgets, no default to reset to. */
  onRemove?: (id: Id) => void;
  /** The widget picked, outlined; a click on a widget in edit mode picks it. */
  selection?: { selectedId: string | null; onSelect: (id: Id) => void };
  /** No edit bar: the page around the grid has its own. */
  hideEditBar?: boolean;
  /** The widgets that have settings of their own (CTEM command center,
   *  dev-docs/ctem-stage-viz.md 8.3): a gear on their edit handle opens them,
   *  and "Overridden" marks the ones that set a value of their own. */
  configure?: { ids: readonly Id[]; overridden: ReadonlySet<Id>; onOpen: (id: Id) => void };
  /** The rows' height and gap: the platform's dashboards keep their own, a custom dashboard takes OpenCTI's. */
  rows?: GridRows;
}) {
  const { t_i18n } = useFormatter();
  const { width, mounted, containerRef } = useContainerWidth({ measureBeforeMount: true });
  const fitsGrid = width >= minWidth;
  const ownWidgets = onRemove !== undefined;

  const gridLayout: Layout = useMemo(
    () =>
      layout.items.map((item) => ({
        ...item,
        minW: registry[item.i].minW,
        // A page may draw a widget shorter than its minimum while it holds one
        // line (the CTEM Fleet activity card with every fleet off): the grid
        // keeps that height instead of raising it.
        minH: Math.min(registry[item.i].minH, item.h),
        maxH: MAX_WIDGET_HEIGHT,
      })),
    [layout.items, registry],
  );

  const commit = (grid: Layout) => {
    const next = withGridItems(layout, grid, registry);
    if (!sameItems(next, layout)) onChange(next);
  };

  // Where a widget the keyboard moved now is, read out by screen readers.
  const [announcement, setAnnouncement] = useState("");
  const onWidgetKey = (event: KeyboardEvent, id: Id) => {
    const arrow = ARROWS[event.key];
    if (!arrow) return;
    event.preventDefault();
    const [dx, dy] = arrow;
    const next = event.shiftKey ? keyboardResize(gridLayout, id, dx, dy) : keyboardMove(gridLayout, id, dx, dy);
    if (!next) return;
    commit(next);
    const item = next.find((candidate) => candidate.i === id);
    if (item) {
      setAnnouncement(
        t_i18n("{name}: column {column}, row {row}, {width} columns wide, {height} rows high", {
          name: labels[id],
          column: item.x + 1,
          row: item.y + 1,
          width: item.w,
          height: item.h,
        }),
      );
    }
  };

  // One size object per widget and layout, so a memoized widget is not drawn
  // again on every render of the grid.
  const sizes = useMemo(
    () => new Map(layout.items.map((item) => [item.i, { cols: item.w, rows: item.h }])),
    [layout.items],
  );

  // The place of each widget in reading order: widgets come in one after the
  // other, top to bottom, when the dashboard opens (`dashboard-grid.css`).
  const enterOrder = useMemo(
    () => new Map(readingOrder(layout.items).map((item, index) => [item.i, Math.min(index, 8)])),
    [layout.items],
  );

  const widget = (id: Id) => (
    <div className={WIDGET_WRAPPER}>
      {/* The widget fills its cell, and scrolls inside it when the person made
          it smaller than its content. While editing, it is a picture of
          itself: nothing in it is clickable or focusable, so a drag never
          opens what it starts on. */}
      <div
        className={cn(
          "dashboard-widget-enter h-full overflow-y-auto overflow-x-hidden rounded-sm [&>*]:min-h-full",
          editing && "pointer-events-none select-none",
        )}
        data-enter={enterOrder.get(id)}
        inert={editing}
      >
        {/* The widget sizes itself to its cell: what it draws depends on it. */}
        <WidgetSizeContext.Provider value={sizes.get(id) ?? null}>{widgets[id]}</WidgetSizeContext.Provider>
      </div>
      {editing && (
        // The overlay is the widget's handle: dragged with the pointer, or
        // focused and moved with the arrow keys (Shift + arrows resize it).
        <div
          role="group"
          tabIndex={0}
          aria-label={t_i18n("{name}: the arrow keys move it, Shift and the arrow keys resize it", { name: labels[id] })}
          aria-current={selection?.selectedId === id ? "true" : undefined}
          onKeyDown={(event) => {
            if (selection && event.key === "Enter" && event.target === event.currentTarget) selection.onSelect(id);
            else onWidgetKey(event, id);
          }}
          onClick={selection ? () => selection.onSelect(id) : undefined}
          className={cn(
            "absolute inset-0 z-10 cursor-move rounded-sm ring-1 ring-accent/50 hover:bg-accent/[0.04] transition-colors outline-none focus-visible:ring-2 focus-visible:ring-focus",
            // A custom dashboard is edited where it is read: its widgets stay
            // visible under the handle.
            !ownWidgets && "bg-elevation-hover",
            selection?.selectedId === id && "ring-2 ring-accent bg-accent/[0.06]",
          )}
        >
          <div
            className={cn(
              NO_DRAG,
              "absolute right-2 flex items-center gap-1 cursor-default",
              // A one- or two-row widget is shorter than the label: centred on it.
              (sizes.get(id)?.rows ?? 3) <= 2 ? "top-1/2 -translate-y-1/2" : "top-2",
            )}
          >
            {/* A custom dashboard's widget shows its own heading. */}
            {!ownWidgets && (
              <span className="max-w-[12rem] truncate rounded-sm layer-2 bg-elevation-default/90 px-2 py-0.5 text-[11px] font-medium text-default-primary shadow-overlay ring-1 ring-[var(--border-elevation-subtle)]">
                {labels[id]}
              </span>
            )}
            {configure?.overridden.has(id) && (
              <Badge variant="cyan" data-testid="dashboard-widget-overridden">
                {t_i18n("Overridden")}
              </Badge>
            )}
            {configure?.ids.includes(id) && (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  configure.onOpen(id);
                }}
                className="inline-flex h-6 w-6 items-center justify-center rounded-sm bg-elevation-default/90 text-default-secondary ring-1 ring-[var(--border-elevation-subtle)] hover:text-default-primary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                aria-label={t_i18n("Settings of {name}", { name: labels[id] })}
                data-testid="dashboard-widget-configure"
              >
                <Icon name="settings" size={16} />
              </button>
            )}
            {/* A custom dashboard keeps at least one widget. */}
            {(!ownWidgets || layout.items.length > 1) && (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  if (onRemove) onRemove(id);
                  else onChange(removeWidget(layout, id));
                }}
                className="inline-flex h-6 w-6 items-center justify-center rounded-sm layer-2 bg-elevation-default/90 text-default-secondary shadow-overlay ring-1 ring-[var(--border-elevation-subtle)] hover:text-feedback-error-primary transition-colors"
                aria-label={t_i18n("Remove {name}", { name: labels[id] })}
              >
                <Icon name="x" size={16} />
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );

  const catalog = layout.hidden.map((id) => ({
    id,
    label: labels[id],
    icon: AddGlyph,
    onSelect: () => onChange(addWidget(layout, id, registry)),
  }));

  return (
    <div className="space-y-4">
      {editing && !hideEditBar && (
        // Solid, never blurred: a backdrop filter would make this bar the
        // containing block of the catalog's fixed-position menu, drawn off place.
        <div className="sticky top-0 z-30 flex flex-wrap items-center justify-between gap-3 rounded-sm border border-accent/40 layer-2 bg-elevation-default px-4 py-2.5 shadow-overlay">
          <p className="content-compact text-default-secondary min-w-0">
            <span className="font-semibold text-default-primary">{title}</span>
            {" - "}
            {fitsGrid
              ? `${t_i18n("Drag a widget to move it and its corner to resize it. Changes are saved as you go.")} ${t_i18n("With the keyboard, focus a widget: the arrow keys move it, Shift and the arrow keys resize it.")}`
              : t_i18n("Widen the window to rearrange the widgets.")}
          </p>
          <div className="flex items-center gap-2 shrink-0">
            {ownWidgets ? null : catalog.length > 0 ? (
              <OverflowMenu
                items={catalog}
                label={t_i18n("Add a widget")}
                side="auto"
                scroll
                trigger={
                  <span className="inline-flex items-center gap-1.5 px-2 content-compact font-medium">
                    <Icon name="plus" size={16} /> {t_i18n("Add a widget")}
                  </span>
                }
              />
            ) : (
              <span className="text-[11px] text-default-secondary">{t_i18n("Every widget is shown")}</span>
            )}
            {!ownWidgets && (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 content-compact gap-1"
                disabled={isDefaultLayout(layout, registry)}
                onClick={() => onChange(null)}
              >
                <Icon name="rotate-ccw" size={16} /> {t_i18n("Reset to default")}
              </Button>
            )}
            <Button size="sm" className="h-7 content-compact gap-1 bg-accent text-negative-primary hover:bg-accent/90" onClick={onDone}>
              <Icon name="check" size={16} /> {t_i18n("Done")}
            </Button>
          </div>
        </div>
      )}

      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>
      <div ref={containerRef} className="dashboard-widget-grid">
        {!mounted ? null : fitsGrid ? (
          <ReactGridLayout
            width={width}
            layout={gridLayout}
            gridConfig={{
              cols: GRID_COLUMNS,
              rowHeight: rows.rowHeight,
              margin: [rows.margin, rows.margin],
              containerPadding: [0, 0],
            }}
            dragConfig={{ enabled: editing, cancel: `.${NO_DRAG}` }}
            resizeConfig={{ enabled: editing, handles: ["se"] }}
            onDragStop={commit}
            onResizeStop={commit}
          >
            {layout.items.map((item) => (
              <div key={item.i}>{widget(item.i)}</div>
            ))}
          </ReactGridLayout>
        ) : (
          <div className="space-y-4">
            {readingOrder(layout.items).map((item) =>
              selection ? (
                // Too narrow to rearrange, still a widget to pick.
                <div
                  key={item.i}
                  role="button"
                  tabIndex={0}
                  aria-label={labels[item.i]}
                  aria-current={selection.selectedId === item.i ? "true" : undefined}
                  onClick={() => selection.onSelect(item.i)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && event.target === event.currentTarget) selection.onSelect(item.i);
                  }}
                  className={cn(
                    "dashboard-widget-enter cursor-pointer rounded-sm outline-offset-4 focus-visible:outline-2 focus-visible:outline-focus",
                    selection.selectedId === item.i && "outline-2 outline-accent",
                  )}
                  data-enter={enterOrder.get(item.i)}
                >
                  {widgets[item.i]}
                </div>
              ) : (
                <div key={item.i} className="dashboard-widget-enter" data-enter={enterOrder.get(item.i)}>
                  {widgets[item.i]}
                </div>
              ),
            )}
          </div>
        )}
      </div>
    </div>
  );
}
