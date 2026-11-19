// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";

/**
 * `value`, kept as the same object while its content `key` does not change.
 *
 * A polled list rebuilt on every tick (a new Set of running agents, a new Map
 * of live edges) hands a drawing a new reference with the same content: the
 * drawing then recomputes everything that depends on it. Passed through here,
 * a tick that changes nothing hands back the object of the last change, so
 * nothing downstream runs again.
 */
export function useByContent<T>(value: T, key: string): T {
  const [held, setHeld] = useState(() => ({ key, value }));
  if (held.key !== key) {
    // Adjusting state while rendering: React renders again at once with it.
    setHeld({ key, value });
    return value;
  }
  return held.value;
}

/** The content key of a set of ids, whatever their order. */
export function idsKey(ids: Iterable<string> | null | undefined): string {
  return ids ? [...ids].sort().join("\n") : "";
}
