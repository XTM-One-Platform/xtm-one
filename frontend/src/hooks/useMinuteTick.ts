// SPDX-License-Identifier: Apache-2.0

import { useSyncExternalStore } from "react";

import { minuteTick } from "@/lib/minute-tick";

/**
 * Re-render the calling component once a minute, on the timer every caller
 * shares. For a relative time ("5m ago") drawn inside a memoized component.
 */
export function useMinuteTick(): number {
  return useSyncExternalStore(minuteTick.subscribe, minuteTick.getSnapshot, minuteTick.getSnapshot);
}
