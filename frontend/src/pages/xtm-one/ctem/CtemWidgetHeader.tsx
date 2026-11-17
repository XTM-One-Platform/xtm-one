// SPDX-License-Identifier: Apache-2.0

/**
 * The title line of every command center widget (#11660, slice 5; design
 * approved in UX round 2 of item 5f629f38, finding B5 of round 1).
 *
 * One line, one height on every widget of a row:
 * - left: the widget's icon, its title (truncates, full text on hover), the
 *   small tags that qualify it (AI, Sample) and the counter;
 * - the description never sits in the card: it is behind the "i" beside the
 *   title, keyboard reachable;
 * - right: the actions and the short facts (counts, "since"), right aligned,
 *   never wrapping under the title. The line is its own container, so an
 *   action can drop its label for its icon on a narrow widget
 *   (`hidden @md:inline` on the label).
 */
import type { ReactNode } from "react";

import { InfoTip } from "@/components/ui/info-tip";
import { cn } from "@/lib/utils";

export function CtemWidgetHeader({
  icon,
  title,
  level = 2,
  tags,
  count,
  hint,
  hintLabel,
  actions,
  className,
  testId,
  countTestId,
}: {
  /** The widget's 16 px glyph, decorative. */
  icon?: ReactNode;
  /** Already translated. */
  title: string;
  /** Heading level inside the page outline. */
  level?: 2 | 3;
  /** Small qualifiers drawn after the title (AI, Sample). */
  tags?: ReactNode;
  /** The counter after the title (a number or a short count phrase). */
  count?: ReactNode;
  /** The widget's description, behind the "i"; never a line in the card. */
  hint?: ReactNode;
  /** Names the "i" for assistive technology (defaults to the title). */
  hintLabel?: string;
  /** Right-aligned facts and buttons, on the title line. */
  actions?: ReactNode;
  className?: string;
  testId?: string;
  /** Test id of the counter (default `ctem-widget-header-count`). */
  countTestId?: string;
}) {
  const Heading = level === 3 ? "h3" : "h2";
  return (
    <div
      className={cn("@container flex h-9 min-w-0 shrink-0 items-center justify-between gap-2 whitespace-nowrap", className)}
      data-testid={testId ?? "ctem-widget-header"}
    >
      <div className="flex min-w-0 items-center gap-2">
        <Heading className="flex min-w-0 items-center gap-2 content-base font-semibold">
          {icon && <span className="inline-flex shrink-0 text-accent" aria-hidden>{icon}</span>}
          <span className="truncate" title={title}>{title}</span>
        </Heading>
        {hint && <InfoTip content={hint} label={hintLabel ?? title} />}
        {tags && <span className="flex shrink-0 items-center gap-1.5">{tags}</span>}
        {count != null && count !== false && (
          <span className="shrink-0 content-compact tabular-nums text-default-secondary" data-testid={countTestId ?? "ctem-widget-header-count"}>
            {count}
          </span>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2 content-caption text-default-secondary">{actions}</div>}
    </div>
  );
}
