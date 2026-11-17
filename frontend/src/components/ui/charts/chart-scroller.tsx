/**
 * The horizontal scroller of a chart wider than its card: a long
 * window writes a figure over every bar, so the plot can need several times the
 * card's width. The Y axis is drawn outside the scroller, pinned at the left,
 * so the scale stays readable at any scroll position; the scroller is a named,
 * keyboard-reachable region; and it opens on its right end (the latest data),
 * again each time `resetKey` changes (a window, bucket, metric or view change).
 *
 * The scroller comes first in the DOM and the axis is moved before it with
 * `order-first`: the first chart surface of the card stays the plot itself.
 *
 * `onPlotWidth` reports the plot box's width (the card's room, or the floor when the
 * plot scrolls) on mount and on every resize, so the figures over the bars are laid
 * out from the room each bucket really has.
 */
import { useEffect, useLayoutEffect, useRef, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/** The width of the pinned Y axis itself, in px (`<YAxis width={...}>` of the axis chart). */
export const PINNED_AXIS_WIDTH = 56;
/**
 * The X axis padding of a line or area chart in the scroller: with the Y axis
 * pinned outside, the plot has no left margin, so the first and last points sit on the
 * scroller's edges and the figure centred over them would be cut. About half the widest
 * figure; give it to the plot's XAxis AND the pinned axis chart's XAxis so their domains match.
 * A bar chart needs none: its category band already offsets the first bar.
 */
export const PLOT_X_PADDING = 20;
/** The axis chart's own plot: a sliver a few px wide, so Recharts lays it out; nothing in it is visible. */
const AXIS_PLOT_WIDTH = 4;

interface ChartScrollerProps {
  /** The region's accessible name, already translated. */
  label: string;
  /** A change of this value brings the scroller back to its right end. */
  resetKey: string;
  /** The plot's floor width (0: it fits the card). */
  minWidth: number;
  /** The plot's height class, e.g. `h-[300px]`; the axis gets the same. */
  heightClassName: string;
  /** The pinned Y axis: a chart of the same height, margins and domain that draws only its axis. */
  axis: ReactNode;
  children: ReactNode;
  testId?: string;
  /** Called with the plot box's width in px whenever it changes. */
  onPlotWidth?: (width: number) => void;
}

export function ChartScroller({ label, resetKey, minWidth, heightClassName, axis, children, testId, onPlotWidth }: ChartScrollerProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const plot = useRef<HTMLDivElement>(null);
  const report = useRef(onPlotWidth);

  useEffect(() => {
    report.current = onPlotWidth;
  }, [onPlotWidth]);

  useLayoutEffect(() => {
    const node = scroller.current;
    if (node) node.scrollLeft = node.scrollWidth;
  }, [resetKey]);

  useEffect(() => {
    const node = plot.current;
    if (!node) return;
    const send = (width: number) => {
      if (width > 0) report.current?.(Math.round(width));
    };
    send(node.getBoundingClientRect().width);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => send(entries[0]?.contentRect.width ?? 0));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="flex min-w-0">
      <div
        ref={scroller}
        role="region"
        aria-label={label}
        tabIndex={0}
        data-testid={testId}
        className="min-w-0 flex-1 overflow-x-auto overflow-y-hidden rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        {/* The tooltip stays inside the chart's own box, so hovering never widens the scroller. */}
        <div ref={plot} className={cn(heightClassName, "overflow-hidden")} style={{ minWidth: minWidth || undefined }}>
          {children}
        </div>
      </div>
      <div
        className={cn(heightClassName, "order-first shrink-0")}
        style={{ width: PINNED_AXIS_WIDTH + AXIS_PLOT_WIDTH }}
        aria-hidden
        data-testid={testId ? `${testId}-axis` : undefined}
      >
        {axis}
      </div>
    </div>
  );
}
