// SPDX-License-Identifier: Apache-2.0

import { DateFilterInput } from "@/components/ui/date-filter-input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { DATE_RANGE_PRESETS, DATE_RANGE_PRESET_LABELS, NO_DATE_RANGE, type DateRange } from "@/lib/date-range";

interface DateRangeFilterProps {
  /** The range the page keeps in its URL (`lib/date-range.ts`). */
  value: DateRange;
  /** Called with the whole new range: a preset clears the days, a day clears the preset. */
  onChange: (next: DateRange) => void;
  /** Inside a Filters panel: the preset fills its row and the From / To days go on the row under it,
   *  so the range never widens the toolbar's one row (#10215). */
  stacked?: boolean;
}

/**
 * The time range of a list page's filter row: a preset ("Last 7 days", or
 * "All time") or a custom From / To day. The page resolves it to the instants
 * it sends with `resolveDateRange()`.
 */
export function DateRangeFilter({ value, onChange, stacked = false }: DateRangeFilterProps) {
  const { t_i18n } = useFormatter();
  const days = (
    <>
      <DateFilterInput aria-label={t_i18n("From")} value={value.from} onChange={(from) => onChange({ preset: "", from, to: value.to })} />
      <span aria-hidden className="text-default-secondary">
        –
      </span>
      <DateFilterInput aria-label={t_i18n("To")} value={value.to} onChange={(to) => onChange({ preset: "", from: value.from, to })} />
    </>
  );
  return (
    <div className={cn(stacked ? "space-y-2" : "flex items-center gap-2")}>
      <SearchableSelect
        aria-label={t_i18n("Time range")}
        className={stacked ? "w-full" : "w-44"}
        searchable={false}
        sort={false}
        options={DATE_RANGE_PRESETS.map((key) => ({ value: key, label: t_i18n(DATE_RANGE_PRESET_LABELS[key]) }))}
        pinned={[{ value: "all", label: t_i18n("All time") }]}
        value={value.preset || (value.from || value.to ? "" : "all")}
        onValueChange={(preset) => onChange(preset === "all" ? NO_DATE_RANGE : { preset, from: "", to: "" })}
        placeholder={t_i18n("Custom range")}
      />
      {stacked ? <div className="flex items-center gap-2">{days}</div> : days}
    </div>
  );
}
