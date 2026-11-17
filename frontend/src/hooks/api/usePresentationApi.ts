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

import { useEffect, useMemo, useRef } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { API_BASE, api, filenameFromContentDisposition } from "@/lib/api";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { saveBlobAs } from "@/lib/utils";
import { newestDeck, shareNewestDeck } from "@/lib/deck-revision";
import type {
  DeckPollsResponse,
  LibraryPage,
  Presentation,
  PresentationComment,
  PresentationCommentCreate,
  PresentationCommentDraftRequest,
  PresentationCommentDraftResponse,
  PresentationCommentUpdate,
  PresentationExportFormat,
  PresentationImportResult,
  PresentationLayoutCheck,
  PresentationPresence,
  PresentationPresenceEntry,
  PresentationPublicLink,
  PresentationSummary,
  PresentationsQuery,
  PresentationUpdate,
  PresentationVersion,
  PublicPresentation,
  SlideLayoutSwitchRequest,
  SlideLayoutSwitchResult,
} from "@/types";

/** Cache key prefix for every deck query. */
export const PRESENTATIONS_KEY = ["presentations"] as const;

/**
 * Presence heartbeat cadence — `PRESENCE_HEARTBEAT_SECONDS` in
 * `backend/app/services/presentation_presence.py`. The server expires an entry
 * after three missed beats; a client beating faster than this would only
 * hammer Redis for the same "who is here" answer.
 * @public
 */
export const PRESENCE_HEARTBEAT_MS = 15_000;

/**
 * The beat while someone edits the deck: fast enough that a
 * collaborator's autosaved change and their selection show within a few
 * seconds, slow enough that a room of editors costs a request each every few
 * seconds. Well inside the server's TTL (three standard beats).
 */
const PRESENCE_FAST_HEARTBEAT_MS = 2_500;

/** How long a new selection settles before it is announced (Tab walking blocks sends one beat). */
const PRESENCE_SELECTION_SETTLE_MS = 500;

/**
 * How long a flip of the studio's in-flight state waits before it is announced.
 * The rail's `isSending` turns on the moment the message leaves the
 * composer, while the server acquires the chat turn marker the heartbeat reads
 * AFTER its `thinking` ack — a beat fired on the same tick would read "no
 * turn" and the announcement would wait for the next scheduled beat instead.
 * Long enough for the marker to land, short enough that the badge still turns
 * on within a couple of seconds; the trailing edge waits the same, so a turn
 * that ends and restarts within the window is one announcement, not three.
 * @public
 */
export const PRESENCE_ANNOUNCE_SETTLE_MS = 1_500;

/** Fetch one deck (spec + resolved template theme overrides).
 *
 *  Decks are written by the agent-facing `generate_presentation` tool, which
 *  validates the spec first, and by the light manual controls behind
 *  `useUpdatePresentation` — which send validated PIECES (title, slides,
 *  sharing), never a raw spec. An edit arrives as a new `revision` on
 *  the same id — the chat marker never changes, and the chat invalidates
 *  `PRESENTATIONS_KEY` when a deck tool returns so a mounted card picks the new
 *  revision up (`refreshAfterToolDone`).
 *
 *  `live` polls the deck while it is on screen: a shared deck is edited
 *  by collaborators and THEIR agents from other sessions, which no stream of
 *  ours observes. The viewer turns it on; structural sharing keeps the deck
 *  reference stable while nothing changed, so the poll costs no re-render -
 *  and keeps the NEWEST revision whatever order the answers land in
 *  (`shareNewestDeck`): a read sent before a save committed and
 *  answered after it never puts the pre-save deck back.
 *
 *  `throwOnError` is disabled deliberately: the app-wide default throws every
 *  query error to the nearest `ErrorBoundary`, which for a deck card embedded in
 *  a chat message means one deleted deck would replace the whole conversation
 *  with an error page. The card renders its own "no longer available" state
 *  instead.
 */
