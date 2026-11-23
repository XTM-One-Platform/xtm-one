// SPDX-License-Identifier: Apache-2.0

/**
 * A row of toggle quick filters above a list (#1503, first on CTEM Cases): one
 * click each, any number on (pressed buttons), each chip with the count one
 * click on it shows. Multi-select only: a single-choice scope of a list is the
 * shared `SegmentedFilter selection="radio"` (UX r1 B1 on #1515, as Signals),
 * never this row; quick-filter-chips-gate.test.ts holds that. Every chip is
 * drawn with the shared filter pill class (`filterPillClass`), so it matches the
 * toolbar's pills and the one-pill grep gate (filter-pill-class.test.ts) holds.
 *
 * The row wraps onto a second line when it does not fit, a chip never breaks
 * inside (its label is nowrap), so a long locale at 1080 stays readable.
 */
import { cn } from "@/lib/utils";
import { filterPillClass } from "@/components/ui/filter-pill";
import { formatCount } from "@/lib/number-format";
import { useFormatter } from "@/i18n";

export interface QuickFilterChip<K extends string> {
  key: K;
  /** Already translated. */
  label: string;
  /** What one click on the chip shows; undefined while the list is loading. */
  count?: number;
}

interface QuickFilterChipsProps<T extends string> {
  toggles: QuickFilterChip<T>[];
  active: readonly T[];
  onToggle: (key: T) => void;
  /** Accessible name of the group, already translated. */
  label: string;
  className?: string;
  testId?: string;
}

function ChipCount({ count }: { count?: number }) {
  const { intl } = useFormatter();
  if (count === undefined) {
    return <span aria-hidden="true" data-count-pending="true" className="ml-1 inline-block h-3 w-3 animate-pulse rounded-sm bg-elevation-hover" />;
  }
  return (
    <span data-count-slot="true" className="ml-1 tabular-nums text-default-secondary">
      {formatCount(intl.locale, count)}
    </span>
  );
}

export function QuickFilterChips<T extends string>({ toggles, active, onToggle, label, className, testId }: QuickFilterChipsProps<T>) {
  const chip = "whitespace-nowrap focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";
  return (
    <div role="group" aria-label={label} className={cn("flex flex-wrap items-center gap-2", className)} data-testid={testId}>
      {toggles.map((t) => {
        const on = active.includes(t.key);
        return (
          <button
            key={t.key}
            type="button"
            aria-pressed={on}
            data-chip={t.key}
            onClick={() => onToggle(t.key)}
            className={filterPillClass(on, chip)}
          >
            {t.label}
            <ChipCount count={t.count} />
          </button>
        );
      })}
    </div>
  );
}
