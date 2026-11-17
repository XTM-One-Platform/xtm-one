// SPDX-License-Identifier: Apache-2.0

/**
 * Shared list control bar for the Operations tabs and the Video Studio's media
 * library.
 *
 * Renders the unified row that sits above every such list: an optional
 * multi-select summary (select-all checkbox, "N selected / select all matching",
 * Clear + bulk Delete) on the left, and inline First/Prev/Next/Last paging on
 * the right. An empty list draws no bar at all: the list's own empty state
 * says there is nothing, so a "No results" row above it says it twice.
 * When `selection` is omitted it degrades to a plain
 * "Showing X-Y of Z" + paging bar, so tabs without bulk actions still get the
 * exact same chrome. This is the single source of truth that previously lived
 * (duplicated) inside TaskHistory and BackgroundTasks. `SelectionBox` is the
 * box it draws, for a row or a card to select itself with the same control.
 *
 * A list with its own bulk actions (the fleet's Members view) passes them in
 * `selection.bulkActions`, drawn after Clear with the same chrome; a list
 * without `onBulkDelete` draws no Delete. Escape with a selection runs Clear,
 * on every list that uses the bar.
 */
import { useEffect, type ElementType, type ReactNode } from "react";
import { Check, ChevronLeft, ChevronRight, ShieldCheck, Trash2, X } from "lucide-react";
import { IconButton, Spinner } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { Skeleton } from "@/components/ui/skeleton";
import { TotalChip } from "@/components/ui/total-chip";
import { cn, formatCompactNumber } from "@/lib/utils";
import { WARNING_ON_FILL_TEXT } from "@/lib/warning-text";
import { useFormatter } from "@/i18n";

/** One bulk action of a list, drawn in the bar while rows are selected. */
export interface ListBulkAction {
  /** Stable key + test handle. */
  id: string;
  label: string;
  icon: ElementType;
  onSelect: () => void;
  disabled?: boolean;
  /** Why the action is disabled, in a tooltip reachable by keyboard. */
  disabledReason?: string;
  busy?: boolean;
  /** Draw the icon alone: the label becomes the button's accessible name and its tooltip (the disabled
   *  reason when disabled). For the action whose label varies most in length across languages, so the bar
   *  keeps its actions on one row at 1024 in every catalog. */
  iconOnly?: boolean;
}

interface ListSelectionState {
  /** Whether the current page has any selectable rows. */
  hasVisibleRows: boolean;
  /** Every visible row on this page is selected. */
  allVisibleSelected: boolean;
  /** Number of explicitly selected rows. */
  selectedCount: number;
  /** "All N matching the filters" mode is active. */
  allMatchingSelected: boolean;
  /** Total rows matching the active filters (for the select-all-matching CTA). */
  totalMatching: number;
  /** Whether the "select all N matching" link should be offered. */
  canSelectAllMatching: boolean;
  onToggleVisible: () => void;
  onSelectAllMatching: () => void;
  onClear: () => void;
  /** Omitted: the list offers no bulk Delete. */
  onBulkDelete?: () => void;
  bulkDeleting?: boolean;
  /** The list's own bulk actions, after Clear. */
  bulkActions?: ListBulkAction[];
  /** How many of the selected rows are awaiting an approval decision.
   *  Zero hides the approve/reject actions — offering them over a selection
   *  none of which can be answered is a control that decides nothing. */
  approvableCount?: number;
  onBulkApprove?: (verdict: "approve" | "approve_always" | "reject") => void;
  bulkApproving?: boolean;
  /** Runs in the selection that hold gated calls. */
  approvableRunCount?: number;
  /** Visible rows awaiting an approval decision, and a shortcut selecting
   *  exactly those (FR-031a). Offered because the useful selection here
   *  is rarely "everything on the page" — approvals sit interleaved with
   *  completed-run notices that cannot be answered at all. */
  approvableVisibleCount?: number;
  onSelectApprovable?: () => void;
  /** Why no row of the page can be selected. When the page has rows but none
   *  is selectable, the header keeps its box, disabled with this reason, so
   *  the bar keeps the same selection column as the rows under it. */
  notSelectableReason?: string;
}

