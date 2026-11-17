// SPDX-License-Identifier: Apache-2.0

/**
 * Shared segmented pill filter — the toggle-group used across the Operations
 * tabs (Executions ownership/status, Background Tasks scope/status, Work Items
 * status/staleness), the visibility filter of every list page and the kind
 * pills of Presentations. Centralizes the pill markup so every filter row is
 * pixel identical instead of each page re-inlining the same buttons.
 *
 * `selection="radio"` makes it a radio group (one tab stop, arrow keys and
 * Home / End move the selection, as WAI-ARIA describes): use it when exactly
 * one option is always selected and the options are the values of one field.
 * The default keeps the toggle-button semantics (`aria-pressed`).
 *
 * A row of options wider than its container wraps onto a second row, the
 * options themselves never: a label never breaks inside its pill, whatever the
 * language (the Decisions outcome filter in French at 1024 px).
 *
 * An option can be `disabled` (radio or toggle): it cannot be picked, the
 * arrow keys of a radio group skip it, and its `disabledHint` shows as text
 * under the row, not only as a tooltip (a disabled button gets no hover and no
 * focus; design dev-docs/roi-outcomes.md 6.1 R2-A1).
 *
 * An option's `tip` (already translated, may hold two lines) is shown through
 * the shared `Tip` on hover AND keyboard focus, in place of the native `title`
 * of `hint`, which a keyboard user never sees: use it when the tooltip carries
 * information the label does not (the fleet autonomy strip: what each step
 * makes automatic for this fleet).
 *
 * Compact toolbars (`useCompactToolbar`): `iconOnly` draws the icon and
 * the count of an option that has an icon, its label kept for assistive
 * technology and as the tooltip; `collapsed` draws the whole group as one
 * select at the same place in the tab order, so a long status filter takes the
 * width of one picker instead of a row.
 *
 * `overflow="menu"`: the row never wraps; the trailing options that do not fit
 * the width the row has go into a "More tabs" menu beside it, the selected option
 * always kept in the row (the rule of the detail tab bars, `fitTabs`). For a
 * row of view tabs a docked panel narrows (the fleet tabs at 1024 px with
 * Modify with AI open, in a long locale: UX round 4 B3 on #10181), where a
 * second row or a cut label would read as broken tabs.
 */
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Tip } from "@/components/ui/tooltip";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { fitTabs } from "@/components/ui/detail-tabs-fit";

/** Width the "More tabs" trigger takes beside the row: its 32 px button and the 8 px gap before it. */
const MORE_WIDTH = 40;

const EMPTY_KEY = "__segment_any__";

/**
 * The 4 px between a segment's label and its count (pending or loaded): the 6 px icon gap
 * left the Knowledge bases toolbar 3 px short of one row at 1080 (UX r5 B8 on #11349).
 */
const COUNT_SPACING = (hasIcon: boolean) => (hasIcon ? "-ml-0.5" : "ml-1");

export interface SegmentedFilterOption<T extends string> {
  key: T;
  label: string;
  /** Shown after the label: how many items the option holds. */
  count?: number;
  /**
   * The count is not read yet: a placeholder about the width of a short figure holds its place, never "0",
   * so the row is as wide while loading as once loaded (UX r3 B5 on #11349).
   */
  countPending?: boolean;
  /** Drawn before the label, decorative (the label names the option). */
  icon?: React.ElementType;
  /** The option's tooltip: what it holds, in one sentence (an i18n key). */
  hint?: string;
  /** Tooltip shown on hover and keyboard focus through the shared `Tip` (already translated); replaces `hint`'s native title. */
  tip?: React.ReactNode;
  /** Cannot be picked; arrow keys skip it. */
  disabled?: boolean;
  /** Why it is disabled (an i18n key): shown as text under the row while it applies. */
  disabledHint?: string;
}

