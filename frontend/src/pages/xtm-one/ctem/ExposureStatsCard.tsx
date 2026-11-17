// SPDX-License-Identifier: Apache-2.0

/**
 * Exposure stats widget of the CTEM command center (#9050 slice 4): the
 * posture score and each stage's score now against the first recorded day of
 * the period (up to 90 days of `GET /ctem-metrics/posture/history`), with the
 * trend lines (`lib/ctem-coverage-stats.ts`). Deterministic: no model, no AI
 * gate. A change that cannot be read (no score now, no recorded day) is a dash
 * with its reason, never 0.
 */
import { useId } from "react";
import { LineChart, TrendingDown, TrendingUp } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING, buttonRoleProps } from "@/lib/button-role";
import { cn } from "@/lib/utils";
import type { ExposureStats } from "@/lib/ctem-coverage-stats";
import { MiniTrend } from "./ctem-viz";
import { stageName } from "./stats-coverage";

/**
 * A change over the period, or a dash with its reason. Standalone, the dash is
 * its own tab stop so the reason is reachable from the keyboard. Inside an
 * interactive row (`whyId` given) it is not focusable: the row is the single
 * tab stop and points at the reason with aria-describedby.
 */
function Change({ value, why, whyId }: { value: number | null; why: string; whyId?: string }) {
  const { t_i18n } = useFormatter();
  if (value === null) {
    const inRow = whyId !== undefined;
    return (
      <Tip content={why}>
        <span
          tabIndex={inRow ? undefined : 0}
          className={cn("content-compact tabular-nums text-default-disabled", !inRow && TIP_WRAP_FOCUS_RING)}
        >
          <span aria-hidden="true">-</span>
          <span id={whyId} className="sr-only">
            {why}
          </span>
        </span>
      </Tip>
    );
  }
  const Icon = value < 0 ? TrendingDown : TrendingUp;
  return (
    <span
      className={cn(
        "content-compact tabular-nums inline-flex items-center gap-1",
        value > 0 && "text-feedback-success-primary",
        value < 0 && "text-feedback-error-primary",
        value === 0 && "text-default-secondary",
      )}
    >
      {value !== 0 && <Icon className="h-3 w-3" aria-hidden="true" />}
      {value > 0 ? `+${value}` : String(value)}
      <span className="sr-only">{t_i18n("points over the period")}</span>
    </span>
  );
}

export function ExposureStatsCard({ stats }: { stats: ExposureStats | null }) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const rowId = useId();
  const noChange = t_i18n("Needs a score now and a recorded day in the period");

  return (
    <Card className="@container h-full overflow-hidden flex flex-col">
      <CardContent className="p-4 flex flex-col flex-1 min-h-0 gap-3">
        <div className="flex min-h-7 items-center justify-between gap-2">
          <h2 className="content-base font-semibold flex items-center gap-2">
            <LineChart className="h-4 w-4 text-accent" /> {t_i18n("Exposure stats")}
          </h2>
          {stats?.since && (
            <span className="content-caption text-default-secondary tabular-nums">
              {t_i18n("Since {date}, {count, plural, one {# day recorded} other {# days recorded}}", {
                date: intl.formatDate(stats.since, { day: "numeric", month: "short" }),
                count: stats.daysRecorded,
              })}
            </span>
          )}
        </div>
        {!stats ? (
          <EmptyState
            variant="inset"
            icon={LineChart}
            title={t_i18n("Stats not read yet")}
            description={t_i18n("The stats show once the exposure posture is read from the connected platforms.")}
          />
        ) : (
          <>
            <div className="grid grid-cols-3 gap-3">
              <div className="min-w-0">
                <p className="content-caption text-default-secondary">{t_i18n("Exposure posture")}</p>
                <p className="content-base font-semibold tabular-nums">
                  {stats.postureScore === null ? "-" : stats.postureScore}
                </p>
                <Change value={stats.postureChange} why={noChange} />
              </div>
              <div className="min-w-0">
                <p className="content-caption text-default-secondary">{t_i18n("Stages improved")}</p>
                <p className="content-base font-semibold tabular-nums">{stats.improved}</p>
              </div>
              <div className="min-w-0">
                <p className="content-caption text-default-secondary">{t_i18n("Stages declined")}</p>
                <p className="content-base font-semibold tabular-nums">{stats.declined}</p>
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto space-y-1">
              {stats.stages.map((stage) => {
                const whyId = `${rowId}-${stage.key}-why`;
                return (
                  <div
                    key={stage.key}
                    {...buttonRoleProps(() => navigate(`/ctem/${stage.key}`))}
                    aria-label={t_i18n("Open stage {stage}", { stage: stageName(stage.key, stage.label, t_i18n) })}
                    aria-describedby={stage.change === null ? whyId : undefined}
                    className={cn(
                      "grid grid-cols-[minmax(0,1fr)_5rem_2.5rem_3rem] items-center gap-2 rounded-sm px-1.5 py-1 cursor-pointer hover:bg-elevation-hover",
                      BUTTON_ROLE_FOCUS_RING,
                    )}
                  >
                    <span className="content-compact truncate">{stageName(stage.key, stage.label, t_i18n)}</span>
                    {/* Two recorded scores at least: never the greyed sample curve here. */}
                    <span className="h-5">
                      {stage.series.filter((v) => v !== null).length >= 2 && <MiniTrend values={stage.series} />}
                    </span>
                    <span className="content-compact tabular-nums text-right">
                      {stage.score === null ? "-" : stage.score}
                    </span>
                    <span className="text-right">
                      <Change value={stage.change} why={noChange} whyId={whyId} />
                    </span>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