interface ListControlBarProps {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  selection?: ListSelectionState;
  /** A control of the list itself (its sort order) drawn before the pager, so
   *  it sits on the list's row instead of widening the filter row above. The
   *  slot is one h-6 row, the pager's height: render the control at that scale
   *  (a `SegmentedFilter` with `size="sm"` and no track inset) so the bar keeps
   *  the height of every other list's bar. */
  extra?: ReactNode;
  /** The list's first read is in flight: the count is not known yet, so the
   *  bar draws no range text and no pager (a "No results" or "0 of 0" above
   *  a loading skeleton contradicts it). A selection still shows, and
   *  so does an `extra` control, alone on the bar. */
  loading?: boolean;
  /** Draw the pager (default). A list that always shows every row on one
   *  page passes `false`: a 1/1 pager only takes the room its bulk actions
   *  need to stay on one row at 1024. The range text still shows. */
  pager?: boolean;
  /** Where the list's count and pager sit. `"bar"` (default): on this bar.
   *  `"footer"`: the list draws its shared pager under the rows, so the bar
   *  draws no range text and no pager and keeps the selection, its bulk actions
   *  and `extra`. */
  paging?: "bar" | "footer";
  /** With `paging="footer"`: the bar says the list's total, once, at its right end,
   *  and the pager under the rows gets `totalShownAbove` (Sam, 11 Nov 2026: the
   *  total at the top only, never a bottom "N results"). Leave it off above a
   *  data table, whose own toolbar says the total. */
  showTotal?: boolean;
}

/**
 * The select box of the bar and of the rows it selects: a `role="checkbox"` button, never a native checkbox.
 * `disabled` keeps the box (same size, same place) for a row that cannot be selected, so every row of a
 * list shares one selection column. It is drawn at full opacity, its border in the disabled text ink
 * (`DISABLED_BOX_BORDER`) over the `bg-elevation-disabled` fill, so its edge keeps 3:1 or more on the card,
 * the page and the control bar in both themes while reading weaker than the enabled box (the
 * `border-elevation-default` it drew read 2.16:1 on the light card and 2.56:1 on the light control bar);
 * `reason` says why, in a tooltip reachable by keyboard. A click on it
 * goes nowhere: it never reaches a clickable row or card around it.
 *
 * The enabled box, checked or not, draws its border in the secondary text ink (`ENABLED_BOX_BORDER`), which
 * keeps 3:1 or more on every surface it sits on in both themes: 5.4 to 8.8:1 on light (the card, the page,
 * the `bg-elevation-hover` control bar, the `bg-elevation-highlight` selected row), 5.9 to 9.0:1 on dark. It
 * reads stronger than the disabled box. The former `border-elevation-subtle-soft` hairline (15% alpha)
 * painted 1.03 to 1.15:1, and the checked box's `border-elevation-default` read 1.93:1 against the
 * selected row on light, where only the glyph told it apart. Hover lifts it to the primary ink.
 * Both borders are `border-current` over a named text ink, so no arbitrary colour utility is needed; the
 * glyph names its own ink.
 */
const ENABLED_BOX_BORDER = "border-current text-default-secondary hover:text-default-primary";
const DISABLED_BOX_BORDER = "border-current text-default-disabled";
/**
 * Whether an Escape belongs to something else than the selection: an open
 * dialog, menu or listbox, or a text field (which clears or leaves itself).
 */
function escapeBelongsElsewhere(target: EventTarget | null): boolean {
  if (typeof document !== "undefined" && document.querySelector('[role="dialog"][data-state="open"], [role="menu"], [role="listbox"]')) {
    return true;
  }
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== "string") return false;
  return el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable === true;
}

/**
 * A bulk action of the list, the bar's ghost button; disabled, its reason in a tooltip.
 * Its label keeps the Button's own type (14px/600), as Clear, Delete and Approve
 * beside it: one size across the bar. It does not shrink, so a long label moves
 * the action to the next line whole rather than squeezing it.
 */
function BulkActionButton({ action }: { action: ListBulkAction }) {
  const Icon = action.icon;
  const button = (
    <Button
      variant="ghost"
      size="sm"
      data-testid={`list-bulk-action-${action.id}`}
      className={cn("h-6 shrink-0 gap-1 text-default-secondary hover:text-default-primary", action.iconOnly ? "w-6 px-0" : "px-2")}
      onClick={action.onSelect}
      disabled={action.disabled || action.busy}
      aria-busy={action.busy || undefined}
      aria-label={action.iconOnly ? action.label : undefined}
    >
      {action.busy && <Spinner size="sm" tone="inherit" />}
      {!action.busy && <Icon className="h-3 w-3" aria-hidden="true" />}
      {!action.iconOnly && action.label}
    </Button>
  );
  if (action.disabled && action.disabledReason) {
    return (
      <Tip content={action.iconOnly ? `${action.label}: ${action.disabledReason}` : action.disabledReason} wrap>
        {button}
      </Tip>
    );
  }
  if (action.iconOnly) return <Tip content={action.label}>{button}</Tip>;
  return button;
}

