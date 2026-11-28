// SPDX-License-Identifier: Apache-2.0

/**
 * Toast notifications, rendered by the design system's `Snackbar`.
 *
 * Usage:
 *   import { useToast } from "@/components/ui/toast";
 *
 *   // In a component:
 *   const { toast } = useToast();
 *   toast({ title: "Saved", description: "Agent updated", variant: "success" });
 *
 * `ToastProvider` is mounted once, in `main.tsx`.
 */

import { useState, useCallback, createContext, useContext, useRef, useEffect } from "react";
import { Snackbar, SnackbarProvider, SnackbarViewport, type SnackbarSeverity } from "@filigran/design-system";
import { useFormatter } from "@/i18n";
import { Button } from "@/components/ui/button";
import { registerGlobalToast, unregisterGlobalToast } from "@/lib/global-toast";
import { forcedRemovalDelay } from "./toast-timing";
import { showToast } from "./toast-list";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ToastVariant = "default" | "success" | "error" | "info" | "warning";

/** One button that also dismisses the toast; `label` doubles as its
 *  screen-reader alternative, so it must say what clicking does. */
export interface ToastAction {
  label: string;
  onClick: () => void;
}

interface ToastItem {
  id: string;
  title: string;
  description?: string;
  variant?: ToastVariant;
  durationMs?: number;
  action?: ToastAction;
  /** A toast shown with the tag of one still on screen replaces it. */
  tag?: string;
}

interface ToastContextValue {
  toast: (opts: Omit<ToastItem, "id">) => void;
}

const ToastContext = createContext<ToastContextValue>({ toast: () => {} });

export function useToast() {
  return useContext(ToastContext);
}

// ---------------------------------------------------------------------------
// Variant -> severity
// ---------------------------------------------------------------------------

/** The library has no neutral severity: a plain notice is information. */
const VARIANT_SEVERITY: Record<ToastVariant, SnackbarSeverity> = {
  default: "info",
  success: "success",
  error: "error",
  info: "info",
  warning: "warning",
};

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

/** Mounted inside `AppIntlProvider` (`main.tsx`): the toasts render here, and
 *  every name they give assistive technologies is translated. */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const { t_i18n } = useFormatter();
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const counterRef = useRef(0);
  // Pending auto-remove timers, cleared on unmount: a timer that outlives the
  // provider would update an unmounted tree (and, in a test, fire after the
  // DOM environment is gone).
  const timersRef = useRef(new Set<ReturnType<typeof setTimeout>>());

  const toast = useCallback((opts: Omit<ToastItem, "id">) => {
    const id = `toast-${++counterRef.current}`;
    setToasts((prev) => showToast(prev, { id, ...opts }));
    // Auto-remove after animation
    const removeAfter = forcedRemovalDelay(opts);
    if (removeAfter === null) return;
    const timer = setTimeout(() => {
      timersRef.current.delete(timer);
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, removeAfter);
    timersRef.current.add(timer);
  }, []);

  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      timers.forEach(clearTimeout);
      timers.clear();
    };
  }, []);

  useEffect(() => {
    registerGlobalToast(toast);
    return unregisterGlobalToast;
  }, [toast]);

  return (
    <ToastContext.Provider value={{ toast }}>
      <SnackbarProvider duration={4000} label={t_i18n("Notification")}>
        {children}
        {toasts.map((t) => (
          <Snackbar
            key={t.id}
            severity={VARIANT_SEVERITY[t.variant ?? "default"]}
            title={t.title}
            // `whitespace-pre-line` honours the `\n` in a multi-line
            // description (a 422 listing one problem per slide) without
            // changing single-line ones.
            description={t.description && <span className="whitespace-pre-line">{t.description}</span>}
            action={
              t.action && (
                <Button size="sm" variant="outline" onClick={t.action.onClick}>
                  {t.action.label}
                </Button>
              )
            }
            actionAltText={t.action?.label}
            closeLabel={t_i18n("Close notification")}
            duration={t.durationMs}
            onOpenChange={(open) => {
              if (!open) setToasts((prev) => prev.filter((x) => x.id !== t.id));
            }}
          />
        ))}
        {/* Radix writes the hotkey (F8) into `{hotkey}` itself, so the
            placeholder is handed through untouched. A notification is never
            part of what the page prints. `z-[100]` keeps it above dialogs
            (`z-50`), which portal after the viewport: at the library's
            default layer a toast raised from a dialog lands under its scrim. */}
        <SnackbarViewport
          label={t_i18n("Notifications ({hotkey})", { hotkey: "{hotkey}" })}
          className="z-[100] print:hidden" />
      </SnackbarProvider>
    </ToastContext.Provider>
  );
}
