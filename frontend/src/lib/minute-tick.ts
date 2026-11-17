// SPDX-License-Identifier: Apache-2.0

/**
 * One shared minute tick for relative times ("5m ago").
 *
 * A memoized list re-renders only when its props change, so a relative time
 * drawn inside it stays frozen at whatever it read last. Each subscriber to
 * this store re-renders once a minute instead, and every subscriber shares ONE
 * timer, armed while at least one of them is mounted.
 *
 * The snapshot is the current minute (`Math.floor(now / 60_000)`): a primitive
 * that stays equal for the whole minute, as `useSyncExternalStore` requires.
 */

export const MINUTE_MS = 60_000;

export interface TickClock {
  now: () => number;
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
}

const systemClock: TickClock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

interface MinuteTick {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => number;
}

export function createMinuteTick(clock: TickClock = systemClock): MinuteTick {
  const listeners = new Set<() => void>();
  let handle: unknown = null;

  const schedule = () => {
    const delay = MINUTE_MS - (clock.now() % MINUTE_MS);
    handle = clock.setTimeout(() => {
      handle = null;
      if (listeners.size === 0) return;
      schedule();
      for (const listener of [...listeners]) listener();
    }, delay);
  };

  return {
    subscribe(listener) {
      listeners.add(listener);
      if (handle === null) schedule();
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0 && handle !== null) {
          clock.clearTimeout(handle);
          handle = null;
        }
      };
    },
    getSnapshot: () => Math.floor(clock.now() / MINUTE_MS),
  };
}

export const minuteTick = createMinuteTick();
