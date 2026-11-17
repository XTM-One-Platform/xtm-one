// SPDX-License-Identifier: Apache-2.0

import { useEffect, type RefObject } from "react";

/**
 * Run a callback when a click lands outside the referenced element(s).
 *
 * Accepts a single ref or an array of refs — the callback fires only when the
 * click is outside ALL of them (useful for trigger + portaled popover pairs).
 *
 * The hook is active only when `enabled` is `true` (default).  Passing
 * `false` avoids adding the listener entirely, which is useful when the
 * dropdown / popover is closed.
 *
 * `ignore` leaves the callback unfired for a press it accepts: a press inside
 * a list a picker opened in a portal, outside the refs (`isInsideFloatingList`).
 * Give it one identity across renders.
 *
 * @example
 * ```tsx
 * const ref = useRef<HTMLDivElement>(null);
 * useClickOutside(ref, () => setOpen(false), open);
 * // or with multiple refs:
 * useClickOutside([btnRef, popoverRef], () => setOpen(false), open);
 * ```
 */
export function useClickOutside(
  ref: RefObject<HTMLElement | null> | RefObject<HTMLElement | null>[],
  onClickOutside: () => void,
  enabled = true,
  ignore?: (target: EventTarget | null) => boolean,
) {
  useEffect(() => {
    if (!enabled) return;

    const refs = Array.isArray(ref) ? ref : [ref];
    const handler = (e: MouseEvent) => {
      if (ignore?.(e.target)) return;
      const target = e.target as Node;
      const mounted = refs.filter((r) => r.current !== null);
      if (mounted.length === 0) return;
      if (mounted.every((r) => !r.current!.contains(target))) {
        onClickOutside();
      }
    };

    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [ref, onClickOutside, enabled, ignore]);
}
