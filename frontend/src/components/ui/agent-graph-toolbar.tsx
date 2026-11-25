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
 * The controls of the agent graph (Agentic Flow map, fleet views).
 *
 * `AgentGraphToolbar` sits ABOVE the canvas: the layout picker (force,
 * hierarchy top-to-bottom or left-to-right, radial, org chart), one compact
 * "Layout: Force" select, a control of the canvas and never a second row of
 * tabs under the page's tabs (#9980, UX round 1 B1), the page's own controls
 * (forces, filters, the focus field) as children, and the graph's legend
 * behind a Legend button that opens downwards over the canvas.
 * `AgentGraphStatusLine` sits under the canvas: one thin line with the
 * counters of the scope shown (agents, handovers, runs in flight, open work
 * items), so the graph never loses room to them.
 */

import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { BookOpen, Filter, SlidersHorizontal, type LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CountBadge } from "@/components/ui/count-badge";
import { Switch } from "@/components/ui/switch";
import { SegmentedFilter, type SegmentedFilterOption } from "@/components/ui/segmented-filter";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { FORCE_LIMITS, type GraphForces, type GraphLayout } from "@/lib/agent-graph-layout";
import {
  AGENT_KINDS,
  DEFAULT_GRAPH_FILTERS,
  activeFilterCount,
  type AgentGraphFilters,
  type AgentKind,
  type EnabledFilter,
} from "@/lib/agent-graph-filters";
import { useFormatter } from "@/i18n";

export interface AgentGraphCounter {
  key: string;
  label: string;
  value: number;
  /** Drawn in the accent colour when above zero (something is happening). */
  live?: boolean;
}

export interface AgentGraphToolbarProps {
  /** The layout picked; no layout control when absent (the map's Schedule view). */
  layout?: GraphLayout;
  onLayoutChange?: (layout: GraphLayout) => void;
  /**
   * The layouts offered, in this order; every layout by default. A graph where
   * one of them means nothing leaves it out (the work items graph has no org chart, #1062).
   */
  layouts?: readonly GraphLayout[];
  /** The toolbar's accessible name; "Graph toolbar" by default. */
  ariaLabel?: string;
  /**
   * Counters drawn at the end of the toolbar row. Prefer `AgentGraphStatusLine`
   * under the canvas; kept for the fleet page until it moves to that layout.
   */
  counters?: AgentGraphCounter[];
  /** The graph's legend (`AgentFlowLegend`), opened from the Legend button. */
  legend?: ReactNode;
  children?: ReactNode;
}

interface DisclosureProps {
  label: string;
  icon: LucideIcon;
  /** A count drawn after the label (filters changed from their default). */
  badge?: number;
  /** Panel width class. */
  width?: string;
  /** Panel side: the Legend opens to the left of its button, the controls to the right. */
  align?: "left" | "right";
  /** Bound the panel to the card that clips it and scroll inside (the legend). */
  fitToClip?: boolean;
  children: ReactNode;
}

// Space kept between the panel's bottom and the edge that clips it.
const PANEL_CLIP_MARGIN = 8;

// The overflow values that clip a descendant.
const CLIPS = /^(hidden|clip|auto|scroll)$/;

/**
 * The bottom of the nearest ancestor that clips `el` (overflow hidden, clip,
 * auto or scroll), else of the viewport: the panel opens over the graph card and
 * must end inside it.
 */
function clipBottom(el: HTMLElement): number {
  let bottom = window.innerHeight;
  for (let node = el.parentElement; node; node = node.parentElement) {
    const { overflow, overflowX, overflowY } = getComputedStyle(node);
    if ([overflow, overflowX, overflowY].some((v) => CLIPS.test(v))) {
      bottom = Math.min(bottom, node.getBoundingClientRect().bottom);
      break;
    }
  }
  return bottom;
}