export function usePresentation(id: string | undefined, options: { live?: boolean } = {}) {
  return useQuery<Presentation>({
    queryKey: [...PRESENTATIONS_KEY, id],
    queryFn: () => api.get(`/presentations/${id}`),
    enabled: !!id,
    throwOnError: false,
    refetchInterval: options.live ? REFETCH_INTERVAL_STANDARD : false,
    refetchIntervalInBackground: false,
    structuralSharing: shareNewestDeck,
  });
}

/**
 * Server-side export of a deck: the platform renders the deck in its
 * own headless browser and streams back a PDF or a PPTX. Resolves with the
 * blob and the filename the server chose (`<slugified title>.<ext>`).
 */
export function useExportPresentation() {
  return useMutation({
    mutationFn: ({ id, format }: { id: string; format: PresentationExportFormat }) =>
      api.postDownload(`/presentations/${id}/export`, { format }),
  });
}

/**
 * Layout check of a deck: the platform renders the deck in the same
 * headless browser the export uses and measures every slide — a body shrunk to
 * fit, text cut by its box, a label running past its line, anything painted
 * off the canvas. A mutation rather than a query because it costs a render
 * (seconds, not milliseconds) and is asked for explicitly; the result is kept
 * by the caller for the revision it describes. A `skipped` report is a
 * successful answer ("could not measure"), not an error.
 */
export function useCheckPresentationLayout() {
  return useMutation({
    mutationFn: (id: string) => api.post<PresentationLayoutCheck>(`/presentations/${id}/layout-check`),
  });
}

function listQuery(query: PresentationsQuery): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/**
 * A page of the decks and pages visible to the current user (own + shared
 * with them + company-managed), the last changed first. The server
 * searches, filters and counts the whole library, so every deck stays
 * reachable; the previous page stays on screen while the next one loads.
 *
 * `inlineError`: the caller draws the read error itself (the library's table shows its
 * error line with Retry under the toolbar), so the error is not thrown to the page's
 * boundary, which would replace the whole screen (UX r1 B2 on #11075).
 */
export function usePresentations(
  query: PresentationsQuery,
  { enabled = true, inlineError = false }: { enabled?: boolean; inlineError?: boolean } = {},
) {
  return useQuery<LibraryPage<PresentationSummary>>({
    queryKey: [...PRESENTATIONS_KEY, "list", query],
    queryFn: () => api.get(`/presentations${listQuery(query)}`),
    placeholderData: keepPreviousData,
    enabled,
    ...(inlineError ? { throwOnError: false } : {}),
  });
}

/**
 * Light manual controls + sharing — `PATCH /presentations/{id}`.
 *
 * The response is the deck as written, so it replaces the cached deck directly
 * (no refetch round-trip between "Save" and the slides updating) - unless the
 * cache already holds a newer revision, which a late answer never rolls back
 * (`newestDeck`) - and the list is invalidated for the title / sharing
 * badges. A 409 (`expected_revision`
 * stale) refetches the deck at once: the revision that won is what the
 * editor merges its draft onto, and until the next poll the cache
 * still holds the revision the server just refused. The reaction itself is
 * left to the caller - it is UI-specific.
 *
 * Every caller (the editor's autosave, the share dialog) reports failures in
 * its own words, so the app-wide "Error" toast is suppressed here — otherwise a
 * 422 naming the offending slides appears twice, once under each title.
 */
export function useUpdatePresentation() {
  const queryClient = useQueryClient();
  return useMutation({
    // `keepalive`: the editor's save as the page goes away.
    mutationFn: ({ id, keepalive, ...body }: PresentationUpdate & { id: string; keepalive?: boolean }) =>
      api.patch<Presentation>(`/presentations/${id}`, body, { keepalive }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (deck) => {
      queryClient.setQueryData<Presentation>([...PRESENTATIONS_KEY, deck.id], (cached) => newestDeck(cached, deck));
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, "list"] });
    },
    onError: (error, { id }) => {
      if ((error as { status?: number }).status !== 409) return;
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, id], exact: true });
    },
  });
}

