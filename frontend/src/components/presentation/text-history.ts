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
 * The undo history of ONE text edited in place - the pure half of
 * `InlineTextEditor`'s `Ctrl+Z` / `Ctrl+Y`.
 *
 * The browser keeps an undo stack of its own for a `contentEditable`, but it
 * knows nothing of what the editor does by hand: a formatting from the bar
 * (`applyFormat`) or a line break rebuilds the field's children, and that
 * reset leaves `Ctrl+Z` a no-op right after a **bold** was applied. So the
 * editor keeps the session's history itself, as snapshots of the runs model
 * (`inline-runs.ts`) plus the selection to restore, and suppresses the
 * browser's - one stack, every kind of change in it.
 *
 * A snapshot is recorded after every change: the browser's `input` events
 * (typing, deleting, pasting, a composition) and the editor's own rebuilds.
 * Typing is COALESCED - consecutive changes of the same `kind` within
 * `TEXT_COALESCE_MS`, each beginning where the last one left the caret,
 * replace the newest snapshot rather than stack one per keystroke - so
 * `Ctrl+Z` takes a burst of typing back, the way a word processor does; a
 * formatting, a line break, a paste, or a keystroke somewhere else in the
 * text is always a step of its own. Recording clears the redo stack, undo /
 * redo walk it.
 *
 * The selection an undo restores is where the change BEGAN, not where the
 * field happened to be when the previous snapshot was taken: a caret moved
 * or a word selected between two edits is not a step, but it is what the
 * next edit is undone back to, so the caller hands `recordSnapshot` the
 * selection as it was right before the change (`before`) and the present
 * snapshot takes it before the change is recorded.
 *
 * Pure: the editor applies a snapshot by rebuilding the field from its runs
 * (`rebuild`), so what these functions return is exactly what is drawn.
 */
import type { TextRun } from "./inline-runs";

/** A selection in plain-text offsets (`start === end` is a caret). */
export interface TextSelection {
  start: number;
  end: number;
}

/** The field at one moment: its runs and the selection (plain-text offsets). */
export interface TextSnapshot extends TextSelection {
  runs: readonly TextRun[];
}

export interface TextHistory {
  past: TextSnapshot[];
  present: TextSnapshot;
  future: TextSnapshot[];
  /** What produced `present` and when - the coalescing key. */
  lastKind: string | null;
  lastAt: number;
}

/** Consecutive changes of one kind within this window are one undo step. */
export const TEXT_COALESCE_MS = 900;
/** The most snapshots a session keeps behind it. */
export const TEXT_HISTORY_LIMIT = 200;

/**
 * The kinds that are never coalesced: each one is a step of its own. The
 * editor's own `format` / `line-break`, and the `InputEvent.inputType`s of a
 * paste, a drop, a spell-checker replacement, a cut, a drag out of the field
 * and the browser's own line / paragraph break (a Shift+Enter the editor did
 * not intercept) - two of any of those within the window are two steps.
 */
const DISCRETE_KINDS = new Set([
  "format",
  "line-break",
  "insertFromPaste",
  "insertFromDrop",
  "insertReplacementText",
  "insertLineBreak",
  "insertParagraph",
  "deleteByCut",
  "deleteByDrag",
]);

export function textHistoryFrom(present: TextSnapshot): TextHistory {
  return { past: [], present, future: [], lastKind: null, lastAt: 0 };
}

/**
 * The history once the field changed to `next` by a change of `kind` (an
 * `InputEvent.inputType`, or the editor's own `format` / `line-break`) at
 * time `now`. `before` is the selection as it was right before the change:
 * the present snapshot takes it (so an undo of the change puts the caret back
 * where the change began, not where the field was when the present was
 * recorded), and a change that does not begin where the previous one left
 * the caret starts a step of its own - a word typed at the end and a word
 * typed at the start within the window are two things to undo, not one. A
 * change that left the runs as they were records nothing beyond that
 * selection (a caret moved is not a step).
 */
export function recordSnapshot(
  history: TextHistory,
  next: TextSnapshot,
  kind: string,
  now: number,
  before?: TextSelection,
): TextHistory {
  const present = before ? { ...history.present, start: before.start, end: before.end } : history.present;
  if (sameRuns(present.runs, next.runs)) return present === history.present ? history : { ...history, present };
  const contiguous = !before || (before.start === history.present.end && before.end === history.present.end);
  const coalesce =
    kind === history.lastKind &&
    !DISCRETE_KINDS.has(kind) &&
    contiguous &&
    now - history.lastAt < TEXT_COALESCE_MS &&
    !history.future.length;
  if (coalesce) return { ...history, present: next, lastAt: now, future: [] };
  return {
    past: [...history.past.slice(-(TEXT_HISTORY_LIMIT - 1)), present],
    present: next,
    future: [],
    lastKind: kind,
    lastAt: now,
  };
}

/** The history one step back, or `null` when there is nothing to undo. */
export function undoSnapshot(history: TextHistory): TextHistory | null {
  const previous = history.past[history.past.length - 1];
  if (!previous) return null;
  return {
    past: history.past.slice(0, -1),
    present: previous,
    future: [history.present, ...history.future],
    lastKind: null,
    lastAt: 0,
  };
}

/** The history one step forward, or `null` when there is nothing to redo. */
export function redoSnapshot(history: TextHistory): TextHistory | null {
  const next = history.future[0];
  if (!next) return null;
  return {
    past: [...history.past, history.present],
    present: next,
    future: history.future.slice(1),
    lastKind: null,
    lastAt: 0,
  };
}

function sameRuns(a: readonly TextRun[], b: readonly TextRun[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((run, index) => {
    const other = b[index];
    return run.text === other.text && run.style === other.style && (run.tone ?? null) === (other.tone ?? null);
  });
}
