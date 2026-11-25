// SPDX-License-Identifier: Apache-2.0

/** FilterPill -- the one filter pill of the list toolbars.
 *
 * The `Properties` pill (`PropertyFilter`), the tag chips (`TagFilter`) and the
 * Fleets `Filters` pill (`FleetMoreFilters`) carried three copies of one
 * pattern: the same trigger classes, the same
 * panel chrome and three ways to close on a press outside. They all draw from
 * here now:
 *
 * - `filterPillClass(active)`: the pill's classes, for a trigger or a chip.
 * - `FilterPillTrigger`: icon, label, optional count badge and chevron.
 * - `FilterPillPanel`: the `layer-2` floating panel under the trigger. It
 *   opens from the trigger's left edge, and from its right edge when that
 *   would carry it past the viewport (a
 *   Properties pill at x 840 scrolled a 1024 page 104 px sideways).
 * - `FilterClearButton`: the one clear control of a toolbar (the DataTable's
 *   "Clear all filters", the Properties and tag "Clear"), at the 12 px compact
 *   text of the pills, so no toolbar draws its own at 10 or 11 px (#9378
 *   slice 4g).
 *
 * Pill and clear control are 32 px high (`h-8`), the one toolbar control height
 * of the design standards s.3: the toolbar search field (`SearchInput toolbar`),
 * the visibility segments and the grid/list toggle are 32 px too, so a list
 * toolbar no longer sets 28 px pills beside a 36 px search (UX r1 A3 on
 * #10109). Tag chips inside a pill's panel stay 28 px (`PANEL_CHIP_HEIGHT`).
 * - `FiltersPill`: the one "Filters" pill of a list toolbar (criterion 6 of
 *   the header density sweep #10215): the secondary filter groups of a list
 *   sit in its panel, the trigger counts those that narrow the list.
 * - `useFilterPopover`: closes on a press outside and on Escape, through the
 *   shared `useClickOutside`; `ignore` keeps it open on a press inside a list
 *   a picker opened in a portal (`isInsideFloatingList`).
 *
 * `filter-pill-class.test.ts` keeps the class string in this file alone.
 */

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ButtonHTMLAttributes, type ElementType, type HTMLAttributes, type ReactNode, type RefObject } from "react";
import { ChevronDown, SlidersHorizontal } from "lucide-react";
import { useClickOutside } from "@/hooks/useClickOutside";
import { CountBadge } from "@/components/ui/count-badge";
import { cn } from "@/lib/utils";

const PILL_BASE =
  "rounded-sm px-2.5 h-8 content-compact font-medium transition-colors border inline-flex items-center gap-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";
const PILL_ON = "bg-accent/15 border-accent/30 text-accent";
const PILL_OFF =
  "bg-elevation-hover border-elevation-subtle-soft text-default-secondary hover:bg-elevation-highlight hover:text-default-primary";

/** The height of a chip listed inside a pill's panel, not a toolbar control. */
export const PANEL_CHIP_HEIGHT = "h-7";

/** The classes of a filter pill: accented when it is open or narrows the list. */
export function filterPillClass(active: boolean, className?: string): string {
  return cn(PILL_BASE, active ? PILL_ON : PILL_OFF, className);
}

// One line, never shrunk: in a one-row toolbar (DataTable `filtersInPill`) a long
// translation such as ja must not wrap inside the 32 px control (#9378 slice 8b).
const CLEAR_CLASS =
  "shrink-0 whitespace-nowrap rounded-sm px-2 h-8 content-compact font-medium text-default-secondary hover:text-default-primary transition-colors inline-flex items-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";

/** The clear control of a filter toolbar: the caller passes the translated label. */
export function FilterClearButton({ className, children, ...rest }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button type="button" className={cn(CLEAR_CLASS, className)} {...rest}>
      {children}
    </button>
  );
}

/** A press inside a portaled list (a select or combobox opened from a panel). */
export function isInsideFloatingList(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    !!target.closest('[data-radix-popper-content-wrapper], [role="listbox"], [role="option"]')
  );
}

/**
 * A press inside a portaled list or a date picker's calendar (a `dialog`)
 * opened from a panel: the `ignore` of a Filters pill holding a time range.
 */
export function isInsideFloatingPicker(target: EventTarget | null): boolean {
  return isInsideFloatingList(target) || (target instanceof Element && !!target.closest('[role="dialog"]'));
}

interface FilterPillTriggerProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon?: ElementType;
  label: ReactNode;
  open: boolean;
  /** How many filters behind the pill narrow the list; a badge when above 0. */
  count?: number;
  /** Accented even with no count (a pill whose choices show as chips beside it). */
  active?: boolean;
}

export function FilterPillTrigger({ icon: Icon, label, open, count = 0, active = false, className, ...rest }: FilterPillTriggerProps) {
  return (
    <button type="button" aria-expanded={open} className={filterPillClass(open || active || count > 0, className)} {...rest}>
      {Icon && <Icon className="h-3 w-3" aria-hidden />}
      {label}
      {count > 0 && (
        <CountBadge className="border-transparent bg-accent font-semibold text-accent-foreground">{count}</CountBadge>
      )}
      <ChevronDown className={cn("h-3 w-3 transition-transform", open && "rotate-180")} aria-hidden />
    </button>
  );
}

/** Space kept between an open panel and the viewport's edge, in px. */
const VIEWPORT_MARGIN = 8;

/**
 * Which edge of its anchor a panel opens from: the left edge, unless the panel
 * would then pass the viewport's right edge and opening from the right edge
 * keeps it inside the viewport.
 */