const publicLinkKey = (id: string | undefined) => [...PRESENTATIONS_KEY, id, "public-link"] as const;

/**
 * A deck's public link - its managers only, so the Share dialog asks
 * for it while it is open on a deck the user may manage (`enabled`); a reader
 * would get a 403. Its own titled states, so no error reaches a boundary.
 */
export function usePresentationPublicLink(id: string | undefined, enabled: boolean) {
  return useQuery<PresentationPublicLink>({
    queryKey: publicLinkKey(id),
    queryFn: () => api.get(`/presentations/${id}/public-link`),
    enabled: !!id && enabled,
    throwOnError: false,
  });
}

/**
 * Turn a deck's public link on (or hand back the one it has), or issue it a
 * new one (`regenerate`: the previous URL stops working at once). The deck
 * and the library are refreshed for their `has_public_link` badge; failures
 * are the Share dialog's own toast.
 */
export function useEnablePresentationPublicLink() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, regenerate = false }: { id: string; regenerate?: boolean }) =>
      api.post<PresentationPublicLink>(`/presentations/${id}/public-link`, { regenerate }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (link, { id }) => {
      queryClient.setQueryData(publicLinkKey(id), link);
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, id], exact: true });
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, "list"] });
    },
  });
}

/** Turn a deck's public link off: whoever opens it is told the presentation is not available. */
export function useDisablePresentationPublicLink() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/presentations/${id}/public-link`),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (_void, id) => {
      queryClient.setQueryData<PresentationPublicLink>(publicLinkKey(id), (cached) => ({
        available: cached?.available ?? true,
        enabled: false,
        path: null,
        url: null,
        created_at: null,
        created_by_name: null,
        allow_pdf_download: false,
      }));
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, id], exact: true });
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, "list"] });
    },
  });
}

/**
 * What a deck's public link offers besides the slides -
 * `PATCH /presentations/{id}/public-link`: its PDF download. Applies at once,
 * like the link's own switch; failures are the Share dialog's own toast.
 */
export function useUpdatePresentationPublicLink() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, allowPdfDownload }: { id: string; allowPdfDownload: boolean }) =>
      api.patch<PresentationPublicLink>(`/presentations/${id}/public-link`, { allow_pdf_download: allowPdfDownload }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (link, { id }) => {
      queryClient.setQueryData(publicLinkKey(id), link);
    },
  });
}

/** Why a public deck could not be loaded: the link is dead (404), or the read failed. */
export class PublicPresentationError extends Error {
  constructor(readonly status: number) {
    super(`The presentation could not be loaded (HTTP ${status}).`);
  }
}

/**
 * The deck a public link opens - `/public/presentations/{token}`, for
 * a visitor with no session. A plain `fetch` on purpose: `api.ts` attaches the
 * session token and sends a 401 to the login page, and this page has neither.
 * A dead link (404) is not retried: it will not come back by itself.
 */
export function usePublicPresentation(token: string | undefined) {
  return useQuery<PublicPresentation, PublicPresentationError>({
    queryKey: ["public-presentation", token],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/public/presentations/${encodeURIComponent(token ?? "")}`, {
        credentials: "omit",
      });
      if (!res.ok) throw new PublicPresentationError(res.status);
      return (await res.json()) as PublicPresentation;
    },
    enabled: !!token,
    throwOnError: false,
    retry: (failures, error) => error.status !== 404 && failures < 2,
    refetchOnWindowFocus: false,
  });
}

/**
 * The PDF a public link offers - `GET /public/presentations/{token}/pdf`,
 * fetched with no session like the deck itself and saved under the name the
 * server chose. Rendering takes seconds; the caller shows that it is working.
 * Rejects with the HTTP status (`PublicPresentationError`): 404 when the
 * link no longer offers it, 429 past a budget, 502 / 503 when the platform
 * cannot render right now.
 */
