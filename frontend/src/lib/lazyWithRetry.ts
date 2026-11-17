// SPDX-License-Identifier: Apache-2.0

import { lazy, type ComponentType, type LazyExoticComponent } from "react";

const RELOAD_KEY = "chunk-reload-attempted";

/** Messages thrown by browsers / the Vite preload helper when a route chunk
 *  fails to download — typically a hashed URL that 404s after a redeploy.
 *  Module-evaluation (runtime) errors do not match any of these and propagate
 *  straight to the route-level `ErrorBoundary` without a reload. */
const CHUNK_LOAD_PATTERNS = [
  /failed to fetch dynamically imported module/i, // Chromium
  /error loading dynamically imported module/i, // Firefox
  /importing a module script failed/i, // Safari
  /unable to preload css/i, // Vite CSS preload helper
];

export function isChunkLoadError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return CHUNK_LOAD_PATTERNS.some((pattern) => pattern.test(message));
}

/** `sessionStorage` can throw (private browsing, storage disabled by policy).
 *  A storage failure must never reject a successfully loaded module, and the
 *  reload must only happen when the one-shot flag could actually be persisted
 *  — otherwise a genuinely unreachable chunk would cause an infinite reload
 *  loop. Returns true when the flag was newly set. */
function trySetReloadFlag(): boolean {
  try {
    if (sessionStorage.getItem(RELOAD_KEY)) return false;
    sessionStorage.setItem(RELOAD_KEY, "1");
    return true;
  } catch {
    return false;
  }
}

function clearReloadFlag(): void {
  try {
    sessionStorage.removeItem(RELOAD_KEY);
  } catch {
    /* storage unavailable — nothing to clear */
  }
}

/** Registry of every lazy route factory, consumed by `prefetchLazyChunks`. */
const chunkFactories: Array<() => Promise<unknown>> = [];

/**
 * `React.lazy` wrapper that recovers from chunk-load failures.
 *
 * After a deploy, hashed chunk URLs from the previous build 404 for users
 * with a stale `index.html`. The standard fix is a one-shot full reload to
 * pick up the new manifest. A sessionStorage flag prevents a reload loop if
 * the chunk is genuinely unreachable — the second failure propagates to the
 * route-level `ErrorBoundary` instead. Non-chunk failures (e.g. an error
 * thrown while evaluating the module) never trigger a reload.
 *
 * `prefetch: false` keeps the chunk out of `prefetchLazyChunks`, for a route
 * whose module must not load where it is not shown (it brings a stylesheet).
 */
export function lazyWithRetry<T extends ComponentType<unknown>>(
  factory: () => Promise<{ default: T }>,
  { prefetch = true }: { prefetch?: boolean } = {},
): LazyExoticComponent<T> {
  if (prefetch) chunkFactories.push(factory);
  return lazy(async () => {
    try {
      const module = await factory();
      clearReloadFlag();
      return module;
    } catch (error) {
      if (isChunkLoadError(error) && trySetReloadFlag()) {
        window.location.reload();
        // Never resolves — the page is reloading.
        return new Promise<{ default: T }>(() => {});
      }
      throw error;
    }
  });
}

/** Subset of the (non-standard) Network Information API we care about. */
interface NetworkInformationLike {
  saveData?: boolean;
  effectiveType?: string;
}

/** True when the user opted into Data Saver or is on a ~2G connection —
 *  speculative chunk downloads would be hostile there. Browsers without
 *  `navigator.connection` (Firefox, Safari) prefetch normally. */
function isConstrainedConnection(): boolean {
  const connection = (navigator as Navigator & { connection?: NetworkInformationLike }).connection;
  if (!connection) return false;
  return connection.saveData === true || /2g/.test(connection.effectiveType ?? "");
}

/** Run `task` once the browser is idle; returns what cancels it. */
function whenIdle(task: () => void): () => void {
  if (typeof window.requestIdleCallback === "function") {
    const handle = window.requestIdleCallback(task, { timeout: 5000 });
    return () => window.cancelIdleCallback(handle);
  }
  const handle = window.setTimeout(task, 1500);
  return () => window.clearTimeout(handle);
}

/** Import `factories` one at a time, none once `stopped()`; a failure is
 *  retried on real use. */
async function warmSequentially(
  factories: ReadonlyArray<() => Promise<unknown>>,
  stopped: () => boolean = () => false,
): Promise<void> {
  for (const factory of factories) {
    if (stopped()) return;
    try {
      await factory();
    } catch {
      /* non-fatal — retried on first real use */
    }
  }
}

/**
 * Warm every registered route chunk once the browser is idle.
 *
 * Without this, the first navigation to each page always pays a
 * chunk-download round trip and flashes the route Suspense skeleton.
 * After prefetching, the module is already in the browser /
 * module cache, so `React.lazy` resolves in a microtask and the fallback
 * never paints. Failures are swallowed — the chunk simply downloads again
 * on first navigation, where `lazyWithRetry`'s stale-deploy reload
 * recovery applies.
 *
 * Chunks are warmed **sequentially** (one request in flight at a time) so
 * the warm-up never bursts dozens of parallel downloads that would compete
 * with critical API fetches still resolving after first paint, and it is
 * skipped entirely on Data Saver / 2G connections.
 */
export function prefetchLazyChunks(): void {
  if (isConstrainedConnection()) return;
  whenIdle(() => void warmSequentially(chunkFactories.splice(0)));
}

/**
 * Warm the chunks of a feature loaded outside the route table (the arcade)
 * the way `prefetchLazyChunks` warms the routes.
 *
 * A deploy replaces every hashed chunk: a tab that has not imported a chunk
 * by then can never load it again, while an imported module outlives the
 * deploy that removed its file. Returns what cancels the warm-up: nothing is
 * imported if the browser was not idle yet, and no further chunk is started
 * if it was.
 */
export function warmChunksWhenIdle(factories: ReadonlyArray<() => Promise<unknown>>): () => void {
  if (isConstrainedConnection()) return () => {};
  let cancelled = false;
  const cancelIdle = whenIdle(() => void warmSequentially(factories, () => cancelled));
  return () => {
    cancelled = true;
    cancelIdle();
  };
}
