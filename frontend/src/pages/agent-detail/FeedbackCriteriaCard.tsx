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

import { useMemo } from "react";
import { useNow } from "@/hooks/useTick";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Tip } from "@/components/ui/tooltip";
import { FEEDBACK_CRITERION_TYPE_CONFIG, FEEDBACK_CRITERION_TYPES } from "@/lib/constants";
import { criterionTimeline, scorePercent } from "@/lib/feedback-criteria";
import { cn, formatCompactNumber, formatShortDate } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { CriterionSummary } from "@/types";

const TIMELINE_DAYS = 30;

function Histogram({ criterion }: { criterion: CriterionSummary }) {
  const peak = Math.max(1, ...criterion.histogram.map((bucket) => bucket.count));
  const buckets = criterion.histogram.map((bucket) => ({
    ...bucket,
    content: `${bucket.label}: ${formatCompactNumber(bucket.count)}`,
  }));
  return (
    <>
      <div className="flex h-8 items-end gap-0.5" aria-hidden>
        {buckets.map((bucket) => (
          <Tip key={bucket.key} content={bucket.content}>
            <div className="flex h-full w-3 items-end">
              <div
                className={cn("w-full rounded-sm", bucket.count > 0 ? "bg-accent/70" : "bg-elevation-hover")}
                style={{ height: `${Math.max(8, (bucket.count / peak) * 100)}%` }}
              />
            </div>
          </Tip>
        ))}
      </div>
      {/* The bars' tooltips need a pointer: the same values, for assistive technology. */}
      <ul className="sr-only">
        {buckets.map((bucket) => (
          <li key={bucket.key}>{bucket.content}</li>
        ))}
      </ul>
    </>
  );
}

function Timeline({ criterion }: { criterion: CriterionSummary }) {
  const { t_i18n } = useFormatter();
  const today = new Date(useNow());
  const slots = criterionTimeline(criterion.daily, TIMELINE_DAYS, today).map(({ date, day }) => {
    // A UTC day read at its midday is the same calendar day in every
    // timezone within 12 hours of UTC.
    const label = formatShortDate(`${date}T12:00:00Z`);
    const content = day
      ? t_i18n("{date}: {score}% from {count} scores", {
          date: label,
          score: scorePercent(day.mean_score) ?? 0,
          count: formatCompactNumber(day.count),
        })
      : t_i18n("{date}: no scores", { date: label });
    return { date, day, content };
  });
  return (
    <>
      <div className="flex h-8 items-end gap-px" aria-hidden>
        {slots.map(({ date, day, content }) => (
          <Tip key={date} content={content}>
            <div className="flex h-full w-1.5 items-end">
              <div
                className={cn("w-full rounded-sm", day ? "bg-accent/70" : "bg-elevation-hover")}
                style={{ height: day ? `${Math.max(8, day.mean_score * 100)}%` : "8%" }}
              />
            </div>
          </Tip>
        ))}
      </div>
      {/* The days' tooltips need a pointer: the scored days, for assistive technology. */}
      <ul className="sr-only">
        {slots
          .filter(({ day }) => day)
          .map(({ date, content }) => (
            <li key={date}>{content}</li>
          ))}
      </ul>
    </>
  );
}

/** The criterion's state, as the Status filter and the badge read it. */
const stateOf = (criterion: CriterionSummary) => (criterion.enabled ? "enabled" : "disabled");

/**
 * Per-criterion scores of an agent's work: how many, how good, how spread, and lately.
 * A bounded list (the criteria an administrator defined): client-side search, filters and sort.
 */
export function FeedbackCriteriaCard({ criteria }: { criteria: CriterionSummary[] }) {
  const { t_i18n } = useFormatter();
  const columns = useMemo<DataTableColumn<CriterionSummary>[]>(
    () => [
      {
        key: "name",
        header: "Criterion",
        // The column takes the free width and cuts a long name on one line (no sideways scroll at
        // 1024); the full name is in the tooltip, reachable by keyboard, the Disabled badge stays whole.
        className: "w-full max-w-0",
        cell: (criterion) => (
          <div className="flex min-w-0 items-center gap-2">
            <Tip content={criterion.name}>
              <span
                tabIndex={0}
                data-testid="feedback-criterion-name"
                className="min-w-0 truncate rounded-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-focus"
              >
                {criterion.name}
              </span>
            </Tip>
            {!criterion.enabled && (
              <Badge variant="outline" className="shrink-0">
                {t_i18n("Disabled")}
              </Badge>
            )}
          </div>
        ),
        sortValue: (criterion) => criterion.name,
        csv: (criterion) => criterion.name,
      },
      {
        key: "type",
        header: "Type",
        cell: (criterion) => (
          <span className="whitespace-nowrap text-default-secondary">
            {t_i18n(FEEDBACK_CRITERION_TYPE_CONFIG[criterion.type].label)}
          </span>
        ),
        sortValue: (criterion) => t_i18n(FEEDBACK_CRITERION_TYPE_CONFIG[criterion.type].label),
        csv: (criterion) => t_i18n(FEEDBACK_CRITERION_TYPE_CONFIG[criterion.type].label),
      },
      {
        key: "weight",
        header: "Weight",
        cell: (criterion) => criterion.weight,
        sortValue: (criterion) => criterion.weight,
        csv: (criterion) => criterion.weight,
        align: "right",
      },
      {
        key: "count",
        header: "Scores",
        cell: (criterion) => formatCompactNumber(criterion.count),
        sortValue: (criterion) => criterion.count,
        csv: (criterion) => criterion.count,
        align: "right",
      },
      {
        key: "average",
        header: "Average",
        cell: (criterion) => {
          const average = scorePercent(criterion.mean_score);
          return average === null ? "-" : `${average}%`;
        },
        sortValue: (criterion) => scorePercent(criterion.mean_score),
        csv: (criterion) => scorePercent(criterion.mean_score),
        align: "right",
      },
      { key: "distribution", header: "Distribution", cell: (criterion) => <Histogram criterion={criterion} />, hideable: true },
      { key: "timeline", header: "Last 30 days", cell: (criterion) => <Timeline criterion={criterion} />, hideable: true },
    ],
    [t_i18n],
  );
  const filters = useMemo<DataTableFilter<CriterionSummary>[]>(
    () => [
      {
        key: "type",
        label: "Type",
        values: (criterion) => [criterion.type],
        options: FEEDBACK_CRITERION_TYPES.map((type) => ({ value: type, label: t_i18n(FEEDBACK_CRITERION_TYPE_CONFIG[type].label) })),
      },
      {
        key: "status",
        label: "Status",
        values: (criterion) => [stateOf(criterion)],
        options: [
          { value: "enabled", label: t_i18n("Enabled") },
          { value: "disabled", label: t_i18n("Disabled") },
        ],
      },
    ],
    [t_i18n],
  );
  return (
    <section className="space-y-2">
      <h3 className="content-base font-medium text-default-secondary">{t_i18n("Feedback criteria")}</h3>
      <DataTable
        rows={criteria}
        columns={columns}
        rowKey={(criterion) => criterion.criterion_id}
        searchFields={(criterion) => [criterion.name, criterion.key]}
        searchPlaceholder={t_i18n("Search...")}
        filters={filters}
        exportName="feedback-criteria"
        urlKey="fcr_"
        storageKey="agent-feedback-criteria"
        emptyLabel={t_i18n("No feedback for this agent yet.")}
      />
    </section>
  );
}
