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
 * Video Studio API hooks - `/api/v1/video-studio`.
 *
 * Recordings, clips and renders run in the worker: the lists poll while one of
 * their rows is still pending or running, and stop polling once everything has
 * finished. The libraries come a page at a time, searched and filtered by the
 * server: a query names its page, and the previous page stays on
 * screen while the next one loads. Media play through a confined token
 * (`useVideoMediaUrl`): a `<video>` element cannot send the session header and
 * asks for byte ranges by itself, so the page asks the server for a URL bound
 * to one file.
 */
import { useCallback } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { API_BASE, api, isNotFoundError } from "@/lib/api";
import type {
  PronunciationEntry,
  PronunciationLexicon,
  PronunciationLexiconVersion,
  PublicVideo,
  SpokenPronunciation,
  VideoAsset,
  VideoAssetsBulkDelete,
  VideoAssetsBulkDeleted,
  VideoAssetsQuery,
  VideoClip,
  VideoClipCreate,
  VideoClipRetry,
  VideoJobStatus,
  VideoLibraryPage,
  VideoMediaAsset,
  VideoMediaDeletion,
  VideoMediaKind,
  VideoMediaToken,
  VideoMusic,
  VideoMusicCreate,
  VideoMusicTrack,
  VideoPreview,
  VideoProject,
  VideoProjectsQuery,
  VideoPublicLink,
  VideoQualityReport,
  VideoRender,
  VideoRenderCleanupRequest,
  VideoRenderCleanupResult,
  VideoRenderGateReason,
  VideoRenderQuality,
  VideoRenderReplaceQuestion,
  VideoRenderRetention,
  VideoRenderRetentionSettings,
  VideoRenderStarted,
  VideoTake,
  VideoTemplateSummary,
  VideoUploadedFile,
} from "@/types";

const VIDEO_STUDIO_KEY = ["video-studio"] as const;
const LIVE_POLL_MS = 3_000;

const unfinished = (status: VideoJobStatus | undefined | null) => status === "pending" || status === "running";

function anyRunning<T>(items: T[] | undefined, statusOf: (item: T) => VideoJobStatus | undefined | null) {
  return !!items?.some((item) => unfinished(statusOf(item)));
}

/**
 * The query string of a library page: an unset value is left out, a list is
 * comma-separated - an empty one kept, so `ids: []` matches nothing.
 */
function libraryQuery(query: object): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, Array.isArray(value) ? value.join(",") : String(value));
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/** The pages of the video library, under the single videos' key: a save refreshes them without touching an open video. */
export const PROJECT_PAGES_KEY = [...VIDEO_STUDIO_KEY, "projects", "pages"] as const;

/**
 * A page of the videos the user can open; polls while one of its videos is being rendered.
 * `inlineError`: the caller draws the read error itself (the library's table shows its error
 * line with Retry), so the error is not thrown to the page's boundary.
 */
export function useVideoProjects(query: VideoProjectsQuery, enabled = true, { inlineError = false }: { inlineError?: boolean } = {}) {
  return useQuery<VideoLibraryPage<VideoProject>>({
    queryKey: [...PROJECT_PAGES_KEY, query],
    queryFn: () => api.get(`/video-studio/projects${libraryQuery(query)}`),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (anyRunning(q.state.data?.items, (p) => p.latest_render?.status) ? LIVE_POLL_MS : false),
    enabled,
    ...(inlineError ? { throwOnError: false } : {}),
  });
}

/** One project; polls while its newest render has not finished. `throwOnError` off: a chat card renders its own gone state. */
export function useVideoProject(id: string | undefined) {
  return useQuery<VideoProject>({
    queryKey: [...VIDEO_STUDIO_KEY, "projects", id],
    queryFn: () => api.get(`/video-studio/projects/${id}`),
    enabled: !!id,
    throwOnError: false,
    retry: false,
    refetchInterval: (query) => (unfinished(query.state.data?.latest_render?.status) ? LIVE_POLL_MS : false),
  });
}