interface SegmentedFilterProps<T extends string> {
  options: SegmentedFilterOption<T>[];
  value: T;
  onChange: (key: T) => void;
  /** Accessible label for the group. */
  ariaLabel?: string;
  className?: string;
  /** `sm`: the switch of a chart or card header (LLM Analytics), 24 px with 12 px text (the type floor); `md`: a 36 px group beside 36 px form controls. */
  size?: "default" | "sm" | "md";
  /**
   * Tighter horizontal padding and gap at the default size (same height and text), for a row of
   * tabs in a column a side panel narrows (the fleet tabs at 1024 px with Modify with AI open).
   */
  dense?: boolean;
  /** `radio`: a radio group with arrow-key navigation; default: toggle buttons. */
  selection?: "toggle" | "radio";
  /** Forwarded to the group element. */
  testId?: string;
  /** Options with an icon show the icon and the count only; the label stays the accessible name and the tooltip. */
  iconOnly?: boolean;
  /** One select instead of the pills (a compact toolbar); needs `ariaLabel`. */
  collapsed?: boolean;
  /** Width classes of the collapsed select. */
  collapsedClassName?: string;
  /** `menu`: one row, the options that do not fit in a "More tabs" menu; default `wrap`: a second row. */
  overflow?: "wrap" | "menu";
}

