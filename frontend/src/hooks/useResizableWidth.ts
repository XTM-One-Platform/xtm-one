// SPDX-License-Identifier: Apache-2.0

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

/**
 * A user-resizable panel width: drag, keyboard, reset, remembered.
 *
 * The one implementation behind every panel a user can widen by dragging its
 * edge (the Presentation Studio rail). It owns the bounds and the
 * interaction; the component owns the markup, the label and the styling.
 *
 * - The user's PREFERENCE is what is stored, the DISPLAYED width is that
 *   preference clamped to the current viewport. A width chosen on a large
 *   screen is honoured again on that screen after a detour through a laptop,
 *   instead of being overwritten by the narrower clamp it got there. With no
 *   preference the default applies, re-read on every render, so a default
 *   that looks at the viewport keeps following it.
 * - Dragging uses pointer capture on the handle, so the pointer keeps driving
 *   the width while it travels over whatever sits next to the panel (a canvas,
 *   an iframe-free stage, plain text). A live drag only updates the state; the
 *   width is written to storage ONCE, when the drag ends (a synchronous
 *   `localStorage` write on every move is drag jank on a slow device).
 *   `Escape` mid-drag puts the width back where the drag started.
 * - The handle is a WAI-ARIA window splitter: `role="separator"`, vertical,
 *   `aria-valuenow/min/max`, focusable. Left / Right nudge it by `step`
 *   (Shift: four steps), Home / End go to the narrowest / widest, a
 *   double-click resets to the default. A pointer press focuses the handle,
 *   so the arrows can continue the adjustment the pointer started.
 *
 * @example
 * ```tsx
 * const rail = useResizableWidth({
 *   defaultWidth: 380,
 *   minWidth: 320,
 *   maxWidth: (viewport) => Math.floor(viewport / 2),
 *   storageKey: "my-rail-width",
 * });
 * <aside className="relative" style={{ width: rail.width }}>
 *   {rail.canResize && (
 *     <div {...rail.handleProps} aria-label="Resize" className="absolute inset-y-0 -right-1 w-2 cursor-col-resize" />
 *   )}
 * </aside>
 * ```
 */
interface ResizableWidthOptions {
  /** Width when nothing is stored (px). A function is re-read whenever the
   *  width renders without a stored preference, so a responsive default (a
   *  `matchMedia` breakpoint) keeps following the viewport like the
   *  responsive class it replaces. */
  defaultWidth: number | (() => number);
  /** Narrowest width the panel stays usable at (px). */
  minWidth: number;
  /** Widest width, in px or derived from the viewport width (px). Never
   *  resolves below `minWidth`. */
  maxWidth: number | ((viewportWidth: number) => number);
  /** `localStorage` key for the preference. Omit for a session-only width. */
  storageKey?: string;
  /** Which edge of the panel carries the handle. `"end"` (default): the handle
   *  is on the RIGHT of a left-hand panel and dragging right widens it.
   *  `"start"`: the handle is on the LEFT of a right-hand panel and dragging
   *  left widens it. Keyboard arrows follow the same direction. */
  edge?: "start" | "end";
  /** What the panel resizes along. `"x"` (default): its width, as above.
   *  `"y"`: its HEIGHT - `"start"` is then the TOP edge of a bottom panel
   *  (dragging up grows it), the bounds and the default are heights read
   *  against the viewport's height, and Up / Down are the arrows. */
  axis?: "x" | "y";
  /** Arrow-key step (px). Shift multiplies it by four. */
  step?: number;
}