/**
 * The quality check of a saved revision - what covers what, what does
 * not fit or is read too fast; the check the agent reads after a save. Keyed
 * by revision and by the preview's `media_key`: a revision's layout
 * never changes, but its picture findings change with its media - a shot
 * generated again in place changes no revision. And by the check's own
 * `version` (the project's `quality_version`): a release that changes
 * what the check finds reads the revision's report again.
 */
export function useVideoQuality(
  projectId: string | undefined,
  revision: number | undefined,
  mediaKey: string | undefined,
  version: number | undefined,
  enabled = true,
) {
  return useQuery<VideoQualityReport>({
    queryKey: [...VIDEO_STUDIO_KEY, "quality", projectId, revision, mediaKey ?? null, version ?? null],
    queryFn: () => api.get(`/video-studio/projects/${projectId}/quality`),
    enabled: !!projectId && revision !== undefined && enabled,
    staleTime: Infinity,
    retry: false,
  });
}

export function useVideoRenders(projectId: string | undefined) {
  return useQuery<VideoRender[]>({
    queryKey: [...VIDEO_STUDIO_KEY, "renders", projectId],
    queryFn: async () => (await api.get<{ items: VideoRender[] }>(`/video-studio/projects/${projectId}/renders`)).items,
    enabled: !!projectId,
    refetchInterval: (query) => (anyRunning(query.state.data, (r) => r.status) ? LIVE_POLL_MS : false),
  });
}

/** The video's scene previews, newest first, each at the size it was drawn; polls while one runs. */
export function useScenePreviews(projectId: string | undefined) {
  return useQuery<VideoRender[]>({
    queryKey: [...VIDEO_STUDIO_KEY, "renders", projectId, "scene-previews"],
    queryFn: async () => (await api.get<{ items: VideoRender[] }>(`/video-studio/projects/${projectId}/renders?scene_previews=true`)).items,
    enabled: !!projectId,
    refetchInterval: (query) => (anyRunning(query.state.data, (r) => r.status) ? LIVE_POLL_MS : false),
  });
}

/** The video templates the person may pick, the platform default first. */
export function useVideoTemplates(enabled = true) {
  return useQuery<VideoTemplateSummary[]>({
    queryKey: [...VIDEO_STUDIO_KEY, "templates"],
    queryFn: async () => (await api.get<{ items: VideoTemplateSummary[] }>("/video-studio/templates")).items,
    enabled,
    staleTime: 60_000,
  });
}

/**
 * A page of the person's recordings and AI shots together, newest first;
 * polls while one of them is being made. `ids` reads a video's own
 * media whatever their age.
 */
export function useVideoAssets(query: VideoAssetsQuery, enabled = true, { inlineError = false }: { inlineError?: boolean } = {}) {
  return useQuery<VideoLibraryPage<VideoAsset>>({
    queryKey: [...VIDEO_STUDIO_KEY, "assets", query],
    queryFn: () => api.get(`/video-studio/assets${libraryQuery(query)}`),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (anyRunning(q.state.data?.items, (a) => a.status) ? LIVE_POLL_MS : false),
    enabled,
    // The Media tab draws the read error itself (the table's error line with Retry).
    ...(inlineError ? { throwOnError: false } : {}),
  });
}

/** One recording (its scene's), a failed one included; polls while it is being made. */
export function useVideoTake(id: string | undefined) {
  return useQuery<VideoTake>({
    queryKey: [...VIDEO_STUDIO_KEY, "takes", id],
    queryFn: () => api.get(`/video-studio/takes/${id}`),
    enabled: !!id,
    throwOnError: false,
    retry: false,
    refetchInterval: (q) => (unfinished(q.state.data?.status) ? LIVE_POLL_MS : false),
  });
}

/** One AI shot (its scene's, a backdrop's, an animated still's), a failed one included; polls while it is being made. */
export function useVideoClip(id: string | undefined) {
  return useQuery<VideoClip>({
    queryKey: [...VIDEO_STUDIO_KEY, "clips", id],
    queryFn: () => api.get(`/video-studio/clips/${id}`),
    enabled: !!id,
    throwOnError: false,
    retry: false,
    refetchInterval: (q) => (unfinished(q.state.data?.status) ? LIVE_POLL_MS : false),
  });
}

