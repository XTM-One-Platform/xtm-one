// SPDX-License-Identifier: Apache-2.0

/**
 * Module-level toast ref – usable outside the React tree
 * (e.g. MutationCache.onError in main.tsx).
 *
 * The ToastProvider registers itself here on mount so that
 * non-React code can show toasts without context access.
 */

interface GlobalToastOpts {
  title: string;
  description?: string;
  variant?: "default" | "success" | "error" | "info" | "warning";
}

let _fn: ((opts: GlobalToastOpts) => void) | null = null;

export function registerGlobalToast(fn: (opts: GlobalToastOpts) => void) {
  _fn = fn;
}

export function unregisterGlobalToast() {
  _fn = null;
}

export function globalToast(opts: GlobalToastOpts) {
  _fn?.(opts);
}
