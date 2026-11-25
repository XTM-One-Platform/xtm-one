// SPDX-License-Identifier: Apache-2.0

/**
 * How often a tab re-reads the CTEM Cases list on live case frames.
 *
 * A case-opening drain publishes one `ctem_case_update` frame per case it
 * opens or joins. Re-reading the whole list on each frame sent about 70
 * `GET /ctem-cases` per minute per open tab while a drain ran, each one a
 * read of the viewer's whole case view, which slowed the drain and every
 * other reader.
 *
 * The first frame after a quiet period refreshes at once, so a single case
 * still appears without a wait. Frames that arrive while the window is open
 * are gathered, and the window ends with one refresh carrying every case id
 * they named. A burst therefore costs at most one list read per window.
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */

/** At most one Cases list refresh per window during a burst of case frames. */
export const CASE_REFRESH_WINDOW_MS = 5_000;

type Timers = {
  set: (run: () => void, delayMs: number) => unknown;
  clear: (handle: unknown) => void;
};

const browserTimers: Timers = {
  set: (run, delayMs) => setTimeout(run, delayMs),
  clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export type CaseRefreshCoalescer = {
  /** A case frame arrived; `caseId` is the case it names, when it names one. */
  push: (caseId?: string) => void;
  /** Stop: a pending refresh is dropped and no timer is left running. */
  dispose: () => void;
};

/**
 * Calls `refresh` with the case ids gathered since the previous call: at once
 * for the first frame after a quiet window, then at most once per `windowMs`
 * while frames keep coming.
 */
export function createCaseRefreshCoalescer(
  refresh: (caseIds: string[]) => void,
  windowMs: number = CASE_REFRESH_WINDOW_MS,
  timers: Timers = browserTimers,
): CaseRefreshCoalescer {
  let timer: unknown = null;
  let pending = false;
  let disposed = false;
  const caseIds = new Set<string>();

  const flush = () => {
    const ids = [...caseIds];
    caseIds.clear();
    pending = false;
    refresh(ids);
  };

  const windowEnded = () => {
    timer = null;
    if (disposed || !pending) return;
    flush();
    timer = timers.set(windowEnded, windowMs);
  };

  return {
    push(caseId) {
      if (disposed) return;
      if (caseId) caseIds.add(caseId);
      if (timer !== null) {
        pending = true;
        return;
      }
      flush();
      timer = timers.set(windowEnded, windowMs);
    },
    dispose() {
      disposed = true;
      pending = false;
      caseIds.clear();
      if (timer !== null) timers.clear(timer);
      timer = null;
    },
  };
}