/** The built-in tracks, each with a URL to listen to it; they never change within a release. */
export function useMusicLibrary(enabled = true) {
  return useQuery<VideoMusicTrack[]>({
    queryKey: [...VIDEO_STUDIO_KEY, "music-library"],
    queryFn: async () => (await api.get<{ items: VideoMusicTrack[] }>("/video-studio/music/library")).items,
    enabled,
    // The listen URLs carry a 45-minute token.
    staleTime: 30 * 60 * 1000,
  });
}

/** The person's AI tracks; polls while one is being composed. */
export function useVideoMusic(enabled = true) {
  return useQuery<VideoMusic[]>({
    queryKey: [...VIDEO_STUDIO_KEY, "music"],
    queryFn: async () => (await api.get<{ items: VideoMusic[] }>("/video-studio/music")).items,
    enabled,
    refetchInterval: (query) => (anyRunning(query.state.data, (m) => m.status) ? LIVE_POLL_MS : false),
  });
}

/** An AI track: composed in the worker, the video's music (with `project_id`) while it is. */
export function useCreateVideoMusic() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (body: VideoMusicCreate) =>
      api.post<{ music: VideoMusic; project: VideoProject | null }>("/video-studio/music", body),
    onSuccess: invalidate,
  });
}

/** A failed AI track composed again in place: every video using it keeps it. */
export function useRetryVideoMusic() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (musicId: string) => api.post<VideoMusic>(`/video-studio/music/${musicId}/retry`, {}),
    onSuccess: invalidate,
  });
}

const LEXICON_KEY = [...VIDEO_STUDIO_KEY, "pronunciation-lexicon"] as const;

/** The platform's pronunciation lexicon, its newest version: every video's voice reads it under its own entries. */
export function usePronunciationLexicon(enabled = true) {
  return useQuery<PronunciationLexicon>({
    queryKey: LEXICON_KEY,
    queryFn: () => api.get("/video-studio/pronunciation-lexicon"),
    enabled,
    staleTime: 60_000,
  });
}

/** Every version of the platform's lexicon, newest first (administrators). */
export function usePronunciationLexiconVersions(enabled = true) {
  return useQuery<PronunciationLexiconVersion[]>({
    queryKey: [...LEXICON_KEY, "versions"],
    queryFn: async () => (await api.get<{ items: PronunciationLexiconVersion[] }>("/video-studio/pronunciation-lexicon/versions")).items,
    enabled,
  });
}

/** A new version of the platform's lexicon (administrators): each entry validated by the server. */
export function useSavePronunciationLexicon() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { entries: PronunciationEntry[]; note?: string | null }) =>
      api.put<PronunciationLexicon>("/video-studio/pronunciation-lexicon", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: LEXICON_KEY }),
  });
}

/** A new version holding an older one's entries (administrators). */
export function useRestorePronunciationLexicon() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (version: number) =>
      api.post<PronunciationLexicon>(`/video-studio/pronunciation-lexicon/versions/${version}/restore`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: LEXICON_KEY }),
  });
}

/** One term spoken as a voice-over says it, to hear it before a render: spoken once per reading, then cached. */
export function useSpeakPronunciation() {
  return useMutation({
    mutationFn: (body: { text: string; voiceover?: Record<string, unknown> | null }) =>
      api.post<SpokenPronunciation>("/video-studio/pronunciations/speak", body),
  });
}

/**
 * Refresh every Video Studio query but the media tokens: a token minted
 * again is a new URL, which reloads every picture and player showing it. A
 * token is renewed once lost (`useRenewVideoMedia`), or read for another file
 * (`useVideoMediaUrl`'s `file`).
 */
export function invalidateVideoStudio(qc: QueryClient) {
  return qc.invalidateQueries({ queryKey: VIDEO_STUDIO_KEY, predicate: (query) => query.queryKey[1] !== "media" });
}