export function SegmentedFilter<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  className,
  size = "default",
  dense = false,
  selection = "toggle",
  testId,
  iconOnly = false,
  collapsed = false,
  collapsedClassName,
  overflow = "wrap",
}: SegmentedFilterProps<T>) {
  const { t_i18n } = useFormatter();
  const fitted = useFittedOptions(options, value, overflow === "menu" && !collapsed, size, dense);
  const radio = selection === "radio";
  const refs = useRef<Partial<Record<T, HTMLButtonElement | null>>>({});
  const keys = options.map((opt) => opt.key);
  // Arrow keys and the tab stop only land on an option that can be picked.
  const enabled = options.filter((opt) => !opt.disabled).map((opt) => opt.key);
  // The tab stop of a radio group is the selected option, or the first one.
  const focusable = enabled.includes(value) ? value : enabled[0];
  const hints = options.filter((opt) => opt.disabled && opt.disabledHint);
  const hintId = useId();

  const move = (from: T, key: string) => {
    if (enabled.length === 0) return false;
    const index = enabled.indexOf(from);
    let next: T | undefined;
    if (key === "ArrowRight" || key === "ArrowDown") next = enabled[(index + 1) % enabled.length];
    else if (key === "ArrowLeft" || key === "ArrowUp") next = enabled[(index - 1 + enabled.length) % enabled.length];
    else if (key === "Home") next = enabled[0];
    else if (key === "End") next = enabled[enabled.length - 1];
    if (next === undefined) return false;
    onChange(next);
    refs.current[next]?.focus();
    return true;
  };

  if (collapsed && ariaLabel) {
    // A select item cannot carry "" (Radix): the "any" option of a filter
    // keyed "" travels as a sentinel.
    const toValue = (key: T) => (key === "" ? EMPTY_KEY : key);
    const fromValue = (next: string) => (next === EMPTY_KEY ? "" : next) as T;
    return (
      <div data-testid={testId} data-collapsed="true" className={cn("shrink-0", className)}>
        <SearchableSelect
          aria-label={ariaLabel}
          className={cn("h-8 w-[170px] content-compact", collapsedClassName)}
          searchable="auto"
          sort={false}
          value={toValue(keys.includes(value) ? value : keys[0])}
          onValueChange={(next) => onChange(fromValue(next))}
          options={options.map((opt) => ({
            value: toValue(opt.key),
            label: t_i18n(opt.label),
            description: opt.count !== undefined && !opt.countPending ? String(opt.count) : undefined,
            disabled: opt.disabled,
          }))}
        />
      </div>
    );
  }

  const group = (
    <div
      role={radio ? "radiogroup" : "group"}
      aria-label={ariaLabel}
      aria-describedby={hints.length > 0 ? hintId : undefined}
      data-testid={testId}
      data-dense={dense ? "true" : undefined}
      data-overflow={fitted.enabled ? "menu" : undefined}
      ref={fitted.groupRef}
      className={cn(
        "flex rounded-sm bg-elevation-hover p-0.5 items-center",
        fitted.enabled ? "w-max shrink-0 flex-nowrap" : "max-w-full flex-wrap",
        dense ? "gap-0.5" : "gap-1",
        className,
      )}
    >
      {fitted.shown.map((opt) => {
        const isActive = opt.key === value;
        const Icon = opt.icon;
        const button = (
          <button
            key={opt.key}
            ref={(node) => {
              refs.current[opt.key] = node;
            }}
            type="button"
            {...(radio
              ? { role: "radio", "aria-checked": isActive, tabIndex: opt.key === focusable ? 0 : -1 }
              : { "aria-pressed": isActive })}
            title={opt.tip ? undefined : opt.hint ? t_i18n(opt.hint) : iconOnly && Icon ? t_i18n(opt.label) : undefined}
            data-segment={opt.key}
            disabled={opt.disabled}
            onClick={() => onChange(opt.key)}
            onKeyDown={
              radio
                ? (event) => {
                    if (move(opt.key, event.key)) event.preventDefault();
                  }
                : undefined
            }
            className={cn(
              "rounded-sm font-medium transition-colors inline-flex items-center whitespace-nowrap",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
              size === "sm" ? "px-2 h-6 content-compact" : size === "md" ? "px-3 h-8 content-compact" : dense ? "px-2 h-7 content-compact" : "px-3 h-7 content-compact",
              Icon && "gap-1.5",
              isActive
                ? "bg-elevation-highlight text-default-primary"
                : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover",
              // The selected option of a read-only group stays at full strength: it is the value the reader must see.
              opt.disabled && "cursor-not-allowed hover:bg-transparent",
              opt.disabled && !isActive && "opacity-50 hover:text-default-secondary",
            )}
          >
            {Icon && <Icon className="h-3.5 w-3.5" aria-hidden />}
            {iconOnly && Icon ? <span className="sr-only">{t_i18n(opt.label)}</span> : t_i18n(opt.label)}
            {opt.countPending ? (
              <span
                aria-hidden="true"
                data-count-pending="true"
                className={cn("inline-block h-3 w-3 animate-pulse rounded-sm bg-elevation-hover", COUNT_SPACING(!!Icon))}
              />
            ) : (
              // A count fits its figure: a reserved width grew the loaded toolbars (UX r5 B8 on #11349).
              opt.count !== undefined && (
                <span data-count-slot="true" className={cn("tabular-nums", COUNT_SPACING(!!Icon))}>
                  {opt.count}
                </span>
              )
            )}
          </button>
        );
        if (!opt.tip) return button;
        // An enabled option is the trigger itself (it gets the tip's aria-describedby);
        // a disabled one is wrapped so the tip keeps a hover and focus target.
        return (
          <Tip key={opt.key} content={opt.tip} side="bottom" wrap={!!opt.disabled} wrapTabIndex={radio ? -1 : 0}>
            {button}
          </Tip>
        );
      })}
    </div>
  );
  const row = fitted.enabled ? (
    <div
      ref={fitted.barRef}
      // While measuring, the bar grows into the row's free room (`flex-auto`),
      // so the fit reads the room the bar may take, not its own content width:
      // a content-sized bar folded options with hundreds of pixels free (UX r3
      // B1 on #10402). Once fitted it is as wide as what it draws and never
      // shrinks below it (`shrink-0`): the fit already left out what does not
      // fit, and a shrinkable bar went to 0 px under a crowded nowrap toolbar,
      // its segments painted over the next control (UX r2 B2 on #11339).
      className={cn("flex min-w-0 items-center gap-2", fitted.measuring ? "flex-auto overflow-hidden" : "shrink-0")}
      data-measuring={fitted.measuring || undefined}
    >
      {group}
      {fitted.hidden.length > 0 && (
        <OverflowMenu
          label={t_i18n("More tabs")}
          className="shrink-0"
          items={fitted.hidden.map((opt) => ({
            id: `segment-${opt.key}`,
            label: t_i18n(opt.label),
            icon: opt.icon,
            disabled: opt.disabled,
            onSelect: () => onChange(opt.key),
          }))}
        />
      )}
    </div>
  ) : (
    group
  );
  if (hints.length === 0) return row;
  return (
    <div className="space-y-1">
      {row}
      <p id={hintId} className="content-compact text-default-secondary">
        {hints.map((opt) => t_i18n(opt.disabledHint as string)).join(" ")}
      </p>
    </div>
  );
}

/**
 * The room around a fitted bar: the width of its row and of every other item
 * of that row. Read in a layout effect only, never during render.
 */
function rowSignature(bar: HTMLElement): string {
  const row = bar.parentElement;
  if (!row) return "";
  const items = Array.from(row.children).filter((item): item is HTMLElement => item !== bar && item instanceof HTMLElement);
  return [row.clientWidth, ...items.map((item) => item.offsetWidth)].join(",");
}