export function useDownloadPublicPresentationPdf() {
  return useMutation<void, PublicPresentationError, string>({
    mutationFn: async (token) => {
      const res = await fetch(`${API_BASE}/public/presentations/${encodeURIComponent(token)}/pdf`, {
        credentials: "omit",
      });
      if (!res.ok) throw new PublicPresentationError(res.status);
      const filename = filenameFromContentDisposition(res.headers.get("content-disposition")) ?? "presentation.pdf";
      saveBlobAs(await res.blob(), filename);
    },
    meta: { suppressGlobalErrorToast: true },
  });
}

/**
 * Rebuild ONE slide as another layout, server-side and deterministically -
 * the editor panel's per-slide layout picker. Nothing is written:
 * the converted slide lands in the panel's draft, previewed on the stage, and
 * the save is `useUpdatePresentation` with its `expected_revision`, so a
 * conversion that is not what the person wanted costs a Discard, never a
 * revision. The 422 (a slide that cannot honestly become the target) carries
 * the reason as `detail`; the panel shows it in its own toast.
 */
export function useSwitchSlideLayout() {
  return useMutation({
    mutationFn: ({ presentationId, ...body }: SlideLayoutSwitchRequest & { presentationId: string }) =>
      api.post<SlideLayoutSwitchResult>(`/presentations/${presentationId}/slide-layout`, body),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Delete a deck (owner, `config` grantee or admin). */
export function useDeletePresentation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/presentations/${id}`),
    onSuccess: (_void, id) => {
      queryClient.removeQueries({ queryKey: [...PRESENTATIONS_KEY, id] });
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, "list"] });
    },
  });
}

/**
 * Copy a deck the user can read into a new deck they own — how a read-only
 * grantee gets a variant of a shared deck without asking for edit rights.
 */
export function useDuplicatePresentation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, title }: { id: string; title?: string }) =>
      api.post<Presentation>(`/presentations/${id}/duplicate`, title ? { title } : undefined),
    onSuccess: (copy) => {
      queryClient.setQueryData([...PRESENTATIONS_KEY, copy.id], copy);
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, "list"] });
    },
  });
}

// ── Portable packages ────────────────────────────────────────────────

/**
 * Download a deck as a portable JSON package (`<title>.presentation.json`):
 * the spec, the template by slug / name, and every referenced file with its
 * bytes embedded — everything another install needs to rebuild the deck.
 * Resolves with the blob and the server-chosen filename.
 */
export function useExportPresentationPackage() {
  return useMutation({
    mutationFn: (id: string) => api.download(`/presentations/${id}/package`),
  });
}

/**
 * Re-create a deck from a package written by `useExportPresentationPackage`
 * (or from a bare `PresentationSpec`). Multipart, because the package carries
 * embedded assets. The response is the new deck plus an `import_report`
 * saying what was matched, what was copied and which template was used.
 */
export function useImportPresentation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, title }: { file: File; title?: string }) => {
      const formData = new FormData();
      formData.append("file", file);
      if (title) formData.append("title", title);
      return api.postForm<PresentationImportResult>("/presentations/import", formData);
    },
    onSuccess: (deck) => {
      queryClient.setQueryData([...PRESENTATIONS_KEY, deck.id], deck);
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, "list"] });
    },
  });
}

// ── Collaboration: revision history ──────────────────────────────────

/**
 * A deck's revision history, newest first — headlines only (no spec).
 *
 * The deck's current `revision` is part of the key: every save (ours, a
 * collaborator's, an agent's) bumps it on the polled deck, which is exactly
 * when the list has a new entry, so the history refreshes without polling of
 * its own and never lags the deck it describes.
 */
export function usePresentationVersions(id: string | undefined, revision: number | undefined) {
  return useQuery<PresentationVersion[]>({
    queryKey: [...PRESENTATIONS_KEY, id, "versions", revision ?? 0],
    queryFn: () => api.get(`/presentations/${id}/versions`),
    enabled: !!id,
    throwOnError: false,
  });
}

/**
 * One revision WITH its spec — what the history panel previews. A stored
 * version never changes, so it is cached for the session.
 */
export function usePresentationVersion(id: string | undefined, revision: number | null) {
  return useQuery<PresentationVersion>({
    queryKey: [...PRESENTATIONS_KEY, id, "version", revision],
    queryFn: () => api.get(`/presentations/${id}/versions/${revision}`),
    enabled: !!id && revision !== null,
    staleTime: Infinity,
    throwOnError: false,
  });
}

/**
 * Bring a past revision back as a NEW revision (managers only). The response is
 * the deck as restored and replaces the cached deck (never a newer one it is
 * answered after, `newestDeck`), which bumps `revision` and so refreshes the
 * history list through its key.
 */
export function useRestorePresentationVersion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, revision }: { id: string; revision: number }) =>
      api.post<Presentation>(`/presentations/${id}/versions/${revision}/restore`),
    onSuccess: (deck) => {
      queryClient.setQueryData<Presentation>([...PRESENTATIONS_KEY, deck.id], (cached) => newestDeck(cached, deck));
      void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, "list"] });
    },
  });
}

// ── Collaboration: review comments ───────────────────────────────────

const commentsKey = (id: string | undefined) => [...PRESENTATIONS_KEY, id, "comments"] as const;

/**
 * Every comment on a deck, oldest first; threads are rebuilt by the panel.
 * `live` polls while the panel is open — reviewers comment from their own
 * sessions, which no stream of ours observes.
 */
/** The live polls of a deck (xtm-one #10157): read every 2 s while `enabled`, so a vote lands on the slide in under 2 s. */
const POLL_REFETCH_MS = 2000;

export function useDeckPolls(id: string | undefined, enabled: boolean) {
  return useQuery<DeckPollsResponse>({
    queryKey: [...PRESENTATIONS_KEY, id, "polls"],
    queryFn: () => api.get(`/presentations/${id}/polls`),
    enabled: !!id && enabled,
    throwOnError: false,
    refetchInterval: enabled ? POLL_REFETCH_MS : false,
    refetchIntervalInBackground: true,
  });
}

export function useControlDeckPoll(id: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ pollId, action, questionId }: { pollId: string; action: string; questionId?: string }) =>
      api.post(`/presentations/${id}/polls/${pollId}/control`, { action, question_id: questionId ?? null }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, id, "polls"] }),
  });
}

export function usePresentationComments(id: string | undefined, options: { live?: boolean } = {}) {
  return useQuery<PresentationComment[]>({
    queryKey: commentsKey(id),
    queryFn: () => api.get(`/presentations/${id}/comments`),
    enabled: !!id,
    throwOnError: false,
    refetchInterval: options.live ? REFETCH_INTERVAL_STANDARD : false,
    refetchIntervalInBackground: false,
  });
}

/**
 * After any comment write: the thread list, the deck (its `open_comment_count`
 * feeds the toolbar badge) and the library (same badge on the card).
 */
function useInvalidateComments() {
  const queryClient = useQueryClient();
  return (presentationId: string) => {
    void queryClient.invalidateQueries({ queryKey: commentsKey(presentationId) });
    void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, presentationId], exact: true });
    void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, "list"] });
  };
}

/** Leave a note on a slide, on the deck, or reply in a thread (anyone who can read). */
export function useCreatePresentationComment() {
  const invalidate = useInvalidateComments();
  return useMutation({
    mutationFn: ({ presentationId, ...body }: PresentationCommentCreate & { presentationId: string }) =>
      api.post<PresentationComment>(`/presentations/${presentationId}/comments`, body),
    onSuccess: (_comment, { presentationId }) => invalidate(presentationId),
  });
}

/** Edit a comment's text and / or resolve / reopen its thread (author or deck manager). */
export function useUpdatePresentationComment() {
  const invalidate = useInvalidateComments();
  return useMutation({
    mutationFn: ({
      presentationId,
      commentId,
      ...body
    }: PresentationCommentUpdate & { presentationId: string; commentId: string }) =>
      api.patch<PresentationComment>(`/presentations/${presentationId}/comments/${commentId}`, body),
    onSuccess: (_comment, { presentationId }) => invalidate(presentationId),
  });
}

/**
 * "Generate with AI" in the comment composer: the brief and the anchor
 * go up, the drafted note comes back for the person to edit and post. Nothing
 * is stored, so no query is invalidated.
 */
export function useDraftPresentationComment() {
  return useMutation({
    mutationFn: ({ presentationId, ...body }: PresentationCommentDraftRequest & { presentationId: string }) =>
      api.post<PresentationCommentDraftResponse>(`/presentations/${presentationId}/comments/draft`, body),
  });
}

/** Delete a comment (author or deck manager); deleting a root takes its replies. */
export function useDeletePresentationComment() {
  const invalidate = useInvalidateComments();
  return useMutation({
    mutationFn: ({ presentationId, commentId }: { presentationId: string; commentId: string }) =>
      api.delete<void>(`/presentations/${presentationId}/comments/${commentId}`),
    onSuccess: (_void, { presentationId }) => invalidate(presentationId),
  });
}

// ── Collaboration: presence ──────────────────────────────────────────

/**
 * "I have this deck open" — and who else does.
 *
 * A heartbeat every `PRESENCE_HEARTBEAT_MS` (`POST /presence`, which returns
 * the room), carrying what the caller is doing: `editing` while the manual
 * editor holds UNSAVED edits, and the slide on screen. Both are read from a
 * ref at beat time so a presenter walking forty slides does not emit forty
 * requests; a flip of `editing` beats at once, because a collaborator wants to
 * know that now. The beat keeps going while the tab is unfocused — a
 * presenter's deck on a second screen is exactly the one that is "open" — and
 * the room is left explicitly on unmount rather than after the TTL.
 *
 * `assistantBusy` is the studio rail's in-flight turn. It is NOT sent:
 * whether the caller's assistant is working on the deck is the server's own
 * reading of the chat turn marker, so it can neither be claimed by a client
 * nor get stuck in one. The flag only decides WHEN to beat — a flip schedules
 * a beat after `PRESENCE_ANNOUNCE_SETTLE_MS`, so the badge follows the turn
 * within seconds instead of waiting for the next scheduled beat. An open rail
 * with nothing in flight changes nothing: that is viewing.
 *
 * While someone edits the beat is FAST (`PRESENCE_FAST_HEARTBEAT_MS`,
 * the tab visible): the caller's own editor is open (`fast`), or another
 * reader's entry says they are in the editor (an agent turn is not that). The
 * server budgets the beats per person and deck. It then also says where the caller
 * works - the slide's stable id and the selected block (`slideId`, `block`),
 * drawn on the others' stages - and the room answers the deck's current
 * revision: a newer one than the cached deck is refetched at once, so a
 * collaborator's save reaches the editor (and its merge) within seconds
 * instead of the 15 s deck poll.
 *
 * Fail-open by design (the server returns an empty room when Redis is down),
 * so the hook never throws into a boundary: presence is a courtesy, not a lock.
 * Returns the OTHER people in the room; the caller is never shown to themself.
 */
export function usePresentationPresence(
  id: string | undefined,
  {
    enabled = true,
    editing = false,
    assistantBusy = false,
    slideIndex,
    slideId = null,
    block = null,
    fast = false,
  }: {
    enabled?: boolean;
    editing?: boolean;
    assistantBusy?: boolean;
    slideIndex?: number;
    slideId?: string | null;
    block?: string | null;
    fast?: boolean;
  } = {},
): PresentationPresenceEntry[] {
  const queryClient = useQueryClient();
  const latest = useRef({ editing, slideIndex, slideId, block });
  latest.current = { editing, slideIndex, slideId, block };
  const active = !!id && enabled;
  const fastRef = useRef(fast);

  const query = useQuery<PresentationPresence>({
    queryKey: [...PRESENTATIONS_KEY, id, "presence"],
    queryFn: async () => {
      const room = await api.post<PresentationPresence>(`/presentations/${id}/presence`, {
        editing: latest.current.editing,
        slide_index: latest.current.slideIndex ?? null,
        slide_id: latest.current.slideId,
        block: latest.current.block,
      });
      // The deck moved on (a collaborator, an agent): read it now.
      const cached = queryClient.getQueryData<Presentation>([...PRESENTATIONS_KEY, id]);
      if (cached && typeof room.revision === "number" && room.revision > cached.revision) {
        void queryClient.invalidateQueries({ queryKey: [...PRESENTATIONS_KEY, id], exact: true });
      }
      return room;
    },
    enabled: active,
    throwOnError: false,
    retry: false,
    refetchInterval: (current) => {
      // Someone in the EDITOR, never an agent turn: an assistant's edit lands
      // at the deck's own poll, and a long turn must not speed every reader up.
      const someoneEditing = current.state.data?.viewers.some((entry) => !entry.is_self && entry.activity === "editing");
      const visible = typeof document === "undefined" || document.visibilityState === "visible";
      return (fastRef.current || someoneEditing) && visible ? PRESENCE_FAST_HEARTBEAT_MS : PRESENCE_HEARTBEAT_MS;
    },
    refetchIntervalInBackground: true,
    // Every beat is a fresh statement; never serve a stale room from cache.
    staleTime: 0,
    gcTime: 0,
  });

  // A change of activity is announced at once instead of on the next beat.
  // Only a CHANGE: the query's own first fetch covers mount and (re)activation.
  const { refetch } = query;
  const announced = useRef(editing);
  useEffect(() => {
    if (announced.current === editing) return;
    announced.current = editing;
    if (active) void refetch();
  }, [editing, active, refetch]);

  // The editor opening switches to the fast beat now, not after the slow one.
  useEffect(() => {
    if (fastRef.current === fast) return;
    fastRef.current = fast;
    if (active && fast) void refetch();
  }, [fast, active, refetch]);

  // A new selection is announced once it settles: walking the blocks with
  // Tab must not send a beat per block.
  const announcedAt = useRef(`${slideId}|${block}`);
  useEffect(() => {
    const where = `${slideId}|${block}`;
    if (!active || !fast || announcedAt.current === where) return;
    const timer = window.setTimeout(() => {
      announcedAt.current = where;
      void refetch();
    }, PRESENCE_SELECTION_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [slideId, block, active, fast, refetch]);

  // The studio turn is announced after a settle delay (see the constant): the
  // beat reads the server's marker, which lands after the rail's optimistic
  // `isSending`. A flip back before the timer fires replaces it — one beat,
  // reading whatever the server says by then.
  const announcedBusy = useRef(assistantBusy);
  useEffect(() => {
    if (announcedBusy.current === assistantBusy) return;
    announcedBusy.current = assistantBusy;
    if (!active) return;
    const timer = window.setTimeout(() => void refetch(), PRESENCE_ANNOUNCE_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [assistantBusy, active, refetch]);

  // Leave the room when the deck is closed (best effort — the TTL is the net).
  useEffect(() => {
    if (!active) return;
    return () => {
      void api.delete<void>(`/presentations/${id}/presence`).catch(() => undefined);
    };
  }, [id, active]);

  const viewers = query.data?.viewers;
  return useMemo(() => (viewers ? viewers.filter((entry) => !entry.is_self) : []), [viewers]);
}
