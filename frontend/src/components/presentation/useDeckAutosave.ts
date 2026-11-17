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
 * The slide editor saves on its own.
 *
 * Owned by the viewer next to the editing session (`useDeckEditor`), so it
 * outlives the editor panel: a change made right before the panel closes is
 * still written. Every save is the revision-checked `PATCH` the explicit Save
 * used to send (`saveBody`), one at a time; the answer is adopted by the
 * session (`adoptSaved`), a 409 hands over to the merge (`saveRefused`: the
 * collaborator's revision is merged in, and the next save extends it). The
 * timing, the retries and what the status line says are `autosave-policy.ts`.
 *
 * Beyond the timer, the changes are saved at once when the tab is hidden or
 * the page is left - as a `keepalive` request, which outlives the page, when
 * the body fits the browser's cap - `Ctrl+S` saves now (`onSaveRequest`), a
 * tab closed over unsaved work asks first, and `flush()` is what leaving the editor awaits:
 * it resolves `true` once nothing is left to save, `false` when the last save
 * failed and waiting will not help.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useToast } from "@/components/ui/toast";
import { useUpdatePresentation } from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";

import {
  AUTOSAVE_RETRY_BASE_MS,
  EDITING_LINGER_MS,
  LEAVE_FLUSH_TIMEOUT_MS,
  autosaveDelay,
  autosaveState,
  failureIsFinal,
  failureKind,
  keepaliveFits,
  retryDelay,
  saveBody,
  type AutosaveState,
  type SaveFailureKind,
} from "./autosave-policy";
import { activeInlineEditor } from "./inline-editor-registry";
import type { DeckDraft, DeckEditor, SaveRequest } from "./presentation-editor";

interface AutosaveStatus {
  state: AutosaveState;
  /** When the last save landed (this session), `null` before the first one. */
  lastSavedAt: number | null;
  /** What the server said of the last failed save, when it said something. */
  error: string | null;
  /** When the next retry of a save that did not reach the server goes. */
  retryAt: number | null;
}

export interface DeckAutosave {
  status: AutosaveStatus;
  /** Save now, whatever the timer says. */
  saveNow: () => void;
  /** Leaving: save what is left and wait for it - `false` when it could not be saved. */
  flush: () => Promise<boolean>;
  /** Drop what is left unsaved ("Leave anyway"): nothing more is sent for it. */
  discard: () => void;
  /** The person is editing right now, for the others in the room (presence). */
  active: boolean;
}

type SaveOutcome = "saved" | "skipped" | SaveFailureKind;

interface Failure {
  kind: Exclude<SaveFailureKind, "conflict">;
  message: string | null;
  /** The draft the failed save sent: a refusal of it says nothing of a newer one. */
  draft: DeckDraft;
  count: number;
  /** How long the server asked to wait (a 429's `Retry-After`, seconds). */
  retryAfter?: number;
}

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

