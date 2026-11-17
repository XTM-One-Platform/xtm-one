/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The Status filter of Operations > Work items, on the toolbar itself (#9378
 * slice 8d): a multi-select Status pill (Open, Claimed, In progress, Blocked,
 * Done, Cancelled) with the count of each status under the list's other
 * filters. It used to be a segment hidden in the Filters pill.
 *
 * The landing selection is "open work" (open, claimed, in progress, blocked):
 * actionable items, never done or cancelled noise. An empty selection reads
 * every status. Controlled: the page keeps it in the URL (`?status=a,b`, no key
 * for open work, `?status=all` for every status).
 */

import { useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { FilterClearButton, FilterPillPanel, FilterPillTrigger, useFilterPopover } from "@/components/ui/filter-pill";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { WorkItemStatus } from "@/types";

/** The statuses the pill offers, in the order of an item's life. */
const WORK_ITEM_STATUSES: readonly WorkItemStatus[] = ["open", "claimed", "in_progress", "blocked", "done", "cancelled"];

/** The landing selection: every status still to do. */
const OPEN_WORK: readonly WorkItemStatus[] = ["open", "claimed", "in_progress", "blocked"];

/** The statuses a URL's `status` names: none = open work, `all` = every status (an empty selection). */
export function statusesFromParam(param: string | null): WorkItemStatus[] {
  if (!param) return [...OPEN_WORK];
  if (param === "all") return [];
  const raw = param.split(",").map((s) => s.trim());
  const known = WORK_ITEM_STATUSES.filter((s) => raw.includes(s));
  return known.length > 0 ? known : [...OPEN_WORK];
}

/** The URL value of a selection: undefined for open work, `all` for none, else `a,b`. */
export function statusParamOf(selection: readonly WorkItemStatus[]): string | undefined {
  const ordered = WORK_ITEM_STATUSES.filter((s) => selection.includes(s));
  if (ordered.length === 0) return "all";
  if (ordered.length === OPEN_WORK.length && OPEN_WORK.every((s) => ordered.includes(s))) return undefined;
  return ordered.join(",");
}

/** The status labels, catalog keys. "Open (status)" is the adjective, never the verb. @i18n-keys */
const LABELS = {
  open: "Open (status)",
  claimed: "Claimed",
  in_progress: "In progress",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
} as const satisfies Record<WorkItemStatus, string>;

interface Props {
  value: WorkItemStatus[];
  onChange: (next: WorkItemStatus[]) => void;
  /** Items per status under the other filters; no number while unknown. */
  counts?: Partial<Record<WorkItemStatus, number>>;
  /** What the Blocked line adds (how many wait on dependencies, how many need an action). */
  blockedHint?: string;
}

export function WorkItemStatusFilter({ value, onChange, counts, blockedHint }: Props) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useFilterPopover(ref, open, setOpen);
  const toggle = (status: WorkItemStatus) =>
    onChange(WORK_ITEM_STATUSES.filter((s) => (s === status ? !value.includes(s) : value.includes(s))));
  const isOpenWork = statusParamOf(value) === undefined;

  return (
    <div ref={ref} className="relative shrink-0" data-testid="work-items-status-filter">
      <FilterPillTrigger
        icon={StatusPillIcon}
        label={t_i18n("Status")}
        open={open}
        // The landing selection is not a filter the person set: no badge for it.
        count={isOpenWork ? 0 : value.length}
        aria-haspopup="true"
        data-testid="work-items-status-pill"
        onClick={() => setOpen(!open)}
      />
      {open && (
        <FilterPillPanel role="group" aria-label={t_i18n("Status filter")} className="w-64 p-1">
          {WORK_ITEM_STATUSES.map((status) => {
            const selected = value.includes(status);
            const n = counts?.[status] ?? (counts ? 0 : undefined);
            return (
              <button
                type="button"
                key={status}
                aria-pressed={selected}
                title={status === "blocked" ? blockedHint : undefined}
                data-testid={`work-items-status-${status}`}
                onClick={() => toggle(status)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left content-compact transition-colors",
                  selected ? "bg-accent/15 text-accent" : "hover:bg-elevation-hover",
                )}
              >
                <span className="min-w-0 flex-1 truncate">{t_i18n(LABELS[status])}</span>
                {n !== undefined && <span className="tabular-nums text-default-secondary">{n}</span>}
                <Icon name="check" size={16} className={cn("h-3.5 w-3.5 shrink-0", !selected && "invisible")} aria-hidden />
              </button>
            );
          })}
          <div className="mt-1 flex items-center gap-1 border-t border-elevation-subtle-soft pt-1">
            {!isOpenWork && (
              <FilterClearButton data-testid="work-items-status-open-work" onClick={() => onChange([...OPEN_WORK])}>
                {t_i18n("Open work")}
              </FilterClearButton>
            )}
            {value.length > 0 && (
              <FilterClearButton data-testid="work-items-status-all" onClick={() => onChange([])}>
                {t_i18n("All")}
              </FilterClearButton>
            )}
          </div>
        </FilterPillPanel>
      )}
    </div>
  );
}

/** The Status pill's glyph, from the design system's icon set (no direct lucide-react import). */
function StatusPillIcon({ className }: { className?: string }) {
  return <Icon name="list-checks" size={16} className={className} aria-hidden />;
}
