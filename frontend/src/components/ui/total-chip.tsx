// SPDX-License-Identifier: Apache-2.0

/**
 * TotalChip: the ONE way a list says its total (dev-docs/reference/data-tables.md).
 *
 * A list says its total once, at the top: the data table's toolbar, a
 * ListControlBar with `showTotal`, or the toolbar row of a hand-made list. The
 * pager under the rows never repeats it (Sam, 11 Nov 2026, #11191), so every
 * list draws this chip, one height and one skin, never a local copy of it
 * (UX r1 B3 on #11192).
 *
 * "N", or "n of N" when a search or a filter narrows the list (`shown`). While
 * the first page loads the count is unknown, never "0": `loading` draws a
 * placeholder in the chip's place.
 *
 * The figures are compact ("12.4K"), the exact ones on hover (#10476).
 */
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/** The chip's height: the 32 px of every list toolbar control. */
export const TOTAL_CHIP_HEIGHT = "h-8";

interface TotalChipProps {
  total: number;
  /** The rows a search or a filter leaves, when fewer than `total`. */
  shown?: number;
  loading?: boolean;
  /**
   * The width of the loading placeholder: "count" for "N", "narrowed" for "n of N" (a scope, search
   * or filter can narrow the list), so the loading toolbar wraps as the loaded one does (UX r3 B5 on
   * #11349). The loaded chip always fits its text: a reserved width there grew the loaded toolbars of
   * Prompts, Skills and Flows library to two rows at 1080 (UX r5 B8 on #11349).
   */
  reserve?: "count" | "narrowed";
  testId?: string;
  className?: string;
}

// 40 px, not 48: an "n of N" placeholder as wide as "6 of 82" (39 px) keeps the loading row of
// Knowledge bases on the loaded row's line count without pushing Skills to a second row (UX r5 B8).
const RESERVED_WIDTH = { count: "min-w-6", narrowed: "min-w-10" } as const;

export function TotalChip({ total, shown, loading = false, reserve, testId = "list-total", className }: TotalChipProps) {
  const { t_i18n } = useFormatter();
  const nf = useNumberFormat();
  const narrowed = shown !== undefined && shown !== total;
  const text = narrowed ? t_i18n("{shown} of {total}", { shown: nf.count(shown), total: nf.count(total) }) : nf.count(total);
  const exact = narrowed
    ? t_i18n("{shown} of {total}", { shown: nf.exactCount(shown), total: nf.exactCount(total) })
    : nf.exactCount(total);
  return (
    <span
      data-testid={testId}
      aria-label={t_i18n("Total")}
      aria-busy={loading || undefined}
      title={loading || exact === text ? undefined : exact}
      className={cn(
        TOTAL_CHIP_HEIGHT,
        "inline-flex shrink-0 items-center rounded-sm bg-elevation-hover px-2.5 content-compact font-medium tabular-nums text-default-secondary",
        loading && "animate-pulse",
        className,
      )}
    >
      {loading ? (
        <span aria-hidden="true" data-reserved-width={reserve ?? "count"} className={cn("inline-block", RESERVED_WIDTH[reserve ?? "count"])} />
      ) : (
        text
      )}
    </span>
  );
}