function useInvalidateStudio() {
  const qc = useQueryClient();
  return () => invalidateVideoStudio(qc);
}

/**
 * Starts a render; refused by its quality gate (409 `render_gate`) unless `force`, and asked first when an
 * older revision's render is in progress (409 `render_replace`) unless `replace`.
 */
export function useRenderVideo() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({
      projectId,
      quality,
      force,
      replace,
    }: {
      projectId: string;
      quality: VideoRenderQuality;
      force?: boolean;
      replace?: boolean;
    }) =>
      api.post<VideoRenderStarted>(`/video-studio/projects/${projectId}/renders`, {
        quality,
        ...(force ? { force: true } : {}),
        ...(replace ? { replace: true } : {}),
      }),
    // The Render panel answers every failure itself: the gate's 409 with its "Render anyway" dialog, the rest in a toast.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: invalidate,
  });
}

/** The reasons of a render the quality gate refused, or `null` for any other error. */
export function renderGateReasons(err: unknown): VideoRenderGateReason[] | null {
  const failure = err as { status?: number; detail?: { code?: string; reasons?: VideoRenderGateReason[] } } | null;
  if (failure?.status !== 409 || failure.detail?.code !== "render_gate") return null;
  return Array.isArray(failure.detail.reasons) ? failure.detail.reasons : [];
}

/** The older revision's renders in progress a start would cancel, or `null` for any other error. */
export function renderReplaceQuestion(err: unknown): VideoRenderReplaceQuestion | null {
  const failure = err as { status?: number; detail?: { code?: string } & Partial<VideoRenderReplaceQuestion> } | null;
  if (failure?.status !== 409 || failure.detail?.code !== "render_replace") return null;
  return { revision: failure.detail.revision ?? 0, renders: Array.isArray(failure.detail.renders) ? failure.detail.renders : [] };
}

export function useCancelRender() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (renderId: string) => api.post<VideoRender>(`/video-studio/renders/${renderId}/cancel`),
    onSuccess: invalidate,
  });
}

export function useDeleteVideoProject() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (projectId: string) => api.delete(`/video-studio/projects/${projectId}`),
    onSuccess: invalidate,
  });
}

/** The sharing fields of `PATCH /video-studio/projects/{id}`, with PATCH semantics. */
export interface VideoSharingUpdate {
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

/** Who may open a video: the Share dialog's save. Failures are the dialog's own toast. */
export function useUpdateVideoSharing() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ projectId, ...body }: VideoSharingUpdate & { projectId: string }) =>
      api.patch<VideoProject>(`/video-studio/projects/${projectId}`, body),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: invalidate,
  });
}

const publicLinkKey = (projectId: string | undefined) => [...VIDEO_STUDIO_KEY, "public-link", projectId] as const;

/** A video's public link - its managers only, asked while the Share dialog is open on it. */
export function useVideoPublicLink(projectId: string | undefined, enabled: boolean) {
  return useQuery<VideoPublicLink>({
    queryKey: publicLinkKey(projectId),
    queryFn: () => api.get(`/video-studio/projects/${projectId}/public-link`),
    enabled: !!projectId && enabled,
    throwOnError: false,
  });
}

/** Turn the public link on (or hand back the one it has), or replace it (`regenerate`). */
export function useEnableVideoPublicLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, regenerate = false }: { projectId: string; regenerate?: boolean }) =>
      api.post<VideoPublicLink>(`/video-studio/projects/${projectId}/public-link`, { regenerate }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (link, { projectId }) => {
      qc.setQueryData(publicLinkKey(projectId), link);
      void qc.invalidateQueries({ queryKey: [...VIDEO_STUDIO_KEY, "projects"] });
    },
  });
}

