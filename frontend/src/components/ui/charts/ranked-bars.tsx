/**
 * A ranking drawn as bars with their figures: label, value and share
 * on every row, the bar under it scaled to the largest - read at a glance,
 * no hover needed. The AI Adoption "Top" cards' rows, for any ranking.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";

export interface RankedBarItem {
  key: string;
  label: ReactNode;
  /** Shown on hover of the label, for one the row truncates. */
  title?: string;
  value: number;
  /** A figure beside the value (a count, a unit price). */
  detail?: ReactNode;
  onClick?: () => void;
  /** The page the row opens: a link, keyboard reachable. Wins over `onClick`. */
  href?: string;
  /** A chip after the label (a status: deleted, disabled). */
  badge?: ReactNode;
  /** Marks a row the page's filters already hold. */
  active?: boolean;
}

interface RankedBarsProps {
  items: RankedBarItem[];
  format: (value: number) => string;
  /** What the shares are of; omitted = no share column. */
  total?: number;
  /** The bar colour class (a `bg-*` token). */
  barClassName?: string;
  /** Already translated. */
  emptyLabel: string;
}

export function RankedBars({ items, format, total, barClassName = "bg-accent", emptyLabel }: RankedBarsProps) {
  if (items.length === 0) {
    return <p className="py-8 text-center content-compact text-default-secondary">{emptyLabel}</p>;
  }
  const max = Math.max(...items.map((item) => item.value), 0) || 1;
  return (
    <ol className="space-y-2.5">
      {items.map((item, index) => {
        const share = total && total > 0 ? (item.value / total) * 100 : null;
        const interactive = Boolean(item.href || item.onClick);
        const rowClassName = cn(
          "group block w-full space-y-1 rounded-sm text-left",
          interactive && "cursor-pointer outline-none hover:bg-elevation-hover focus-visible:ring-2 focus-visible:ring-focus",
        );
        const content = (
          <>
              <div className="flex items-center justify-between gap-3 px-0.5">
                <span className="flex min-w-0 items-center gap-2">
                  <span className="w-4 shrink-0 text-right content-caption text-default-disabled tabular-nums">{index + 1}</span>
                  {/* The full label on the truncated element itself: a Tip on a
                      span inside the row link never opened, and left no title. A title
                      never shows on keyboard focus, so a focused row unfolds its label. */}
                  <span
                    title={item.title || undefined}
                    className={cn(
                      "truncate content-compact font-medium",
                      interactive && "group-focus-visible:whitespace-normal group-focus-visible:break-words",
                      item.active && "text-accent",
                    )}
                  >
                    {item.label}
                  </span>
                  {item.badge}
                </span>
                <span className="flex shrink-0 items-baseline gap-2 tabular-nums">
                  {item.detail && <span className="content-caption text-default-secondary">{item.detail}</span>}
                  <span className="content-compact font-semibold text-default-primary">{format(item.value)}</span>
                  {share !== null && (
                    <span className="w-12 text-right content-caption text-default-secondary">
                      {share > 0 && share < 0.1 ? "<0.1%" : `${share.toFixed(1)}%`}
                    </span>
                  )}
                </span>
              </div>
              <div className="ml-6 h-1.5 rounded-full bg-elevation-hover" aria-hidden>
                <div
                  className={cn("h-1.5 rounded-full transition-all duration-700", barClassName)}
                  style={{ width: `${Math.max(1, (item.value / max) * 100)}%` }}
                />
              </div>
          </>
        );
        return (
          <li key={item.key}>
            {item.href ? (
              <Link to={item.href} className={rowClassName}>
                {content}
              </Link>
            ) : item.onClick ? (
              <button type="button" onClick={item.onClick} className={rowClassName}>
                {content}
              </button>
            ) : (
              <div className={rowClassName}>{content}</div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
