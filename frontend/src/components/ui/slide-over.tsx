// SPDX-License-Identifier: Apache-2.0

/**
 * A panel sliding in from the right over the page, for what accompanies a
 * conversation (its files, its workspace's knowledge): the dimmed overlay that
 * closes it, the panel, and Escape. The caller renders the header and body.
 *
 * Escape closes it unless a dialog opened from the panel (a confirm, a picker)
 * is still open: that one takes its own Escape.
 *
 * It is a modal dialog for the keyboard too: focus moves into the panel when it
 * opens, Tab and Shift+Tab stay inside it, and focus returns to the element that
 * opened it (a table row, a button) when it closes (UX round 1 on #1461, B2).
 */
import { useEffect, useRef, type ReactNode } from "react";

import { cn } from "@/lib/utils";

export function SlideOver({
  open,
  onClose,
  label,
  width = "w-[420px]",
  children,
}: {
  open: boolean;
  onClose: () => void;
  /** The panel's accessible name. */
  label: string;
  /** A Tailwind width class. */
  width?: string;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  // Callers pass an inline onClose: read it through a ref so a re-render never moves the focus again.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    if (panel && !panel.contains(document.activeElement)) (focusablesIn(panel)[0] ?? panel).focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !document.querySelector('[role="dialog"][data-state="open"]')) onCloseRef.current();
      // A dialog opened from the panel (a confirm, a picker) keeps its own Tab.
      if (e.key !== "Tab" || !panel || document.querySelector('[role="dialog"][data-state="open"]')) return;
      const items = focusablesIn(panel);
      if (items.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (!panel.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (opener?.isConnected) opener.focus();
    };
  }, [open]);

  if (!open) return null;
  // `m-0`: a caller's stack spacing (`space-y-*` sets a block margin on its
  // children) must not shorten a fixed overlay.
  return (
    <div className="fixed inset-0 z-50 m-0 flex justify-end">
      {/* A dimming scrim, never the opaque page surface (`bg-elevation-default`), which hid the page behind the panel. */}
      <div className="absolute inset-0 bg-scrim backdrop-blur-xs animate-in fade-in-0" onClick={onClose} />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        className={cn(
          "relative flex h-full max-w-full flex-col border-l border-elevation-subtle layer-1 bg-elevation-default",
          "shadow-overlay animate-in slide-in-from-right-4 duration-200",
          "outline-hidden",
          width,
        )}
      >
        {children}
      </div>
    </div>
  );
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** The elements of the panel Tab reaches, in document order. */
function focusablesIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => !el.closest("[hidden], [inert]"));
}
