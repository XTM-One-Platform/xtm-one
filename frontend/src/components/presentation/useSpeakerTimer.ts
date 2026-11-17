/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The presenter's stopwatch.
 *
 * Elapsed talk time, running or paused, kept in `sessionStorage` under the
 * deck's id so a reload of the speaker window mid-talk (a crashed tab, a
 * mis-click on refresh) comes back with the clock still running from where it
 * was — the timer is the one thing on the speaker screen that cannot be
 * recomputed from the deck. Session storage is per window, so two speaker
 * windows for two decks never share a clock, and closing the window ends the
 * session as a presenter expects.
 *
 * Time is measured from wall-clock timestamps, not by counting ticks: a
 * background tab's timers are throttled, and a stopwatch that lost a minute
 * every time the presenter looked at another window would be worse than none.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

interface TimerState {
  /** Seconds accumulated across earlier running stretches. */
  banked: number;
  /** Wall-clock start of the current running stretch, `null` while paused. */
  since: number | null;
}

const IDLE: TimerState = { banked: 0, since: null };

function storageKey(presentationId: string | undefined): string {
  return `xtm-speaker-timer:${presentationId ?? "none"}`;
}

function load(key: string): TimerState {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return IDLE;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return IDLE;
    const { banked, since } = parsed as Partial<TimerState>;
    return {
      banked: typeof banked === "number" && Number.isFinite(banked) && banked >= 0 ? banked : 0,
      since: typeof since === "number" && Number.isFinite(since) ? since : null,
    };
  } catch {
    return IDLE;
  }
}

function elapsedOf(state: TimerState, now: number): number {
  return state.banked + (state.since === null ? 0 : Math.max(0, (now - state.since) / 1000));
}

interface SpeakerTimer {
  /** Whole seconds on the clock. */
  elapsed: number;
  running: boolean;
  /** True until the clock has run at least once (drives the auto-start). */
  untouched: boolean;
  start: () => void;
  pause: () => void;
  toggle: () => void;
  reset: () => void;
}

export function useSpeakerTimer(presentationId: string | undefined): SpeakerTimer {
  const key = storageKey(presentationId);
  const [state, setState] = useState<TimerState>(() => load(key));
  const [now, setNow] = useState(() => Date.now());

  // A different deck in the same window (a navigation) gets its own clock.
  useEffect(() => {
    setState(load(key));
  }, [key]);

  useEffect(() => {
    try {
      if (state.banked === 0 && state.since === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, JSON.stringify(state));
    } catch {
      // Storage full or forbidden: the clock still runs for this window.
    }
  }, [key, state]);

  // Re-render every 250ms while running: often enough that the seconds never
  // visibly skip, and the elapsed value is derived from timestamps regardless.
  useEffect(() => {
    if (state.since === null) return;
    setNow(Date.now());
    const interval = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(interval);
  }, [state.since]);

  const start = useCallback(() => {
    setState((current) => (current.since === null ? { ...current, since: Date.now() } : current));
  }, []);
  const pause = useCallback(() => {
    setState((current) =>
      current.since === null ? current : { banked: elapsedOf(current, Date.now()), since: null },
    );
  }, []);
  const toggle = useCallback(() => {
    setState((current) =>
      current.since === null
        ? { ...current, since: Date.now() }
        : { banked: elapsedOf(current, Date.now()), since: null },
    );
  }, []);
  const reset = useCallback(() => setState(IDLE), []);

  const running = state.since !== null;
  // While paused the banked seconds are the whole answer and `now` is unused;
  // while running the tick above keeps `now` fresh.
  const elapsed = Math.floor(elapsedOf(state, now));

  return useMemo(
    () => ({
      elapsed,
      running,
      untouched: state.banked === 0 && state.since === null,
      start,
      pause,
      toggle,
      reset,
    }),
    [elapsed, pause, reset, running, start, state.banked, state.since, toggle],
  );
}
