// SPDX-License-Identifier: Apache-2.0

/**
 * The faceted sidebar ("basket"): a sticky left aside of checkbox facets grouped under headings, an icon and a
 * count per value, "Clear" per group and "Clear all" (#1501, Sam 23 Nov 2026). One component for the pattern: the
 * integration catalog and the CTEM asset list both draw it. Multi-select within a group is OR, across groups AND;
 * the caller owns the selection and the counts.
 */
import { useState, type CSSProperties, type ElementType, type ReactNode } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { TruncatedText } from "@/components/ui/truncated-text";
import { FIT_BESIDE_ATTRIBUTE, useFitBeside } from "@/hooks/useFitToViewport";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

export function FacetCheckbox({
  checked, onToggle, label, count, icon: Icon, leading, indent,
}: {
  checked: boolean;
  onToggle: () => void;
  label: string;
  count: number;
  /** A component glyph (lucide), tinted with the row's state. */
  icon?: ElementType;
  /** A ready-made mark (a product logo, a category glyph) drawn as is. */
  leading?: ReactNode;
  /** A nested value (a subcategory under its category). */
  indent?: boolean;
}) {
  // The box is the shared SelectionBox, the one every selectable list draws, so its edge keeps
  // 3:1 in both themes; the row around it is a pointer target only (the box is the keyboard stop and
  // carries the checkbox role, its name and its state). A facet with no match and not checked is disabled.
  const disabled = count === 0 && !checked;
  return (
    <div
      className={cn(
        "group flex w-full items-center gap-2.5 rounded-sm px-2 py-1.5 text-left transition-colors",
        indent && "pl-7",
        disabled ? "cursor-not-allowed" : "cursor-pointer hover:bg-elevation-hover",
      )}
      onClick={disabled ? undefined : onToggle}
    >
      <SelectionBox checked={checked} onToggle={onToggle} label={label} disabled={disabled} />
      {Icon && (
        <Icon
          className={cn(
            "h-3.5 w-3.5 shrink-0 transition-colors",
            checked ? "text-accent" : disabled ? "text-default-disabled" : "text-default-secondary",
          )}
        />
      )}
      {leading && <span className={cn("inline-flex shrink-0", disabled && "opacity-50")}>{leading}</span>}
      {/* One line per facet, as in OpenCTI and OpenAEV (Sam, 18 Nov 2026): a label too long for the
          fixed aside is cut with an ellipsis and named whole by the design-system tooltip, on hover
          and on keyboard focus, so every row keeps the same height and the count stays right-aligned. */}
      <TruncatedText
        text={label}
        className={cn(
          "flex-1 content-compact transition-colors",
          checked
            ? "font-medium text-default-primary"
            : disabled
              ? "text-default-disabled"
              : "text-default-secondary group-hover:text-default-primary",
        )}
      />
      <span
        className={cn(
          "shrink-0 rounded-sm bg-elevation-hover px-1.5 py-px content-compact tabular-nums",
          // A disabled facet's count takes the disabled ink with its label and icon, so its "0" never reads as enabled.
          disabled ? "text-default-disabled" : "text-default-secondary",
        )}
      >
        {count}
      </span>
    </div>
  );
}

export function FacetGroupLabel({ text }: { text: string }) {
  // One line too: a long group name ("Engineering & Operations") is cut and named whole by the tooltip.
  return (
    <p className="flex min-w-0 px-2 font-sans-geologica content-compact font-semibold uppercase tracking-[0.12em] text-default-secondary">
      <TruncatedText text={text} className="flex-1" />
    </p>
  );
}

/** One group of facets under its heading; with onClear, a "Clear" on the heading while a value of it is picked. */
export function FacetGroup({
  label, children, onClear, divided, testId,
}: {
  label: string;
  children: ReactNode;
  onClear?: () => void;
  /** A rule above the group (a new family of facets). */
  divided?: boolean;
  testId?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className={cn("space-y-1", divided && "border-t border-elevation-subtle-soft pt-4")} data-testid={testId}>
      <div className="flex min-w-0 items-center gap-2">
        <div className="min-w-0 flex-1">
          <FacetGroupLabel text={label} />
        </div>
        {onClear && (
          <Button variant="ghost" size="sm" className="h-6 px-2 content-compact text-default-secondary" onClick={onClear}>
            {t_i18n("Clear")}
          </Button>
        )}
      </div>
      <div className="space-y-px">{children}</div>
    </div>
  );
}

/** A facet value with nested values (a category and its subcategories), the nested list folded by default. */
export function FacetBranch({
  row, children, expandLabel, defaultOpen = false,
}: {
  row: ReactNode;
  children: ReactNode;
  expandLabel: string;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div>
      <div className="flex min-w-0 items-center">
        <div className="min-w-0 flex-1">{row}</div>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-6 shrink-0 p-0 text-default-secondary"
          aria-expanded={open}
          aria-label={expandLabel}
          onClick={() => setOpen((value) => !value)}
        >
          <Icon name={open ? "chevron-down" : "chevron-right"} size={16} aria-hidden />
        </Button>
      </div>
      {open && <div className="space-y-px">{children}</div>}
    </div>
  );
}

/**
 * The sticky aside: a "Filters" heading, "Clear all" while anything is picked, then the groups.
 * With `fitBeside` (beside a records table that fits the window), the aside ends at the window's
 * bottom, as the table does, and is the only scroller of the facets: the page never scrolls (#1501
 * UX r1 B1). Without it (a long-form page, the integration catalog), it sticks while the page scrolls.
 */
export function FacetSidebar({
  children, hasFilters, onClearAll, className, testId, fitBeside = false,
}: {
  children: ReactNode;
  hasFilters: boolean;
  onClearAll: () => void;
  className?: string;
  testId?: string;
  fitBeside?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const [aside, setAside] = useState<HTMLElement | null>(null);
  const room = useFitBeside(fitBeside ? aside : null);
  const fitted = fitBeside && room !== undefined;
  return (
    <aside
      ref={setAside}
      {...(fitBeside ? { [FIT_BESIDE_ATTRIBUTE]: "" } : {})}
      style={fitted ? ({ "--facet-fit": `${room}px` } as CSSProperties) : undefined}
      className={cn(
        "w-full shrink-0 lg:w-64 lg:overflow-y-auto lg:pr-1",
        fitted ? "lg:max-h-[var(--facet-fit)]" : "lg:sticky lg:top-6 lg:max-h-[calc(100vh-3rem)]",
        className,
      )}
      data-testid={testId}
    >
      <div className="space-y-5 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-4 backdrop-blur-sm">
        <div className="flex items-center justify-between">
          <p className="font-sans-geologica content-base font-semibold">{t_i18n("Filters")}</p>
          {hasFilters && (
            <Button variant="ghost" size="sm" className="h-6 gap-1 px-2 content-compact text-default-secondary" onClick={onClearAll}>
              <Icon name="rotate-ccw" size={16} aria-hidden /> {t_i18n("Clear all")}
            </Button>
          )}
        </div>
        {children}
      </div>
    </aside>
  );
}
