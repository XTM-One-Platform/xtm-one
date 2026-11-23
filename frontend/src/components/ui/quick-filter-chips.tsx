// SPDX-License-Identifier: Apache-2.0

/**
 * A row of quick filters above a list (#1503, first on CTEM Cases): one click each,
 * each chip with the count one click on it shows. Two kinds in one row: the scope
 * chips (exactly one on, a radio group) and the toggle chips (any number on,
 * pressed buttons, combined with the scope). Every chip is drawn with the shared
 * filter pill class (`filterPillClass`), so it matches the toolbar's pills and
 * the one-pill grep gate (filter-pill-class.test.ts) holds.
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

interface QuickFilterChipsProps<S extends string, T extends string> {
  scopes: QuickFilterChip<S>[];
  scope: S | null;
  onScope: (key: S) => void;
  toggles: QuickFilterChip<T>[];
  active: readonly T[];
  onToggle: (key: T) => void;
  /** Accessible names of the two groups, already translated. */
  scopeLabel: string;
  toggleLabel: string;
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

export function QuickFilterChips<S extends string, T extends string>({
  scopes,
  scope,
  onScope,
  toggles,
  active,
  onToggle,
  scopeLabel,
  toggleLabel,
  className,
  testId,
}: QuickFilterChipsProps<S, T>) {
  const chip = "whitespace-nowrap focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";
  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)} data-testid={testId}>
      <div role="radiogroup" aria-label={scopeLabel} className="flex flex-wrap items-center gap-2">
        {scopes.map((s) => {
          const on = s.key === scope;
          return (
            <button
              key={s.key}
              type="button"
              role="radio"
              aria-checked={on}
              data-chip={s.key}
              onClick={() => onScope(s.key)}
              className={filterPillClass(on, chip)}
            >
              {s.label}
              <ChipCount count={s.count} />
            </button>
          );
        })}
      </div>
      <span aria-hidden="true" className="mx-1 h-4 w-px shrink-0 bg-elevation-hover" />
      <div role="group" aria-label={toggleLabel} className="flex flex-wrap items-center gap-2">
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
    </div>
  );
}