/**
 * Keyboard focus on the enabled box draws the design-system ring (2 px offset plus 2 px `ring-focus`, as the
 * Button and the Tip wrapper), not the browser's default outline in currentColor. The disabled box is
 * not a tab stop of its own: its Tip wrapper owns the ring (TIP_WRAP_FOCUS_RING in tooltip.tsx).
 */
const SELECTION_BOX_FOCUS_RING =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 focus-visible:ring-offset-focus";

export function SelectionBox({
  checked,
  onToggle,
  label,
  className,
  disabled = false,
  reason,
}: {
  checked: boolean | "mixed";
  onToggle: () => void;
  label: string;
  className?: string;
  disabled?: boolean;
  reason?: string;
}) {
  if (disabled) {
    return (
      <span
        className={cn("inline-flex shrink-0", className)}
        onClick={(event) => event.stopPropagation()}
      >
        <Tip
          content={reason}
          wrap
          wrapClassName="inline-flex rounded-[3px]"
        >
          <button
            type="button"
            role="checkbox"
            aria-checked={false}
            disabled
            aria-label={label}
            className={cn("h-4 w-4 rounded-[3px] border bg-elevation-disabled cursor-not-allowed shrink-0", DISABLED_BOX_BORDER)}
          />
        </Tip>
      </span>
    );
  }
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      onClick={(event) => {
        event.stopPropagation();
        onToggle();
      }}
      aria-label={label}
      className={cn(
        "h-4 w-4 rounded-[3px] border transition-all flex items-center justify-center shrink-0",
        ENABLED_BOX_BORDER,
        SELECTION_BOX_FOCUS_RING,
        checked === true
          ? "bg-elevation-highlight"
          : checked === "mixed"
            ? "bg-elevation-highlight/50"
            : undefined,
        className,
      )}
    >
      {checked !== false && <Check className="h-3 w-3 text-default-primary" />}
    </button>
  );
}

