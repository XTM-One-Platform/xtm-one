// SPDX-License-Identifier: Apache-2.0
/**
 * The dashboard context of the CTEM command center (dev-docs/ctem-stage-viz.md
 * sections 8.1 to 8.3, slice S2f): the framework and the period every widget
 * reads, saved with the person's layout (`context`), and a widget's own
 * values over it (`settings` on its layout item).
 *
 * Both are optional fields of the saved layout: a layout saved without them
 * reads with the defaults and is saved back without them until the person
 * changes one, so every layout saved before keeps loading unchanged. A value
 * equal to the default is never stored.
 *
 * No framework is a constant here: the frameworks are the kill chains the
 * Validation outcome matrix carries (`viz.outcome_matrix.chains`).
 */
import type {
  CtemContextPeriod,
  CtemDashboardContext,
  CtemOutcomeMatrix,
  CtemWidgetId,
  CtemWidgetSettings,
  DashboardLayout,
} from "@/types";

/** The periods the context bar offers, in days. */
export const CONTEXT_PERIODS: readonly CtemContextPeriod[] = [7, 30, 90];
/** The period of a dashboard with none set. */
export const DEFAULT_PERIOD: CtemContextPeriod = 30;

/** The widgets that read the period and offer their own (section 8.3): the
 *  ones drawn from the posture history. */
export const PERIOD_WIDGETS: readonly CtemWidgetId[] = ["hero", "value_band", "exposure_stats", "stats_coverage"];

/** One framework of the selector: the chain's id, its name and how many
 *  techniques carry data under it. */
export interface ContextFramework {
  id: string;
  label: string;
  count: number;
}

function isContextPeriod(value: unknown): value is CtemContextPeriod {
  return typeof value === "number" && (CONTEXT_PERIODS as readonly number[]).includes(value);
}

/** The frameworks the data carries, as the outcome matrix lists them. */
export function contextFrameworks(matrix: CtemOutcomeMatrix | null | undefined): ContextFramework[] {
  return (matrix?.chains ?? []).map((chain) => ({ id: chain.id, label: chain.label, count: chain.count }));
}

/**
 * The framework with the most data (section 8.1): the chain the data marks as
 * default, else the one with the most techniques carrying data, a tie going to
 * the one with more phases. Null with no framework, or none carrying data.
 */
export function defaultFramework(matrix: CtemOutcomeMatrix | null | undefined): string | null {
  const chains = matrix?.chains ?? [];
  const marked = chains.find((chain) => chain.default && chain.count > 0);
  if (marked) return marked.id;
  const ranked = chains
    .filter((chain) => chain.count > 0)
    .sort((a, b) => b.count - a.count || b.groups.length - a.groups.length);
  return ranked[0]?.id ?? null;
}

/** The context as read: the saved values, a period not offered read as the default. */
export function readContext(layout: Pick<DashboardLayout, "context">): { framework: string | null; period: CtemContextPeriod } {
  const context = layout.context;
  return {
    framework: typeof context?.framework === "string" && context.framework ? context.framework : null,
    period: isContextPeriod(context?.period) ? context.period : DEFAULT_PERIOD,
  };
}

/**
 * The framework the widgets draw: the one the context names while the data
 * still carries it, else the default (section 8.2: a value that no longer
 * exists falls back, `fellBack` says so for the one-line notice).
 */
export function effectiveFramework(
  layout: Pick<DashboardLayout, "context">,
  matrix: CtemOutcomeMatrix | null | undefined,
): { id: string | null; fellBack: boolean } {
  const { framework } = readContext(layout);
  const chains = matrix?.chains ?? [];
  if (framework && chains.some((chain) => chain.id === framework)) return { id: framework, fellBack: false };
  return { id: defaultFramework(matrix), fellBack: Boolean(framework && chains.length > 0) };
}

/**
 * The framework a stage page draws for the person's saved layout of a vertical
 * (section 8.2: the Validation matrix opens on the context's framework): the
 * saved one while the data carries it, else the one with the most data.
 */
export function savedLayoutFramework(
  saved: { context?: unknown } | null | undefined,
  matrix: CtemOutcomeMatrix | null | undefined,
): string | null {
  const context = saved?.context && typeof saved.context === "object" ? (saved.context as CtemDashboardContext) : undefined;
  return effectiveFramework({ context }, matrix).id;
}

/** The period one widget reads: its own when set, else the dashboard's. */
export function widgetPeriod(layout: DashboardLayout<CtemWidgetId>, id: CtemWidgetId): CtemContextPeriod {
  const own = layout.items.find((item) => item.i === id)?.settings?.period;
  return isContextPeriod(own) ? own : readContext(layout).period;
}

/** The context with the defaults left out; undefined when nothing is set. */
function compactContext(context: CtemDashboardContext): CtemDashboardContext | undefined {
  const next: CtemDashboardContext = {};
  if (context.framework) next.framework = context.framework;
  if (isContextPeriod(context.period) && context.period !== DEFAULT_PERIOD) next.period = context.period;
  return Object.keys(next).length > 0 ? next : undefined;
}

/** `layout` with `patch` applied to its context (a null value resets that field). */
export function withContext<Id extends string>(
  layout: DashboardLayout<Id>,
  patch: { framework?: string | null; period?: CtemContextPeriod | null },
): DashboardLayout<Id> {
  const merged: CtemDashboardContext = { ...layout.context };
  if ("framework" in patch) merged.framework = patch.framework ?? undefined;
  if ("period" in patch) merged.period = patch.period ?? undefined;
  const context = compactContext(merged);
  const { context: _old, ...rest } = layout;
  return context ? { ...rest, context } : rest;
}

/** `layout` with the context back to its defaults. */
export function withDefaultContext<Id extends string>(layout: DashboardLayout<Id>): DashboardLayout<Id> {
  const { context: _old, ...rest } = layout;
  return rest;
}

/** `layout` with one widget's own settings (`null` period: it reads the dashboard's again). */
export function withWidgetSettings<Id extends string>(
  layout: DashboardLayout<Id>,
  id: Id,
  settings: { period: CtemContextPeriod | null },
): DashboardLayout<Id> {
  return {
    ...layout,
    items: layout.items.map((item) => {
      if (item.i !== id) return item;
      const { settings: _old, ...rest } = item;
      const own: CtemWidgetSettings = {};
      if (settings.period !== null && isContextPeriod(settings.period)) own.period = settings.period;
      return Object.keys(own).length > 0 ? { ...rest, settings: own } : rest;
    }),
  };
}

/** The widgets shown with their own period, in layout order. */
export function overriddenWidgets(layout: DashboardLayout<CtemWidgetId>): { id: CtemWidgetId; period: CtemContextPeriod }[] {
  return layout.items.flatMap((item) =>
    isContextPeriod(item.settings?.period) ? [{ id: item.i, period: item.settings.period }] : [],
  );
}

/** Whether the context holds a value other than the default. */
export function contextIsDefault(layout: Pick<DashboardLayout, "context">): boolean {
  const { framework, period } = readContext(layout);
  return framework === null && period === DEFAULT_PERIOD;
}
