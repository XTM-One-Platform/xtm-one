// SPDX-License-Identifier: Apache-2.0

/**
 * The read view of an entity's settings: a grid of label/value tiles whose
 * rows are always full (#11838).
 *
 * The grid has one column count per breakpoint (`OVERVIEW_GRID_COLUMNS` of
 * overview-grid-spans.ts: 1, then
 * 2 at `sm`, 4 at `md`, 6 at `lg`, 12 at `2xl`) and each tile declares how many
 * of those columns it spans at each breakpoint (`span`). A page chooses the
 * spans so that, in its tile order, every row adds up to the column count: no
 * tile is left alone on a row and no row ends in empty space. A breakpoint a
 * tile does not name keeps the span of the narrower one; the base span is 1.
 *
 * The 12-column layout starts at `2xl` (1536 px viewport), not `xl`: at 1280 px
 * the content beside the navbar is about 1020 px wide, so a one-column tile
 * would be about 78 px, too narrow for a label such as "Human in the loop".
 *
 * A tile never grows taller for a long value: the label and every value line
 * are cut on one line with the design-system tooltip naming them whole
 * (`TruncatedText`), and a chip row folds into `+N` (`TagList`).
 */
import type { ReactNode } from "react";

import { overviewSpanClasses, type OverviewSpan } from "@/components/ui/overview-grid-spans";
import { TruncatedText } from "@/components/ui/truncated-text";
import { cn } from "@/lib/utils";

export function OverviewGrid({ children, className, testId }: { children: ReactNode; className?: string; testId?: string }) {
  return (
    <div
      data-testid={testId}
      className={cn("grid grid-cols-1 gap-2 sm:grid-cols-2 md:grid-cols-4 lg:grid-cols-6 2xl:grid-cols-12", className)}
    >
      {children}
    </div>
  );
}

interface OverviewTileProps {
  /** The field name, cut with a tooltip when the tile is narrow. */
  label: string;
  /** A small icon drawn before the label. */
  icon?: ReactNode;
  /** Columns spanned per breakpoint. */
  span?: OverviewSpan;
  /** The value; a string is cut on one line with a tooltip. */
  children: ReactNode;
  /** A secondary line under the value (where a setting comes from), cut on one line. */
  hint?: string;
  testId?: string;
  className?: string;
}

export function OverviewTile({ label, icon, span, children, hint, testId, className }: OverviewTileProps) {
  return (
    <div
      data-testid={testId}
      data-overview-tile=""
      className={cn("min-w-0 rounded-sm bg-elevation-hover px-3 py-2.5", overviewSpanClasses(span), className)}
    >
      <div className="flex min-w-0 items-center gap-1.5">
        {icon}
        <TruncatedText text={label} className="content-caption text-default-secondary leading-none" />
      </div>
      <div className="mt-1.5 min-w-0">
        {typeof children === "string" ? <TruncatedText text={children} className="content-base" /> : children}
      </div>
      {hint && <TruncatedText text={hint} className="content-caption text-default-secondary mt-1" />}
    </div>
  );
}
