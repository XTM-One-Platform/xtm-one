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
 * Default / Customized / Custom (#9836 R3b, design dev-docs/roi-outcomes.md
 * 10.4 and the vocabulary of 10.6): the words, the table filter and the
 * "Changed by" line every ROI surface uses for where a value comes from.
 */
import type { DataTableFilter } from "@/components/ui/data-table";
import type { RoiOrigin, RoiOriginFields } from "@/hooks/api/useRoiCatalogApi";
import { useFormatter } from "@/i18n";

/** The words a person reads for each origin, never "Override" (10.6). */
export const ORIGIN_LABELS: Record<RoiOrigin, string> = {
  default: "Default",
  customized: "Customized",
  custom: "Custom",
};

/** The origin of a row, `default` when an older server answers none. */
export const originOf = (row: RoiOriginFields): RoiOrigin => row.origin ?? "default";

/** The Origin filter of a table (10.4, round 2 A5): the table's own pill, one value per origin. */
export function originFilter<T extends RoiOriginFields>(translate: (id: string) => string): DataTableFilter<T> {
  return {
    key: "origin",
    label: "Origin",
    values: (row) => [originOf(row)],
    options: (Object.keys(ORIGIN_LABELS) as RoiOrigin[]).map((value) => ({ value, label: translate(ORIGIN_LABELS[value]) })),
  };
}

/** "Changed by Jane Doe on 8 Nov 2026", or the parts the server knows; `null` on a default. */
export function useCustomizedLine() {
  const { t_i18n, intl } = useFormatter();
  return (row: RoiOriginFields): string | null => {
    if (originOf(row) === "default") return null;
    const at = row.customized_at ? Date.parse(row.customized_at) : Number.NaN;
    const date = Number.isNaN(at) ? null : intl.formatDate(at, { dateStyle: "medium" });
    if (row.customized_by && date) return t_i18n("Changed by {name} on {date}", { name: row.customized_by, date });
    if (row.customized_by) return t_i18n("Changed by {name}", { name: row.customized_by });
    if (date) return t_i18n("Changed on {date}", { date });
    return null;
  };
}
