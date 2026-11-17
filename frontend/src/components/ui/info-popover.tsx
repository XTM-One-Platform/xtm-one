// SPDX-License-Identifier: Apache-2.0

/**
 * The one info popover of the app (#9836 R4b, UX round 1 B6): an "i" button
 * and the small dialog it opens, explaining a figure where it is shown.
 * `MetricInfo` (CTEM metrics) and `RoiHowComputed` (ROI figures) are built on
 * it; a new "how is this computed" bubble is built on it too, never on a
 * second portal (the gate in info-popover.test.tsx fails on one).
 *
 * What it guarantees, once for every caller:
 * - portalled to <body> with fixed positioning, so a card's overflow never
 *   clips it; placed under its trigger, flipped above when it would pass the
 *   bottom of the viewport, its height capped to the viewport (B5);
 * - closed on Escape wherever the focus is (trigger, popover, page) (B2), on a
 *   click outside, on a page scroll or a resize, so it never drifts away from
 *   its trigger (B4), and when the focus leaves both trigger and popover;
 * - opened by keyboard, the focus moves into the dialog, so its content is
 *   read and its links reached with Tab (B3); Escape gives it back;
 * - the click and the Enter / Space of the trigger stop there, so a clickable
 *   card or row it sits in never opens.
 */
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type FocusEvent, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { placeInfoPopover, type InfoPopoverPlacement } from "@/lib/info-popover-placement";
import { cn } from "@/lib/utils";

function viewportSize(): { width: number; height: number } {
  const width = typeof globalThis.innerWidth === "number" && globalThis.innerWidth > 0 ? globalThis.innerWidth : 1024;
  const height = typeof globalThis.innerHeight === "number" && globalThis.innerHeight > 0 ? globalThis.innerHeight : 768;
  return { width, height };
}

export interface InfoPopoverProps {
  /** The trigger's accessible name (and its tooltip, through `title`). */
  label: string;
  /** The trigger's icon. */
  icon: ReactNode;
  /** The popover's content. */
  children: ReactNode;
  /** The popover's heading, used as its accessible name. */
  heading: ReactNode;
  /** Shown on the heading's line, at the end (a window, a source badge). */
  headingAside?: ReactNode;
  /** The popover's width in px (kept inside the viewport). */
  width: number;
  /** Which edge of the trigger the popover lines up with. */
  align?: "start" | "end";
  /** Hovering (or focusing) the trigger opens it too, as the CTEM metric bubbles do. */
  hoverOpens?: boolean;
  triggerClassName?: string;
  headingClassName?: string;
  popoverClassName?: string;
  triggerTestId?: string;
  popoverTestId?: string;
}

export function InfoPopover({
  label,
  icon,
  children,
  heading,
  headingAside,
  width,
  align = "start",
  hoverOpens = false,
  triggerClassName,
  headingClassName,
  popoverClassName,
  triggerTestId,
  popoverTestId,
}: InfoPopoverProps) {
  const [open, setOpen] = useState(false);
  const [place, setPlace] = useState<InfoPopoverPlacement | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const focusOnOpen = useRef(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const headingId = useId();

  const cancelClose = useCallback(() => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = null;
  }, []);
  const openNow = useCallback(() => {
    cancelClose();
    setOpen(true);
  }, [cancelClose]);
  const closeNow = useCallback(
    (refocus: boolean) => {
      cancelClose();
      setOpen(false);
      if (refocus) btnRef.current?.focus();
    },
    [cancelClose],
  );
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = setTimeout(() => setOpen(false), 120);
  }, [cancelClose]);
  useEffect(() => cancelClose, [cancelClose]);

  // Placed after the popover is in the DOM, so its height decides the flip.
  useLayoutEffect(() => {
    if (!open) {
      setPlace(null);
      return;
    }
    const btn = btnRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    setPlace(placeInfoPopover(r, popRef.current?.scrollHeight ?? 0, width, viewportSize(), align));
  }, [open, width, align]);

  useEffect(() => {
    if (open && place && focusOnOpen.current) {
      focusOnOpen.current = false;
      popRef.current?.focus();
    }
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const inside = (target: EventTarget | null) =>
      target instanceof Node && (btnRef.current?.contains(target) || popRef.current?.contains(target));
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeNow(inside(document.activeElement) ?? false);
    };
    const onPointer = (e: PointerEvent | MouseEvent) => {
      if (!inside(e.target)) closeNow(false);
    };
    // A scroll of the page moves the trigger: close rather than drift (B4). The popover's own scroll does not.
    const onScroll = (e: Event) => {
      if (!inside(e.target)) closeNow(false);
    };
    const onResize = () => closeNow(false);
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
    };
  }, [open, closeNow]);

  /** The focus left the trigger or the popover: close when it went to neither. */
  const onBlur = (e: FocusEvent) => {
    const next = e.relatedTarget;
    if (next instanceof Node && (btnRef.current?.contains(next) || popRef.current?.contains(next))) return;
    if (hoverOpens) scheduleClose();
    else if (next !== null) closeNow(false);
  };
  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key === "Escape" && open) {
      e.stopPropagation();
      e.preventDefault();
      closeNow(true);
    } else if (e.key === "Enter" || e.key === " ") {
      // The trigger's own activation; never the card's or the row's.
      e.stopPropagation();
    }
  };

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-label={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        data-testid={triggerTestId}
        onMouseEnter={hoverOpens ? openNow : undefined}
        onMouseLeave={hoverOpens ? scheduleClose : undefined}
        onFocus={hoverOpens ? openNow : undefined}
        onBlur={onBlur}
        onKeyDown={onKeyDown}
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          // detail 0: activated by the keyboard, so the focus follows into the dialog (B3).
          if (e.detail === 0) focusOnOpen.current = true;
          if (open && !hoverOpens) closeNow(false);
          else openNow();
        }}
        className={triggerClassName}
      >
        {icon}
      </button>
      {open &&
        createPortal(
          <div
            ref={popRef}
            role="dialog"
            aria-labelledby={headingId}
            tabIndex={-1}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={onKeyDown}
            onBlur={onBlur}
            onFocus={hoverOpens ? cancelClose : undefined}
            onMouseEnter={hoverOpens ? openNow : undefined}
            onMouseLeave={hoverOpens ? scheduleClose : undefined}
            style={{
              position: "fixed",
              top: place?.top ?? 0,
              left: place?.left ?? 0,
              width: place?.width ?? width,
              maxHeight: place?.maxHeight,
              visibility: place ? undefined : "hidden",
            }}
            className={cn(
              "z-[60] overflow-y-auto rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default shadow-overlay animate-fade-in focus-visible:outline-hidden",
              popoverClassName,
            )}
            data-testid={popoverTestId}
          >
            <div className="mb-2 flex items-baseline justify-between gap-3">
              <p id={headingId} className={cn("font-semibold", headingClassName)}>
                {heading}
              </p>
              {headingAside}
            </div>
            {children}
          </div>,
          document.body,
        )}
    </>
  );
}
