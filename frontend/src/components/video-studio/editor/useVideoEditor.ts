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
 * The Video Studio editor's state: the draft, its history, its saves
 * and its live preview.
 *
 * - The draft is the spec the author wrote (`project.spec`), every scene with
 *   an id; each edit is a step of the undo history (a run of keystrokes in one
 *   field is one step, `coalesce`).
 * - Saves are automatic: the draft settles, then goes whole to
 *   `PUT /projects/{id}/spec` (the tools' validator). A spec it refuses keeps
 *   the draft and shows the problems; nothing is lost.
 * - A revision someone else saved (the assistant in the rail, a colleague) is
 *   adopted when there is nothing unsaved here - as an undo step, so "undo"
 *   takes the assistant's edit back too.
 * - The preview is the render's own planner run on the draft
 *   (`POST /projects/{id}/preview`), asked for once the draft settles,
 *   again while a recording, an AI shot or a narration it shows is still being
 *   made, and when a failed shot is generated again or a media URL stopped
 *   answering (`refreshPreview`). Those plans are quiet: only an edit
 *   shows "Updating the preview"; a quiet preview is planned again before its
 *   URLs' tokens run out, and a hidden page waits until it is seen.
 * - A reader of a rendered video plays the render instead: no preview
 *   is asked for (`preview: false`).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { usePreviewVideo, useSaveVideoSpec } from "@/hooks/api/useVideoStudioApi";
import type { VideoPreview, VideoProject } from "@/types";

import {
  ensureSceneIds,
  historyInit,
  historyPush,
  historyRedo,
  historyUndo,
  type EditorSpec,
  type History,
} from "./video-editor-model";

/** How long the draft must settle before it is saved (ms). */
const SAVE_DELAY_MS = 1200;
/** ... and before the preview is planned again (ms). */
const PREVIEW_DELAY_MS = 280;
/** How often a preview showing media still being made is planned again (ms). */
const PENDING_MEDIA_POLL_MS = 5000;
/** ... and one showing a failed shot, which the assistant may generate again (ms). */
const FAILED_MEDIA_POLL_MS = 20000;
/** ... and any other, so the stage always holds media URLs whose tokens (45 min) have time left (ms). */
const TOKEN_REFRESH_MS = 10 * 60 * 1000;
/** Edits of the same field within this window are one undo step (ms). */
const COALESCE_MS = 900;

export type SaveStatus = "saved" | "dirty" | "saving" | "error";

export interface SpecProblem {
  path: string;
  message: string;
}

function problemsOf(err: unknown): SpecProblem[] {
  const detail = (err as { detail?: unknown } | null)?.detail as { problems?: unknown; message?: unknown } | undefined;
  if (detail && Array.isArray(detail.problems)) return detail.problems as SpecProblem[];
  const message = err instanceof Error ? err.message : typeof detail?.message === "string" ? detail.message : "";
  return message ? [{ path: "", message }] : [];
}

const asSpec = (project: VideoProject): EditorSpec => ensureSceneIds(structuredClone(project.spec) as EditorSpec);

export function useVideoEditor(project: VideoProject | undefined, { preview: previewWanted = true }: { preview?: boolean } = {}) {
  const canEdit = !!project?.can_manage;
  const [history, setHistory] = useState<History<EditorSpec> | null>(null);
  const [status, setStatus] = useState<SaveStatus>("saved");
  const [problems, setProblems] = useState<SpecProblem[]>([]);
  const baseRevision = useRef<number | null>(null);
  const statusRef = useRef(status);
  statusRef.current = status;
  const lastEdit = useRef<{ key: string; at: number } | null>(null);
  const save = useSaveVideoSpec(project?.id);
  const { mutateAsync: saveSpec } = save;

  // The first read, then the revisions someone else saved.
  useEffect(() => {
    if (!project) return;
    if (baseRevision.current === null) {
      baseRevision.current = project.revision;
      setHistory(historyInit(asSpec(project)));
      return;
    }
    if (project.revision <= baseRevision.current) return;
    baseRevision.current = project.revision;
    if (statusRef.current === "saving") return; // our own save landing
    if (statusRef.current === "dirty") return; // ours wins on the next save; theirs stays one undo away
    setHistory((h) => (h ? historyPush(h, asSpec(project)) : historyInit(asSpec(project))));
  }, [project]);

  const draft = history?.present ?? null;

  /** Change the draft; `coalesce` folds a run of edits of one field into one undo step. */
  const edit = useCallback(
    (change: (spec: EditorSpec) => EditorSpec, coalesce?: string) => {
      if (!canEdit) return;
      const now = Date.now();
      const replace = !!coalesce && lastEdit.current?.key === coalesce && now - lastEdit.current.at < COALESCE_MS;
      lastEdit.current = coalesce ? { key: coalesce, at: now } : null;
      setHistory((h) => {
        if (!h) return h;
        const next = change(h.present);
        if (next === h.present) return h;
        return historyPush(h, next, replace);
      });
      setStatus("dirty");
    },
    [canEdit],
  );
  const undo = useCallback(() => {
    if (!canEdit) return;
    lastEdit.current = null;
    setHistory((h) => (h && h.past.length ? historyUndo(h) : h));
    setStatus("dirty");
  }, [canEdit]);
  const redo = useCallback(() => {
    if (!canEdit) return;
    lastEdit.current = null;
    setHistory((h) => (h && h.future.length ? historyRedo(h) : h));
    setStatus("dirty");
  }, [canEdit]);

  /** Save the draft now; `false` when the validator refused it (the problems say why). */
  const saveNow = useCallback(async (): Promise<boolean> => {
    if (!draft || !canEdit) return true;
    setStatus("saving");
    try {
      const saved = await saveSpec(draft as unknown as Record<string, unknown>);
      baseRevision.current = Math.max(baseRevision.current ?? 0, saved.revision);
      setProblems([]);
      // An edit made while the save was on its way is still to save.
      setStatus((current) => (current === "saving" ? "saved" : current));
      return true;
    } catch (err) {
      setProblems(problemsOf(err));
      setStatus("error");
      return false;
    }
  }, [canEdit, draft, saveSpec]);

  useEffect(() => {
    if (status !== "dirty") return;
    const timer = window.setTimeout(() => void saveNow(), SAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [status, saveNow, draft]);

  // Leaving with an unsaved edit asks first.
  useEffect(() => {
    if (status !== "dirty" && status !== "saving") return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [status]);

  /* ---------- Preview ---------- */
  const previewMutation = usePreviewVideo(project?.id);
  const { mutateAsync: requestPreview } = previewMutation;
  const [preview, setPreview] = useState<VideoPreview | null>(null);
  const [previewProblems, setPreviewProblems] = useState<SpecProblem[]>([]);
  const [previewing, setPreviewing] = useState(false);
  const requestSeq = useRef(0);
  const [pollTick, setPollTick] = useState(0);
  // The draft the last plan was asked for: the same one again is a quiet plan (a poll, a lost URL).
  const plannedDraft = useRef<EditorSpec | null>(null);

  const projectId = project?.id;
  useEffect(() => {
    if (!draft || !projectId || !previewWanted) return;
    const seq = ++requestSeq.current;
    const quiet = plannedDraft.current === draft;
    const timer = window.setTimeout(async () => {
      plannedDraft.current = draft;
      if (!quiet) setPreviewing(true);
      try {
        // A reader who cannot edit previews the video as stored.
        const next = await requestPreview(canEdit ? (draft as unknown as Record<string, unknown>) : null);
        if (seq !== requestSeq.current) return;
        setPreview(next);
        setPreviewProblems([]);
      } catch (err) {
        if (seq !== requestSeq.current) return;
        setPreviewProblems(problemsOf(err));
      } finally {
        if (seq === requestSeq.current) setPreviewing(false);
      }
    }, PREVIEW_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [canEdit, draft, previewWanted, projectId, requestPreview, pollTick]);

  // A shot or a recording being made is polled until it is ready; a failed one
  // more slowly (the assistant may generate it again from the rail), and at
  // once when the editor asks for a new attempt (`refreshPreview`).
  const mediaPoll = useMemo(() => {
    const scenes = preview?.document.scenes ?? [];
    const states: (string | undefined)[] = scenes.map((s) => s.preview_state?.state);
    // An AI music track being composed, a narration being spoken and a figure's still
    // or build being drawn are media being made too.
    states.push(preview?.music?.state?.state);
    states.push(...(preview?.narration ?? []).map((line) => line.state));
    if (scenes.some((s) => s.preview_state?.drawing || s.preview_building)) return PENDING_MEDIA_POLL_MS;
    // A shot generated again plays its previous take meanwhile: watched until its new take lands.
    if ((preview?.regenerating ?? []).length) return PENDING_MEDIA_POLL_MS;
    if (states.some((s) => s === "recording" || s === "clip" || s === "music" || s === "speaking")) return PENDING_MEDIA_POLL_MS;
    if (states.includes("failed")) return FAILED_MEDIA_POLL_MS;
    return preview ? TOKEN_REFRESH_MS : null;
  }, [preview]);
  useEffect(() => {
    if (!mediaPoll) return;
    let missed = false;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") {
        missed = true;
        return;
      }
      setPollTick((n) => n + 1);
    }, mediaPoll);
    const seen = () => {
      if (document.visibilityState === "hidden" || !missed) return;
      missed = false;
      setPollTick((n) => n + 1);
    };
    document.addEventListener("visibilitychange", seen);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", seen);
    };
  }, [mediaPoll]);
  const refreshPreview = useCallback(() => setPollTick((n) => n + 1), []);

  return {
    draft,
    canEdit,
    edit,
    undo,
    redo,
    canUndo: !!history?.past.length,
    canRedo: !!history?.future.length,
    status,
    problems,
    saveNow,
    preview,
    previewProblems,
    previewing,
    refreshPreview,
  };
}
