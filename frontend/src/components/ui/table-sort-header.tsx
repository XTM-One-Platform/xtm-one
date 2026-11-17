// SPDX-License-Identifier: Apache-2.0

/**
 * Sortable `<th>` — one visual idiom for every sortable table.
 *
 * Used by `EntityTable` (which sorts the rows it already holds) and by the
 * server-sorted observability tables (Traces, Trace Sessions), so a header
 * looks and behaves the same whether the sort happens in the browser or in SQL.
 * Non-sortable columns render the plain label through the same cell, keeping
 * padding and alignment identical across a row of headers.
 */
import type { CSSProperties } from "react";
import { Icon } from "@filigran/design-system";
import type { SortDirection } from "@/lib/table-rows";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { InfoTip } from "@/components/ui/info-tip";
import { TruncatedText } from "@/components/ui/truncated-text";

export type { SortDirection };

interface TableSortHeaderProps {
  /** English header text — translated here, so callers pass the raw label. */
  label: string;
  /** Sort key for this column. Omit to render a plain, non-sortable header. */
  sortKey?: string;
  /** Key of the column the table is currently sorted on. */
  activeKey?: string | null;
  direction?: SortDirection;
  /** Called with this column's key on click. Required for a sortable header. */
  onSort?: (key: string) => void;
  align?: "left" | "right";
  className?: string;
  /** Inline style of the cell: a data table's fixed column width. */
  style?: CSSProperties;
  /** What the column means (already translated): an "i" beside the label explains it on hover or focus. */
  hint?: string;
  /** The column holds a required field (a row editor in edit mode): the label carries the required marker. */
  required?: boolean;
  /**
   * The label is cut with an ellipsis inside the column even without a width (a data
   * table's `layout="fixed"`, where the column with no width takes what is left).
   */
  truncate?: boolean;
}

export function TableSortHeader({
  label,
  sortKey,
  activeKey,
  direction = "asc",
  onSort,
  align = "left",
  className,
  style,
  hint,
  required = false,
  truncate = false,
}: TableSortHeaderProps) {
  const { t_i18n } = useFormatter();
  const sortable = !!sortKey && !!onSort;
  const isSorted = sortable && activeKey === sortKey;
  // Action columns carry no header — don't hand react-intl an empty id.
  const text = label ? t_i18n(label) : "";
  // A column of a fixed width (a data table's `layout="fixed"`) never lets its label
  // run into the next header (UX r3 B1 on #10581, "Knowledge BasesCreated"): a label
  // longer than the column, in any locale, ends with an ellipsis and is named whole by the
  // design-system tooltip on hover and focus, never a native title (UX r1 B4 on #10853).
  const fixed = truncate || style?.width !== undefined;
  const labelNode = fixed ? <TruncatedText text={text} /> : text;
  const requiredMark = required && (
    <span className="ml-0.5 shrink-0 text-feedback-error-primary" aria-label={t_i18n("Required")}>
      *
    </span>
  );
  // The hint sits in the same row as the label and the sort glyph, never as a loose sibling
  // of the cell (retest of #10853, "Personnes" in a 112 px column): under a fixed width the
  // row shrinks the label first (its tooltip then names it) and the icons stay whole, where
  // the cell's own ellipsis used to clip the "i" instead.
  const hintNode = hint && <InfoTip content={hint} label={text} className="ml-1" />;

  return (
    <th
      scope="col"
      style={style}
      aria-sort={isSorted ? (direction === "asc" ? "ascending" : "descending") : sortable ? "none" : undefined}
      className={cn(
        "font-medium px-4 py-2 whitespace-nowrap",
        fixed && "overflow-hidden text-ellipsis",
        align === "right" ? "text-right" : "text-left",
        className,
      )}
    >
      <span className="inline-flex max-w-full items-center">
        {sortable ? (
          <button
            type="button"
            onClick={() => onSort(sortKey)}
            className={cn(
              "inline-flex min-w-0 items-center gap-1 transition-colors hover:text-default-primary",
              isSorted && "text-default-primary",
            )}
          >
            {labelNode}
            {requiredMark}
            {isSorted ? (
              direction === "asc" ? <Icon name="arrow-up" size={16} className="shrink-0" /> : <Icon name="arrow-down" size={16} className="shrink-0" />
            ) : (
              <Icon name="chevrons-up-down" size={16} className="shrink-0 opacity-40" />
            )}
          </button>
        ) : fixed ? (
          <span className="inline-flex min-w-0 items-center">
            {labelNode}
            {requiredMark}
          </span>
        ) : (
          <>
            {text}
            {requiredMark}
          </>
        )}
        {hintNode}
      </span>
    </th>
  );
}
