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
 * The Dashboard Studio's design of a definition: its undo history, the items
 * picked on a canvas, the clipboard and the keyboard shortcuts.
 *
 * The history is a list of definitions (`History<T>`, the video editor's):
 * every change - a gesture on the canvas, a tab added, an option of an element
 * - pushes the definition it makes, which is shown at once and saved as the
 * difference from the one before (`definitionOperations`, through the page's
 * queued edit, or as a whole definition when the difference is too large for
 * one edit). Undo and redo save the difference back. A change saved by
 * someone else (the assistant, the inspector, a pack, the source editor) is a
 * revision this history did not write: the history starts again from it.
 * A refused save shows why and starts again from the stored definition.
 *
 * A run of changes of one field (a colour dragged, a text typed: `coalesce`
 * names the field) is one step of the history and one save, made once the
 * run has paused for `COALESCE_MS`.
 *
 * The clipboard lives as long as the page (so a copy can be pasted into
 * another tab or another dashboard of the session) and holds what was copied
 * with the data sources it reads.
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  historyInit,
  historyPush,
  historyRedo,
  historyUndo,
  type History,
} from "@/components/video-studio/editor/video-editor-model";
import { invalidateDashboards } from "@/hooks/api/useCustomDashboardApi";
import type { CustomDashboard, CustomDashboardOperation } from "@/types";

import {
  PASTE_OFFSET,
  copyItems,
  definitionOperations,
  pasteItems,
  removeItems,
  type CanvasClipboard,
} from "./canvas";

type Definition = Record<string, unknown>;

/** The most operations one edit holds (`MAX_OPERATIONS`): a larger change is saved whole. */
const MAX_OPERATIONS = 40;
/** How long a run of changes of one field pauses before it is saved. */
export const COALESCE_MS = 600;

let clipboard: CanvasClipboard | null = null;

interface Handlers {
  onSuccess?: (saved: CustomDashboard) => void;
  onError?: (error: Error & { status?: number }) => void;
}

export interface DashboardDesigner {
  /** The definition as the studio shows it (saved, or being saved). */
  definition: Definition | null;
  /** A change: one undo step, saved; with `coalesce`, a run of changes of that field is one. */
  change: (update: (definition: Definition) => Definition, coalesce?: string) => void;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
  selection: string[];
  select: (ids: string[]) => void;
  copy: (layoutId: string) => void;
  cut: (layoutId: string) => void;
  paste: (layoutId: string) => void;
  /** Copy the selection next to itself; `newDataSources`: each copy reads a copy of its data source. */
  duplicate: (layoutId: string, newDataSources?: boolean) => void;
  canPaste: boolean;
  /** The canvas's keys that are the studio's: copy, cut, paste, duplicate, undo, redo. */
  onShortcut: (layoutId: string) => (event: KeyboardEvent) => void;
  /** A run of changes waiting to be saved, changes being saved, or everything saved. */
  saveState: "unsaved" | "saving" | "saved";
}