export function filterPanelAlign(
  anchor: { left: number; right: number },
  panelWidth: number,
  viewportWidth: number,
): "left" | "right" {
  const overflowsRight = anchor.left + panelWidth > viewportWidth - VIEWPORT_MARGIN;
  const fitsFromRight = anchor.right - panelWidth >= VIEWPORT_MARGIN;
  return overflowsRight && fitsFromRight ? "right" : "left";
}

/** The smallest height a fitted panel keeps, in px, so a pill near the bottom still opens a usable panel. */
const MIN_FITTED_PANEL_HEIGHT = 160;

/**
 * The tallest a fitted panel may be: the space left between its top edge and
 * the viewport's bottom edge, less the viewport margin. The panel scrolls inside
 * itself past that height, so an open Filters panel never makes the page scroll
 * (design standards s.3, one scroller; UX r1 B1 on #1822: the panel ran to
 * y 1602 in a 1080 viewport and hid its last facet below the fold).
 */
export function filterPanelMaxHeight(panelTop: number, viewportHeight: number): number {
  return Math.max(MIN_FITTED_PANEL_HEIGHT, Math.floor(viewportHeight - panelTop - VIEWPORT_MARGIN));
}

/**
 * The floating panel under a pill; the caller sets its padding and content.
 *
 * The edge it opens from is measured again after every render of the panel
 * (a pick inside it re-renders the toolbar, which may re-wrap and move the
 * pill), whenever the panel or its anchor changes size, and on a window
 * resize: measured once at open, a right-aligned panel kept its edge after the
 * pill moved left and opened 26 px past the content area (UX r1 B2 on #10402).
 */
export function FilterPillPanel({
  className,
  children,
  style,
  fitViewport = false,
  ...rest
}: HTMLAttributes<HTMLDivElement> & {
  /** Cap the panel's height at the space left below it, with its own scroller. */
  fitViewport?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [align, setAlign] = useState<"left" | "right">("left");
  const [maxHeight, setMaxHeight] = useState<number | undefined>(undefined);

  const measure = useCallback(() => {
    const panel = ref.current;
    const anchor = panel ? (panel.offsetParent ?? panel.parentElement) : null;
    if (!panel || !anchor) return;
    setAlign(filterPanelAlign(anchor.getBoundingClientRect(), panel.offsetWidth, document.documentElement.clientWidth));
    if (fitViewport) {
      setMaxHeight(filterPanelMaxHeight(panel.getBoundingClientRect().top, document.documentElement.clientHeight));
    }
  }, [fitViewport]);

  // After every render: the toolbar may have moved the pill (same value, no re-render).
  useLayoutEffect(measure);

  useLayoutEffect(() => {
    const panel = ref.current;
    const anchor = panel ? (panel.offsetParent ?? panel.parentElement) : null;
    window.addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    if (panel) observer?.observe(panel);
    if (anchor) observer?.observe(anchor);
    if (anchor?.parentElement) observer?.observe(anchor.parentElement);
    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [measure]);

  return (
    <div
      ref={ref}
      data-align={align}
      className={cn(
        "absolute top-full z-50 mt-1.5 w-72 rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay",
        align === "right" ? "right-0" : "left-0",
        fitViewport && "overflow-y-auto overscroll-contain",
        className,
      )}
      style={fitViewport && maxHeight !== undefined ? { ...style, maxHeight } : style}
      {...rest}
    >
      {children}
    </div>
  );
}

interface FiltersPillProps {
  /** The translated label of the trigger and the accessible name of its panel. */
  label: string;
  /** How many of the filters in the panel narrow the list. */
  count: number;
  children: ReactNode;
  testId?: string;
}

/**
 * The "Filters" pill of a list toolbar: one trigger with an active-count badge
 * and a panel holding every secondary filter group, so the toolbar keeps one
 * row. The panel is as wide as its widest group (`w-max`, at least the 288 px
 * of a pill panel) and stays open on a press inside a picker's portaled list
 * or calendar.
 */
export function FiltersPill({ label, count, children, testId }: FiltersPillProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  useFilterPopover(rootRef, open, setOpen, isInsideFloatingPicker);
  return (
    <div ref={rootRef} className="relative shrink-0" data-testid={testId}>
      <FilterPillTrigger
        icon={SlidersHorizontal}
        label={label}
        open={open}
        count={count}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((v) => !v)}
      />
      {open && (
        <FilterPillPanel
          id={panelId}
          role="group"
          aria-label={label}
          fitViewport
          className="w-max min-w-72 space-y-3 p-3"
        >
          {children}
        </FilterPillPanel>
      )}
    </div>
  );
}

/**
 * Closes the pill's panel on a press outside `ref` and on Escape. A press, or
 * the focus, inside what `ignore` accepts (a portaled list) leaves it open.
 * `ignore` must keep one identity across renders (a module-level function).
 * Escape hands the focus back to the pill's trigger (the `aria-expanded`
 * button inside `ref`) when the focus was inside the pill or lost to the
 * body, so a keyboard user stays on the toolbar (UX r2 A4 on #10312).
 */
export function useFilterPopover(
  ref: RefObject<HTMLElement | null>,
  open: boolean,
  setOpen: (open: boolean) => void,
  ignore?: (target: EventTarget | null) => boolean,
) {
  const close = useCallback(() => setOpen(false), [setOpen]);
  useClickOutside(ref, close, open, ignore);
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || ignore?.(document.activeElement)) return;
      const root = ref.current;
      const active = document.activeElement;
      const focusWasHere = !active || active === document.body || !!root?.contains(active);
      close();
      if (focusWasHere) root?.querySelector<HTMLElement>("button[aria-expanded]")?.focus();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [ref, open, close, ignore]);
}
