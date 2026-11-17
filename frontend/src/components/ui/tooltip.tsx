// SPDX-License-Identifier: Apache-2.0

import * as React from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@filigran/design-system";
import { capTipText } from "@/lib/tip-text";
import { cn } from "@/lib/utils";

/**
 * Tooltip — the design system's primitives, plus this app's `Tip` convenience.
 *
 * The four primitives are re-exported unchanged: the library wraps the same
 * `@radix-ui/react-tooltip` this file used to wrap, so trigger/content/provider
 * behaviour is identical and the 98 importing files need no change. What the
 * library additionally owns now is the content surface — its padding, its arrow
 * and its `bg-tooltip` token — which this file used to style by hand.
 *
 * `Tip` stays local. It is not a component the library is missing; it is a
 * calling convention (one `content` prop instead of a trigger/content pair, plus
 * the wrapper needed to make a disabled control hoverable) that exists because
 * this app replaces native `title=` with it hundreds of times.
 *
 * THE FULLSCREEN ESCAPE HATCH, and why it exists.
 * `FullscreenTooltipContent` below is the library's content surface re-created
 * on Radix, for one reason: FDS's `TooltipContent` portals to `document.body`
 * internally and takes no `container`. While an element is fullscreen, only it
 * and its descendants paint — so a body-portaled tooltip is simply never seen,
 * and the presentation deck (which is a fullscreen surface) would lose every
 * tooltip on its toolbar.
 *
 * It is used ONLY where a `container` is passed, which today is the deck and
 * the Video Studio player (`CinemaPlayer`, also shown in a theater above every
 * body portal); every other call site gets the library's own component. Filed
 * upstream as F11 — `TooltipPrimitive.Portal` is not exported and the internal
 * portal has no seam — and this whole block should be deleted the moment the
 * library accepts a container.
 */

/**
 * Space kept between a tooltip and the viewport edge: Radix shifts the
 * content inward by this much instead of letting it sit flush on the edge.
 */
const TIP_COLLISION_PADDING = 8;

/** The library's tooltip, re-created on Radix so it can portal somewhere else. */
const FullscreenTooltipContent = React.forwardRef<
  React.ComponentRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content> & {
    container?: HTMLElement | null;
  }
>(({ className, sideOffset = 8, collisionPadding = TIP_COLLISION_PADDING, container, ...props }, ref) => (
  <TooltipPrimitive.Portal container={container ?? undefined}>
    <TooltipPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      collisionPadding={collisionPadding}
      className={cn(
        "box-border z-[var(--fds-z-overlay,50)] max-w-75 rounded-sm bg-tooltip px-2 py-1",
        "content-compact leading-relaxed text-default-primary shadow-overlay",
        "animate-in fade-in-0 zoom-in-95",
        "data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95",
        "data-[side=bottom]:slide-in-from-top-1 data-[side=left]:slide-in-from-right-1",
        "data-[side=right]:slide-in-from-left-1 data-[side=top]:slide-in-from-bottom-1",
        className,
      )}
      {...props}
    />
  </TooltipPrimitive.Portal>
));
FullscreenTooltipContent.displayName = "FullscreenTooltipContent";

/* ── Tip — high-level wrapper (replaces native title=) ──────── */

interface TipProps {
  /** Tooltip content (string or rich ReactNode). If empty/undefined, children are rendered without a tooltip. */
  content?: React.ReactNode;
  /** Radix side: "top" | "bottom" | "left" | "right" */
  side?: "top" | "bottom" | "left" | "right";
  /** Radix align: "start" | "center" | "end" */
  align?: "start" | "center" | "end";
  /** Delay before showing in ms (default 300) */
  delayDuration?: number;
  /**
   * When true, the children are wrapped in an extra ``<span tabIndex={0}
   * className="inline-flex" aria-disabled="true">`` that becomes the
   * tooltip trigger. Use this whenever the immediate child is (or can
   * become) a natively-disabled element — ``<button disabled>``,
   * a disabled text field, or a ``<Select disabled>`` whose ``SelectTrigger``
   * renders ``<button disabled>`` underneath. A native disabled control
   * swallows pointer/focus events, so Radix Tooltip never fires on hover;
   * the wrapper restores hoverability (via the span's own bounding box)
   * and keyboard accessibility (via ``tabIndex={0}``). ``aria-disabled``
   * on the wrapper communicates the disabled state to screen readers,
   * which would otherwise just announce the span as an unlabelled focus
   * stop — defaulting it to ``true`` is safe because ``wrap`` is
   * documented as "only for disabled (or can-become-disabled) children".
   *
   * Default ``false`` so existing callers — most of which wrap icons,
   * badges, or always-enabled buttons — are not affected by an extra DOM
   * node that would change layout / focus order / accessibility tree.
   *
   * ``"auto"`` is for a control that is enabled most of the time and disabled
   * now and then (a button busy while its request runs): the wrapper is always
   * rendered, so the control is never re-created and keeps its focus, but it is
   * a tab stop marked ``aria-disabled`` only while the child's own ``disabled``
   * prop is set. The state is read from the child, never repeated by the caller.
   */
  wrap?: boolean | "auto";
  /**
   * Override the wrapper ``<span>`` class when ``wrap`` is true. Defaults
   * to ``"inline-flex"`` which keeps the trigger inline and the same
   * intrinsic width as a native button. Pass ``"inline-flex w-full"`` (or
   * a different display value entirely) when the wrapped element relies on
   * a stretching parent for its layout — e.g. a full-width ``<Select>``.
   * Layout only: the wrapper's keyboard focus ring is drawn by ``Tip``
   * itself (``TIP_WRAP_FOCUS_RING``), never passed here.
   */
  wrapClassName?: string;
  /**
   * ``tabIndex`` of the wrapper ``<span>`` when ``wrap`` is true (default
   * ``0``, a keyboard-reachable trigger). Pass ``-1`` when the wrapped
   * control sits inside a composite widget that owns a single tab stop (a
   * radio group) and the reason it is disabled reaches assistive tech another
   * way, e.g. an ``aria-describedby`` on the control itself.
   */
  wrapTabIndex?: number;
  /** Portal target for the tooltip (see `TooltipContent`); needed inside a
   *  fullscreen element. */
  container?: HTMLElement | null;
  /** The element to wrap */
  children: React.ReactNode;
}

