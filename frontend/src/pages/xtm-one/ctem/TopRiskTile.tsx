// SPDX-License-Identifier: Apache-2.0

/**
 * The top risk tile of the command center hero (S2b of #11547, design note
 * section 4, row `hero`): the known-exploited exposures beside the posture
 * gauge, so a reader names the posture and the top risk within five seconds.
 *
 * - the figure is the posture's known-exploited count, restricted to the
 *   organization's software inventory when the backend could match it (the same
 *   figure the Act now row reads), compact, its exact value on hover;
 * - the sparkline is the same count over the recorded days of
 *   `GET /ctem-metrics/posture/history` at a fixed token height (h-8), so
 *   the tile never grows the fixed-height hero; fewer than two recorded
 *   days: no line, one caption, never a made-up trend;
 * - the whole tile opens the in-app Prioritization page;
 * - OpenCTI not connected: the not-configured state, one action to connect
 *   it, never a number.
 */
import { Icon } from "@filigran/design-system";

import { CompactFigure } from "@/components/ui/compact-figure";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING, buttonRoleProps } from "@/lib/button-role";
import { exactCount, formatCount } from "@/lib/number-format";
import { cn } from "@/lib/utils";
import { MiniTrend, signedDelta } from "./ctem-viz";
import { seriesChange } from "./trend-window";

export interface TopRiskTileProps {
  /** Known-exploited exposures now; `null` when OpenCTI is not connected. */
  count: number | null;
  /** The count on each recorded day, oldest first (`null` on a day without it). */
  history: (number | null)[];
  /** True when the count is restricted to the organization's software inventory. */
  tailored: boolean;
  /** Opens the Prioritization page. */
  onOpen: () => void;
  /** Opens the dialog connecting OpenCTI (not-configured state). */
  onConnect: () => void;
  className?: string;
}

export function TopRiskTile({ count, history, tailored, onOpen, onConnect, className }: TopRiskTileProps) {
  const { t_i18n, intl } = useFormatter();
  const title = t_i18n("Top risk");

  if (count == null) {
    return (
      <section
        className={cn("flex min-w-0 flex-col gap-2 rounded-sm border border-dashed border-elevation-subtle-soft p-3", className)}
        data-testid="ctem-top-risk"
        data-state="not-configured"
      >
        <h3 className="flex items-center gap-1.5 content-compact font-medium text-default-secondary">
          <Icon name="triangle-alert" size={16} className="shrink-0" /> {title}
        </h3>
        <p className="content-compact text-default-secondary">{t_i18n("Known-exploited exposures")}</p>
        <button
          type="button"
          onClick={onConnect}
          className={cn("self-start content-caption font-medium text-accent hover:underline rounded-sm", BUTTON_ROLE_FOCUS_RING)}
        >
          {t_i18n("Connect OpenCTI")}
        </button>
      </section>
    );
  }

  const trend = seriesChange(history);
  const drawn = history.filter((v) => v != null).length >= 2;
  const label = tailored ? t_i18n("Known-exploited in your software") : t_i18n("Known-exploited exposures");
  return (
    <section
      className={cn(
        "flex min-w-0 flex-col gap-1 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3 cursor-pointer hover:bg-elevation-hover transition-colors",
        BUTTON_ROLE_FOCUS_RING,
        className,
      )}
      aria-label={`${title}: ${label}`}
      data-testid="ctem-top-risk"
      data-state="live"
      {...buttonRoleProps(onOpen)}
    >
      <h3 className="flex items-center gap-1.5 content-compact font-medium text-default-secondary">
        <Icon name="triangle-alert" size={16} className="shrink-0 text-feedback-error-primary" /> {title}
      </h3>
      <div className="flex items-baseline gap-2">
        <CompactFigure
          exact={exactCount(intl.locale, count)}
          className={cn("text-2xl font-bold leading-none", count > 0 ? "text-feedback-error-primary" : "text-default-primary")}
          testId="ctem-top-risk-count"
        >
          {formatCount(intl.locale, count)}
        </CompactFigure>
        {trend.change != null && (
          <span
            className={cn(
              "content-caption font-medium tabular-nums",
              // Fewer known-exploited exposures is better.
              trend.change < 0 ? "text-feedback-success-primary" : trend.change > 0 ? "text-feedback-error-primary" : "text-default-secondary",
            )}
            aria-label={t_i18n("{delta} since the start of the period", { delta: signedDelta(trend.change) })}
            data-testid="ctem-top-risk-delta"
          >
            {signedDelta(trend.change)}
          </span>
        )}
      </div>
      <p className="content-caption text-default-secondary truncate" title={label}>{label}</p>
      {drawn ? (
        <div className="h-8 shrink-0" data-testid="ctem-top-risk-trend">
          {/* A count, not a 0-100 score: the domain is the period's peak. */}
          <MiniTrend values={history} color="var(--color-feedback-error-primary)" yMax={Math.max(1, ...history.map((v) => v ?? 0))} />
        </div>
      ) : (
        <p className="content-caption text-default-disabled">{t_i18n("Trend after 2 days of history")}</p>
      )}
    </section>
  );
}