/** Turn the public link off: whoever opens it is told the video is not available. */
export function useDisableVideoPublicLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) => api.delete<void>(`/video-studio/projects/${projectId}/public-link`),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (_void, projectId) => {
      qc.setQueryData<VideoPublicLink>(publicLinkKey(projectId), (cached) => ({
        available: cached?.available ?? true,
        enabled: false,
        path: null,
        url: null,
        created_at: null,
        created_by_name: null,
        allow_download: false,
      }));
      void qc.invalidateQueries({ queryKey: [...VIDEO_STUDIO_KEY, "projects"] });
    },
  });
}

/** Whether the link's visitors may also download the MP4 and its captions; applies at once. */
export function useUpdateVideoPublicLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, allowDownload }: { projectId: string; allowDownload: boolean }) =>
      api.patch<VideoPublicLink>(`/video-studio/projects/${projectId}/public-link`, { allow_download: allowDownload }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (link, { projectId }) => {
      qc.setQueryData(publicLinkKey(projectId), link);
    },
  });
}

/** Keep a render out of the render retention, or make it the one the video plays. */
export function useUpdateRender() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ renderId, ...body }: { renderId: string; keep?: boolean; current?: true }) =>
      api.patch<VideoRender>(`/video-studio/renders/${renderId}`, body),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: invalidate,
  });
}

/** Delete a render and its files. */
export function useDeleteRender() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (renderId: string) => api.delete<void>(`/video-studio/renders/${renderId}`),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: invalidate,
  });
}

/** What a render cleanup would delete: its dry run, the same list the cleanup answers, kept in step with the selectors. */
export function useRenderCleanupPreview(projectId: string | undefined, body: VideoRenderCleanupRequest, enabled: boolean) {
  return useQuery<VideoRenderCleanupResult>({
    queryKey: [...VIDEO_STUDIO_KEY, "renders", projectId, "cleanup", body],
    queryFn: () => api.post<VideoRenderCleanupResult>(`/video-studio/projects/${projectId}/renders/cleanup`, { ...body, dry_run: true }),
    enabled: enabled && !!projectId,
    retry: false,
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Delete a video's renders by selector or by id, with their files. */
export function useCleanupRenders() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ projectId, ...body }: VideoRenderCleanupRequest & { projectId: string }) =>
      api.post<VideoRenderCleanupResult>(`/video-studio/projects/${projectId}/renders/cleanup`, { ...body, dry_run: false }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: invalidate,
  });
}

const RENDER_RETENTION_KEY = [...VIDEO_STUDIO_KEY, "render-retention"] as const;

/** The render retention: the days a render nothing else keeps is kept, and whether the reader may change them. */
export function useRenderRetention(enabled = true) {
  return useQuery<VideoRenderRetentionSettings>({
    queryKey: RENDER_RETENTION_KEY,
    queryFn: () => api.get("/video-studio/render-retention"),
    enabled,
    staleTime: 60_000,
  });
}

/** Change the render retention (administrators). */
export function useSaveRenderRetention() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (body: Partial<VideoRenderRetention>) => api.put<VideoRenderRetentionSettings>("/video-studio/render-retention", body),
    onSuccess: invalidate,
  });
}

/** Follow a download URL: the server answers an attachment, the browser saves it without holding it in memory. */
function followDownload(url: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.rel = "noopener";
  anchor.click();
}

/**
 * Download one file of a render, a take or a clip: a download token
 * names the file after the video and its quality ("Launch film - 1080p60.mp4"),
 * and the MP4 streams from the server to the disk.
 */
