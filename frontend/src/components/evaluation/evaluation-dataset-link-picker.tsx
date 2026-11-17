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

import { Icon } from "@filigran/design-system";
import { useState } from "react";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useDatasets } from "@/hooks/api/useDatasetApi";
import { useDebounce } from "@/hooks/useDebounce";
import { useFormatter } from "@/i18n";
import type { Dataset } from "@/types";

export interface EvaluationDatasetLinkPickerProps {
  /** Only datasets of this output family are offered; every dataset without one. */
  family?: string;
  /** The datasets already linked, left out of the list. */
  linkedIds: string[];
  onLink: (dataset: Dataset) => void;
}

/**
 * The evaluation creation page's "Search datasets to link..." picker: the server
 * searches, and the list opens above the page, never under the next card.
 */
export function EvaluationDatasetLinkPicker({ family, linkedIds, onLink }: EvaluationDatasetLinkPickerProps) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 300);
  const { data: datasets, isPending, isError } = useDatasets(debouncedSearch || undefined, family || undefined, {
    throwOnError: false,
  });
  // Until the server answers what is typed (or answers at all, on the first
  // open), the rows would be the previous query's or none: none is offered, so
  // Enter cannot link a dataset the field does not match.
  const searching = search !== debouncedSearch || isPending;
  // A failed search is not an empty answer: no row, and the list says so.
  const available = searching || isError ? [] : (datasets ?? []).filter((dataset) => !linkedIds.includes(dataset.id));

  return (
    <SearchableSelect
      // Keyed on the count: each link starts an empty field again.
      key={linkedIds.length}
      aria-label={t_i18n("Search datasets to link...")}
      placeholder={t_i18n("Search datasets to link...")}
      options={available.map((dataset) => ({
        value: dataset.id,
        label: dataset.name,
        description: [
          dataset.needs_family ? t_i18n("Needs family") : null,
          dataset.is_read_only ? t_i18n("Read-only") : null,
          t_i18n("{count, plural, one {# entry} other {# entries}}", { count: dataset.entry_count }),
        ]
          .filter(Boolean)
          .join(" · "),
        // A dataset without a family refuses every run of the evaluation:
        // the warning shows before it blocks one, as `NeedsFamilyBadge` does,
        // in the row's text and as the warning icon.
        icon: dataset.needs_family ? (
          <Icon name="triangle-alert" size={16} className="text-feedback-warning-primary" />
        ) : (
          <Icon name="database" size={16} className="text-default-secondary" />
        ),
      }))}
      value=""
      onValueChange={(id) => {
        const dataset = available.find((d) => d.id === id);
        if (!dataset) return;
        onLink(dataset);
        // The field starts empty again (the key), and so does the search.
        setSearch("");
      }}
      onSearchChange={setSearch}
      // The server's answer, in its order.
      sort={false}
      emptyMessage={
        searching
          ? t_i18n("Searching…")
          : isError
            ? t_i18n("The datasets could not be loaded.")
            : search
              ? t_i18n("No matching datasets.")
              : t_i18n("No datasets available. Create one above or link later.")
      }
    />
  );
}
