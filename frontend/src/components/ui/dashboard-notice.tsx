// SPDX-License-Identifier: Apache-2.0

import {
  AlertTriangle,
  CheckCircle2,
  Info,
  Loader2,
  X,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { cn } from "@/lib/utils";

/**
 * Severity of a dashboard notice - drives a single, consistent color set so the
 * SAME kind of message always looks the same everywhere:
 * - `error`    : blocking / broken state (red)
 * - `warning`  : needs attention, not blocking (amber)
 * - `progress` : something is actively running (cyan, animated)
 * - `info`     : neutral context / preview (cyan, subtle)
 * - `success`  : healthy / done (green)
 */
type NoticeVariant = "error" | "warning" | "progress" | "info" | "success";

interface VariantStyle {
  container: string;
  icon: string;
  title: string;
  body: string;
  action: string;
}

// The tone lives on the icon, the border and the tint. The copy reads in the
// default text colours, never a tone faded with an opacity modifier: over the
// translucent tint on the bare page a faded tone fell under 4.5:1 in the light
// theme (error body 3.49:1). Warning keeps its own amber text on the
// opaque warning box. tests_unit/warning-text-contrast.test.ts computes every
// variant on the page background in both themes.
const VARIANT_STYLES: Record<NoticeVariant, VariantStyle> = {
  error: {
    container: "border-feedback-error-primary/30 bg-feedback-error-primary/[0.06]",
    icon: "text-feedback-error-primary",
    title: "text-default-primary",
    body: "text-default-secondary",
    action: "text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10",
  },
  warning: {
    // Opaque: the notice sits on the page, where a translucent amber tint
    // leaves warning text under 4.5:1 in the light theme.
    container: "border-feedback-alert-primary/30 bg-warning-box",
    icon: "text-feedback-warning-primary",
    title: "text-feedback-warning-primary",
    body: "text-feedback-warning-primary",
    action: "text-feedback-warning-tertiary hover:text-feedback-warning-tertiary hover:bg-feedback-alert-primary/10",
  },
  progress: {
    container: "border-accent/30 bg-accent/[0.06]",
    icon: "text-accent",
    title: "text-default-primary",
    body: "text-default-secondary",
    action: "text-accent hover:text-accent hover:bg-accent/10",
  },
  info: {
    container: "border-accent/20 bg-accent/[0.05]",
    icon: "text-accent",
    title: "text-default-primary",
    body: "text-default-secondary",
    action: "text-accent hover:text-accent hover:bg-accent/10",
  },
  success: {
    container: "border-feedback-success-primary/30 bg-feedback-success-primary/[0.06]",
    icon: "text-feedback-success-primary",
    title: "text-default-primary",
    body: "text-default-secondary",
    action: "text-feedback-success-primary hover:text-feedback-success-primary hover:bg-feedback-success-primary/10",
  },
};

const DEFAULT_ICON: Record<NoticeVariant, LucideIcon> = {
  error: AlertTriangle,
  warning: AlertTriangle,
  progress: Loader2,
  info: Info,
  success: CheckCircle2,
};

/**
 * A single dashboard notice banner. One layout, one size, semantic color per
 * `variant`. Pass `actions` for trailing CTA buttons (use `NoticeAction`),
 * `onClick` to make the copy a navigation target, and `onDismiss` for an X
 * that hides the notice without running `onClick`. Renders its title +
 * optional body. Pass `footer` for content that needs the whole width of the
 * notice (a table, a debug panel): it is drawn below the icon, copy and
 * actions row instead of in the copy column beside the actions.
 */
export function DashboardNotice({
  variant,
  title,
  children,
  actions,
  icon,
  spin,
  onClick,
  onDismiss,
  dismissLabel,
  className,
  footer,
}: {
  variant: NoticeVariant;
  title: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  /** Override the default per-variant icon. */
  icon?: LucideIcon;
  /** Show the library Spinner instead of the icon (something is running). */
  spin?: boolean;
  /** When set, the icon + copy become a button (the whole banner, unless `onDismiss` or `actions` is also set). */
  onClick?: () => void;
  /**
   * Trailing X that hides this notice. Nested `<button>` is invalid HTML, so
   * when `onClick` is set with `onDismiss` or `actions` the click target is
   * the copy only, and the X and the actions sit beside it.
   */
  onDismiss?: () => void;
  /** Accessible name + tooltip for the dismiss control. Defaults to a localized "Dismiss". */
  dismissLabel?: string;
  className?: string;
  /** Full-width content below the notice row (never inside a click target). */
  footer?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const s = VARIANT_STYLES[variant];
  const Icon = icon ?? DEFAULT_ICON[variant];
  const iconEl = spin ? (
    <span className={cn("flex shrink-0 mt-0.5", s.icon)}>
      <Spinner size="sm" tone="inherit" />
    </span>
  ) : (
    <Icon className={cn("h-4 w-4 shrink-0 mt-0.5", s.icon)} />
  );
  const text = (
    <div className="flex-1 min-w-0">
      <p className={cn("content-base font-medium", s.title)}>{title}</p>
      {children != null && (
        <div className={cn("content-compact mt-0.5 leading-relaxed", s.body)}>{children}</div>
      )}
    </div>
  );
  // Icon-only control: never let the accessible name (and the tooltip, which
  // Tip skips for empty content) go missing when a caller omits dismissLabel.
  const effectiveDismissLabel = dismissLabel || t_i18n("Dismiss");
  const dismissBtn = onDismiss ? (
    <Tip content={effectiveDismissLabel}>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label={effectiveDismissLabel}
        className={cn("h-7 w-7", s.action)}
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          onDismiss();
        }}
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </Tip>
  ) : null;
  const trailing = (actions || dismissBtn) ? (
    <div className="flex items-center gap-1.5 shrink-0">
      {actions}
      {dismissBtn}
    </div>
  ) : null;
  const base = "rounded-sm border px-4 py-3.5 flex items-start gap-3";
  // The copy is a `<button>`: a keyboard user sees the Button's ring on it
  // (2 px offset plus 2 px ring-focus), the same as the NoticeAction beside it.
  const COPY_TARGET = cn("flex flex-1 items-start gap-3 min-w-0 text-left rounded-sm", BUTTON_ROLE_FOCUS_RING);

  if (footer != null) {
    // The row keeps its own layout; the footer spans the notice width, so a
    // table in it never shares the copy column with the actions.
    return (
      <div className={cn("rounded-sm border", s.container, className)}>
        <div className="px-4 py-3.5 flex items-start gap-3">
          {onClick ? (
            <button
              type="button"
              onClick={onClick}
              className={COPY_TARGET}
            >
              {iconEl}
              {text}
            </button>
          ) : (
            <>
              {iconEl}
              {text}
            </>
          )}
          {trailing}
        </div>
        <div data-slot="notice-footer" className="px-4 pb-3.5 min-w-0">
          {footer}
        </div>
      </div>
    );
  }

  // A nested interactive control inside a `<button>` is invalid HTML: when the
  // notice has actions or an X, only the icon and copy are the click target.
  if (onClick && (onDismiss || actions)) {
    return (
      <div
        className={cn(base, "w-full transition-colors hover:bg-elevation-hover", s.container, className)}
      >
        <button
          type="button"
          onClick={onClick}
          className={COPY_TARGET}
        >
          {iconEl}
          {text}
        </button>
        {trailing}
      </div>
    );
  }

  const inner = (
    <>
      {iconEl}
      {text}
      {trailing}
    </>
  );
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={cn(base, "w-full text-left transition-colors hover:bg-elevation-hover", BUTTON_ROLE_FOCUS_RING, s.container, className)}
      >
        {inner}
      </button>
    );
  }
  return <div className={cn(base, s.container, className)}>{inner}</div>;
}

/**
 * Consistent CTA button for a `DashboardNotice` (ghost, sized + colored to match
 * the notice variant). Pass the label and any icons as children. Pass `to`
 * for a call to action that opens a page: it renders a route link with the
 * same look. Pass `busy` while the action's request is pending: the control
 * carries aria-busy (design system Spinner usage contract RULE-02).
 */
export function NoticeAction({
  variant,
  onClick,
  to,
  disabled,
  busy,
  children,
}: {
  variant: NoticeVariant;
  onClick?: () => void;
  to?: string;
  disabled?: boolean;
  busy?: boolean;
  children: ReactNode;
}) {
  const ariaBusy = busy ? true : undefined;
  const className = cn("shrink-0 h-7 content-compact gap-1.5", VARIANT_STYLES[variant].action);
  if (to !== undefined && !disabled) {
    return (
      <Button asChild variant="ghost" size="sm" className={className}>
        <Link to={to} aria-busy={ariaBusy}>{children}</Link>
      </Button>
    );
  }
  return (
    <Button type="button" variant="ghost" size="sm" disabled={disabled} aria-busy={ariaBusy} onClick={onClick} className={className}>
      {children}
    </Button>
  );
}
