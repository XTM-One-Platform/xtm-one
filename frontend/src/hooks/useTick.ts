// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from "react";
import { REFETCH_INTERVAL_SLOW } from "@/lib/constants";

/**
 * Forces a component re-render every `intervalMs` milliseconds.
 *
 * Use in pages / components that display relative timestamps (`timeAgo`)
 * so they stay fresh even when no new data arrives from the server.
 *
 * @default intervalMs REFETCH_INTERVAL_SLOW (30 seconds)
 */
export function useTick(intervalMs = REFETCH_INTERVAL_SLOW) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
}

/**
 * The current time in milliseconds, read again every `intervalMs`.
 *
 * Use where a state is derived from the clock (a key that expires, a deadline)
 * so it changes while the page stays open, without reading the clock in render.
 *
 * @default intervalMs REFETCH_INTERVAL_SLOW (30 seconds)
 */
export function useNow(intervalMs = REFETCH_INTERVAL_SLOW): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
