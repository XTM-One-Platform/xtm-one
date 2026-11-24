// SPDX-License-Identifier: Apache-2.0

/**
 * "Group by" of the CTEM Cases list (#1176): a toolbar pill like Columns, its
 * panel a single choice (No grouping, Owner, State, Priority, Fix family). The
 * pill is accented while a grouping is on and names it.
 */
import { useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { FilterPillPanel, FilterPillTrigger, useFilterPopover } from "@/components/ui/filter-pill";
import { useFormatter } from "@/i18n";
import { CASE_GROUP_BYS, CASE_GROUP_BY_LABEL, type CaseGroupBy } from "./cases-grouping";

/** The pill's glyph, from the design system. */
const GroupGlyph = ({ className }: { className?: string }) => <Icon name="list-tree" size={16} className={className ?? "size-3"} />;

export function CasesGroupByPill({ value, onChange }: { value: CaseGroupBy; onChange: (next: CaseGroupBy) => void }) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useFilterPopover(ref, open, setOpen);
  const label = t_i18n("Group by");
  return (
    <div ref={ref} className="relative" data-testid="ctem-cases-group-by">
      <FilterPillTrigger
        icon={GroupGlyph}
        label={value === "none" ? label : t_i18n("Group by: {group}", { group: t_i18n(CASE_GROUP_BY_LABEL[value]) })}
        open={open}
        active={value !== "none"}
        className="h-8"
        onClick={() => setOpen(!open)}
      />
      {open && (
        <FilterPillPanel role="radiogroup" aria-label={label} className="w-56 p-1">
          {CASE_GROUP_BYS.map((key) => {
            const checked = key === value;
            return (
              <button
                type="button"
                role="radio"
                aria-checked={checked}
                key={key}
                data-testid={`ctem-cases-group-by-${key}`}
                onClick={() => {
                  onChange(key);
                  setOpen(false);
                }}
                className="flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left content-compact hover:bg-elevation-hover"
              >
                <span className="min-w-0 flex-1 truncate">{t_i18n(CASE_GROUP_BY_LABEL[key])}</span>
                {checked && <Icon name="check" size={16} className="size-3.5 shrink-0" aria-hidden />}
              </button>
            );
          })}
        </FilterPillPanel>
      )}
    </div>
  );
}