/**
 * Keyboard focus ring of the focusable ``wrap`` span: the same 2 px offset plus
 * 2 px ``ring-focus`` ring the design-system Button draws, so Tab moving from an
 * enabled button to a disabled one keeps one ring style. Owned here so no call
 * site writes its own (gate: tip-wrap-focus-ring.test.ts).
 */
export const TIP_WRAP_FOCUS_RING =
  "rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 focus-visible:ring-offset-focus";

/**
 * Drop-in replacement for native `title` attributes.
 *
 * ```tsx
 * // Before:
 * <Button title="Delete">🗑️</Button>
 *
 * // After:
 * <Tip content="Delete"><Button>🗑️</Button></Tip>
 *
 * // Disabled control — pass `wrap` so the tooltip still fires:
 * <Tip content="Activate a license to enable" wrap>
 *   <Button disabled>Save</Button>
 * </Tip>
 * ```
 */
function Tip({
  content,
  side = "top",
  align = "center",
  delayDuration = 300,
  wrap = false,
  wrapClassName = "inline-flex",
  wrapTabIndex = 0,
  container,
  children,
}: TipProps) {
  const [open, setOpen] = React.useState(false);
  const pointerDown = React.useRef(false);
  const focused = React.useRef(false);
  if (!content) return <>{children}</>;
  // A long string is cut at a word, so the tooltip stays in the viewport.
  const body = typeof content === "string" ? capTipText(content) : content;

  const childDisabled =
    React.isValidElement<{ disabled?: boolean }>(children) && !!children.props.disabled;
  const trigger =
    wrap === "auto" ? (
      <span
        tabIndex={childDisabled ? wrapTabIndex : undefined}
        aria-disabled={childDisabled ? "true" : undefined}
        className={cn(TIP_WRAP_FOCUS_RING, wrapClassName)}
      >
        {children}
      </span>
    ) : wrap ? (
      <span tabIndex={wrapTabIndex} aria-disabled="true" className={cn(TIP_WRAP_FOCUS_RING, wrapClassName)}>
        {children}
      </span>
    ) : (
      children
    );

  // Focusing a trigger below the fold scrolls its list, and Radix closes a
  // tooltip whose trigger's ancestor scrolls - so a focus opens it one frame
  // later, after that scroll event. A pointer press focuses without opening,
  // as in Radix.
  const openOnFocus = (event: React.FocusEvent<HTMLElement>) => {
    event.preventDefault();
    if (pointerDown.current) return;
    focused.current = true;
    requestAnimationFrame(() => {
      if (focused.current) setOpen(true);
    });
  };
  const notePointerDown = () => {
    pointerDown.current = true;
    document.addEventListener("pointerup", () => (pointerDown.current = false), { once: true });
  };

  return (
    <TooltipProvider delayDuration={delayDuration}>
      <Tooltip open={open} onOpenChange={setOpen}>
        <TooltipTrigger
          asChild
          onFocus={openOnFocus}
          onBlur={() => (focused.current = false)}
          onPointerDown={notePointerDown}
        >
          {trigger}
        </TooltipTrigger>
        {container ? (
          <FullscreenTooltipContent side={side} align={align} container={container} collisionPadding={TIP_COLLISION_PADDING}>
            {body}
          </FullscreenTooltipContent>
        ) : (
          <TooltipContent side={side} align={align} collisionPadding={TIP_COLLISION_PADDING}>
            {body}
          </TooltipContent>
        )}
      </Tooltip>
    </TooltipProvider>
  );
}

export { TooltipProvider, Tooltip, TooltipTrigger, TooltipContent, Tip };