export function useDownloadVideoAsset() {
  return useMutation({
    mutationFn: async ({ kind, id, asset }: { kind: VideoMediaKind; id: string; asset: VideoMediaAsset }) => {
      const token = await api.post<VideoMediaToken>("/video-studio/media-token", { kind, id, asset, download: true });
      followDownload(token.url);
    },
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Why a public video could not be loaded: the link is dead (404), or the read failed. */
export class PublicVideoError extends Error {
  constructor(readonly status: number) {
    super(`The video could not be loaded (HTTP ${status}).`);
  }
}

/** Where a public page plays or downloads one file of the video's current render. */
export function publicVideoMediaUrl(token: string, asset: "video" | "poster" | "captions", download = false): string {
  const base = `${API_BASE}/public/videos/${encodeURIComponent(token)}/media/${asset}`;
  return download ? `${base}?download=true` : base;
}

/**
 * The video a public link opens - `/public/videos/{token}`, for a
 * visitor with no session: a plain `fetch` (no session header, no sign-in
 * redirect), a dead link (404) never retried.
 */
export function usePublicVideo(token: string | undefined) {
  return useQuery<PublicVideo, PublicVideoError>({
    queryKey: ["public-video", token],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/public/videos/${encodeURIComponent(token ?? "")}`, { credentials: "omit" });
      if (!res.ok) throw new PublicVideoError(res.status);
      return (await res.json()) as PublicVideo;
    },
    enabled: !!token,
    throwOnError: false,
    retry: (failures, error) => error.status !== 404 && failures < 2,
    refetchOnWindowFocus: false,
  });
}

/** Follow a public video's download: the server names the file and refuses it when the link offers none. */
export function downloadPublicVideo(token: string, asset: "video" | "captions"): void {
  followDownload(publicVideoMediaUrl(token, asset, true));
}

/** A video made by hand (blank, from a template, from media): the spec goes through the tools' validator. */
export function useCreateVideoProject() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (spec: Record<string, unknown>) => api.post<VideoProject>("/video-studio/projects", { spec }),
    onSuccess: invalidate,
  });
}

export function useDuplicateVideoProject() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (projectId: string) => api.post<VideoProject>(`/video-studio/projects/${projectId}/duplicate`),
    onSuccess: invalidate,
  });
}

/**
 * The editor's save: the whole draft spec. The project in the cache is
 * replaced by the answer, so the stage and the library read the new revision
 * without a refetch.
 */
export function useSaveVideoSpec(projectId: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (spec: Record<string, unknown>) =>
      api.put<VideoProject>(`/video-studio/projects/${projectId}/spec`, { spec }),
    onSuccess: (project) => {
      qc.setQueryData([...VIDEO_STUDIO_KEY, "projects", projectId], project);
      qc.invalidateQueries({ queryKey: PROJECT_PAGES_KEY });
    },
  });
}

/**
 * The live preview of a draft (`null` = the video as stored): the render's
 * timeline document and confined media URLs. A mutation, not a query: the
 * editor asks for one per settled edit and keeps the last good one on screen.
 */
export function usePreviewVideo(projectId: string | undefined) {
  return useMutation({
    mutationFn: (spec: Record<string, unknown> | null) =>
      api.post<VideoPreview>(`/video-studio/projects/${projectId}/preview`, { spec }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** An AI shot: generated in the worker, placed in a video (or a new one) while it does. */
export function useCreateVideoClip() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (body: VideoClipCreate) =>
      api.post<{ clip: VideoClip; project: VideoProject | null }>("/video-studio/clips", body),
    onSuccess: invalidate,
  });
}

/** A failed AI shot generated again in place: every video using it keeps it. */
export function useRetryVideoClip() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ clipId, ...body }: VideoClipRetry & { clipId: string }) =>
      api.post<VideoClip>(`/video-studio/clips/${clipId}/retry`, body),
    onSuccess: invalidate,
  });
}

/** Rename a recording or an AI shot, or keep it out of the media retention. */
export function useUpdateVideoAsset() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ kind, id, ...body }: { kind: "take" | "clip"; id: string; name?: string; keep?: boolean }) =>
      api.patch<VideoTake | VideoClip>(`/video-studio/${kind === "take" ? "takes" : "clips"}/${id}`, body),
    onSuccess: invalidate,
  });
}

/** A picture, a sound or a video file for a video, the person's own. */
export function useUploadVideoFile() {
  return useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.postForm<VideoUploadedFile>("/video-studio/files", form);
    },
  });
}

export function useDeleteVideoTake() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ id, force }: VideoMediaDeletion) => api.delete(`/video-studio/takes/${id}${force ? "?force=true" : ""}`),
    onSuccess: invalidate,
  });
}

export function useDeleteVideoClip() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ id, force }: VideoMediaDeletion) => api.delete(`/video-studio/clips/${id}${force ? "?force=true" : ""}`),
    onSuccess: invalidate,
  });
}

/**
 * Recordings and AI shots deleted in bulk: the ones named, or every one the library's filters match.
 * Never one a video uses: the answer counts them in `kept_in_use`.
 */
export function useBulkDeleteVideoAssets() {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: (body: VideoAssetsBulkDelete) =>
      api.post<VideoAssetsBulkDeleted>("/video-studio/assets/bulk-delete", body),
    onSuccess: invalidate,
  });
}

/** How long before a media token expires a new one is minted: a player switches to it the moment the old one fails. */
export const MEDIA_TOKEN_REFRESH_MARGIN_MS = 5 * 60 * 1000;
/** The soonest a token is minted again, whatever its expiry says (a clock off by minutes). */
const MEDIA_TOKEN_MIN_REFRESH_MS = 30 * 1000;

/** Milliseconds until a media token should be minted again: its expiry less the margin; `false` before it is minted. */
export function mediaTokenRefreshIn(token: VideoMediaToken | undefined, now = Date.now()): number | false {
  if (!token?.expires_at) return false;
  return Math.max(MEDIA_TOKEN_MIN_REFRESH_MS, token.expires_at * 1000 - MEDIA_TOKEN_REFRESH_MARGIN_MS - now);
}

/** How long a media token fetched at *fetchedAt* is fresh: until its expiry less the margin (0 for none). */
export function mediaTokenFreshFor(token: VideoMediaToken | undefined, fetchedAt: number): number {
  if (!token?.expires_at) return 0;
  return Math.max(0, token.expires_at * 1000 - MEDIA_TOKEN_REFRESH_MARGIN_MS - fetchedAt);
}

/**
 * Whether a failed media-token request is asked once more: a 404 once only - the file a shot or a
 * render is being given, read a moment too early - and nothing else, so a player falls back at once otherwise.
 */
export function retryMediaToken(failures: number, error: unknown): boolean {
  return failures < 1 && isNotFoundError(error);
}

/**
 * A playable URL for one media file of a take, clip, render or track, or
 * `undefined` while it is minted. Tokens last 45 minutes; each is minted
 * again before it expires (`mediaTokenRefreshIn`), in the background too, so
 * a player that loses its URL finds a fresh one waiting instead of showing
 * its backdrop while one is asked, and a 404 is asked once more
 * before the player falls back. `file`, when the caller knows it: the file
 * the asset is now, so a shot generated again reads its new poster or frame
 * instead of the token of the old one.
 */
export function useVideoMediaUrl(
  kind: VideoMediaKind,
  id: string | null | undefined,
  asset: VideoMediaAsset,
  enabled = true,
  file?: string | null,
) {
  const query = useQuery<VideoMediaToken>({
    queryKey: [...VIDEO_STUDIO_KEY, "media", kind, id, asset, file ?? null],
    queryFn: () => api.post("/video-studio/media-token", { kind, id, asset }),
    enabled: !!id && enabled,
    staleTime: (query) => mediaTokenFreshFor(query.state.data, query.state.dataUpdatedAt),
    refetchInterval: (query) => mediaTokenRefreshIn(query.state.data),
    refetchIntervalInBackground: true,
    gcTime: 40 * 60 * 1000,
    throwOnError: false,
    retry: retryMediaToken,
    retryDelay: 1000,
  });
  return query.data?.url;
}

/**
 * Mint again the media URLs of one take, clip, render or track: the
 * ones a player holds stopped answering (their token outlived), so each
 * `useVideoMediaUrl` of it reads a fresh one.
 */
export function useRenewVideoMedia(kind: VideoMediaKind, id: string | null | undefined) {
  const qc = useQueryClient();
  return useCallback(() => {
    void qc.invalidateQueries({ queryKey: [...VIDEO_STUDIO_KEY, "media", kind, id] });
  }, [qc, kind, id]);
}
