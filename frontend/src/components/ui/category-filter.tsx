// SPDX-License-Identifier: Apache-2.0

/** CategoryFilter -- one category among a list (Integrations, Users).
 *
 * The default draws the categories as segments. `collapsed` draws one
 * "Category" filter pill, the same kind as the Tags and property pills (data
 * table standard v2, Sam 10 Nov 2026: a category is a pill in the toolbar,
 * never a plain select): its panel lists the categories, the pill shows the
 * chosen one and clears from the panel. A long category list no longer sets a
 * 1,210 px segment row that scrolls a 1024 page sideways (#9378).
 */
import { useMemo, useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { sortByLabel } from "@/lib/select-options";
import { FilterClearButton, FilterPillPanel, FilterPillTrigger, useFilterPopover } from "@/components/ui/filter-pill";

interface CategoryFilterProps {
  categories: string[];
  selected: string | null;
  onChange: (category: string | null) => void;
  /** Optional label map — falls back to the raw category string. */
  labels?: Record<string, string>;
  /** One picker instead of the segments (a toolbar with a long category list). */
  collapsed?: boolean;
  className?: string;
}

export function CategoryFilter({
  categories: given,
  selected,
  onChange,
  labels,
  collapsed = false,
  className,
}: CategoryFilterProps) {
  const { locale, t_i18n } = useFormatter();
  // Alphabetical on the label shown, "All" first (#12289).
  const categories = useMemo(() => sortByLabel(given, (cat) => t_i18n(labels?.[cat] ?? cat), locale), [given, labels, locale, t_i18n]);
  if (collapsed) {
    return <CategoryPill categories={categories} selected={selected} onChange={onChange} labels={labels} className={className} />;
  }
  return (
    <div className={cn("flex gap-1 rounded-sm bg-elevation-hover p-0.5 items-center", className)}>
      <button
        className={cn(
          "rounded-sm px-3 h-7 content-compact font-medium transition-colors inline-flex items-center",
          !selected
            ? "bg-elevation-highlight text-default-primary"
            : "text-default-secondary hover:bg-elevation-hover",
        )}
        onClick={() => onChange(null)}
      >
        {t_i18n("All")}
      </button>
      {categories.map((cat) => (
        <button
          key={cat}
          className={cn(
            "rounded-sm px-3 h-7 content-compact font-medium transition-colors inline-flex items-center",
            selected === cat
              ? "bg-elevation-highlight text-default-primary"
              : "text-default-secondary hover:bg-elevation-hover",
          )}
          onClick={() => onChange(cat)}
        >
          {t_i18n(labels?.[cat] ?? cat)}
        </button>
      ))}
    </div>
  );
}

/** The pill's glyph, from the design system (no direct lucide-react import in components/ui). */
const CategoryGlyph = ({ className }: { className?: string }) => <Icon name="layout-dashboard" size={16} className={cn("size-3", className)} />;

/** The collapsed form: one single-choice filter pill. */
function CategoryPill({ categories, selected, onChange, labels, className }: Omit<CategoryFilterProps, "collapsed">) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useFilterPopover(ref, open, setOpen);
  const current = selected && categories.includes(selected) ? selected : null;
  const label = (cat: string) => t_i18n(labels?.[cat] ?? cat);
  return (
    <div ref={ref} data-collapsed="true" data-testid="category-filter" className={cn("relative flex shrink-0 items-center", className)}>
      <FilterPillTrigger
        icon={CategoryGlyph}
        label={current ? label(current) : t_i18n("Category")}
        open={open}
        count={current ? 1 : 0}
        onClick={() => setOpen(!open)}
      />
      {open && (
        <FilterPillPanel role="group" aria-label={t_i18n("Category")} className="max-h-72 w-60 overflow-y-auto p-1">
          {categories.map((cat) => {
            const isSelected = cat === current;
            return (
              <button
                type="button"
                key={cat}
                aria-pressed={isSelected}
                onClick={() => {
                  onChange(isSelected ? null : cat);
                  setOpen(false);
                }}
                className={cn(
                  "flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left content-compact transition-colors",
                  isSelected ? "bg-accent/15 text-accent" : "hover:bg-elevation-hover",
                )}
              >
                <span className="min-w-0 flex-1 truncate" title={label(cat)}>{label(cat)}</span>
                {isSelected && <Icon name="check" size={16} className="size-3.5 shrink-0" />}
              </button>
            );
          })}
          {current && (
            <div className="border-t border-elevation-subtle-soft p-1">
              <FilterClearButton onClick={() => { onChange(null); setOpen(false); }}>{t_i18n("Clear")}</FilterClearButton>
            </div>
          )}
        </FilterPillPanel>
      )}
    </div>
  );
}
