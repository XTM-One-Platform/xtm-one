// SPDX-License-Identifier: Apache-2.0

/**
 * The faceted Filters panel of a DataTable (`facetGrid`, #2556): how many rows
 * each value of a filter would keep, and the active filters as chips.
 *
 * A count is faceted: the rows that pass every OTHER active filter and the
 * search, then carry that value. Picking a value with count N therefore shows
 * N rows (OR inside a filter, AND across filters), and a value of a filter
 * already narrowed still shows what adding it would bring. Computed on the rows
 * the table holds (client mode): no request.
 */
import { matchesFilters } from "@/lib/data-table-state";

export interface FacetFilter<T> {
  key: string;
  values?: (row: T) => readonly (string | null | undefined)[];
}

function rowValues<T>(filter: FacetFilter<T> | undefined, row: T): string[] {
  return (filter?.values?.(row) ?? []).filter((value): value is string => !!value);
}

/** Per filter key, the count of each of its values over `rows` (already narrowed by the search). */
export function facetValueCounts<T>(
  rows: readonly T[],
  filters: readonly FacetFilter<T>[],
  selected: Record<string, readonly string[]>,
): Record<string, Record<string, number>> {
  const byKey = new Map(filters.map((filter) => [filter.key, filter]));
  const counts: Record<string, Record<string, number>> = {};
  for (const filter of filters) {
    const others = Object.fromEntries(Object.entries(selected).filter(([key]) => key !== filter.key));
    const tally: Record<string, number> = {};
    for (const row of rows) {
      if (!matchesFilters(others, (key) => rowValues(byKey.get(key), row))) continue;
      for (const value of new Set(rowValues(filter, row))) tally[value] = (tally[value] ?? 0) + 1;
    }
    counts[filter.key] = tally;
  }
  return counts;
}

export interface ActiveFilterChip {
  key: string;
  /** The filter's label, as given (translated by the caller). */
  label: string;
  /** The labels of its selected values, in the order of its options. */
  values: string[];
}

/** The filters that narrow the list, one chip each, in the order of `filters`. */
export function activeFilterChips(
  filters: readonly { key: string; label: string }[],
  options: readonly (readonly { value: string; label: string }[])[],
  selected: Record<string, readonly string[]>,
): ActiveFilterChip[] {
  return filters.flatMap((filter, index) => {
    const picked = selected[filter.key] ?? [];
    if (picked.length === 0) return [];
    const known = options[index] ?? [];
    const ordered = [
      ...known.filter((option) => picked.includes(option.value)).map((option) => option.label),
      ...picked.filter((value) => !known.some((option) => option.value === value)),
    ];
    return [{ key: filter.key, label: filter.label, values: ordered }];
  });
}
