// SPDX-License-Identifier: Apache-2.0

/** PropertyFilter -- `has:` property multi-select for entity list pages.
 *
 * Renders a trigger pill that opens a checkable list of filterable
 * properties (`has:channels-bots`, `has:web-exposure`, ...). Checked
 * properties are shown as removable chips next to the trigger; the caller
 * (via `useEntityFilter`) combines them with AND semantics on top of the
 * free-text search and the tag/visibility filters.
 *
 * The option list is data-driven, so a new property is added by extending
 * the caller's `PropertyFilterOption[]` -- no structural change here.
 */

import { useMemo, useRef, useState } from "react";
import { Check, ListFilter, X } from "lucide-react";
import { FilterClearButton, FilterPillPanel, FilterPillTrigger, filterPillClass, useFilterPopover } from "@/components/ui/filter-pill";
import { cn, toggleArrayItem } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { sortByLabel } from "@/lib/select-options";

export interface PropertyFilterOption {
  /** Token used in the `has:` filter and in the `has` URL param (e.g. "web-exposure"). */
  key: string;
  /** Human label shown in the checklist and on the chip. */
  label: string;
  /** Lucide icon rendered next to the label. */
  icon: React.ElementType;
  /** Optional one-line explanation shown under the label in the checklist. */
  description?: string;
}

interface PropertyFilterProps {
  options: PropertyFilterOption[];
  /** Currently checked property keys. */
  selected: string[];
  onChange: (keys: string[]) => void;
  className?: string;
}

export function PropertyFilter({ options: given, selected, onChange, className }: PropertyFilterProps) {
  const { locale, t_i18n } = useFormatter();
  // Alphabetical on the label shown (#12289).
  const options = useMemo(() => sortByLabel(given, (option) => t_i18n(option.label), locale), [given, locale, t_i18n]);
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useFilterPopover(containerRef, open, setOpen);

  // Chips follow the option order so the bar doesn't reshuffle on toggle.
  const selectedOptions = useMemo(
    () => options.filter((o) => selected.includes(o.key)),
    [options, selected],
  );

  if (options.length === 0) return null;

  const toggle = (key: string) => onChange(toggleArrayItem(selected, key));

  return (
    <div ref={containerRef} className={cn("relative flex items-center gap-1.5 flex-wrap", className)}>
      <FilterPillTrigger
        icon={ListFilter}
        label={t_i18n("Properties")}
        open={open}
        active={selected.length > 0}
        onClick={() => setOpen(!open)}
      />

      {/* Selected properties as removable chips */}
      {selectedOptions.map(({ key, label, icon: Icon }) => (
        <span
          key={key}
          className={filterPillClass(true, "pl-2.5 pr-1")}
        >
          <Icon className="h-3 w-3" />
          {t_i18n(label)}
          <button
            type="button"
            onClick={() => toggle(key)}
            aria-label={t_i18n("Remove filter {name}", { name: t_i18n(label) })}
            className="rounded-sm p-0.5 text-accent/70 hover:text-accent hover:bg-accent/15 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            <X className="h-3 w-3" />
          </button>
        </span>
      ))}

      {selected.length > 0 && (
        <FilterClearButton onClick={() => onChange([])}>{t_i18n("Clear")}</FilterClearButton>
      )}

      {/* Checklist dropdown — a plain group of toggle buttons rather than a
          listbox: Tab moves between them and Enter/Space toggles, so there is
          no roving focus to manage. `aria-pressed` carries the checked state. */}
      {open && (
        <FilterPillPanel role="group" aria-label={t_i18n("Properties")} className="p-1">
          {options.map(({ key, label, icon: Icon, description }) => {
            const isSelected = selected.includes(key);
            return (
              <button
                type="button"
                key={key}
                aria-pressed={isSelected}
                onClick={() => toggle(key)}
                className={cn(
                  // Tab walks the rows: each one draws the design-system focus ring,
                  // inset so the panel edge never clips it (UX r1 A1 on #10527).
                  "flex w-full items-start gap-2.5 rounded-sm px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus",
                  // The selected row keeps its label in the primary ink: accent text on
                  // bg-accent/15 read 4.08:1 in dark at 12 px (UX r1 A1 on #10372). The
                  // accent stays on the icon and the check, which need 3:1.
                  isSelected ? "bg-accent/15 text-default-primary" : "hover:bg-elevation-hover",
                )}
              >
                <Icon className={cn("h-3.5 w-3.5 mt-0.5 shrink-0", isSelected ? "text-accent" : "text-default-secondary")} />
                <span className="min-w-0 flex-1">
                  <span className="block content-compact font-medium">{t_i18n(label)}</span>
                  <span className="block font-mono-plex content-compact text-default-secondary">has:{key}</span>
                  {description && (
                    <span className="block content-compact text-default-secondary mt-0.5">{t_i18n(description)}</span>
                  )}
                </span>
                {isSelected && <Check className="h-3.5 w-3.5 shrink-0 mt-0.5 text-accent" />}
              </button>
            );
          })}
        </FilterPillPanel>
      )}
    </div>
  );
}