interface ResizableWidthHandleProps {
  role: "separator";
  "aria-orientation": "vertical" | "horizontal";
  "aria-valuemin": number;
  "aria-valuemax": number;
  "aria-valuenow": number;
  tabIndex: 0;
  "data-resizing": "true" | undefined;
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerMove: (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerUp: (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerCancel: (event: ReactPointerEvent<HTMLElement>) => void;
  onLostPointerCapture: () => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
  onDoubleClick: () => void;
}

export interface ResizableWidth {
  /** The width to render (px): the preference clamped to the viewport - the
   *  height for a `"y"` panel. */
  width: number;
  /** True while a pointer drag is in progress. */
  isResizing: boolean;
  /** The current bounds (px), for the caller's own display. */
  min: number;
  max: number;
  /** Whether there is room between the bounds on this viewport. When there is
   *  none the caller leaves the handle out: a focusable divider that cannot
   *  move, with a "drag to resize" tip, is a promise the screen cannot keep. */
  canResize: boolean;
  /** Set the preference (clamped and, with a `storageKey`, remembered). A
   *  value that clamps to the rendered width is a no-op, so it never pins the
   *  clamp over a wider preference the viewport is hiding. */
  setWidth: (px: number) => void;
  /** Back to the default width; forgets the stored preference. */
  reset: () => void;
  /** Spread onto the drag handle. Add the `aria-label` and the styling. */
  handleProps: ResizableWidthHandleProps;
}

const DEFAULT_STEP = 16;
const SHIFT_STEP_MULTIPLIER = 4;

interface Drag {
  pointerId: number;
  /** The pointer's coordinate along the axis when the drag started. */
  startAt: number;
  /** The rendered width when the drag started (the arithmetic base). */
  startWidth: number;
  /** The preference when the drag started (`null` = the default), so `Escape`
   *  restores THAT rather than pinning the default as an explicit choice. */
  startPreferred: number | null;
  /** The width the drag has reached, persisted once when it ends. */
  width: number;
  /** Whether the width changed at all: a plain click on the divider is not a
   *  choice and leaves the stored preference as it was. */
  moved: boolean;
  target: HTMLElement;
}

function viewportSize(axis: "x" | "y"): number {
  if (typeof window === "undefined") return Number.POSITIVE_INFINITY;
  return axis === "y" ? window.innerHeight : window.innerWidth;
}

function readStored(key: string | undefined): number | null {
  if (!key || typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

function writeStored(key: string | undefined, value: number | null) {
  if (!key || typeof window === "undefined") return;
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, String(Math.round(value)));
  } catch {
    // Storage full or disabled: the width still applies for this session.
  }
}

export function useResizableWidth({
  defaultWidth,
  minWidth,
  maxWidth,
  storageKey,
  edge = "end",
  axis = "x",
  step = DEFAULT_STEP,
}: ResizableWidthOptions): ResizableWidth {
  const resolveDefault = useCallback(
    () => (typeof defaultWidth === "function" ? defaultWidth() : defaultWidth),
    [defaultWidth],
  );

  // The preference (`null`: none, the default applies), then the viewport it
  // is clamped against.
  const [preferred, setPreferred] = useState<number | null>(() => readStored(storageKey));
  const [viewport, setViewport] = useState<number>(() => viewportSize(axis));

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onResize = () => setViewport(viewportSize(axis));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [axis]);

  const min = Math.max(0, Math.round(minWidth));
  const max = useMemo(() => {
    const resolved = typeof maxWidth === "function" ? maxWidth(viewport) : maxWidth;
    return Math.max(min, Math.round(Number.isFinite(resolved) ? resolved : min));
  }, [maxWidth, min, viewport]);

  const clamp = useCallback((px: number) => Math.min(max, Math.max(min, Math.round(px))), [max, min]);
  // Without a preference the default is resolved HERE, on every render: the
  // `resize` listener above re-renders on a viewport change, so a default
  // keyed on a breakpoint moves with the window exactly as the responsive
  // class it replaced did.
  const width = clamp(preferred ?? resolveDefault());

  // A deliberate choice (a keyboard step, `setWidth`): clamped, kept, stored.
  // A command that lands where the rail already is - `End` at the widest, an
  // arrow pressed against a bound - is not a choice: it must not pin the
  // clamped width over a wider preference the viewport is currently hiding
  // (900px stored, 600px rendered: `ArrowRight` leaves the 900px alone).
  const commit = useCallback(
    (px: number) => {
      const next = clamp(px);
      if (next === width) return;
      setPreferred(next);
      writeStored(storageKey, next);
    },
    [clamp, storageKey, width],
  );

  const reset = useCallback(() => {
    setPreferred(null);
    writeStored(storageKey, null);
  }, [storageKey]);

  /* ---------- Pointer drag (captured on the handle) ---------- */
  const [isResizing, setIsResizing] = useState(false);
  const drag = useRef<Drag | null>(null);
  const direction = edge === "end" ? 1 : -1;

  // `keep`: the pointer was released (or the drag was interrupted) and the
  // width on screen stands - stored once, and only if the drag moved it.
  // Otherwise (`Escape`) the preference from before the drag comes back.
  const stopDrag = useCallback(
    (keep: boolean) => {
      const active = drag.current;
      drag.current = null;
      setIsResizing(false);
      if (typeof document !== "undefined") {
        document.body.style.removeProperty("cursor");
        document.body.style.removeProperty("user-select");
      }
      if (!active) return;
      if (keep) {
        if (active.moved) writeStored(storageKey, active.width);
      } else {
        setPreferred(active.startPreferred);
      }
      if (active.target.hasPointerCapture?.(active.pointerId)) {
        active.target.releasePointerCapture(active.pointerId);
      }
    },
    [storageKey],
  );
  const endDrag = useCallback(() => stopDrag(true), [stopDrag]);
  const cancelDrag = useCallback(() => stopDrag(false), [stopDrag]);
  // The release of the pointer that is dragging - never a second finger's.
  const onPointerEnd = useCallback(
    (event: { pointerId: number }) => {
      if (drag.current && event.pointerId === drag.current.pointerId) endDrag();
    },
    [endDrag],
  );

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      // Primary button (or a touch / pen contact) only: a right-click on the
      // divider is a context menu, not a drag.
      if (event.button !== 0) return;
      const target = event.currentTarget;
      drag.current = {
        pointerId: event.pointerId,
        startAt: axis === "y" ? event.clientY : event.clientX,
        startWidth: width,
        startPreferred: preferred,
        width,
        moved: false,
        target,
      };
      try {
        // Capture keeps the move/up events on the handle however far the
        // pointer strays; a browser that refuses (no active pointer of that
        // id) still gets a working drag while the pointer stays on the handle,
        // and the window listeners below end it wherever it is released.
        target.setPointerCapture?.(event.pointerId);
      } catch {
        // Capture is an optimisation of the drag, never a precondition of it.
      }
      // The handle takes the focus, so the arrow keys can continue the
      // adjustment the pointer started. Explicitly: a mouse press on a
      // `tabindex` element focuses it natively, a touch or a pen not reliably.
      // Deliberately NO `preventDefault()` on this event: the body's
      // `user-select: none` below already stops a selection from starting,
      // and a prevented pointerdown would also skip the tooltip trigger's own
      // close-on-press (Radix skips its handler on a prevented event), which
      // would leave the tip floating beside the divider for the whole drag.
      target.focus({ preventScroll: true });
      setIsResizing(true);
      // The pointer travels over the neighbouring content while it drags: keep
      // the resize cursor and stop it from selecting text on the way.
      document.body.style.setProperty("cursor", axis === "y" ? "row-resize" : "col-resize");
      document.body.style.setProperty("user-select", "none");
    },
    [axis, preferred, width],
  );

  const onPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      const active = drag.current;
      if (!active || event.pointerId !== active.pointerId) return;
      const at = axis === "y" ? event.clientY : event.clientX;
      const next = clamp(active.startWidth + (at - active.startAt) * direction);
      if (next === active.width) return;
      active.width = next;
      active.moved = true;
      // State only: the storage write waits for the end of the drag.
      setPreferred(next);
    },
    [axis, clamp, direction],
  );

  // While a drag is on: `Escape` puts the width back where the drag started;
  // the window catches a release the handle cannot see - the pointer was never
  // captured and is let go over the neighbour - and a drag the window loses
  // altogether (Alt-Tab, a native dialog: no `pointerup` ever comes), so no
  // drag is left running with a resize cursor on the body.
  useEffect(() => {
    if (!isResizing || typeof window === "undefined") return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      cancelDrag();
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerup", onPointerEnd);
    window.addEventListener("pointercancel", onPointerEnd);
    window.addEventListener("blur", endDrag);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerup", onPointerEnd);
      window.removeEventListener("pointercancel", onPointerEnd);
      window.removeEventListener("blur", endDrag);
    };
  }, [cancelDrag, endDrag, isResizing, onPointerEnd]);

  // Unmount mid-drag: never leave the document with a resize cursor.
  useEffect(() => endDrag, [endDrag]);

  /* ---------- Keyboard (WAI-ARIA window splitter) ---------- */
  const onKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>) => {
      const stride = step * (event.shiftKey ? SHIFT_STEP_MULTIPLIER : 1);
      let next: number;
      switch (event.key) {
        case "ArrowRight":
        case "ArrowLeft":
        case "ArrowDown":
        case "ArrowUp": {
          // The arrows along the panel's axis only: Left / Right for a width, Up / Down for a height.
          const vertical = event.key === "ArrowDown" || event.key === "ArrowUp";
          if (vertical !== (axis === "y")) return;
          const forward = event.key === "ArrowRight" || event.key === "ArrowDown";
          next = width + (forward ? stride : -stride) * direction;
          break;
        }
        case "Home":
          next = min;
          break;
        case "End":
          next = max;
          break;
        default:
          return;
      }
      event.preventDefault();
      commit(next);
    },
    [axis, commit, direction, max, min, step, width],
  );

  const handleProps = useMemo<ResizableWidthHandleProps>(
    () => ({
      role: "separator",
      // A splitter between side-by-side panels is vertical; between stacked ones, horizontal.
      "aria-orientation": axis === "y" ? "horizontal" : "vertical",
      "aria-valuemin": min,
      "aria-valuemax": max,
      "aria-valuenow": width,
      tabIndex: 0,
      "data-resizing": isResizing ? "true" : undefined,
      onPointerDown,
      onPointerMove,
      onPointerUp: onPointerEnd,
      onPointerCancel: onPointerEnd,
      onLostPointerCapture: endDrag,
      onKeyDown,
      onDoubleClick: reset,
    }),
    [axis, endDrag, isResizing, max, min, onKeyDown, onPointerDown, onPointerEnd, onPointerMove, reset, width],
  );

  return { width, isResizing, min, max, canResize: max > min, setWidth: commit, reset, handleProps };
}
