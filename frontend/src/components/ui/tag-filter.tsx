// SPDX-License-Identifier: Apache-2.0

/** TagFilter -- pill-style tag filter bar for list pages.
 *
 * Shows up to `maxVisible` tags inline. When there are more, a "+N more"
 * button opens a searchable dropdown with all tags. Selected tags are always
 * promoted to the inline row regardless of the cutoff.
 *
 * `collapsed` draws one "Tags" pill instead (the count badge shows how many
 * tags narrow the list), with every tag in its dropdown. List pages whose
 * toolbar row already holds a search, the visibility segments and other
 * filters use it, so the tag chips never wrap the row at 1024 or 1440.
 */

import { useState, useRef, useMemo, useCallback } from "react";
import { Tag, Search } from "lucide-react";
import { FilterClearButton, FilterPillPanel, FilterPillTrigger, PANEL_CHIP_HEIGHT, filterPillClass, useFilterPopover } from "@/components/ui/filter-pill";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { sortByLabel } from "@/lib/select-options";

interface TagFilterProps {
  allTags: string[];
  selected: string[];
  onChange: (tags: string[]) => void;
  /** Maximum number of unselected tags to show inline. @default 8 */
  maxVisible?: number;
  /** One "Tags" pill opening every tag in its dropdown, no chips inline. */
  collapsed?: boolean;
  /**
   * The list is still on its first read (collapsed only): the "Tags" pill holds its
   * place, disabled, so the toolbar wraps as it will once the tags land (#11345).
   */
  pending?: boolean;
  className?: string;
}

export function TagFilter({
  allTags,
  selected,
  onChange,
  maxVisible = 8,
  collapsed = false,
  pending = false,
  className,
}: TagFilterProps) {
  const { locale, t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const [popoverSearch, setPopoverSearch] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  useFilterPopover(containerRef, open, setOpen);

  const handleOpen = useCallback((nextOpen: boolean) => {
    setOpen(nextOpen);
    if (nextOpen) {
      setPopoverSearch("");
      requestAnimationFrame(() => searchInputRef.current?.focus());
    }
  }, []);

  const popoverTags = useMemo(() => {
    const q = popoverSearch.toLowerCase();
    // The dropdown lists every tag alphabetically (#12289).
    return sortByLabel(
      allTags.filter((t) => !q || t.toLowerCase().includes(q)),
      (t) => t,
      locale,
    );
  }, [allTags, popoverSearch, locale]);

  if (collapsed && pending) {
    return (
      <div className={cn("relative flex shrink-0 items-center", className)} data-pending="true">
        <FilterPillTrigger icon={Tag} label={t_i18n("Tags")} open={false} disabled aria-busy="true" />
      </div>
    );
  }
  if (allTags.length === 0) return null;

  const toggle = (tag: string) => {
    if (selected.includes(tag)) {
      onChange(selected.filter((t) => t !== tag));
    } else {
      onChange([...selected, tag]);
    }
  };

  const selectedSet = new Set(selected);

  if (collapsed) {
    return (
      <div ref={containerRef} className={cn("relative flex shrink-0 items-center", className)}>
        <FilterPillTrigger
          icon={Tag}
          label={t_i18n("Tags")}
          open={open}
          count={selected.length}
          onClick={() => handleOpen(!open)}
        />
        {open && (
          <FilterPillPanel role="group" aria-label={t_i18n("Tags")}>
            <div className="flex items-center gap-2 px-3 py-2 border-b border-elevation-subtle-soft">
              <Search className="h-3.5 w-3.5 text-default-secondary shrink-0" />
              <input
                ref={searchInputRef}
                value={popoverSearch}
                onChange={(e) => setPopoverSearch(e.target.value)}
                placeholder={t_i18n("Search tags...")}
                aria-label={t_i18n("Search tags...")}
                className="flex-1 bg-transparent content-base text-default-primary placeholder:text-default-secondary outline-none"
              />
              {selected.length > 0 && (
                <FilterClearButton className={cn("shrink-0", PANEL_CHIP_HEIGHT)} onClick={() => onChange([])}>
                  {t_i18n("Clear")}
                </FilterClearButton>
              )}
            </div>
            <div className="max-h-52 overflow-y-auto p-2 flex flex-wrap gap-1.5">
              {popoverTags.length === 0 ? (
                <p className="content-compact text-default-secondary px-1 py-2 w-full text-center">{t_i18n("No tags found")}</p>
              ) : (
                popoverTags.map((tag) => (
                  <button
                    key={tag}
                    type="button"
                    aria-pressed={selectedSet.has(tag)}
                    onClick={() => toggle(tag)}
                    className={filterPillClass(selectedSet.has(tag), PANEL_CHIP_HEIGHT)}
                  >
                    {tag}
                  </button>
                ))
              )}
            </div>
          </FilterPillPanel>
        )}
      </div>
    );
  }

  const unselectedTags = allTags.filter((t) => !selectedSet.has(t));
  const inlineTags = unselectedTags.slice(0, maxVisible);
  const overflowCount = unselectedTags.length - inlineTags.length;

  return (
    <div ref={containerRef} className={cn("relative flex items-center gap-1.5 flex-wrap", className)}>
      <Tag className="h-3.5 w-3.5 text-default-secondary shrink-0" />

      {/* Selected tags always shown */}
      {selected.map((tag) => (
        <button
          key={tag}
          onClick={() => toggle(tag)}
          className={filterPillClass(true)}
        >
          {tag}
        </button>
      ))}

      {/* Inline unselected tags (up to maxVisible) */}
      {inlineTags.map((tag) => (
        <button
          key={tag}
          onClick={() => toggle(tag)}
          className={filterPillClass(false)}
        >
          {tag}
        </button>
      ))}

      {/* Overflow trigger */}
      {overflowCount > 0 && (
        <FilterPillTrigger
          open={open}
          onClick={() => handleOpen(!open)}
          label={t_i18n("+{count} more", { count: overflowCount })}
        />
      )}

      {/* Clear button */}
      {selected.length > 0 && (
        <FilterClearButton onClick={() => onChange([])}>{t_i18n("Clear")}</FilterClearButton>
      )}

      {/* Popover dropdown */}
      {open && (
        <FilterPillPanel>
          <div className="flex items-center gap-2 px-3 py-2 border-b border-elevation-subtle-soft">
            <Search className="h-3.5 w-3.5 text-default-secondary shrink-0" />
            <input
              ref={searchInputRef}
              value={popoverSearch}
              onChange={(e) => setPopoverSearch(e.target.value)}
              placeholder={t_i18n("Search tags...")}
              className="flex-1 bg-transparent content-base text-default-primary placeholder:text-default-secondary outline-none"
            />
          </div>
          <div className="max-h-52 overflow-y-auto p-2 flex flex-wrap gap-1.5">
            {popoverTags.length === 0 ? (
              <p className="content-compact text-default-secondary px-1 py-2 w-full text-center">{t_i18n("No tags found")}</p>
            ) : (
              popoverTags.map((tag) => (
                <button
                  key={tag}
                  onClick={() => toggle(tag)}
                  className={filterPillClass(selectedSet.has(tag), PANEL_CHIP_HEIGHT)}
                >
                  {tag}
                </button>
              ))
            )}
          </div>
        </FilterPillPanel>
      )}
    </div>
  );
}