/**
 * The options a one-row group shows and the ones its "More tabs" menu holds. A
 * measuring pass renders every option at its natural width (`w-max` in a
 * clipped bar), then `fitTabs` keeps what fits the bar; a new bar width, a new
 * label (a locale switch), a new selection or a change of the row around the
 * bar (its width, a sibling drawn or resized) measures again.
 */
function useFittedOptions<T extends string>(
  options: SegmentedFilterOption<T>[],
  value: T,
  enabled: boolean,
  size: string,
  dense: boolean,
) {
  const { t_i18n } = useFormatter();
  const barRef = useRef<HTMLDivElement>(null);
  const groupRef = useRef<HTMLDivElement>(null);
  const [kept, setKept] = useState<T[] | null>(null);
  const [layoutTick, setLayoutTick] = useState(0);
  const signature = `${size}|${dense}|${options.map((opt) => `${opt.key}:${t_i18n(opt.label)}:${opt.countPending ? "pending" : (opt.count ?? "")}`).join("|")}`;
  const measuredFor = useRef<string | null>(null);
  // The bar width once the fitted options are drawn. A bar sized by its content
  // (a flex item of a one-line toolbar) narrows when options move to the menu:
  // that width is the result of the fit, not a new width to fit, so only a width
  // other than the settled one measures again. Keying on the width measured
  // with every option shown looped wide/narrow until React gave up (#185, E2E
  // of #10402).
  const settledWidth = useRef<number | null>(null);
  // The row around the bar once the fit is drawn. A content-sized bar never
  // grows by itself when the row gains room (a wider window, a sibling that
  // shrank or went away): the free width goes to the flexible search. A change
  // of the row's width or of a fixed sibling measures again, so the folded
  // options come back (UX r3 B1 on #10402).
  const settledRow = useRef<string | null>(null);

  useLayoutEffect(() => {
    if (!enabled) return;
    const bar = barRef.current;
    const group = groupRef.current;
    if (!bar || !group) return;
    const key = `${signature}|${value}`;
    if (kept !== null) {
      if (measuredFor.current !== key) setKept(null);
      else if (settledWidth.current === null) {
        settledWidth.current = bar.clientWidth;
        settledRow.current = rowSignature(bar);
      } else if (bar.clientWidth !== settledWidth.current || rowSignature(bar) !== settledRow.current) setKept(null);
      return;
    }
    const buttons = [...group.querySelectorAll<HTMLElement>("[data-segment]")];
    if (buttons.length !== options.length) return;
    const style = getComputedStyle(group);
    const px = (raw: string) => Number.parseFloat(raw) || 0;
    const next = fitTabs({
      widths: buttons.map((button) => button.getBoundingClientRect().width),
      gap: px(style.columnGap),
      chrome: px(style.paddingLeft) + px(style.paddingRight),
      available: bar.clientWidth,
      more: MORE_WIDTH,
      activeIndex: options.findIndex((opt) => opt.key === value),
    }).map((index) => options[index].key);
    measuredFor.current = key;
    settledWidth.current = null;
    settledRow.current = null;
    setKept(next);
    // `options` is a fresh array on most renders; `signature` stands for it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, kept, layoutTick, signature, value]);

  useEffect(() => {
    const bar = barRef.current;
    if (!enabled || !bar || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setLayoutTick((tick) => tick + 1));
    observer.observe(bar);
    // The row and its items: a sibling drawn, resized or removed moves the
    // flexible search, so watching every item sees it.
    const row = bar.parentElement;
    if (row) {
      observer.observe(row);
      for (const item of Array.from(row.children)) {
        if (item !== bar) observer.observe(item);
      }
    }
    return () => observer.disconnect();
  }, [enabled]);

  if (!enabled) return { enabled, measuring: false, shown: options, hidden: [] as SegmentedFilterOption<T>[], barRef, groupRef };
  const measuring = kept === null;
  return {
    enabled,
    measuring,
    shown: measuring ? options : options.filter((opt) => kept.includes(opt.key)),
    hidden: measuring ? [] : options.filter((opt) => !kept.includes(opt.key)),
    barRef,
    groupRef,
  };
}
