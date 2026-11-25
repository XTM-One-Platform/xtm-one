/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * A custom dashboard: each tab its widgets on the 12-column grid
 * (`DashboardGrid`) or its widgets and elements on a canvas in pixels
 * (`DashboardCanvas`).
 *
 * The grid answers to the width of its CONTAINER, not of the window: the same
 * dashboard stacks its widgets in reading order in a phone or a narrow chat
 * bubble, and places each one where it was put on a wider page. In edit mode
 * (the studio, "Rearrange") a widget is dragged to move it and by its corner
 * to resize it; each change is one `layout` operation, shown at once and
 * saved by the page. A canvas is scaled to the container by its display mode;
 * the studio edits it (`design`).
 *
 * A dashboard with several tabs shows its tab bar above the dashboard or on
 * its left (unless its definition hides it); each tab is a layout of its own,
 * and a drag saves the places of the tab on screen (`layoutId`). In the studio
 * the bar also adds, renames, moves, retypes and removes tabs.
 *
 * A dashboard with a theme of its own (`theme`) is drawn in it whatever the
 * app's: the subtree carries the design system's mode class. Out of edit mode,
 * a widget can be expanded to fill the screen.
 *
 * Every widget is drawn inside its own error boundary: a widget that fails to
 * draw (a shape no test foresaw) is one placeholder card, and the rest of the
 * dashboard stays on screen. A widget of an unknown kind is a placeholder too
 * (`normalizeDashboard`), so a row written by a newer agent never blanks the
 * page of an older build.
 */
import { useCallback, useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { AlertTriangle, Maximize2, X } from "lucide-react";

import { DashboardGrid } from "@/components/dashboard-grid/DashboardGrid";
import { OPENCTI_GRID_ROWS } from "@/components/dashboard-grid/layout";
import { WidgetSizeContext } from "@/components/dashboard-grid/widget-size-context";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ErrorBoundary } from "@/components/ui/error-boundary";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CustomDashboardOperation, DashboardLayout, DashboardLayoutItem } from "@/types";

import { cellOfBox } from "./canvas";
import { DashboardCanvas } from "./DashboardCanvas";
import { DashboardTabsBar } from "./DashboardTabsBar";
import { layoutOperation, placeWidgets, readCell, widgetRegistry } from "./grid";
import { InputBar, SourceResults } from "./DashboardTokens";
import { DashboardTokensContext, useTokenState } from "./token-state";
import { DashboardLiveContext, type DashboardLive } from "./live";
import { isVisible } from "./interactions";
import { fillOrKeep, type Tokens } from "./tokens";
import { WidgetDataMenu } from "./WidgetDataMenu";
import { frameTitleOptions, headerTitleKey, useDrawnResult } from "./drawn-result";
import { HeaderSlotScope, HeaderSlotTarget } from "./header-slot";
import type { DashboardTab, DashboardWidget, NormalizedDashboard } from "./spec";
import { ActionControl, WidgetBody } from "./widgets";

/** Narrowest container the dashboard is drawn on its grid in: a studio stage, a chat bubble. */
const DASHBOARD_GRID_MIN_WIDTH = 520;

type Definition = Record<string, unknown>;

/** The widget picked on the studio's stage, and how a click picks another. */
export interface DashboardSelection {
  selectedId: string | null;
  onSelect: (widget: DashboardWidget) => void;
}

/** What the person may do to the widgets while the dashboard is in edit mode. */
export interface DashboardEditing {
  /** The grid's new arrangement, as the `layout` operation to save. */
  onLayout: (operation: CustomDashboardOperation) => void;
  onRemove: (widget: DashboardWidget) => void;
  /** Done rearranging (the edit bar's button); omitted with `embedded`. */
  onDone?: () => void;
  /** The page has its own toolbar (the studio): no edit bar. */
  embedded?: boolean;
}

/** The studio's design of the definition: tabs and canvases, one undo step per change. */
export interface DashboardDesign {
  /** The definition being edited (what a change is computed from). */
  definition: Definition;
  onChange: (update: (definition: Definition) => Definition) => void;
  /** The items picked on a canvas (several with Shift). */
  selection: readonly string[];
  onSelectIds: (ids: string[]) => void;
  onShortcut?: (event: KeyboardEvent) => void;
  zoom: number | null;
}

