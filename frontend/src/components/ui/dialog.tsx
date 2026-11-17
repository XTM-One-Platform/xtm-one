// SPDX-License-Identifier: Apache-2.0

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent as FdsDialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
  DialogTrigger,
  Icon,
} from "@filigran/design-system";
import { InModalDialogContext } from "@/components/ui/modal-dialog-context";
import { cn } from "@/lib/utils";

/**
 * Dialog — the design system's dialog, with this app's export surface.
 *
 * `Dialog`, `DialogTrigger`, `DialogClose`, `DialogTitle` and
 * `DialogDescription` are the library's, unchanged. What that takes over is
 * everything this file used to hand-build: the portal, the scrim and its blur,
 * the centring, the enter/exit animation, the close button and the title's
 * typography.
 *
 * Three things stay local, each for a different reason:
 *
 *  - `DialogContent` is wrapped only to set a default `size`. The library sizes
 *    a dialog with `size="sm" | "md" | "lg"` (420/640/960px fixed) where this app
 *    caps it with a `max-w-*` class at 49 of its 53 call sites. `lg` is the
 *    default here because it is the only size wide enough to let every existing
 *    cap still decide: with `md`, the sixteen `max-w-2xl`/`max-w-3xl` dialogs
 *    would silently narrow to 640px. The five call sites that pass no cap get
 *    960px rather than the old 576px.
 *  - `DialogHeader` has no counterpart. The library offers `DialogBody` and
 *    `DialogFooter` (re-exported here) but no header block, so this stays a
 *    layout `<div>` — 152 call sites use it. Filed as a gap; a two-class flex
 *    column is not a component fork.
 *
 * HEIGHT. The library panel is a flex column capped at the viewport
 * (`max-h-screen overflow-hidden`): only a `DialogBody` child scrolls, so a
 * form taller than the screen that does not use one is clipped, footer and
 * all, with nothing to scroll. Two things follow:
 *
 *  - a long form wraps its fields in `DialogBody` and its actions in
 *    `DialogFooter`: the header and the actions stay in view, the fields
 *    scroll (the secret vault dialog does);
 *  - every other dialog gets a safety net below: the panel keeps a margin to
 *    the viewport edge and scrolls as a whole instead of clipping. A dialog
 *    with a `DialogBody` never reaches it — the body shrinks first.
 *  - `DialogPortal` and `DialogOverlay` are re-exported from Radix directly.
 *    The library does not expose them, and `CommandPalette` composes its own
 *    overlay from them — a ⌘K palette is not a dialog, and the library has no
 *    component for one.
 */

const DialogPortal = DialogPrimitive.Portal;

const DialogOverlay = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-50 bg-scrim backdrop-blur-xs data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
      className,
    )}
    {...props}
  />
));
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName;

/**
 * The safety net of the HEIGHT note above, merged after the library's own
 * classes: a 2rem margin to the viewport (dynamic viewport units, so a mobile
 * toolbar does not hide the footer), and vertical scrolling of the whole
 * panel in place of the library's `overflow-hidden` clip. `overflow-x` stays
 * hidden as before.
 */
const VIEWPORT_BOUND = "max-h-[calc(100dvh-2rem)] overflow-x-hidden overflow-y-auto";

/**
 * THE FULLSCREEN ESCAPE HATCH.
 *
 * A dialog opened from inside a fullscreen element must portal INTO that
 * element: while it is fullscreen nothing outside it paints, so a
 * body-portaled dialog opens invisibly and traps focus off-screen. The
 * library's `DialogContent` portals to `document.body` internally and exposes
 * no container, and `DialogPortal`/`DialogOverlay` are not part of its export
 * surface either — F11, filed before this was a blocker and now one.
 *
 * So this is the app's own content surface on Radix, reached ONLY when a
 * `container` is passed. Today that is the presentation deck and nothing else;
 * the other 188 dialog call sites get the library's component untouched. Delete
 * this the moment the library accepts a portal container.
 */
const FullscreenDialogContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
    container?: HTMLElement | null;
  }
>(({ className, children, container, ...props }, ref) => (
  <DialogPortal container={container ?? undefined}>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        "fixed left-[50%] top-[50%] z-50 grid max-h-[85vh] w-full max-w-xl translate-x-[-50%] translate-y-[-50%] gap-4 overflow-y-auto rounded-sm layer-1 bg-elevation-default p-6 shadow-overlay duration-200 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
        // The content is a single-column grid, and a grid item's automatic
        // minimum width is its min-content size: one unbreakable run (a URL, an
        // id, a token) wider than the dialog widens the column past the box and
        // the dialog grows a horizontal scrollbar, clipping its own right edge —
        // no `truncate` below can help, since the item was never asked to
        // shrink. Letting every item shrink to zero keeps the column at the
        // dialog's width, so overflow handling inside it actually applies.
        "[&>*]:min-w-0",
        className,
      )}
      {...props}
    >
      {children}
      <DialogPrimitive.Close
        aria-label="Close"
        className="absolute right-4 top-4 rounded-sm text-default-secondary opacity-70 transition-opacity hover:opacity-100 hover:text-default-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 focus-visible:ring-offset-focus disabled:pointer-events-none"
      >
        <Icon name="x" size={16} />
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPortal>
));
FullscreenDialogContent.displayName = "FullscreenDialogContent";