/** A toolbar button opening a panel over the canvas; closed by Escape or a click outside. */
function ToolbarDisclosure({ label, icon: Icon, badge, width = "w-56", align = "right", fitToClip = false, children }: DisclosureProps) {
  const [open, setOpen] = useState(false);
  const [maxHeight, setMaxHeight] = useState<number | undefined>(undefined);
  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  // Bound the panel to the card that clips it, so a long legend scrolls
  // inside the card instead of being cut off below it.
  useLayoutEffect(() => {
    if (!open || !fitToClip) return;
    const fit = () => {
      const panel = panelRef.current;
      if (!panel) return;
      const room = clipBottom(panel) - panel.getBoundingClientRect().top - PANEL_CLIP_MARGIN;
      setMaxHeight(Math.max(room, 0));
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [open, fitToClip]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon className="h-3.5 w-3.5" aria-hidden="true" />
        {label}
        {badge ? (
          <CountBadge className="ml-1 border-transparent bg-accent font-semibold text-accent-foreground">{badge}</CountBadge>
        ) : null}
      </Button>
      {open && (
        <div
          ref={panelRef}
          id={panelId}
          role="region"
          aria-label={label}
          // Focusable so a panel that scrolls is scrolled from the keyboard, with the
          // design system's focus ring (the same as ChartScroller), never the native outline.
          tabIndex={fitToClip ? 0 : undefined}
          data-focus-ring={fitToClip ? "ring-focus" : undefined}
          style={fitToClip && maxHeight !== undefined ? { maxHeight } : undefined}
          className={`absolute top-full ${align === "right" ? "left-0" : "right-0"} z-50 mt-2 ${width} ${fitToClip ? "overflow-y-auto outline-none focus-visible:ring-2 focus-visible:ring-focus" : ""} rounded-sm border border-elevation-subtle layer-2 bg-elevation-default p-3 content-compact text-default-secondary shadow-overlay`}
        >
          {children}
        </div>
      )}
    </div>
  );
}

function LegendDisclosure({ children }: { children: ReactNode }) {
  const { t_i18n } = useFormatter();
  return (
    <ToolbarDisclosure label={t_i18n("Legend")} icon={BookOpen} align="left" width="w-64" fitToClip>
      {children}
    </ToolbarDisclosure>
  );
}

export interface AgentGraphForcesProps {
  forces: GraphForces;
  onChange: (forces: GraphForces) => void;
}

/** The force controls (charge, link distance, collide), behind a Forces button. */
export function AgentGraphForces({ forces, onChange }: AgentGraphForcesProps) {
  const { t_i18n } = useFormatter();
  const rows: { key: keyof GraphForces; label: string }[] = [
    { key: "charge", label: t_i18n("Repulsion") },
    { key: "linkDistance", label: t_i18n("Link distance") },
    { key: "collide", label: t_i18n("Node spacing") },
  ];
  return (
    <ToolbarDisclosure label={t_i18n("Forces")} icon={SlidersHorizontal} width="w-64">
      <div className="flex flex-col gap-3">
        {rows.map(({ key, label }) => {
          const limit = FORCE_LIMITS[key];
          return (
            <label key={key} className="flex flex-col gap-1">
              <span className="flex justify-between">
                <span>{label}</span>
                <span className="tabular-nums text-default-primary">{forces[key]}</span>
              </span>
              <input
                type="range"
                min={limit.min}
                max={limit.max}
                step={limit.step}
                value={forces[key]}
                onChange={(e) => onChange({ ...forces, [key]: Number(e.target.value) })}
                className="w-full cursor-pointer accent-accent"
              />
            </label>
          );
        })}
      </div>
    </ToolbarDisclosure>
  );
}

export interface AgentGraphFiltersProps {
  filters: AgentGraphFilters;
  onChange: (filters: AgentGraphFilters) => void;
  /**
   * The built-in fold of the map (#11882): the switch that shows the built-in
   * catalogue under Company. In the panel, never as a line under the map (#11905).
   */
  builtin?: { shown: boolean; onChange: (shown: boolean) => void };
}

/** One filter toggle: the design system's Switch with its label, the whole row clickable. */
function FilterSwitch({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  const id = useId();
  return (
    <div className="flex items-center justify-between gap-3">
      <label htmlFor={id} className="cursor-pointer">{label}</label>
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

/** The filters of the drawn agents (kind, enabled, activity), behind a Filters button. */
export function AgentGraphFilterControls({ filters, onChange, builtin }: AgentGraphFiltersProps) {
  const { t_i18n } = useFormatter();
  const kindLabels: Record<AgentKind, string> = {
    builtin: t_i18n("Built-in"),
    company: t_i18n("Company-managed"),
    personal: t_i18n("Personal"),
  };
  const enabledOptions: SegmentedFilterOption<EnabledFilter>[] = [
    { key: "all", label: t_i18n("All") },
    { key: "enabled", label: t_i18n("Enabled") },
    { key: "disabled", label: t_i18n("Disabled") },
  ];
  const toggleKind = (kind: AgentKind) =>
    onChange({
      ...filters,
      kinds: filters.kinds.includes(kind) ? filters.kinds.filter((k) => k !== kind) : [...filters.kinds, kind],
    });
  const count = activeFilterCount(filters);
  return (
    <ToolbarDisclosure label={t_i18n("Filters")} icon={Filter} badge={count} width="w-72">
      <div className="flex flex-col gap-3">
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-default-primary">{t_i18n("Agent type")}</legend>
          {AGENT_KINDS.map((kind) => (
            <FilterSwitch
              key={kind}
              label={kindLabels[kind]}
              checked={filters.kinds.includes(kind)}
              onChange={() => toggleKind(kind)}
            />
          ))}
        </fieldset>
        <SegmentedFilter
          options={enabledOptions}
          value={filters.enabled}
          onChange={(enabled) => onChange({ ...filters, enabled })}
          ariaLabel={t_i18n("Enabled")}
        />
        <FilterSwitch
          label={t_i18n("With open work items")}
          checked={filters.withOpenWorkItems}
          onChange={(withOpenWorkItems) => onChange({ ...filters, withOpenWorkItems })}
        />
        <FilterSwitch
          label={t_i18n("With runs in flight")}
          checked={filters.withRunsInFlight}
          onChange={(withRunsInFlight) => onChange({ ...filters, withRunsInFlight })}
        />
        {builtin && (
          <FilterSwitch
            label={t_i18n("Show the built-in catalogue under Company")}
            checked={builtin.shown}
            onChange={builtin.onChange}
          />
        )}
        {count > 0 && (
          <Button type="button" variant="ghost" size="sm" onClick={() => onChange(DEFAULT_GRAPH_FILTERS)}>
            {t_i18n("Clear filters")}
          </Button>
        )}
      </div>
    </ToolbarDisclosure>
  );
}

function CounterList({ counters }: { counters: AgentGraphCounter[] }) {
  return (
    <dl className="flex flex-wrap items-center gap-x-4 gap-y-1 content-compact tabular-nums text-default-secondary">
      {counters.map((c) => (
        <div key={c.key} className="flex items-baseline gap-1">
          <dd className={c.live && c.value > 0 ? "text-accent font-semibold" : "text-default-primary font-semibold"}>
            {c.value}
          </dd>
          <dt>{c.label}</dt>
        </div>
      ))}
    </dl>
  );
}

const ALL_LAYOUTS: readonly GraphLayout[] = ["force", "hierarchy-tb", "hierarchy-lr", "radial", "orgchart"];

export function AgentGraphToolbar({
  layout,
  onLayoutChange,
  layouts = ALL_LAYOUTS,
  ariaLabel,
  counters,
  legend,
  children,
}: AgentGraphToolbarProps) {
  const { t_i18n } = useFormatter();
  // The button picked shows at once, while the page may hand the new layout to
  // the graph only once its move ends: this
  // re-renders the toolbar alone, never the page and its graph.
  const [picked, setPicked] = useState(layout);
  const [given, setGiven] = useState(layout);
  if (given !== layout) {
    setGiven(layout);
    setPicked(layout);
  }
  const pick = (next: GraphLayout) => {
    setPicked(next);
    onLayoutChange?.(next);
  };
  const labels: Record<GraphLayout, string> = {
    force: t_i18n("Force"),
    "hierarchy-tb": t_i18n("Top down"),
    "hierarchy-lr": t_i18n("Left to right"),
    radial: t_i18n("Radial"),
    orgchart: t_i18n("Org chart"),
  };
  const options = layouts.map((key) => ({ key, label: labels[key] }));
  return (
    <div
      role="toolbar"
      aria-label={ariaLabel ?? t_i18n("Graph toolbar")}
      className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-elevation-subtle-soft px-4 py-2"
    >
      {picked && (
        <div className="flex items-center gap-2">
          <span className="content-compact text-default-secondary" aria-hidden="true">
            {t_i18n("Layout")}
          </span>
          <Select value={picked} onValueChange={(next) => pick(next as GraphLayout)}>
            <SelectTrigger className="w-40" aria-label={t_i18n("Graph layout")} data-testid="graph-layout-select">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {options.map((option) => (
                <SelectItem key={option.key} value={option.key}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
      {children}
      {(legend || counters) && (
        <div className="ml-auto flex items-center gap-4">
          {legend && <LegendDisclosure>{legend}</LegendDisclosure>}
          {counters && <CounterList counters={counters} />}
        </div>
      )}
    </div>
  );
}

export interface AgentGraphStatusLineProps {
  counters: AgentGraphCounter[];
}

/** The counters of the scope shown, in one thin line under the canvas. */
export function AgentGraphStatusLine({ counters }: AgentGraphStatusLineProps) {
  const { t_i18n } = useFormatter();
  return (
    <div
      role="status"
      aria-label={t_i18n("Graph status")}
      className="flex items-center border-t border-elevation-subtle-soft px-4 py-1"
    >
      <CounterList counters={counters} />
    </div>
  );
}