function WidgetFailed() {
  const { t_i18n } = useFormatter();
  return (
    <Card className="flex items-center gap-3 border-dashed p-4">
      <AlertTriangle className="h-4 w-4 shrink-0 text-feedback-warning-primary" aria-hidden="true" />
      <span className="content-compact text-default-secondary">{t_i18n("This widget could not be drawn.")}</span>
    </Card>
  );
}

/** Show one widget filling the screen: shown while the pointer or the focus is on the widget, or always (`shown`). */
function ExpandButton({ widget, onExpand, className, shown = false }: { widget: DashboardWidget; onExpand: () => void; className?: string; shown?: boolean }) {
  const { t_i18n } = useFormatter();
  return (
    <Tip content={t_i18n("Expand the visualization")}>
      <Button
        variant="ghost"
        size="icon"
        className={cn(
          "h-6 w-6",
          !shown && "opacity-0 transition-opacity group-focus-within/widget:opacity-100 group-hover/widget:opacity-100 focus-visible:opacity-100",
          className,
        )}
        aria-label={t_i18n("Expand {title}", { title: widget.title ?? widget.id })}
        onClick={(event) => {
          event.stopPropagation();
          onExpand();
        }}
      >
        <Maximize2 className="h-3.5 w-3.5" />
      </Button>
    </Tip>
  );
}

/** A title with its first letter raised, in the reader's locale rules (`toLocaleUpperCase`). */
function firstUpper(text: string): string {
  return text ? text.charAt(0).toLocaleUpperCase() + text.slice(1) : text;
}

/** The title as its module writes it when the widget draws with that module's look (OpenCTI, OpenAEV), else XTM One's. */
function WidgetTitle({ widget, tokens }: { widget: DashboardWidget; tokens: Tokens }) {
  const { t_i18n } = useFormatter();
  const { titleStyle, headerTitle } = frameTitleOptions(widget, useDrawnResult(widget));
  const header = headerTitle ? headerTitleKey(headerTitle) : undefined;
  const text = header ? t_i18n(header) : (headerTitle ?? fillOrKeep(widget.title ?? "", tokens));
  return (
    <h2
      className={cn(
        "min-w-0",
        // OpenCTI's title wraps onto a second line rather than ending in "...".
        titleStyle === "capitalize" ? "line-clamp-2 break-words" : "truncate",
        // The module's quiet title, with its first letter raised - not every word's, which a sentence in French
        // ("Répartition des malwares par type") or any language would read as a heading in English title case.
        titleStyle === "capitalize" && "content-compact font-normal text-default-secondary",
        titleStyle === "caps" && "font-heading text-[11px] font-semibold uppercase tracking-[0.08em] text-default-primary opacity-85",
        !titleStyle && "content-base font-semibold text-default-primary",
      )}
      data-title-style={titleStyle ?? "default"}
    >
      {titleStyle === "capitalize" ? firstUpper(text) : text}
    </h2>
  );
}

function WidgetHeader({
  widget,
  onExpand,
  inspectable,
  tokens,
}: {
  widget: DashboardWidget;
  onExpand?: () => void;
  /** Whether its data can be inspected and exported (a stored dashboard, out of edit mode). */
  inspectable: boolean;
  tokens: Tokens;
}) {
  const { expandShown } = frameTitleOptions(widget, useDrawnResult(widget));
  if (!widget.title && !widget.link && !inspectable) {
    // No heading row of its own: the button floats over the widget's corner.
    return onExpand ? <ExpandButton widget={widget} onExpand={onExpand} className="absolute right-1 top-1 z-10" /> : null;
  }
  return (
    <div className="flex items-center justify-between gap-3 px-1">
      {widget.title ? (
        <WidgetTitle widget={widget} tokens={tokens} />
      ) : (
        <span />
      )}
      <div className="flex shrink-0 items-center gap-1">
        {widget.link && <ActionControl action={widget.link} priority="link" className="h-auto min-h-0 px-0" />}
        {/* What the content puts in its header (`InHeader`), then, shown always, Expand: before the menu, as OpenAEV. */}
        <HeaderSlotTarget />
        {onExpand && expandShown && <ExpandButton widget={widget} onExpand={onExpand} shown />}
        {inspectable && (widget.kind === "platform" || widget.kind === "data") && <WidgetDataMenu widget={widget} />}
        {onExpand && !expandShown && <ExpandButton widget={widget} onExpand={onExpand} />}
      </div>
    </div>
  );
}

