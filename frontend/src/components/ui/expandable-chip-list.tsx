// SPDX-License-Identifier: Apache-2.0

import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * A wrapping list of chips that shows the first `limit` items, then a "+N more"
 * toggle that expands the rest in place and reads "Show fewer" once open
 * (`aria-expanded` follows). One component for every such list, so two lists on
 * the same page never draw their toggle differently (UX round 1 B1 of #10124:
 * the regions and the PIRs of Settings > Organization had two copies).
 * Guard: tests_unit/expandable-chip-list-guard.test.ts.
 *
 * ```tsx
 * <ExpandableChipList
 *   items={regions}
 *   getKey={(region) => region.code}
 *   ariaLabel={t_i18n("Regions of operation")}
 *   renderChip={(region) => <Badge variant="secondary">{region.label}</Badge>}
 * />
 * ```
 */
interface ExpandableChipListProps<T> {
  items: readonly T[];
  getKey: (item: T) => string;
  renderChip: (item: T) => ReactNode;
  ariaLabel: string;
  /** Chips shown before "+N more" (default 8). */
  limit?: number;
  className?: string;
}

const DEFAULT_CHIP_LIMIT = 8;

export function ExpandableChipList<T>({ items, getKey, renderChip, ariaLabel, limit = DEFAULT_CHIP_LIMIT, className }: ExpandableChipListProps<T>) {
  const { t_i18n } = useFormatter();
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? items : items.slice(0, limit);
  const hidden = items.length - shown.length;
  return (
    <ul className={cn("flex flex-wrap items-center gap-1.5", className)} aria-label={ariaLabel}>
      {shown.map((item) => (
        <li key={getKey(item)} className="max-w-full">
          {renderChip(item)}
        </li>
      ))}
      {items.length > limit && (
        <li>
          <Button variant="ghost" size="sm" className="h-6 px-1.5 content-compact" aria-expanded={expanded} onClick={() => setExpanded((open) => !open)}>
            {expanded ? t_i18n("Show fewer") : t_i18n("+{count} more", { count: hidden })}
          </Button>
        </li>
      )}
    </ul>
  );
}
