// SPDX-License-Identifier: Apache-2.0

import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";

/**
 * A date in a table row: the absolute date on the row ("Nov 7, 11:52 AM", "11月7日 11:52")
 * and the relative time ("2 hours ago", "2 時間前") in the shared tooltip, both in the app
 * language. The row stays on one line; the tooltip opens on hover and on keyboard focus.
 * An empty value reads "-". In a row that opens on its own the row is the only tab stop,
 * so the date is not focusable there (#12070, D2 of the #12074 retest).
 */
export function DateTimeCell({ value, testId }: { value: string | null | undefined; testId?: string }) {
  const { intl } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  if (!value) return <span>-</span>;
  return (
    <Tip content={formatTimeAgo(intl, value)}>
      <span
        tabIndex={tabIndex}
        data-testid={testId}
        className="whitespace-nowrap rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        {formatShortDateTimeIntl(intl, value)}
      </span>
    </Tip>
  );
}