/** One widget filling the screen (inside the dashboard, so a fullscreen dashboard shows it too). */
function ExpandedWidget({ widget, revision, onClose }: { widget: DashboardWidget; revision?: number; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={widget.title ?? widget.id}
      className="fixed inset-0 z-50 flex flex-col gap-4 layer-1 bg-elevation-default p-6"
      data-testid="dashboard.expanded"
    >
      <div className="flex items-center justify-between gap-3">
        <h2 className="min-w-0 truncate text-xl font-semibold text-default-primary">{widget.title ?? ""}</h2>
        <Button variant="ghost" size="icon" aria-label={t_i18n("Close")} onClick={onClose} autoFocus>
          <X className="h-4 w-4" />
        </Button>
      </div>
      <div className="@container min-h-0 flex-1 overflow-auto">
        {/* Sized to the screen less the heading, as a grid cell that tall. */}
        <WidgetSizeContext.Provider value={cellOfBox({ w: window.innerWidth - 48, h: window.innerHeight - 96 })}>
          <ErrorBoundary fallback={<WidgetFailed />} resetKey={revision}>
            <WidgetBody widget={widget} />
          </ErrorBoundary>
        </WidgetSizeContext.Provider>
      </div>
    </div>
  );
}

export function CustomDashboardView({
  dashboard,
  revision,
  editing,
  dashboardId,
  selection,
  design,
  tabId: controlledTabId,
  onTabChange,
  expandable = true,
  className,
  onTokens,
  onNavigate,
  onKeepInput,
}: {
  /** Keep an input's value as its default, saved with the dashboard (those who may edit it). */
  onKeepInput?: DashboardLive["keepInput"];
  /** How a page of the web app is opened from an interaction (the router's navigate). */
  onNavigate?: (url: string) => void;
  /** Told the page's tokens each time they change (the studio's token manager). */
  onTokens?: (tokens: Tokens) => void;
  dashboard: NormalizedDashboard;
  /** The stored dashboard's id: its live platform widgets read their data through it. */
  dashboardId?: string;
  /** The studio's stage: a widget is picked by a click (or Enter), and outlined. */
  selection?: DashboardSelection | null;
  /** The stored revision: a new one clears a widget's error state and a shown arrangement. */
  revision?: number;
  /** Edit mode, when the person may rearrange the dashboard. */
  editing?: DashboardEditing | null;
  /** The studio's edit mode: tabs and canvases. */
  design?: DashboardDesign | null;
  /** The open tab, when the page keeps it (the studio: new visualizations are placed there). */
  tabId?: string | null;
  onTabChange?: (id: string) => void;
  /** Whether a widget can be expanded out of edit mode. */
  expandable?: boolean;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  // A dashboard built by hand (a test, a chat card) may come without tabs: its widgets are one.
  const tabs = useMemo<DashboardTab[]>(
    () => (dashboard.tabs?.length ? dashboard.tabs : [{ id: "layout_1", label: "", type: "grid", widgets: dashboard.widgets }]),
    [dashboard.tabs, dashboard.widgets],
  );
  const [ownTabId, setOwnTabId] = useState(tabs[0].id);
  const tabId = controlledTabId ?? ownTabId;
  // A tab removed by a later revision falls back to the first one.
  const tab = tabs.find((candidate) => candidate.id === tabId) ?? tabs[0];
  const switchTab = useCallback(
    (next: string) => {
      setOwnTabId(next);
      onTabChange?.(next);
    },
    [onTabChange],
  );
  const setTabId = switchTab;
  const showTabBar = (Boolean(dashboard.showTabBar) || Boolean(design)) && tabs.length > 0;
  const barPosition = dashboard.tabBarPosition === "left" ? "left" : "top";
  // A widget without a valid place (a dashboard not read through
  // `normalizeDashboard`) is placed here: the grid never gets a position it
  // cannot compute with.
  const widgets = useMemo(
    () =>
      placeWidgets(
        tab.widgets.map((widget) =>
          readCell(widget.layout) ? widget : ({ ...widget, layout: undefined } as unknown as DashboardWidget),
        ),
      ),
    [tab.widgets],
  );
  const hideOpenLinks = Boolean(dashboard.appProperties?.hideOpenInPlatform);
  const hideExport = Boolean(dashboard.appProperties?.hideExport);
  const sources = dashboard.sources;
  const live = useMemo<DashboardLive | null>(
    () => (dashboardId ? { dashboardId, hideOpenLinks, hideExport, sources, keepInput: onKeepInput } : null),
    [dashboardId, hideOpenLinks, hideExport, sources, onKeepInput],
  );
  const { value: tokenState, resultReads, onResult } = useTokenState(dashboard, switchTab, live, onNavigate);
  const pageTokens = tokenState.tokens;
  useEffect(() => onTokens?.(pageTokens), [onTokens, pageTokens]);
  // What shows: every widget while editing; otherwise those whose visibility holds now, the
  // grid closing up around the others. One hidden for having no data is still drawn off the
  // grid, so it keeps reading its source and comes back when it has data.
  const conditions = dashboard.conditions;
  const empty = tokenState.empty;
  const { visible, offstage } = useMemo(() => {
    if (editing || design) return { visible: widgets, offstage: [] as DashboardWidget[] };
    const shownNow = widgets.filter((widget) => isVisible(widget.visibility, conditions ?? {}, pageTokens, empty.has(widget.id)));
    const hidden = widgets.filter((widget) => !shownNow.includes(widget) && widget.visibility?.hideWhenNoData);
    return { visible: shownNow, offstage: hidden };
  }, [editing, design, widgets, conditions, pageTokens, empty]);
  const registry = useMemo(() => widgetRegistry(visible), [visible]);
  const stored = useMemo<DashboardLayout<string>>(
    () => ({ items: visible.map((widget) => ({ i: widget.id, ...widget.layout })), hidden: [] }),
    [visible],
  );
  // A drag shows at once; the saved dashboard (a new revision) replaces it.
  const [shown, setShown] = useState<DashboardLayoutItem<string>[] | null>(null);
  useEffect(() => setShown(null), [revision, widgets]);
  const layout = useMemo(() => (shown ? { items: shown, hidden: [] } : stored), [shown, stored]);
  const byId = useMemo(() => new Map(visible.map((widget) => [widget.id, widget])), [visible]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const expandedWidget = expanded ? byId.get(expanded) : undefined;

  const labels = useMemo(() => Object.fromEntries(visible.map((w) => [w.id, w.title ?? w.id])), [visible]);
  // In edit mode the grid's overlay picks a widget; out of it, the widget itself does.
  const pickable = selection && !editing;
  const canExpand = expandable && !editing && !design;
  const frame = (widget: DashboardWidget): ReactNode => (
    <section
      key={widget.id}
      aria-label={widget.title ?? undefined}
      className={cn(
        "group/widget @container relative flex h-full min-w-0 flex-col gap-2.5",
        pickable && "cursor-pointer rounded-sm outline-offset-4 focus-visible:outline-2 focus-visible:outline-focus",
        pickable && selection.selectedId === widget.id && "outline-2 outline-accent",
      )}
      data-widget-kind={widget.kind}
      data-widget-id={widget.id}
      {...(pickable
        ? {
            tabIndex: 0,
            "aria-current": selection.selectedId === widget.id ? ("true" as const) : undefined,
            onClick: () => selection.onSelect(widget),
            onKeyDown: (event: KeyboardEvent) => {
              if (event.key === "Enter" && event.target === event.currentTarget) selection.onSelect(widget);
            },
          }
        : {})}
    >
      <HeaderSlotScope>
        <WidgetHeader
          widget={widget}
          onExpand={canExpand ? () => setExpanded(widget.id) : undefined}
          inspectable={Boolean(live) && !editing && !design}
          tokens={pageTokens}
        />
        <div className="min-h-0 flex-1">
          <ErrorBoundary fallback={<WidgetFailed />} resetKey={revision}>
            <WidgetBody widget={widget} />
          </ErrorBoundary>
        </div>
      </HeaderSlotScope>
    </section>
  );
  const drawn: Record<string, ReactNode> = Object.fromEntries(visible.map((widget) => [widget.id, frame(widget)]));

  const body =
    tab.type === "absolute" && tab.canvas ? (
      <DashboardCanvas
        canvas={tab.canvas}
        widgets={byId}
        renderWidget={frame}
        dashboardId={dashboardId}
        label={tab.label || t_i18n("Dashboard")}
        editing={
          design
            ? {
                layoutId: tab.id,
                selection: design.selection,
                onSelect: design.onSelectIds,
                onChange: design.onChange,
                onShortcut: design.onShortcut,
                zoom: design.zoom,
              }
            : null
        }
      />
    ) : (
      <DashboardGrid
        registry={registry}
        layout={layout}
        widgets={drawn}
        labels={labels}
        title={t_i18n("Rearrange the dashboard")}
        editing={Boolean(editing)}
        minWidth={DASHBOARD_GRID_MIN_WIDTH}
        rows={OPENCTI_GRID_ROWS}
        hideEditBar={editing?.embedded}
        onChange={(next) => {
          if (!next || !editing) return;
          const operation = layoutOperation(widgets, next.items);
          if (!operation) return;
          // The places are this tab's; a dashboard of one tab names none (its first).
          if (tabs.length > 1) operation.layoutId = tab.id;
          setShown(next.items);
          editing.onLayout(operation);
        }}
        onDone={() => editing?.onDone?.()}
        onRemove={(id) => {
          const widget = byId.get(id);
          if (widget && editing) editing.onRemove(widget);
        }}
        selection={
          selection && editing
            ? {
                selectedId: selection.selectedId,
                onSelect: (id) => {
                  const widget = byId.get(id);
                  if (widget) selection.onSelect(widget);
                },
              }
            : undefined
        }
      />
    );

  const theme = dashboard.theme;
  return (
    <DashboardLiveContext.Provider value={live}>
      <DashboardTokensContext.Provider value={tokenState}>
      <SourceResults dashboardId={dashboardId} reads={resultReads} tokens={tokenState.tokens} onResult={onResult} />
      <div
        className={cn(
          theme && [theme, "layer-0 rounded-sm bg-elevation-default p-4 text-default-primary"],
          showTabBar && barPosition === "left" && "flex gap-4",
          className,
        )}
        data-mode={theme}
        data-testid="dashboard.view"
      >
        <InputBar ids={dashboard.inputs?.globalInputs ?? []} />
        {showTabBar && (
          <DashboardTabsBar
            tabs={tabs}
            value={tab.id}
            onValueChange={setTabId}
            position={barPosition}
            editing={design ? { definition: design.definition, onChange: design.onChange } : null}
            className={barPosition === "left" ? "w-44 shrink-0" : "mb-4"}
          />
        )}
        <div className={cn(barPosition === "left" && showTabBar && "min-w-0 flex-1")}>{body}</div>
        {expandedWidget && <ExpandedWidget widget={expandedWidget} revision={revision} onClose={() => setExpanded(null)} />}
        {offstage.length > 0 && (
          <div hidden aria-hidden="true">
            {offstage.map((widget) => (
              <ErrorBoundary key={widget.id} fallback={null} resetKey={revision}>
                <WidgetBody widget={widget} />
              </ErrorBoundary>
            ))}
          </div>
        )}
      </div>
      </DashboardTokensContext.Provider>
    </DashboardLiveContext.Provider>
  );
}
