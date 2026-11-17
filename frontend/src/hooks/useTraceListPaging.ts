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

import { useCallback, useEffect, useMemo, useState } from "react";
import { planTraceListPage, readsFromFirstRow, type TraceListPagePlan } from "@/lib/trace-list-paging";

interface CountedTotal {
  fingerprint: string;
  total: number;
}

interface TraceListPagingInput {
  /** The filters and sort the count belongs to: a new fingerprint drops it. */
  fingerprint: string;
  /** Zero-based page index. */
  page: number;
  limit: number;
  /** Whether the newest-first keyset walk holds this page's cursor. */
  hasCursor: boolean;
}

interface TraceListPaging {
  plan: TraceListPagePlan;
  /** The count pages past the offset bound are planned from, `null` until one was read. */
  planningTotal: number | null;
  recordTotal: (total: number) => void;
}

/**
 * How the page a trace list shows is read, and the count it is
 * planned from. That count comes from the last page read from the first row,
 * or from the count a deep link past the bound reads first
 * ({@link useRecordTraceListTotal}); a far-end read never replaces it.
 */
export function useTraceListPaging({ fingerprint, page, limit, hasCursor }: TraceListPagingInput): TraceListPaging {
  const [counted, setCounted] = useState<CountedTotal | null>(null);
  const planningTotal = counted?.fingerprint === fingerprint ? counted.total : null;
  const plan = useMemo(
    () => planTraceListPage({ page, limit, total: planningTotal, hasCursor }),
    [page, limit, planningTotal, hasCursor],
  );
  const recordTotal = useCallback(
    (total: number) =>
      setCounted((prev) => (prev?.fingerprint === fingerprint && prev.total === total ? prev : { fingerprint, total })),
    [fingerprint],
  );
  return { plan, planningTotal, recordTotal };
}

/**
 * Keeps the planning count of {@link useTraceListPaging} current: from the page
 * shown when it was read from the first row, and from the count query while
 * the plan waits on one.
 */
export function useRecordTraceListTotal(
  { plan, recordTotal }: TraceListPaging,
  pageTotal: number | undefined,
  countTotal: number | undefined,
) {
  useEffect(() => {
    if (pageTotal !== undefined && readsFromFirstRow(plan)) recordTotal(pageTotal);
  }, [pageTotal, plan, recordTotal]);
  useEffect(() => {
    if (countTotal !== undefined && plan.kind === "count-first") recordTotal(countTotal);
  }, [countTotal, plan.kind, recordTotal]);
}
