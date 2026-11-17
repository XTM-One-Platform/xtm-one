// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react";
import { DatePicker } from "@filigran/design-system";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

interface DateFilterInputBaseProps {
  /** Committed day from the parent, `yyyy-mm-dd` (typically a URL search param), or `""`. */
  value: string;
  /** Called with the new day (`yyyy-mm-dd`, or `""` once cleared) when the person commits one. */
  onChange: (value: string) => void;
  /** Width classes. The field keeps the design system's control height. */
  className?: string;
  /** The id of the input, for a form label's htmlFor. */
  id?: string;
  /** The helper line under a form field. */
  "aria-describedby"?: string;
  /** Earliest day the calendar offers, `yyyy-mm-dd` (an end date bound by its start). */
  min?: string;
  /** Latest day the calendar offers, `yyyy-mm-dd` (a start date bound by its end). */
  max?: string;
  /** The design system's error line under the field; its presence paints the error state. */
  error?: string;
}

/** Named by "aria-label" in a toolbar with no visible label ("From", "To"),
 *  or by "aria-labelledby" under a form field's visible label. */
type DateFilterInputProps = DateFilterInputBaseProps &
  ({ "aria-label": string; "aria-labelledby"?: never } | { "aria-labelledby": string; "aria-label"?: never });

const DAY_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

/** A `yyyy-mm-dd` day as local midnight, the instant the picker shows as that day. */
function parseFilterDay(value: string): Date | null {
  const match = DAY_PATTERN.exec(value);
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(date.getTime()) ? null : date;
}

/** The local day of an instant, as `yyyy-mm-dd`. */
function formatFilterDay(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/**
 * Date filter field: the design system's `DatePicker` behind the `yyyy-mm-dd`
 * string list pages keep in their URL.
 *
 * The parent hears only committed days (`onAccept`: a calendar pick, Enter, a
 * blur after typing, the clear button), so a half-typed date never reaches the
 * URL, and the picker keeps its own draft while the person types. The parsed
 * value is memoised on the string: a new `Date` on every render would read as
 * an external change and overwrite that draft.
 */
export function DateFilterInput({
  value,
  onChange,
  className,
  id,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  "aria-describedby": ariaDescribedBy,
  min,
  max,
  error,
}: DateFilterInputProps) {
  const { t_i18n, locale } = useFormatter();
  const day = useMemo(() => parseFilterDay(value), [value]);
  const minDate = useMemo(() => (min ? (parseFilterDay(min) ?? undefined) : undefined), [min]);
  const maxDate = useMemo(() => (max ? (parseFilterDay(max) ?? undefined) : undefined), [max]);

  return (
    <DatePicker
      id={id}
      aria-label={ariaLabel}
      aria-labelledby={ariaLabelledBy}
      aria-describedby={ariaDescribedBy}
      value={day}
      minDate={minDate}
      maxDate={maxDate}
      error={error}
      onAccept={(next) => {
        const committed = next ? formatFilterDay(next) : "";
        if (committed !== value) onChange(committed);
      }}
      clearable
      locale={locale}
      openCalendarLabel={t_i18n("Open calendar")}
      clearLabel={t_i18n("Clear date")}
      calendarLabel={t_i18n("Calendar")}
      previousMonthLabel={t_i18n("Previous month")}
      nextMonthLabel={t_i18n("Next month")}
      monthSelectLabel={t_i18n("Month")}
      yearSelectLabel={t_i18n("Year")}
      className={cn("w-44 shrink-0", className)}
    />
  );
}