/**
 * FOCUS RETURN FOR A CONTROLLED DIALOG.
 *
 * Radix returns focus on close to its `DialogTrigger` only: its modal content
 * cancels the focus scope's own return and focuses `triggerRef`, which is null
 * when the dialog is opened through its `open` prop from a plain button (a
 * list row's action, a menu). Focus then falls to `document.body`.
 *
 * So the content records the element that had focus when it opened and, on
 * close, focuses it again while it is still in the document. A call site that
 * handles `onCloseAutoFocus` itself (calls `preventDefault()`) keeps the last
 * word. An opener that is gone (a menu item, a row's button that unmounted
 * once its action was confirmed) gives way to `returnFocusFallback` when the
 * call site names one (the row's actions button).
 *
 * Without one, a dialog opened from a menu row returns focus
 * to the menu's trigger: the row is the focused element when the dialog
 * opens and is unmounted with its menu, but the menu names its trigger
 * (`aria-labelledby` on the `role="menu"` content), so the trigger is
 * recorded on open, as Escape on the menu itself would return to it. Else
 * Radix's own behaviour is left untouched.
 */
function menuTriggerOf(element: HTMLElement): HTMLElement | null {
  const menu = element.closest('[role="menu"]');
  const triggerId = menu?.getAttribute("aria-labelledby");
  if (!triggerId) return null;
  const trigger = document.getElementById(triggerId);
  return trigger instanceof HTMLElement ? trigger : null;
}

function useReturnFocus(
  onOpenAutoFocus: ((event: Event) => void) | undefined,
  onCloseAutoFocus: ((event: Event) => void) | undefined,
  returnFocusFallback: (() => HTMLElement | null | undefined) | undefined,
) {
  const opener = React.useRef<HTMLElement | null>(null);
  const fallback = React.useRef<HTMLElement | null>(null);
  return {
    onOpenAutoFocus: (event: Event) => {
      const active = typeof document === "undefined" ? null : document.activeElement;
      opener.current = active instanceof HTMLElement && active !== document.body ? active : null;
      fallback.current = opener.current ? menuTriggerOf(opener.current) : null;
      onOpenAutoFocus?.(event);
    },
    onCloseAutoFocus: (event: Event) => {
      onCloseAutoFocus?.(event);
      const target = opener.current;
      const menuTrigger = fallback.current;
      opener.current = null;
      fallback.current = null;
      if (event.defaultPrevented) return;
      const next = [target, returnFocusFallback?.(), menuTrigger].find((element) => element?.isConnected);
      if (!next) return;
      event.preventDefault();
      next.focus();
    },
  };
}

const DialogContent = React.forwardRef<
  React.ComponentRef<typeof FdsDialogContent>,
  React.ComponentPropsWithoutRef<typeof FdsDialogContent> & {
    /**
     * Portal target. Only honoured by the fullscreen path below — the library's
     * own dialog portals to `document.body` internally and takes no container,
     * so passing this selects a Radix-composed content instead. See the note
     * above `FullscreenDialogContent`.
     */
    container?: HTMLElement | null;
    /**
     * Where focus goes on close when the element that opened the dialog is no
     * longer in the document. Read at close time, so it can look up a
     * list row the confirmed action re-rendered.
     */
    returnFocusFallback?: () => HTMLElement | null | undefined;
  }
>(({ size = "lg", container, className, children, onOpenAutoFocus, onCloseAutoFocus, returnFocusFallback, ...props }, ref) => {
  const focus = useReturnFocus(onOpenAutoFocus, onCloseAutoFocus, returnFocusFallback);
  const content = <InModalDialogContext.Provider value>{children}</InModalDialogContext.Provider>;
  return container ? (
    <FullscreenDialogContent ref={ref} container={container} className={className} {...props} {...focus}>
      {content}
    </FullscreenDialogContent>
  ) : (
    <FdsDialogContent ref={ref} size={size} className={cn(VIEWPORT_BOUND, className)} {...props} {...focus}>
      {content}
    </FdsDialogContent>
  );
});
DialogContent.displayName = "DialogContent";

const DialogHeader = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("flex flex-col space-y-1.5 text-center sm:text-left", className)} {...props} />
);
DialogHeader.displayName = "DialogHeader";

export {
  Dialog,
  DialogPortal,
  DialogOverlay,
  DialogClose,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogBody,
  DialogFooter,
  DialogTitle,
  DialogDescription,
};
