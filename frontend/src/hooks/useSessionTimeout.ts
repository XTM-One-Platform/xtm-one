// SPDX-License-Identifier: Apache-2.0

/**
 * The web session's absolute lifetime and idle timeout, as an open
 * tab follows them (the rules are `lib/session.ts`).
 *
 * - Real input - a click, a key, the wheel, a touch, coming back to the tab -
 *   is signalled to the server (`POST /auth/session/activity`), at most once
 *   per `activitySignalIntervalMs`. Nothing the app requests on its own does:
 *   its refreshes, polls, streams and keepalives leave the deadline alone.
 * - Shortly before the idle deadline the person is warned, and any input, or
 *   "Stay signed in", keeps the session.
 * - At a deadline the tab asks the server, which answers for every tab of the
 *   session: a 401 signs out (`api.ts`), a later deadline is waited for again.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRecordSessionActivity, useSessionStatus } from "@/hooks/api/useAuthApi";
import { activitySignalIntervalMs, secondsLeft, sessionTiming, timerDelay } from "@/lib/session";

/**
 * The input that says the person is there. Not a scroll, which the chat makes
 * on its own while an answer streams in, nor a pointer move, which browsers
 * fire when the page moves under a still pointer.
 */
const ACTIVITY_EVENTS = ["pointerdown", "keydown", "wheel", "touchstart"] as const;

/** A deadline is checked this long after it falls, so the server sees it passed. */
const DEADLINE_CHECK_GRACE_MS = 2_000;

interface SessionTimeoutState {
  /** The seconds left before the idle sign-out while the warning shows, else `null`. */
  warningSecondsLeft: number | null;
  /** "Stay signed in": signal activity now. */
  staySignedIn: () => void;
}

export function useSessionTimeout(): SessionTimeoutState {
  const { data: status, dataUpdatedAt, refetch } = useSessionStatus();
  const { mutate: recordActivity } = useRecordSessionActivity();
  const timing = useMemo(() => (status ? sessionTiming(status, dataUpdatedAt) : null), [status, dataUpdatedAt]);
  const [now, setNow] = useState(() => Date.now());
  const warning = timing !== null && timing.warnAt !== null && timing.idleEndsAt !== null && now >= timing.warnAt;

  const lastSignalAt = useRef(0);
  const intervalMs = activitySignalIntervalMs(status?.idle_timeout_seconds ?? 0);
  const signal = useCallback(
    (force: boolean) => {
      const at = Date.now();
      if (!force && at - lastSignalAt.current < intervalMs) return;
      lastSignalAt.current = at;
      recordActivity();
    },
    [intervalMs, recordActivity],
  );

  const warningRef = useRef(warning);
  useEffect(() => {
    warningRef.current = warning;
  }, [warning]);
  // Input is signalled unless the server said this is no web session: a status
  // that failed to load must not leave an active person to the idle timeout.
  const followsInput = status?.kind !== "other";
  useEffect(() => {
    if (!followsInput) return;
    // Under the warning every input counts at once: it is the answer to it.
    const onInput = (event: Event) => {
      if (event.isTrusted) signal(warningRef.current);
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") signal(warningRef.current);
    };
    for (const name of ACTIVITY_EVENTS) window.addEventListener(name, onInput, { capture: true, passive: true });
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      for (const name of ACTIVITY_EVENTS) window.removeEventListener(name, onInput, { capture: true });
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [followsInput, signal]);

  useEffect(() => {
    if (!timing) return;
    const timers: ReturnType<typeof setTimeout>[] = [];
    const at = (when: number, run: () => void) => timers.push(setTimeout(run, timerDelay(when, Date.now())));
    if (timing.endsAt !== null) at(timing.endsAt + DEADLINE_CHECK_GRACE_MS, () => void refetch());
    if (timing.warnAt !== null) {
      // Another tab may have kept the session meanwhile: the answer says so.
      at(timing.warnAt, () => {
        setNow(Date.now());
        void refetch();
      });
    }
    return () => timers.forEach(clearTimeout);
  }, [timing, refetch]);

  useEffect(() => {
    if (!warning) return;
    const tick = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(tick);
  }, [warning]);

  const staySignedIn = useCallback(() => signal(false), [signal]);
  return {
    warningSecondsLeft: warning && timing?.idleEndsAt != null ? secondsLeft(timing.idleEndsAt, now) : null,
    staySignedIn,
  };
}