export function ListControlBar({
  page,
  pageSize,
  total,
  onPageChange,
  selection,
  extra,
  loading = false,
  pager = true,
  paging = "bar",
  showTotal = false,
}: ListControlBarProps) {
  const inFooter = paging === "footer";
  const { t_i18n, intl } = useFormatter();
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const singlePage = totalPages <= 1;
  const from = total === 0 ? 0 : page * pageSize + 1;
  const to = Math.min((page + 1) * pageSize, total);
  const countLabel = t_i18n("Showing {from}–{to} of {total}", { from: intl.formatNumber(from), to: intl.formatNumber(to), total: formatCompactNumber(total) });

  const hasSelection =
    !!selection && (selection.allMatchingSelected || selection.selectedCount > 0);
  const onClear = selection?.onClear;

  // Escape clears the selection, unless a dialog, a menu or a field takes it.
  useEffect(() => {
    if (!hasSelection || !onClear) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (escapeBelongsElsewhere(event.target)) return;
      onClear();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [hasSelection, onClear]);

  // Nothing to select, nothing to page: the empty state below speaks alone.
  // While the list loads the count is unknown: the skeleton speaks alone,
  // unless the bar carries a control of the list (`extra`, e.g. a sort): that
  // control stays mounted through its own refetch so it keeps focus.
  if (total === 0 && !loading && !hasSelection) return null;
  // A selectable list keeps its header row through the first read: its
  // select-all box and its total drawn as placeholders, so the row does not
  // pop in above the rows when they land (#11345).
  if (loading && !hasSelection && !extra && !selection) return null;
  // The footer carries the pager: a bar with nothing to select, no control and no
  // total of its own would be an empty row.
  if (inFooter && !showTotal && !hasSelection && !extra && !selection?.hasVisibleRows && !selection?.notSelectableReason) return null;

  return (
    <div
      data-testid="list-control-bar"
      className={cn(
        "flex items-center gap-3 rounded-sm border px-4 py-2 transition-all",
        hasSelection
          ? "border-elevation-default bg-elevation-highlight/[0.04]"
          : "border-elevation-subtle-soft bg-elevation-hover",
      )}
    >
      {loading && !hasSelection && selection ? (
        <span className="inline-flex shrink-0" data-testid="list-control-bar-select-skeleton" aria-hidden="true">
          <Skeleton className="h-4 w-4 rounded-[3px]" />
        </span>
      ) : selection && selection.hasVisibleRows ? (
        <SelectionBox
          checked={selection.allVisibleSelected ? true : selection.selectedCount > 0 ? "mixed" : false}
          onToggle={selection.onToggleVisible}
          label={selection.allVisibleSelected ? t_i18n("Deselect all") : t_i18n("Select all")}
        />
      ) : (
        selection?.notSelectableReason && (
          <SelectionBox
            checked={false}
            onToggle={selection.onToggleVisible}
            label={t_i18n("Select all")}
            disabled
            reason={selection.notSelectableReason}
          />
        )
      )}

      {/* The entry point to a bulk approval, so it must be reachable with
          nothing selected yet — approvals sit interleaved with notices that
          cannot be answered, and "select all" is rarely the useful set
          (FR-031a). */}
      {(selection?.approvableVisibleCount ?? 0) > 0 && selection?.onSelectApprovable && (
        <Button
          variant="ghost"
          size="sm"
          className={cn("h-6 shrink-0 px-2", WARNING_ON_FILL_TEXT)}
          onClick={selection.onSelectApprovable}
        >
          {/* The design-system Button sets its own type style (14px/600) on the
              button, which a type class passed in className does not
              override, so the label carries `content-compact` (12px) itself
              and matches the 12px counter beside it. */}
          <span className="content-compact">
            {t_i18n("Select the {n} awaiting approval", { n: selection.approvableVisibleCount ?? 0 })}
          </span>
        </Button>
      )}

      {/* min-h-6 matches the h-6 paging controls, so the bar keeps its height
          when a selection outlives the rows it was made on. The row
          wraps (flex-wrap, data-wrap) so a long set of bulk actions moves to a
          second line instead of running under the pager or past the viewport
          at 1024. */}
      <div
        data-part="selection-actions"
        data-wrap="true"
        className="flex flex-wrap items-center gap-x-2 gap-y-1 flex-1 min-w-0 min-h-6"
      >
        {hasSelection && selection ? (
          <>
            <span className="content-compact text-default-primary font-medium whitespace-nowrap">
              {selection.allMatchingSelected ? (
                <>{t_i18n("All {count} selected", { count: formatCompactNumber(selection.totalMatching) })}</>
              ) : (
                <>
                  {t_i18n("{count} selected", { count: selection.selectedCount })}
                  {selection.canSelectAllMatching && (
                    <span className="font-normal text-default-secondary">
                      {" — "}
                      <button
                        type="button"
                        onClick={selection.onSelectAllMatching}
                        className="text-accent hover:text-accent/80 underline underline-offset-2 transition-colors"
                      >
                        {t_i18n("select all {count}", { count: formatCompactNumber(selection.totalMatching) })}
                      </button>
                    </span>
                  )}
                </>
              )}
            </span>
            <div className="h-4 w-px bg-elevation-hover shrink-0" />
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-default-secondary hover:text-default-primary"
              onClick={selection.onClear}
            >
              {t_i18n("Clear")}
            </Button>
            {(selection.approvableCount ?? 0) > 0 && selection.onBulkApprove && (
              <>
                {(selection.approvableRunCount ?? 0) > 0 && (
                  <span className="shrink-0 content-compact text-default-secondary">
                    {t_i18n("across {n, plural, one {# run} other {# runs}}", { n: selection.approvableRunCount ?? 0 })}
                  </span>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 gap-1 px-2 text-feedback-warning-tertiary hover:text-feedback-warning-tertiary hover:bg-feedback-warning-primary/10"
                  onClick={() => selection.onBulkApprove?.("reject")}
                  disabled={selection.bulkApproving}
                >
                  <X className="h-3 w-3" />
                  {t_i18n("Reject {n} selected", { n: selection.approvableCount ?? 0 })}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 gap-1 px-2 text-feedback-success-primary hover:text-feedback-success-primary hover:bg-feedback-success-primary/10"
                  onClick={() => selection.onBulkApprove?.("approve")}
                  disabled={selection.bulkApproving}
                  aria-busy={selection.bulkApproving || undefined}
                >
                  {selection.bulkApproving && <Spinner size="sm" tone="inherit" />}
                  {!selection.bulkApproving && <Check className="h-3 w-3" aria-hidden="true" />}
                  {t_i18n("Approve {n} selected", { n: selection.approvableCount ?? 0 })}
                </Button>
                {/* Separated rather than folded into a split button: this one
                    outlives the runs it is answering, and a menu item one pixel
                    from "Approve" is how it gets clicked by accident. Its
                    confirm names every tool it would un-gate (FR-032). */}
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 gap-1 px-2 text-feedback-warning-tertiary hover:text-feedback-warning-tertiary hover:bg-feedback-alert-primary/10"
                  onClick={() => selection.onBulkApprove?.("approve_always")}
                  disabled={selection.bulkApproving}
                >
                  <ShieldCheck className="h-3 w-3" />
                  {t_i18n("Approve always {n} selected", { n: selection.approvableCount ?? 0 })}
                </Button>
                <div className="h-4 w-px bg-elevation-hover shrink-0" />
              </>
            )}
            {selection.bulkActions?.map((action) => (
              <BulkActionButton key={action.id} action={action} />
            ))}
            {selection.onBulkDelete && (
              <Button
                variant="ghost"
                size="sm"
                className="h-6 gap-1 px-2 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
                onClick={selection.onBulkDelete}
                disabled={selection.bulkDeleting}
                aria-busy={selection.bulkDeleting || undefined}
              >
                {selection.bulkDeleting && <Spinner size="sm" tone="inherit" />}
                {!selection.bulkDeleting && <Trash2 className="h-3 w-3" aria-hidden="true" />}
                {t_i18n("Delete")}
              </Button>
            )}
          </>
        ) : loading || inFooter ? null : (
          /* One line in every locale, never under the controls beside it: fr
             "Affichage de 1 à 18 sur 18" at 1024 first wrapped beside the 12 px
             sort switch (UX r1 B1 on #10420), then, kept on one line, ran 7 px
             under it (UX r2 B1). The count truncates inside its slot and the
             design-system Tip carries the whole text. Format with the app's
             resolved locale (not the browser's) so the range matches the rest
             of the localized UI. */
          <Tip content={countLabel}>
            <span data-testid="list-control-bar-count" className="content-compact truncate min-w-0 text-default-secondary">
              {countLabel}
            </span>
          </Tip>
        )}
      </div>

      {extra && (
        <div data-testid="list-control-bar-extra" className="flex h-6 shrink-0 items-center">
          {extra}
        </div>
      )}

      {inFooter && showTotal && (loading ? (
        <TotalChip testId="list-control-bar-total" total={0} loading />
      ) : total > 0 && (
        <TotalChip testId="list-control-bar-total" total={total} />
      ))}

      {/* An empty list has no page to move to: no pager; nor has a
          list whose count is still loading. */}
      {pager && !inFooter && total > 0 && !loading && (
        <div className="flex items-center gap-1 shrink-0">
          <Button
            variant="ghost"
            size="sm"
            className="h-6 content-compact px-1.5"
            disabled={singlePage || page === 0}
            onClick={() => onPageChange(0)}
          >
            <span className="content-compact">{t_i18n("First")}</span>
          </Button>
          <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Previous page")} icon={<ChevronLeft className="h-3.5 w-3.5" />} className="h-6 w-6" disabled={singlePage || page === 0} onClick={() => onPageChange(page - 1)} />
          <span className="content-compact text-default-secondary px-1.5">
            {page + 1}/{totalPages}
          </span>
          <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Next page")} icon={<ChevronRight className="h-3.5 w-3.5" />} className="h-6 w-6" disabled={singlePage || page >= totalPages - 1} onClick={() => onPageChange(page + 1)} />
          <Button
            variant="ghost"
            size="sm"
            className="h-6 content-compact px-1.5"
            disabled={singlePage || page >= totalPages - 1}
            onClick={() => onPageChange(totalPages - 1)}
          >
            <span className="content-compact">{t_i18n("Last")}</span>
          </Button>
        </div>
      )}
    </div>
  );
}
