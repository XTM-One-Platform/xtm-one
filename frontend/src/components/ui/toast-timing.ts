// SPDX-License-Identifier: Apache-2.0

/**
 * When the toast provider (`toast.tsx`) drops a toast on its own.
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */

/** How long a toast stays when its caller names no duration. */
export const DEFAULT_TOAST_MS = 5000;

/**
 * The delay after which the provider removes a toast whatever happens, or
 * `null` for a toast with an action: that one is closed by Radix, whose timer
 * pauses while the toast is hovered or focused, so the button never vanishes
 * under the person reaching for it.
 */
export function forcedRemovalDelay(toast: { durationMs?: number; action?: unknown }): number | null {
  if (toast.action) return null;
  return toast.durationMs ?? DEFAULT_TOAST_MS;
}
