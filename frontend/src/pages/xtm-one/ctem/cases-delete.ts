// SPDX-License-Identifier: Apache-2.0

import type { CtemCaseDeleteResult } from "@/hooks/api/useCtemCasesApi";

/** The totals of a case delete dry run the confirm dialog shows (#1723). */
export function deletePreviewTotals(preview: CtemCaseDeleteResult): { workItems: number; timers: number; signals: number } {
  return {
    workItems: preview.cases.reduce((n, c) => n + c.work_items.length, 0),
    timers: preview.cases.reduce((n, c) => n + c.timers.length, 0),
    signals: preview.cases.reduce((n, c) => n + c.signals.length, 0),
  };
}
