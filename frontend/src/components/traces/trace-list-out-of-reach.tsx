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

import { ArrowDownWideNarrow, ChevronFirst, ListFilter } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { useFormatter } from "@/i18n";
import { TRACE_LIST_MAX_OFFSET } from "@/lib/trace-list-paging";

interface TraceListOutOfReachProps {
  /** Zero-based index of the page that is not read. */
  page: number;
  /** Whether the list is already in the newest-first order, which the keyset cursor walks without a depth limit. */
  newestFirst: boolean;
  onSortNewestFirst: () => void;
  onFirstPage: () => void;
}

/**
 * Shown by the Trace Explorer lists instead of a page that lies deeper than the
 * API's offset bound from both ends: the page is never requested, and
 * the notice says why and how to reach it.
 */
export function TraceListOutOfReach({ page, newestFirst, onSortNewestFirst, onFirstPage }: TraceListOutOfReachProps) {
  const { t_i18n, intl } = useFormatter();
  const values = { page: intl.formatNumber(page + 1), max: intl.formatNumber(TRACE_LIST_MAX_OFFSET) };

  if (newestFirst) {
    return (
      <EmptyState
        icon={ListFilter}
        title={t_i18n("This page is too deep to open directly")}
        description={t_i18n(
          "Page {page} lies more than {max} results from both ends of this list, so it is only reached by paging forward from the first page. Narrow the filters (a shorter date range, an agent, a status) to bring it within reach.",
          values,
        )}
        actionLabel={t_i18n("Go to the first page")}
        actionIcon={ChevronFirst}
        onAction={onFirstPage}
      />
    );
  }
  return (
    <EmptyState
      icon={ListFilter}
      title={t_i18n("This page is too deep to open in this order")}
      description={t_i18n(
        "Page {page} lies more than {max} results from both ends of this sorted list, and a sorted list is only read that far from either end. Narrow the filters (a shorter date range, an agent, a status) to bring it within reach, or sort newest first to page through every result.",
        values,
      )}
      actionLabel={t_i18n("Sort newest first")}
      actionIcon={ArrowDownWideNarrow}
      onAction={onSortNewestFirst}
    />
  );
}