export function useDeckAutosave(editor: DeckEditor | null, enabled: boolean): DeckAutosave {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { mutateAsync } = useUpdatePresentation();
  const editorRef = useRef(editor);
  editorRef.current = editor;
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const [saving, setSaving] = useState(false);
  const [lastSavedAt, setLastSavedAt] = useState<number | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const failureRef = useRef(failure);
  failureRef.current = failure;
  const [retryAt, setRetryAt] = useState<number | null>(null);
  const [online, setOnline] = useState(() => typeof navigator === "undefined" || navigator.onLine !== false);
  const [lingering, setLingering] = useState(false);
  const inFlight = useRef<Promise<SaveOutcome> | null>(null);
  // When the changes still unsaved began, and when the last one was made.
  const changes = useRef<{ first: number | null; last: number | null }>({ first: null, last: null });
  // "Leave anyway": what the draft holds is no longer to be written.
  const abandoned = useRef(false);

  const draft = editor?.draft;
  const dirty = !!editor?.dirty;
  const outOfDate = !!editor?.outOfDate;
  const deckId = editor?.presentation.id;

  useEffect(() => {
    if (!dirty) {
      changes.current = { first: null, last: null };
      abandoned.current = false;
      return;
    }
    const now = Date.now();
    changes.current = { first: changes.current.first ?? now, last: now };
  }, [draft, dirty]);

  // Another deck on the same page (the router reuses it): its own history.
  useEffect(() => {
    setFailure(null);
    setLastSavedAt(null);
    setRetryAt(null);
  }, [deckId]);

  const save = useCallback((request?: SaveRequest): Promise<SaveOutcome> => {
    if (inFlight.current) return inFlight.current;
    const ed = editorRef.current;
    // Nothing to write, or not yet: a newer revision is known and is merged
    // first (`outOfDate`, or the query past the seeded base for the length of
    // a render), so a save now could only meet a 409.
    if (!enabledRef.current || abandoned.current || !ed || !ed.dirty || ed.outOfDate) return Promise.resolve("skipped");
    if (ed.presentation.revision !== ed.baseRevision) return Promise.resolve("skipped");
    const sent = { id: ed.presentation.id, revision: ed.baseRevision };
    const sentDraft = ed.draft;
    const body = saveBody(ed.presentation, sentDraft, sent.revision);
    const keepalive = !!request?.leaving && keepaliveFits(body);
    changes.current = { first: null, last: null };
    ed.beginSave(sent);
    setSaving(true);
    const run = (async (): Promise<SaveOutcome> => {
      try {
        const saved = await mutateAsync(keepalive ? { ...body, keepalive } : body);
        // The draft this save WROTE travels along: one typed into during the
        // round trip is kept and rebased on the saved deck.
        ed.adoptSaved(saved, sent, sentDraft);
        setFailure(null);
        setRetryAt(null);
        setLastSavedAt(Date.now());
        return "saved";
      } catch (err) {
        const kind = failureKind((err as { status?: number }).status);
        if (kind === "conflict") {
          // A collaborator saved first: their revision is fetched and merged
          // in (`useUpdatePresentation` refetches it), then the save goes again.
          ed.saveRefused(sent);
          setFailure(null);
          return kind;
        }
        const message = err instanceof Error ? err.message : null;
        const retryAfter = (err as { retryAfter?: number }).retryAfter;
        setFailure((previous) => ({
          kind,
          message,
          draft: sentDraft,
          count: previous?.kind === kind ? previous.count + 1 : 1,
          retryAfter,
        }));
        return kind;
      } finally {
        ed.endSave(sent);
        inFlight.current = null;
        setSaving(false);
      }
    })();
    inFlight.current = run;
    return run;
  }, [mutateAsync]);
  const saveRef = useRef(save);
  saveRef.current = save;

  // The timer: a pause in the changes, a long burst, or the next retry.
  useEffect(() => {
    if (!enabled || !dirty || outOfDate || !online || saving) return;
    // A refused right or an ended session: no retry can succeed.
    if (failureIsFinal(failure?.kind)) return;
    if (failure?.kind === "invalid" && failure.draft === draft) return;
    const now = Date.now();
    const retrying = failure?.kind === "transport";
    const delay = retrying
      ? retryDelay(failure.count, failure.retryAfter)
      : autosaveDelay(now, changes.current.last ?? now, changes.current.first ?? now);
    setRetryAt(retrying ? now + delay : null);
    const timer = window.setTimeout(() => void saveRef.current(), delay);
    return () => window.clearTimeout(timer);
  }, [enabled, dirty, draft, outOfDate, online, saving, failure, editor?.baseRevision]);

  // What a refusal says, once per refusal: the status line keeps saying it.
  useEffect(() => {
    if (!failure || failure.count !== 1) return;
    if (failure.kind === "invalid") {
      toast({ title: t_i18n("Your last change could not be saved"), description: failure.message ?? undefined, variant: "error" });
    } else if (failure.kind === "denied") {
      toast({ title: t_i18n("You can no longer edit this presentation"), variant: "error" });
    } else if (failure.kind === "unauthorized") {
      toast({ title: t_i18n("Your session has expired. Sign in again to continue."), variant: "error" });
    }
  }, [failure, t_i18n, toast]);

  // Offline: nothing is sent, the changes are kept; back online, at once.
  useEffect(() => {
    const up = () => {
      setOnline(true);
      setFailure((current) => (current?.kind === "transport" ? null : current));
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);

  // The tab hidden, the page left: saved now, the words of an open text
  // included, in a request that outlives the page.
  useEffect(() => {
    const now = () => editorRef.current?.requestSave({ leaving: true });
    const onVisibility = () => {
      if (document.visibilityState === "hidden") now();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", now);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", now);
    };
  }, []);

  // A tab closed over work not saved yet asks first (the wording is the
  // browser's) - and the save is started all the same.
  useEffect(() => {
    if (!enabled) return;
    const warn = (event: BeforeUnloadEvent) => {
      const handover = activeInlineEditor()?.flush() ?? "clean";
      const ed = editorRef.current;
      if (handover === "clean" && !ed?.dirty && !inFlight.current) return;
      void saveRef.current({ leaving: true });
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [enabled]);

  // `Ctrl+S` on the stage (through `requestSave`, which hands an open text
  // over first) saves now.
  const onSaveRequest = editor?.onSaveRequest;
  useEffect(() => {
    if (!onSaveRequest) return;
    onSaveRequest.current = (request) => void saveRef.current(request);
    return () => {
      onSaveRequest.current = null;
    };
  }, [onSaveRequest]);

  // The viewer unmounted (the router left the deck): what is left is sent.
  useEffect(
    () => () => {
      void saveRef.current();
    },
    [],
  );

  // "Editing" for the others lingers a little after a save: an autosave every
  // few seconds must not make the badge flicker.
  useEffect(() => {
    if (lastSavedAt === null) return;
    setLingering(true);
    const timer = window.setTimeout(() => setLingering(false), EDITING_LINGER_MS);
    return () => window.clearTimeout(timer);
  }, [lastSavedAt]);

  const saveNow = useCallback(() => {
    editorRef.current?.requestSave();
  }, []);

  const flush = useCallback(async (): Promise<boolean> => {
    const deadline = Date.now() + LEAVE_FLUSH_TIMEOUT_MS;
    for (;;) {
      if (activeInlineEditor()?.flush() === "refused") return false;
      // The words handed over, the answer of a save in flight: rendered first.
      await sleep(50);
      const ed = editorRef.current;
      if (!ed) return true;
      await ed.whenSaveSettles();
      await sleep(50);
      const now = editorRef.current;
      if (!now || (!now.dirty && !now.outOfDate)) return true;
      const failed = failureRef.current;
      if (failureIsFinal(failed?.kind) || (failed?.kind === "invalid" && failed.draft === now.draft)) return false;
      if (!navigator.onLine || Date.now() > deadline) return false;
      if (now.outOfDate) {
        await sleep(150);
        continue;
      }
      const outcome = await saveRef.current();
      if (outcome === "invalid" || failureIsFinal(outcome)) return false;
      if (outcome === "transport") await sleep(Math.min(AUTOSAVE_RETRY_BASE_MS, Math.max(0, deadline - Date.now())));
    }
  }, []);

  const discard = useCallback(() => {
    abandoned.current = true;
    setFailure(null);
    editorRef.current?.reload();
  }, []);

  const failureOnDraft = !!failure && failure.draft === draft;
  const state = autosaveState({
    dirty,
    saving,
    outOfDate,
    online,
    failure: failure?.kind ?? null,
    failureOnDraft,
  });
  const status = useMemo<AutosaveStatus>(
    () => ({ state, lastSavedAt, error: failureOnDraft || failure?.kind === "transport" ? (failure?.message ?? null) : null, retryAt }),
    [state, lastSavedAt, failure, failureOnDraft, retryAt],
  );

  return {
    status,
    saveNow,
    flush,
    discard,
    active: enabled && (dirty || saving || lingering || !!editor?.editingText),
  };
}
