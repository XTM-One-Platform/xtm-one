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
 * Following the panel's turns from outside it, for the pane's own sequences
 * (a document review asks one question per section, one turn each).
 *
 * The panel says when a turn starts and stops running, and how it ended
 * (`onTurnRunningChange(running, { ended })`): this only sequences on it.
 *
 * Pure and import-free: the frontend's unit tests read it
 * (`frontend/tests_unit/office-addin-turns.test.ts`) without the add-in's
 * dependencies.
 */

/** How a turn ended, as the panel says it (`ChatTurnOutcome`). */
export type TurnEnd = "answered" | "stopped" | "failed";

export interface TurnWatch {
  /** From `onTurnRunningChange`: *ended* comes with `false`. */
  setRunning(running: boolean, ended?: TurnEnd): void;
  /** How many turns have started so far: what `endAfter` counts from. */
  starts(): number;
  /** Resolves once no turn runs: true, or false when the watch is closed first. */
  idle(): Promise<boolean>;
  /**
   * Resolves when the first turn started after the *since*-th one ends, with
   * how it ended; `"closed"` when the watch is closed first.
   */
  endAfter(since: number): Promise<TurnEnd | "closed">;
  /** The pane is gone: every wait resolves as closed, and later ones at once. */
  close(): void;
}

export function createTurnWatch(): TurnWatch {
  let running = false;
  let started = 0;
  let closed = false;
  // How the last turn to end ended.
  let lastEnd: TurnEnd = "stopped";
  const listeners = new Set<(ended: TurnEnd | null) => void>();
  const notify = (ended: TurnEnd | null) => {
    for (const listener of [...listeners]) listener(ended);
  };

  return {
    setRunning(next, ended) {
      if (closed || next === running) return;
      running = next;
      if (next) {
        started += 1;
        notify(null);
        return;
      }
      lastEnd = ended ?? "stopped";
      notify(lastEnd);
    },
    starts: () => started,
    idle() {
      if (closed) return Promise.resolve(false);
      if (!running) return Promise.resolve(true);
      return new Promise((resolve) => {
        const listener = (ended: TurnEnd | null) => {
          if (closed || ended) {
            listeners.delete(listener);
            resolve(!closed);
          }
        };
        listeners.add(listener);
      });
    },
    endAfter(since) {
      if (closed) return Promise.resolve("closed" as const);
      // That turn has come and gone already.
      if (started > since && !running) return Promise.resolve(lastEnd);
      return new Promise((resolve) => {
        const listener = (ended: TurnEnd | null) => {
          if (closed) {
            listeners.delete(listener);
            resolve("closed");
          } else if (ended && started > since) {
            listeners.delete(listener);
            resolve(ended);
          }
        };
        listeners.add(listener);
      });
    },
    close() {
      if (closed) return;
      closed = true;
      notify(null);
      listeners.clear();
    },
  };
}

/** One question of a sequence: what is sent, and what the user's bubble shows. */
export interface SequenceStep {
  text: string;
  displayText: string;
}

/**
 * Send *steps* one per turn: each waits until no turn runs, and the next goes
 * only once the turn it started has ended with its answer. The sequence stops
 * when a send does not start a turn of its own (`send` resolves other than
 * `"sent"`: it failed, was dropped, or joined or waits on another turn), when
 * a turn ends without its answer (Stop, an error, a new conversation), when
 * *cancelled* says so, or when the watch closes. Resolves to how many steps
 * were sent.
 */
export async function runOnePerTurn(
  steps: readonly SequenceStep[],
  send: (step: SequenceStep) => Promise<string | undefined>,
  watch: TurnWatch,
  cancelled: () => boolean = () => false,
): Promise<number> {
  for (let i = 0; i < steps.length; i++) {
    if (!(await watch.idle()) || cancelled()) return i;
    const since = watch.starts();
    if ((await send(steps[i])) !== "sent") return i;
    if (i === steps.length - 1) return i + 1;
    if ((await watch.endAfter(since)) !== "answered") return i + 1;
  }
  return steps.length;
}
