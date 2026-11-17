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
 * One fleet that is on, as a tile of the home Fleets widget (#11996, design
 * rounds 1 to 3 on the issue): who it is (icon in its colour, name, agents),
 * how it is (one health chip that says the count behind it), three KPIs
 * (automations on of all with the off ones named on hover, the success rate
 * of the last 7 days, open work items), the runs of the last 7 days as solid
 * stacked bars (succeeded under failed, one per day, each named for assistive
 * tech) and its last run with the one-line result of the newest ended run.
 *
 * Every figure comes from `GET /fleets/{id}/health` (`lib/fleet-activity`).
 * The name and the failing chip open the fleet's About page; nothing here
 * switches a fleet on or off. Domain neutral: the CTEM command center's
 * Fleets row and the Copilot home's Fleets widget (#12063) draw it alike.
 */
import { Chip, Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { ICON_MAP } from "@/lib/constants";
import { WIDGET_DAYS, successRate, type FleetActivityTile } from "@/lib/fleet-activity";
import { formatTimeAgo } from "@/lib/relative-time";
import { cn } from "@/lib/utils";

/** The Fleets glyph from the library, for a slot that renders an icon component with a className (EmptyState). */
export function FleetsGlyph({ className }: { className?: string }) {
  return <Icon name="network" size={16} className={className} aria-hidden />;
}

/** The library's open-elsewhere arrow, for the same kind of slot (an EmptyState action). */
export function OpenGlyph({ className }: { className?: string }) {
  return <Icon name="arrow-up-right" size={16} className={className} aria-hidden />;
}

/**
 * The whole "last run" sentence per outcome: one key per status, so each
 * language agrees the outcome with "run" (es "fallida", fr "terminée") instead
 * of reusing a generic word whose catalogs serve a count ("failed"). @i18n-keys
 */
const LAST_RUN_SENTENCES = {
  running: "Last run {when}, running",
  pending: "Last run {when}, pending",
  completed: "Last run {when}, completed",
  failed: "Last run {when}, failed",
  cancelled: "Last run {when}, cancelled",
  unknown: "Last run {when}, status unknown",
} as const;

/** A known outcome's sentence, else the unknown one (a future backend status is never shown raw). */
export const lastRunSentence = (status: string | null): string =>
  (status && (LAST_RUN_SENTENCES as Record<string, string | undefined>)[status]) || LAST_RUN_SENTENCES.unknown;

const BAR_OK = "var(--color-feedback-success-primary)";
const BAR_FAILED = "var(--color-feedback-error-primary)";

/**
 * The last {@link WIDGET_DAYS} days, oldest first, today last: the short weekday shown under
 * each bar (today too, so no label is cut in a narrow column: round 1 B3 on #12015) and the
 * name assistive tech reads, where today is said "Today".
 */
function dayLabels(
  intl: { formatDate: (d: Date, o: Intl.DateTimeFormatOptions) => string },
  today: string,
  now = new Date(),
): { shown: string; spoken: string }[] {
  return Array.from({ length: WIDGET_DAYS }, (_, i) => {
    const back = WIDGET_DAYS - 1 - i;
    const day = new Date(now);
    day.setDate(now.getDate() - back);
    const shown = intl.formatDate(day, { weekday: "short" });
    return { shown, spoken: back === 0 ? today : shown };
  });
}

/** The fleet's health in one chip that names the count behind it (round 2, B2). */
function HealthChip({ tile, onOpen }: { tile: FleetActivityTile; onOpen: () => void }) {
  const { t_i18n } = useFormatter();
  if (tile.status === "failing" && tile.memberCount) {
    const label = t_i18n("{total, plural, =0 {No agent failing} one {# agent failing} other {{failing} of # agents failing}}", {
      failing: tile.failingMembers ?? 0,
      total: tile.memberCount,
    });
    return (
      <button
        type="button"
        onClick={onOpen}
        className={cn("inline-flex rounded-sm leading-none", TIP_WRAP_FOCUS_RING)}
        aria-label={label}
        data-testid="fleet-tile-health"
      >
        <Chip label={label} severity="critical" />
      </button>
    );
  }
  if (tile.status === "stale") {
    const label = t_i18n("{count, plural, one {# stale work item} other {# stale work items}}", { count: tile.staleWorkItems ?? 0 });
    return <Chip label={label} severity="medium" data-testid="fleet-tile-health" />;
  }
  if (tile.status === "disabled") {
    const total = tile.automationsTotal ?? 0;
    const label =
      total > 0
        ? t_i18n("{off} of {total, plural, one {# automation} other {# automations}} off", { off: total - (tile.automationsOn ?? 0), total })
        : t_i18n("Agents off");
    return <Chip label={label} severity="medium" data-testid="fleet-tile-health" />;
  }
  if (tile.status === "idle") return <Chip label={t_i18n("Idle")} severity="neutral" data-testid="fleet-tile-health" />;
  return <Chip label={t_i18n("Healthy")} severity="low" data-testid="fleet-tile-health" />;
}

/**
 * One KPI box: its label over its value, the value's explanation on hover and focus. Every box
 * fills its grid cell and lets its label and value wrap on two lines, so the three boxes of a
 * tile share one width and one height in every language (round 1 B3 on #12015).
 */
function Kpi({ label, value, tip, testId }: { label: string; value: string; tip?: string; testId: string }) {
  const body = (
    <span className="flex h-full w-full min-w-0 flex-col justify-between gap-0.5 rounded-sm bg-elevation-hover px-2 py-1.5" data-testid={testId}>
      <span className="line-clamp-2 break-words content-caption text-default-secondary" title={label}>
        {label}
      </span>
      <span className="line-clamp-2 break-words content-base font-semibold tabular-nums" title={value}>
        {value}
      </span>
    </span>
  );
  if (!tip) return body;
  return (
    <Tip content={tip}>
      <span tabIndex={0} className={cn("flex h-full w-full min-w-0 rounded-sm", TIP_WRAP_FOCUS_RING)}>
        {body}
      </span>
    </Tip>
  );
}

/** The runs of the last 7 days: one solid stacked bar per day, succeeded under failed. */
function RunBars({ ok, failed }: { ok: readonly number[]; failed: readonly number[] }) {
  const { t_i18n, intl } = useFormatter();
  const labels = dayLabels(intl, t_i18n("Today"));
  const max = Math.max(1, ...ok.map((v, i) => v + (failed[i] ?? 0)));
  return (
    <div className="grid grid-cols-7 gap-1" data-testid="fleet-tile-bars">
      {ok.map((v, i) => {
        const f = failed[i] ?? 0;
        return (
          <div key={i} className="flex min-w-0 flex-col items-center gap-1">
            <span
              role="img"
              aria-label={t_i18n("{day}: {ok} succeeded, {failed} failed", { day: labels[i].spoken, ok: v, failed: f })}
              className="flex h-12 w-full max-w-6 flex-col justify-end"
              data-testid="fleet-tile-bar"
            >
              {f > 0 && <span className="w-full rounded-t-sm" style={{ height: `${(f / max) * 100}%`, backgroundColor: BAR_FAILED }} />}
              {v > 0 && (
                <span className={cn("w-full", f > 0 ? "" : "rounded-t-sm")} style={{ height: `${(v / max) * 100}%`, backgroundColor: BAR_OK }} />
              )}
              {v + f === 0 && <span className="h-px w-full bg-[var(--border-elevation-subtle)]" />}
            </span>
            <span className="w-full truncate text-center content-caption text-default-secondary" aria-hidden>
              {labels[i].shown}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function FleetTile({
  tile,
  onOpen,
  onHide,
  className,
}: {
  tile: FleetActivityTile;
  onOpen: () => void;
  onHide?: () => void;
  className?: string;
}) {
  const { t_i18n, intl } = useFormatter();
  const numbers = useNumberFormat();
  const FleetIcon = tile.icon ? ICON_MAP[tile.icon] : undefined;
  const ok = tile.succeededByDay ?? Array<number>(WIDGET_DAYS).fill(0);
  const failed = tile.failedByDay ?? Array<number>(WIDGET_DAYS).fill(0);
  const rate = successRate(ok, failed);
  const runs = ok.reduce((a, b) => a + b, 0) + failed.reduce((a, b) => a + b, 0);
  const failedRuns = failed.reduce((a, b) => a + b, 0);
  const off = tile.offAutomations ?? [];
  const automations =
    tile.automationsTotal != null ? `${numbers.count(tile.automationsOn ?? 0)}/${numbers.count(tile.automationsTotal)}` : "-";
  // Five sections (header, health, KPIs, runs, last run) on the five rows of the list's subgrid:
  // the tiles of one row share each section's height, so one wrapping value moves the whole row (round 2 B1 on #12015).
  return (
    <li
      className={cn("row-span-5 grid min-w-0 grid-rows-subgrid gap-y-3 rounded-md border border-[var(--border-elevation-subtle)] p-3", className)}
      data-testid="fleet-tile"
    >
      <div className="flex items-start gap-3">
        <span
          aria-hidden
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm"
          style={{ backgroundColor: `color-mix(in srgb, ${tile.color} 20%, transparent)`, color: tile.color }}
        >
          {FleetIcon ? <FleetIcon className="h-4 w-4" /> : <Icon name="network" size={16} />}
        </span>
        {/* Two lines for the name in every tile, so the KPI strips of one row start at the same y (round 3, B3). */}
        <span className="flex min-h-10 min-w-0 flex-1 flex-col">
          <button
            type="button"
            onClick={onOpen}
            aria-label={t_i18n("Open fleet {name}", { name: tile.name })}
            className={cn("line-clamp-2 min-w-0 rounded-sm text-left content-compact font-semibold break-words hover:underline", TIP_WRAP_FOCUS_RING)}
            title={tile.name}
          >
            {tile.name}
          </button>
          <span className="content-caption text-default-secondary">{t_i18n("{count, plural, one {# agent} other {# agents}}", { count: tile.members })}</span>
        </span>
        {onHide && (
          <Tip content={t_i18n("Hide this fleet from the page")}>
            <Button variant="ghost" size="icon" className="h-9 w-9 shrink-0" onClick={onHide} aria-label={t_i18n("Hide {name} from this page", { name: tile.name })}>
              <Icon name="eye-off" size={16} />
            </Button>
          </Tip>
        )}
      </div>
      <div className="flex">
        <HealthChip tile={tile} onOpen={onOpen} />
      </div>
      <div className="grid grid-cols-3 items-stretch gap-2">
        <Kpi
          label={t_i18n("Automations")}
          value={automations}
          tip={off.length > 0 ? t_i18n("Off: {list}", { list: intl.formatList(off, { type: "conjunction" }) }) : t_i18n("Every automation is on")}
          testId="fleet-tile-automations"
        />
        <Kpi
          label={t_i18n("Success rate")}
          value={rate == null ? t_i18n("No runs") : numbers.percent(rate)}
          tip={
            rate == null
              ? t_i18n("No run ended in the last 7 days, so there is no rate yet.")
              : t_i18n("Runs that ended in success, of the runs that ended in the last 7 days. Interrupted runs are not counted.")
          }
          testId="fleet-tile-success"
        />
        <Kpi label={t_i18n("Open work items")} value={tile.openWorkItems == null ? "-" : numbers.count(tile.openWorkItems)} testId="fleet-tile-open" />
      </div>
      <div className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-x-2 content-caption text-default-secondary">
          <span>{t_i18n("Runs, last 7 days")}</span>
          <span className="tabular-nums" data-testid="fleet-tile-runs">
            {t_i18n("{runs, plural, one {# run} other {# runs}} · {failed} failed", { runs, failed: failedRuns })}
          </span>
        </div>
        <RunBars ok={ok} failed={failed} />
      </div>
      <div className="flex min-w-0 flex-col gap-0.5 border-t border-[var(--border-elevation-subtle-soft)] pt-2">
        <span className="content-compact text-default-secondary" data-testid="fleet-tile-last-run">
          {tile.lastRunAt
            ? t_i18n(lastRunSentence(tile.lastRunStatus), { when: formatTimeAgo(intl, tile.lastRunAt) })
            : t_i18n("No run yet")}
        </span>
        {tile.lastRunAt && (
          <span className="truncate content-compact" title={tile.lastSummary ?? undefined} data-testid="fleet-tile-summary">
            {tile.lastSummary ?? <span className="text-default-secondary">{t_i18n("No summary for this run")}</span>}
          </span>
        )}
      </div>
    </li>
  );
}