export function useDashboardDesigner({
  dashboard,
  edit,
  replace,
  onRefused,
}: {
  dashboard: Pick<CustomDashboard, "id" | "revision" | "spec"> | undefined;
  edit: (operations: CustomDashboardOperation[], handlers: Handlers) => void;
  replace: (definition: unknown, handlers: Handlers) => void;
  /** Say why a save was refused (a 409, a 422). */
  onRefused: (error: Error & { status?: number }) => void;
}): DashboardDesigner {
  const qc = useQueryClient();
  const [history, setHistory] = useState<History<Definition> | null>(null);
  const current = useRef<History<Definition> | null>(null);
  const pending = useRef(0);
  const savedRevision = useRef<number | null>(null);
  // A run of changes of one field: where it started, saved when it pauses.
  const run = useRef<{ key: string; base: Definition; timer: ReturnType<typeof setTimeout> } | null>(null);
  const [selection, setSelection] = useState<string[]>([]);
  const [canPaste, setCanPaste] = useState(clipboard !== null);
  // What the save state shows: the saves in flight, and a run not saved yet.
  const [inFlight, setInFlight] = useState(0);
  const [waiting, setWaiting] = useState(false);

  const apply = useCallback((next: History<Definition>) => {
    current.current = next;
    setHistory(next);
  }, []);

  const spec = dashboard?.spec as Definition | undefined;
  const revision = dashboard?.revision;
  const dashboardId = dashboard?.id;
  // A revision this history did not write: start again from it (none while ours are being saved).
  useEffect(() => {
    if (!spec || revision === undefined) return;
    if (pending.current > 0 || revision === savedRevision.current) return;
    // A run not saved yet was made on what is no longer there: dropped.
    if (run.current) {
      clearTimeout(run.current.timer);
      run.current = null;
      setWaiting(false);
    }
    savedRevision.current = revision;
    apply(historyInit(spec));
  }, [apply, revision, spec]);

  const save = useCallback(
    (before: Definition, after: Definition) => {
      const operations = definitionOperations(before, after);
      if (operations.length === 0) return;
      pending.current += 1;
      setInFlight((n) => n + 1);
      const handlers: Handlers = {
        onSuccess: (saved) => {
          savedRevision.current = saved.revision;
          pending.current -= 1;
          setInFlight((n) => n - 1);
        },
        onError: (error) => {
          pending.current -= 1;
          setInFlight((n) => n - 1);
          onRefused(error);
          // Start again from what is stored; a newer revision, if any, follows with the refetch.
          const stored = qc.getQueryData<CustomDashboard>(["custom-dashboards", dashboardId]);
          if (stored && pending.current === 0) {
            savedRevision.current = stored.revision;
            apply(historyInit(stored.spec as Definition));
          }
          void invalidateDashboards(qc, dashboardId);
        },
      };
      if (operations.length > MAX_OPERATIONS) replace(after, handlers);
      else edit(operations as CustomDashboardOperation[], handlers);
    },
    [apply, dashboardId, edit, onRefused, qc, replace],
  );

  const flush = useCallback(() => {
    const open = run.current;
    if (!open) return;
    clearTimeout(open.timer);
    run.current = null;
    setWaiting(false);
    if (current.current) save(open.base, current.current.present);
  }, [save]);
  useEffect(() => flush, [flush]);

  const change = useCallback(
    (update: (definition: Definition) => Definition, coalesce?: string) => {
      const now = current.current;
      if (!now) return;
      const next = update(now.present);
      if (next === now.present || JSON.stringify(next) === JSON.stringify(now.present)) return;
      if (coalesce && run.current?.key === coalesce) {
        clearTimeout(run.current.timer);
        run.current.timer = setTimeout(flush, COALESCE_MS);
        apply(historyPush(now, next, true));
        return;
      }
      flush();
      if (coalesce) {
        run.current = { key: coalesce, base: now.present, timer: setTimeout(flush, COALESCE_MS) };
        setWaiting(true);
      } else save(now.present, next);
      apply(historyPush(now, next));
    },
    [apply, flush, save],
  );

  const undo = useCallback(() => {
    flush();
    const now = current.current;
    if (!now || now.past.length === 0) return;
    const next = historyUndo(now);
    save(now.present, next.present);
    apply(next);
  }, [apply, flush, save]);

  const redo = useCallback(() => {
    flush();
    const now = current.current;
    if (!now || now.future.length === 0) return;
    const next = historyRedo(now);
    save(now.present, next.present);
    apply(next);
  }, [apply, flush, save]);

  // What is picked may be gone after an undo, a removal or a save elsewhere.
  useEffect(() => {
    const visualizations = (history?.present.visualizations ?? {}) as Definition;
    setSelection((ids) => {
      const kept = ids.filter((id) => id in visualizations);
      return kept.length === ids.length ? ids : kept;
    });
  }, [history?.present]);

  const copy = useCallback(
    (layoutId: string) => {
      const now = current.current;
      if (!now || selection.length === 0) return;
      const copied = copyItems(now.present, layoutId, selection);
      if (copied) {
        clipboard = copied;
        setCanPaste(true);
      }
    },
    [selection],
  );

  const cut = useCallback(
    (layoutId: string) => {
      copy(layoutId);
      const ids = selection;
      change((definition) => removeItems(definition, ids));
      setSelection([]);
    },
    [change, copy, selection],
  );

  const paste = useCallback(
    (layoutId: string) => {
      const copied = clipboard;
      if (!copied) return;
      let ids: string[] = [];
      change((definition) => {
        const pasted = pasteItems(definition, layoutId, copied, { offset: PASTE_OFFSET });
        ids = pasted.ids;
        return pasted.definition;
      });
      setSelection(ids);
    },
    [change],
  );

  const duplicate = useCallback(
    (layoutId: string, newDataSources = false) => {
      const now = current.current;
      if (!now || selection.length === 0) return;
      const copied = copyItems(now.present, layoutId, selection);
      if (!copied) return;
      let ids: string[] = [];
      change((definition) => {
        const pasted = pasteItems(definition, layoutId, copied, { newDataSources });
        ids = pasted.ids;
        return pasted.definition;
      });
      setSelection(ids);
    },
    [change, selection],
  );

  const onShortcut = useCallback(
    (layoutId: string) => (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      const action =
        key === "z" && !event.shiftKey ? undo
        : (key === "z" && event.shiftKey) || key === "y" ? redo
        : key === "c" ? () => copy(layoutId)
        : key === "x" ? () => cut(layoutId)
        : key === "v" ? () => paste(layoutId)
        : key === "d" ? () => duplicate(layoutId)
        : null;
      if (!action) return;
      event.preventDefault();
      action();
    },
    [copy, cut, duplicate, paste, redo, undo],
  );

  return {
    definition: history?.present ?? null,
    change,
    undo,
    redo,
    canUndo: (history?.past.length ?? 0) > 0,
    canRedo: (history?.future.length ?? 0) > 0,
    selection,
    select: setSelection,
    copy,
    cut,
    paste,
    duplicate,
    canPaste,
    onShortcut,
    saveState: waiting ? "unsaved" : inFlight > 0 ? "saving" : "saved",
  };
}
