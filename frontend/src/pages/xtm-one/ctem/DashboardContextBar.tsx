// SPDX-License-Identifier: Apache-2.0
/**
 * The context bar of the CTEM command center (dev-docs/ctem-stage-viz.md 8.2,
 * slice S2f): one row of chips under the page's top line, each a select, that
 * every widget reads. A chip holding a value other than the default is filled
 * and "Reset" brings the defaults back. The context is saved with the person's
 * layout of the vertical shown.
 *
 * - Framework: the kill chains the data carries (8.1), each with the count of
 *   techniques carrying data; one with no data is listed with "no data". The
 *   default is the one with the most data. The Validation stage page reads the
 *   same pick (its matrix opens on it). A saved framework the data no longer
 *   carries shows the effective one, unfilled (UX round 1, B1); the page draws
 *   the one-line notice under the toolbar row.
 * - Period: 7, 30 or 90 days, 30 by default. The widgets drawn from the
 *   posture history read it, unless a widget has its own (8.3); the bar names
 *   how many do.
 *
 * Scope, asset groups, intel type and sources of 8.2 wait for the data that
 * fills them; they are not drawn empty.
 */
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CtemContextPeriod } from "@/types";
import { frameworkLabel } from "./ctem-framework-labels";
import { CONTEXT_PERIODS, DEFAULT_PERIOD, type ContextFramework } from "./dashboard-context";

/** The value of the Framework select that stands for "the one with the most data". */
const AUTO = "__auto__";

export interface DashboardContextBarProps {
  frameworks: readonly ContextFramework[];
  /** The framework the context names; null: the default. */
  framework: string | null;
  /** The framework drawn when none is named (the one with the most data). */
  defaultFramework: string | null;
  /** The context named a framework the data no longer carries: the chip shows
   *  the default, unfilled, and Reset clears the stale value. */
  frameworkFellBack: boolean;
  period: CtemContextPeriod;
  /** The labels of the widgets with their own period. */
  overridden: readonly { label: string; period: CtemContextPeriod }[];
  onFramework: (id: string | null) => void;
  onPeriod: (period: CtemContextPeriod) => void;
  onReset: () => void;
}

const CHIP = "h-8 min-w-0 max-w-64 gap-1.5 content-compact";
const CHIP_SET = "bg-accent/15 border-accent/30 text-accent";

export function DashboardContextBar({
  frameworks,
  framework,
  defaultFramework,
  frameworkFellBack,
  period,
  overridden,
  onFramework,
  onPeriod,
  onReset,
}: DashboardContextBarProps) {
  const { t_i18n } = useFormatter();
  const nf = useNumberFormat();
  const days = (count: number) => t_i18n("{count, plural, one {Last # day} other {Last # days}}", { count });
  const isDefault = framework === null && period === DEFAULT_PERIOD;
  const frameworkName = (id: string | null) => {
    const found = frameworks.find((f) => f.id === id);
    return found ? frameworkLabel(found.label) : "";
  };
  const defaultName = frameworkName(defaultFramework);
  // The framework the chip shows as picked: a stale one reads as the default.
  const shownFramework = frameworkFellBack ? null : framework;

  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label={t_i18n("Dashboard context")} data-testid="ctem-context-bar">
      <span className="content-caption uppercase tracking-wider text-default-secondary">{t_i18n("Context")}</span>
      {frameworks.length > 0 && (
        <Select value={shownFramework ?? AUTO} onValueChange={(value) => onFramework(value === AUTO ? null : value)}>
          <SelectTrigger
            className={cn(CHIP, shownFramework !== null && CHIP_SET)}
            aria-label={t_i18n("Framework")}
            data-testid="ctem-context-framework"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={AUTO}>
              {defaultName ? t_i18n("Most data ({name})", { name: defaultName }) : t_i18n("Most data")}
            </SelectItem>
            {frameworks.map((f) => (
              <SelectItem key={f.id} value={f.id}>
                {f.count > 0 ? `${frameworkLabel(f.label)} · ${nf.count(f.count)}` : `${frameworkLabel(f.label)} · ${t_i18n("no data")}`}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <Select value={String(period)} onValueChange={(value) => onPeriod(Number(value) as CtemContextPeriod)}>
        <SelectTrigger
          className={cn(CHIP, period !== DEFAULT_PERIOD && CHIP_SET)}
          aria-label={t_i18n("Period")}
          data-testid="ctem-context-period"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {CONTEXT_PERIODS.map((p) => (
            <SelectItem key={p} value={String(p)}>
              {days(p)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {overridden.length > 0 && (
        <Tip
          content={overridden.map((o) => `${o.label}: ${days(o.period)}`).join(" · ")}
        >
          <span
            tabIndex={0}
            className="inline-flex h-8 items-center rounded-sm border border-elevation-subtle-soft px-2 content-compact text-default-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            data-testid="ctem-context-overridden"
          >
            {t_i18n("{count, plural, one {# widget has its own period} other {# widgets have their own period}}", {
              count: overridden.length,
            })}
          </span>
        </Tip>
      )}
      {!isDefault && (
        <Button variant="ghost" size="sm" className="h-8 content-compact" onClick={onReset} data-testid="ctem-context-reset">
          {t_i18n("Reset")}
        </Button>
      )}
    </div>
  );
}
