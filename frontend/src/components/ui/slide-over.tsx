// SPDX-License-Identifier: Apache-2.0

/**
 * A panel sliding in from the right over the page, for what accompanies a
 * conversation (its files, its workspace's knowledge): the dimmed overlay that
 * closes it, the panel, and Escape. The caller renders the header and body.
 *
 * Escape closes it unless a dialog opened from the panel (a confirm, a picker)
 * is still open: that one takes its own Escape.
 */
import { useEffect, type ReactNode } from "react";

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
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !document.querySelector('[role="dialog"][data-state="open"]')) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  // `m-0`: a caller's stack spacing (`space-y-*` sets a block margin on its
  // children) must not shorten a fixed overlay.
  return (
    <div className="fixed inset-0 z-50 m-0 flex justify-end">
      {/* A dimming scrim, never the opaque page surface (`bg-elevation-default`), which hid the page behind the panel. */}
      <div className="absolute inset-0 bg-scrim backdrop-blur-xs animate-in fade-in-0" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={label}
        className={cn(
          "relative flex h-full max-w-full flex-col border-l border-elevation-subtle layer-1 bg-elevation-default",
          "shadow-overlay animate-in slide-in-from-right-4 duration-200",
          width,
        )}
      >
        {children}
      </div>
    </div>
  );
}
