// SPDX-License-Identifier: Apache-2.0

import { CalendarRange, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { MultiSelect } from "@/components/ui/multi-select";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import { PERIOD_LABELS, PERIODS } from "@/pages/analytics/constants";
import type { TraceAnalyticsPeriod } from "@/types";

const PERIOD_OPTIONS = PERIODS.map((key) => ({ key, label: PERIOD_LABELS[key] }));

interface Option {
  value: string;
  label: string;
}

export type CsvFilterKey = "group_ids" | "agent_ids" | "models" | "sources";

interface AnalyticsFiltersProps {
  period: TraceAnalyticsPeriod;
  dateFrom: string;
  dateTo: string;
  selected: Record<CsvFilterKey, string[]>;
  groupOptions: Option[];
  agentOptions: Option[];
  modelOptions: Option[];
  sourceOptions: Option[];
  /** A manager's groups are their teams: the picker says so. */
  managerScope: boolean;
  onPeriodChange: (period: TraceAnalyticsPeriod) => void;
  onDateFromChange: (value: string) => void;
  onDateToChange: (value: string) => void;
  onToggle: (key: CsvFilterKey, value: string) => void;
  onClear: () => void;
}

/**
 * The dashboard's filters in two compact rows: the window first, as
 * the AI Adoption switch, then one picker per dimension; what narrows the
 * read shows as chips inside its picker, and one button clears them all.
 */
export function AnalyticsFilters({
  period,
  dateFrom,
  dateTo,
  selected,
  groupOptions,
  agentOptions,
  modelOptions,
  sourceOptions,
  managerScope,
  onPeriodChange,
  onDateFromChange,
  onDateToChange,
  onToggle,
  onClear,
}: AnalyticsFiltersProps) {
  const { t_i18n } = useFormatter();
  const active = Object.values(selected).reduce((count, values) => count + values.length, 0) + (period !== "30d" ? 1 : 0);
  return (
    <Card className="space-y-2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <CalendarRange className="h-4 w-4 text-default-secondary" aria-hidden />
        <SegmentedFilter options={PERIOD_OPTIONS} value={period} onChange={onPeriodChange} ariaLabel={t_i18n("Time range")} />
        {period === "custom" && (
          <div className="flex items-center gap-2">
            <DateFilterInput aria-label={t_i18n("From")} value={dateFrom} onChange={onDateFromChange} />
            <span aria-hidden className="text-default-secondary">
              -
            </span>
            <DateFilterInput aria-label={t_i18n("To")} value={dateTo} onChange={onDateToChange} />
          </div>
        )}
        {active > 0 && (
          <Button variant="ghost" size="sm" className="ml-auto h-7 gap-1" onClick={onClear}>
            <X className="h-3.5 w-3.5" />
            {t_i18n("Clear filters ({count})", { count: active })}
          </Button>
        )}
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {groupOptions.length > 0 && (
          <MultiSelect
            options={groupOptions}
            selected={selected.group_ids}
            onToggle={(value) => onToggle("group_ids", value)}
            placeholder={managerScope ? t_i18n("All the groups you manage") : t_i18n("All groups")}
            searchPlaceholder={t_i18n("Search groups...")}
            emptyOptionsLabel={t_i18n("No groups available")}
            noMatchLabel={t_i18n("No matching groups")}
          />
        )}
        <MultiSelect
          options={agentOptions}
          selected={selected.agent_ids}
          onToggle={(value) => onToggle("agent_ids", value)}
          placeholder={t_i18n("All agents")}
          searchPlaceholder={t_i18n("Search agents...")}
          emptyOptionsLabel={t_i18n("No agents available")}
          noMatchLabel={t_i18n("No matching agents.")}
        />
        <MultiSelect
          options={modelOptions}
          sort={false}
          selected={selected.models}
          onToggle={(value) => onToggle("models", value)}
          placeholder={t_i18n("All models")}
          searchPlaceholder={t_i18n("Search models...")}
          emptyOptionsLabel={t_i18n("No models available yet")}
          noMatchLabel={t_i18n("No matching models.")}
        />
        <MultiSelect
          options={sourceOptions}
          selected={selected.sources}
          onToggle={(value) => onToggle("sources", value)}
          placeholder={t_i18n("All sources")}
          searchPlaceholder={t_i18n("Search sources...")}
          emptyOptionsLabel={t_i18n("No sources available")}
          noMatchLabel={t_i18n("No matching sources")}
        />
      </div>
    </Card>
  );
}
